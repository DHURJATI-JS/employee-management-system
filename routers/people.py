"""People search for a company's owner/co-owners: profiles that opted in (profiles.discoverable), ranked by pgvector against what
you're looking for (or, with no words, what your company is hiring for). "Approach" = a job offer (message + salary) stored in the
approaches relation table, shown in that person's notifications (the bell) and emailed to them (SMTP, or the console in dev).
Someone who couldn't join you under the "other companies" rule (their company keeps its employees to itself, or yours only takes
people who work nowhere else) can't be approached: you get a mailto link to ask if they'd switch."""
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from assist import keywords, match_percent, profile_text, rank_people, rate_limited
from form import ActionForm, ApproachForm
from fx import pay_in
from guard import halted_reason, no_funds, no_room
from routers.chats import access, names
from routers.companies import can_edit, other_companies_rule
from routers.discover import SEARCH_MAX_DISTANCE
from routers.friends import blocked_between, friend_ids
from system import APP_NAME, FormError, back, flash, render, send_email
from tables import JOIN_AS, POSITIONS, TOP, Approach, ChatMember, ChatMessage, Company, Membership, Notice, Payout, Profile, User, get_db, utcnow
router = APIRouter(tags=["people"])
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
def salary(a) -> str: return f"{a.salary_amount:,} {a.salary_currency} per {a.salary_period}" if a.salary_amount else "not given"
async def _recruiting_for(db: AsyncSession, uid: int) -> list[Membership]:  # companies you own / co-own, company joined in (+ its salaries: 1 IN query)
    return list((await db.scalars(select(Membership).where(Membership.user_id == uid, Membership.position.in_(TOP)).order_by(Membership.joined_at)
                                  .options(joinedload(Membership.company, innerjoin=True).selectinload(Company.salaries)))).all())
def floor(company) -> int | None: return company.salary_of().get(JOIN_AS) if company.salary_currency else None  # offers start at the Rookie minimum
def suggest(p: Profile, c, minimum: int | None) -> int | None:  # the offer box's starting value: what they want, in your currency (the day's rate) + period, never below your minimum
    if not minimum or not p.want_min: return minimum
    return max(minimum, pay_in(p.want_min, p.want_currency, p.want_period, c.salary_currency, c.salary_period))
def _blocked(locked: bool, elsewhere: bool, company) -> str | None:  # the join rule, as a reason (other companies are never named)
    if locked: return "works at a company that doesn't let its employees join other companies"
    if elsewhere and not company.allow_other_companies: return f"already works at another company, and {company.name} only takes people who work nowhere else"
    return None
@router.get("/people")
async def people(request: Request, q: str = "", company: uuid.UUID | None = None, db: AsyncSession = Depends(get_db)):
    uid, q = _uid(request), " ".join(q.split())[:100]
    if not (tops := await _recruiting_for(db, uid)): return render(request, "people", {"tops": [], "q": q})
    mine = next((m for m in tops if m.company_id == company), tops[0])
    if rate_limited(f"people:{uid}", 60, 300): raise HTTPException(429, "Too many searches; wait a few minutes.")
    wants = q or " ".join(filter(None, [mine.company.hiring, mine.company.description]))  # no words: what the company is hiring for
    minimum = floor(mine.company)
    rows = [{"p": p, "match": match_percent(d), "why": sorted(keywords(wants) & keywords(profile_text(p)))[:8], "blocked": _blocked(lk, el, mine.company), "approached": ap,
             "suggest": suggest(p, mine.company, minimum)} for p, d, lk, el, ap in (await rank_people(db, query=wants, company=mine.company, me=uid) if wants else []) if d <= SEARCH_MAX_DISTANCE]
    return render(request, "people", {"tops": tops, "mine": mine, "q": q, "wants": wants, "rows": rows, "floor": floor(mine.company), "join_as": JOIN_AS, "positions": POSITIONS})
@router.get("/people/{user_id}")
async def person_page(request: Request, user_id: int, chat: int | None = None, db: AsyncSession = Depends(get_db)):
    """A name in a chat opens this. A colleague: their company profile, with its own limits (of the chat's company when it's one you share). Anyone else you're in a chat with, who wrote in a chat you
    can read, or who's your friend: a basic page with nothing private on it (user's rule). Anyone else, and anyone who blocked you or you blocked: the same 404 as someone who doesn't exist."""
    uid = _uid(request)
    if user_id == uid: return RedirectResponse("/profile", 303)
    here = (await access(db, uid, chat))[0] if chat else None  # (a chat you can't read: its own 404, so nothing is learned from it)
    shared = (await db.scalars(select(Membership.company_id).where(Membership.user_id == uid, Membership.company_id.in_(select(Membership.company_id).where(Membership.user_id == user_id))).order_by(Membership.joined_at))).all()
    if shared: return RedirectResponse(f"/companies/{here.company_id if here and here.company_id in shared else shared[0]}/members/{user_id}", 303)
    together = await db.scalar(select(ChatMember.chat_id).where(ChatMember.user_id == user_id, ChatMember.chat_id.in_(select(ChatMember.chat_id).where(ChatMember.user_id == uid))).limit(1))
    wrote = await db.scalar(select(ChatMessage.id).where(ChatMessage.chat_id == here.id, ChatMessage.sender_id == user_id).limit(1)) if here else None
    friends = await friend_ids(db, uid)
    someone = await db.get(User, user_id, options=[joinedload(User.profile)])
    if not someone or await blocked_between(db, uid, user_id) or not (together or wrote or user_id in friends): raise HTTPException(404, "There's no such person.")
    return render(request, "person", {"someone": someone, "profile": someone.profile, "k": (await names(db, [user_id]))[user_id], "friend": user_id in friends, "chat": here.id if here else None})
