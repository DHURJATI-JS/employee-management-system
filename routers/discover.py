"""Find companies: public search by what they do / who they hire (pgvector), the biodata profile (typed in or read from a PDF CV),
and recommendations: listed companies nearest to your profile's vector."""
import contextlib
import changelog, completion
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from assist import card_columns, company_text, keywords, match_percent, profile_text, rank_companies, rate_limited, update_profile_embedding
from decoraters import public
from grow import get_review, instant_profile
from form import DobForm, ProfileForm, ResumeForm, SalaryWantForm, adult_since
from fx import pay_in
from regions import currency_of
from payroll import at_work, hours_label, tz_name
from resume import ResumeError, extract
from system import AI_ASSIST, UPLOAD_DIR, FormError, client_ip, flash, render, unflash_saved
from tables import CURRENCIES, DEFAULT_CURRENCY, JOIN_AS, POSITIONS, SALARY_PERIODS, Company, Profile, User, get_db, single_company_mode
router = APIRouter(tags=["discover"])
SEARCH_MAX_DISTANCE = 0.85  # pgvector cosine distance: further than this (under 15% match) isn't shown
def _uid(request: Request) -> int | None: return int(u["user_id"]) if (u := getattr(request.state, "user", None)) else None
def _why(source: str, c: Company) -> list[str]: return sorted(keywords(source) & keywords(company_text(c)))[:8]  # shared words, shown as "matched"
def _rows(found, source: str = "") -> list[dict]:  # [(company, distance | None, members, joined)] -> what _company_card.html shows
    return [{"company": c, "match": match_percent(d) if d is not None else None, "why": _why(source, c) if source else [], "members": n, "joined": j} for c, d, n, j in found]
async def on_duty(request: Request, db: AsyncSession, uid: int | None) -> RedirectResponse | None:
    """During their company's working hours, staff (everyone but owners, co-owners and freelancers) can't look at or search other
    companies: they're sent back to that company's dashboard."""
    if not uid or not (c := await at_work(db, uid)): return None
    flash(request, f"It's working hours at {c.name} ({hours_label(c)}, {tz_name(c)}): other companies can't be viewed or searched until they end. Owners and co-owners aren't limited.", "info")
    return RedirectResponse(f"/companies/{c.id}/dashboard", 303)
async def solo(request: Request, db: AsyncSession) -> RedirectResponse | None:  # single company mode: the AI company finder + matching are off; to the one company
    if not await single_company_mode(db, lock=False): return None
    flash(request, "Single company mode: there's only one company here, so the AI company finder and matching are off. This is the company.", "info")
    return RedirectResponse("/companies/search", 303)
# ---------- search: anyone, logged in or not (staff: not during working hours) ----------
@router.get("/companies/search")
@public
async def search(request: Request, q: str = "", db: AsyncSession = Depends(get_db)):
    q = " ".join(q.split())[:100]
    if rate_limited(f"search:{client_ip(request)}", 60, 300): raise HTTPException(429, "Too many searches; wait a few minutes.")
    uid = _uid(request)  # one query either way: companies + member counts + "you're in it" flags together
    if busy := await on_duty(request, db, uid): return busy
    if await single_company_mode(db, lock=False):  # (user asked) no search: the one company fills the page (unlisted: only for its members)
        found = [(c, None, n, j) for c, n, j in await db.execute(select(Company, *card_columns(uid)).order_by(Company.created_at).limit(1).options(selectinload(Company.salaries)))]
        return render(request, "search", {"q": "", "rows": _rows([r for r in found if r[0].listed or r[3]]), "single": True, "positions": POSITIONS, "join_as": JOIN_AS})
    if q: found = [r for r in await rank_companies(db, query=q, viewer=uid, limit=30) if r[1] <= SEARCH_MAX_DISTANCE]
    else: found = [(c, None, n, j) for c, n, j in await db.execute(select(Company, *card_columns(uid)).where(Company.listed, Company.halted_at.is_(None)).order_by(Company.created_at.desc()).limit(30)
                                                                       .options(selectinload(Company.salaries)))]
    return render(request, "search", {"q": q, "rows": _rows(found, q), "positions": POSITIONS, "join_as": JOIN_AS})
# ---------- profile: biodata typed in, or extracted from a PDF ----------
async def _profile(db: AsyncSession, uid: int) -> Profile | None: return await db.get(Profile, uid)
@router.get("/profile")
async def profile_page(request: Request, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    p = await _profile(db, uid)  # + growth suggestions: instant (rules + pgvector) and the latest AI review (grow.py)
    region = await db.scalar(select(User.region).where(User.id == uid))  # the salary you want defaults to your region's currency
    dob = await db.scalar(select(User.dob).where(User.id == uid))
    return render(request, "profile", {"profile": p, "ai_engine": AI_ASSIST, "currencies": CURRENCIES, "periods": SALARY_PERIODS, "default_currency": currency_of(region) or DEFAULT_CURRENCY,
                                       "region": region, "dob": dob, "max_dob": adult_since().isoformat(), "done": completion.of_profile(request.state.user, p, dob),
                                       "instant": await instant_profile(db, uid, p), "review": await get_review(db, "u", uid)})
@router.post("/profile")
async def save_profile(request: Request, form: ProfileForm = Depends(ProfileForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    p = await _profile(db, uid) or Profile(user_id=uid)
    before = _facts(p)
    p.headline, p.bio, p.skills, p.years, p.discoverable = form.headline, form.bio, form.skill_list(), form.years, form.discoverable
    _set_wants(p, form)
    db.add(p)
    await db.flush()
    await update_profile_embedding(db, p)
    await db.commit()
    await changelog.person(db, uid, request.state.user["username"], "profile updated", changelog.diff(before, _facts(p)) or "saved again, nothing changed")
    flash(request, "Profile saved." + ("" if await single_company_mode(db, lock=False) else " Your recommendations are updated."), "success")
    return RedirectResponse("/profile", 303)
@router.post("/profile/dob")
async def add_dob(request: Request, form: DobForm = Depends(DobForm.as_form), db: AsyncSession = Depends(get_db)):
    """Once only, for accounts made before signup asked for a date of birth: 18+ like at signup, and locked afterwards."""
    account = await db.get(User, _uid(request), with_for_update=True)
    if account.dob: raise FormError("Your date of birth is already set and can't be changed.")
    account.dob, name = form.dob, account.username
    await db.commit()
    await changelog.person(db, account.id, name, "date of birth added", f"{form.dob:%Y-%m-%d}")
    flash(request, "Date of birth saved.", "success")
    return RedirectResponse("/profile", 303)
def _facts(p: Profile) -> dict:  # what the change log compares (old -> new)
    return {"headline": p.headline, "years of experience": p.years, "skills": list(p.skills or []), "about": p.bio, "salary wanted": p.wants() if p.want_min else "",
            "findable by companies": bool(p.discoverable), "CV": p.resume_file or ""}
def _set_wants(p: Profile, form) -> None:  # the wanted salary range; no lower end = no salary preference at all
    p.want_min, p.want_max = form.want_min, form.want_max if form.want_min else None
    p.want_currency, p.want_period = (form.want_currency, form.want_period) if form.want_min else (None, None)
@router.post("/profile/salary")
async def save_wanted_salary(request: Request, form: SalaryWantForm = Depends(SalaryWantForm.as_form), db: AsyncSession = Depends(get_db)):
    """The instruction bar's "I want 50k-80k a month", after you confirm it."""
    uid = _uid(request)
    p = await _profile(db, uid) or Profile(user_id=uid, headline="", bio="", skills=[])
    was = p.wants() if p.want_min else ""
    _set_wants(p, form)
    db.add(p)
    await db.commit()
    await changelog.person(db, uid, request.state.user["username"], "salary wanted changed", f"{was or '—'} → {p.wants() or '—'}")
    alone = await single_company_mode(db, lock=False)  # no recommendations then: nothing to promise, and nowhere to send them
    flash(request, (f"Expected salary saved: {p.wants()}." + ("" if alone else " Your recommendations now show which companies pay that.")) if p.wants() else "Salary preference cleared.", "success")
    return RedirectResponse("/recommendations" if profile_text(p).strip() and not alone else "/profile", 303)
def _discard(name: str | None) -> None:
    with contextlib.suppress(OSError):
        if name and (path := (UPLOAD_DIR / name).resolve()).parent == UPLOAD_DIR.resolve(): path.unlink(missing_ok=True)
@router.post("/profile/resume")
async def upload_resume(request: Request, form: ResumeForm = Depends(ResumeForm.as_form), db: AsyncSession = Depends(get_db)):
    unflash_saved(request, form.file)  # the outcome is flashed below
    try: points, engine = await extract(UPLOAD_DIR / form.file)
    except ResumeError as e:
        _discard(form.file)  # only kept if it was a readable PDF
        raise FormError(str(e)) from None
    uid = _uid(request)
    p = await _profile(db, uid) or Profile(user_id=uid, headline="", bio="", skills=[])
    old, before = p.resume_file, _facts(p)
    p.headline, p.bio = points["headline"] or p.headline, points["bio"] or p.bio  # keep what you typed where the PDF had nothing
    p.skills = list(dict.fromkeys([*points["skills"], *p.skills]))[:40]
    p.years, p.resume_file, p.extracted_by = points["years"] or p.years, form.file, engine
    db.add(p)
    await db.flush()
    await update_profile_embedding(db, p)
    await db.commit()
    await changelog.person(db, uid, request.state.user["username"], "CV uploaded", f"read by the {'Claude' if engine == 'claude' else 'local'} reader · " + (changelog.diff(before, _facts(p)) or "no profile fields changed"))
    if old != form.file: _discard(old)  # one CV per person on disk
    found = [f"{len(points['skills'])} skills", *([f"{points['years']} years' experience"] if points["years"] else []), *(["a headline"] if points["headline"] else [])]
    flash(request, f"Read your PDF ({'Claude' if engine == 'claude' else 'local reader'}): found {', '.join(found)}. Check the details below" + ("." if await single_company_mode(db, lock=False) else ", then see your recommendations."), "success")
    return RedirectResponse("/profile", 303)
# ---------- recommendations: listed companies you're not in, nearest to your profile vector ----------
@router.get("/recommendations")
async def recommendations(request: Request, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    if busy := await on_duty(request, db, uid) or await solo(request, db): return busy
    p = await _profile(db, uid)
    if not p or not profile_text(p).strip(): return render(request, "recommendations", {"profile": None, "rows": []})
    found = await rank_companies(db, user_id=uid, viewer=uid, exclude_joined=True, limit=24)  # one query: the profile vector is a subquery
    rows = _rows(found, profile_text(p))
    for r in rows: r["fit"] = pay_fit(r["company"], p)
    rows.sort(key=lambda r: (r["fit"][0] if r["fit"] else 1, -r["match"]))  # companies that pay your range first, then by profile match
    return render(request, "recommendations", {"profile": p, "rows": rows[:12], "positions": POSITIONS, "join_as": JOIN_AS})
def pay_fit(c: Company, p: Profile) -> tuple[int, str] | None:
    """How a company's minimum salaries compare with the range p wants: (0 pays it / 1 can't compare / 2 below it, why), None = no range.
    Amounts are converted to p's currency (the day's rate, fx.py) and period (x12 / ÷12): 10k-15k INR a month is matched with a
    company paying 160 USD a month (≈ 15,247 INR). A converted amount is marked ≈ and shown as the company states it too."""
    if not p.want_min: return None
    if not c.salary_currency: return 1, "salaries not published yet"
    own = c.salary_of()
    pay = {pos: pay_in(amount, c.salary_currency, c.salary_period, p.want_currency, p.want_period) for pos, amount in own.items()}
    fx, unit = c.salary_currency != p.want_currency, f"{p.want_currency} per {p.want_period}"
    amt = lambda pos: f"{'≈ ' if fx else ''}{pay[pos]:,}"
    said = lambda pos: f" (it pays {own[pos]:,} {c.salary_currency} per {c.salary_period}, at today's rate)" if fx else ""
    if pay.get(JOIN_AS, 0) >= p.want_min: return 0, f"pays your range from day one: {POSITIONS[JOIN_AS]['label']} starts at {amt(JOIN_AS)} {unit}{said(JOIN_AS)}"
    if reach := sorted((POSITIONS[pos]["rank"], pos) for pos, amount in pay.items() if amount >= p.want_min):
        pos = reach[-1][1]  # the most junior position that already pays it
        return 0, f"pays your range as {POSITIONS[pos]['label']} ({amt(pos)}+ {unit}{said(pos)}); you'd start as {POSITIONS[JOIN_AS]['label']}"
    if not pay: return 1, "salaries not published yet"
    top = max(pay, key=pay.get)
    return 2, f"pays at most {amt(top)} {unit} at minimum{said(top)}: below your {p.want_min:,}"