@router.post("/companies/{company_id}/approach/{user_id}")
async def approach(request: Request, company_id: uuid.UUID, user_id: int, form: ApproachForm = Depends(ApproachForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    actor = await db.get(Membership, (uid, company_id), options=[joinedload(Membership.company, innerjoin=True).selectinload(Company.salaries)])
    if not can_edit(actor): raise FormError("Only a company's owner or co-owners can approach people for it.")
    c = actor.company
    if c.halted_at: raise FormError(halted_reason(c, "make job offers"))
    if why := no_funds(c, "make job offers"): raise FormError(why)  # nobody could accept it: joining needs funds
    if await no_room(db, c): raise FormError(f"{c.name} has reached its employee limit of {c.max_members}: raise it in the company's details first, or nobody could accept an offer.")  # (only owners and co-owners get here)
    if not (minimum := floor(c)): raise FormError(f"Set {c.name}'s minimum salaries first (workplaces → edit), then make offers.")
    if form.salary_amount < minimum: raise FormError(f"The offer must be at least {minimum:,} {c.salary_currency} per {c.salary_period}: {c.name}'s minimum for {POSITIONS[JOIN_AS]['label']}.")
    p = await db.get(Profile, user_id, options=[joinedload(Profile.user, innerjoin=True)])
    if not p or not p.discoverable or user_id == uid: raise FormError("That person isn't open to being approached.")  # same answer either way
    name, email, cname = p.user.username, p.user.email, actor.company.name  # plain strings: a rollback below expires every loaded object
    if await db.get(Membership, (user_id, company_id)): raise FormError(f"{name} is already in {cname}.")
    if await other_companies_rule(db, user_id, actor.company, lock=False):  # they couldn't join: the page offers email contact instead
        raise FormError(f"{name} can't join {cname} right now (company rules), so they can't be approached. Use “contact” to ask if they'd switch.")
    if rate_limited(f"approach:{uid}", 30, 3600): raise FormError("That's a lot of approaches in a short time; try again later.")
    offer = Approach(company_id=company_id, user_id=user_id, by_user_id=uid, message=form.message,  # in the company's salary unit
                     salary_amount=form.salary_amount, salary_currency=c.salary_currency, salary_period=c.salary_period)
    db.add(offer)
    try: await db.commit()
    except IntegrityError:  # one per company + person: pending, or declined earlier
        await db.rollback()
        old = await db.get(Approach, (company_id, user_id))
        flash(request, f"{name} declined {cname}'s earlier offer." if old and old.dismissed_at else f"You've already approached {name} for {cname}.", "info")
        return back(request)
    try:  # email the offer (plain text, so the message can't inject markup); the notification in the app is what counts
        await send_email(email, f"Job offer from {cname} on {APP_NAME}", f"Hi {name},\n\n{request.state.user['username']} from {cname} saw your profile on {APP_NAME} "
                         f"and would like you to join.\n\nOffered salary: {salary(offer)}\n\nTheir message:\n{form.message}\n\n"
                         f"Log in to {APP_NAME} and open your notifications (the bell) to join or decline.\n")
        emailed = True
    except Exception as e:  # SMTP down etc.: never undo the offer for it
        print(f"approach email to user {user_id} failed: {type(e).__name__}"); emailed = False
    flash(request, f"Offer sent to {name} for {cname}: it's in their notifications{' and their email' if emailed else ''}.", "success")
    return back(request)
# ---------- the approached person's side: notifications (the bell), decline ----------
@router.get("/notifications")
async def notifications(request: Request, db: AsyncSession = Depends(get_db)):
    """Salary credited (your latest payouts, each opening your statement on that payment) + your open offers, newest first (company +
    who sent it joined in); opening the page marks them seen."""
    uid = _uid(request)
    offers = (await db.scalars(select(Approach).where(Approach.user_id == uid, Approach.dismissed_at.is_(None)).order_by(Approach.created_at.desc())
                               .options(joinedload(Approach.company, innerjoin=True), joinedload(Approach.by)))).all()
    payouts = (await db.scalars(select(Payout).where(Payout.user_id == uid).order_by(Payout.created_at.desc()).limit(20))).all()
    notices = (await db.scalars(select(Notice).where(Notice.user_id == uid).order_by(Notice.created_at.desc(), Notice.id.desc()).limit(30))).all()  # the payroll guard's (guard.py)
    new, paid, fresh = {a.company_id for a in offers if a.seen_at is None}, {p.id for p in payouts if p.seen_at is None}, {n.id for n in notices if n.seen_at is None}
    if new or paid or fresh:
        for table in (Approach, Payout, Notice): await db.execute(update(table).where(table.user_id == uid, table.seen_at.is_(None)).values(seen_at=utcnow()))
        await db.commit()
        request.state.user["unread"] = 0  # the bell on this page already counts them as read
    return render(request, "notifications", {"offers": offers, "new": new, "salary": salary, "payouts": payouts, "paid": paid, "notices": notices, "fresh": fresh})  # money: the template global (+ the viewer's currency)
@router.post("/approaches/{company_id}/dismiss")
async def dismiss(request: Request, company_id: uuid.UUID, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """Decline an offer: hidden from you, shown as declined to the company, which can't send another (only ever your own)."""
    await db.execute(update(Approach).where(Approach.user_id == _uid(request), Approach.company_id == company_id, Approach.dismissed_at.is_(None))
                     .values(dismissed_at=utcnow(), seen_at=func.coalesce(Approach.seen_at, utcnow())))
    await db.commit()
    flash(request, "Offer declined.", "info")
    return RedirectResponse("/notifications", 303)
