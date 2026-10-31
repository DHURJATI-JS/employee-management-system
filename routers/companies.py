import uuid
import changelog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from assist import update_company_embedding, user_context as _memberships  # memberships + companies + members + users in 2 queries
from decoraters import public
from form import ActionForm, CompanyForm, CompanySettingsForm, ConfirmForm, DecisionForm, JoinCodeForm, JoinForm, PositionForm, TermsForm
from fx import convert, pay_in
from guard import halted_reason, join_block, no_room, notify, notify_many
from payroll import approved_leaves, at_work, cycle_pay, hours_label, hours_locked, keep_pay, money, pay_rows, unsettled
from media import PictureError, discard_upload, remove_id_photo, remove_picture, store_id_photo, store_picture
from meet import remove_files
from shop import COMMISSION_PERCENT, cancel
from places import PlaceError, verify_place
from routers.chats import unread_by_company
from routers.tasks import review_panel
from routers.discover import on_duty
from routers.leave import JOIN_APPROVERS, can_decide
from bankcheck import BankCheckError, verify_bank
from system import APP_NAME, MAX_COMPANIES_PER_ACCOUNT, UPLOAD_DIR, FormError, flash, picture, remove_chat_files, render, seal, send_email, unflash_saved
from tables import (ACCOUNT_TYPES, COMPANY_PROOFS, CURRENCIES, DEFAULT_CURRENCY, FREELANCE, HOURS_EVERY_DAYS, ID_TYPES, MAX_EMPLOYEE_LIMIT, MAX_SALARY, MIN_EMPLOYEE_LIMIT, TOP, JOIN_AS, MAX_WORK_HOURS, POSITIONS, REGISTRATION_TYPES, SALARY_PERIODS, STAFF, STANDARD_TERMS, Approach,
                    BankAccount, Chat, Company, CompanyBankAccount, CompanySalary, DeletionRequest, IdentityProof, Meeting, MemberRequest, Membership, Order, OwnershipOffer, Payout, Shop, User, back_in_company, get_db,
                    single_company_mode, utcnow)
router = APIRouter(tags=["companies"])  # @public only where a signed-out visitor must be redirected; every query is scoped to the user
MAX_MEMBERSHIPS = 25  # workplaces one person can join; how many companies an account can own is MAX_COMPANIES_PER_ACCOUNT (.env, system.py)
label = lambda key: POSITIONS.get(key, {"label": key})["label"]
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
def consistent(m: Membership, c: Company) -> bool:
    """Cross-check one membership: only the company's owner_id holds "owner"; co-owners are top level (not part of the offer);
    everyone else holds a position the company offers."""
    return (m.position == "owner") == (m.user_id == c.owner_id) and (POSITIONS.get(m.position, {}).get("top") or m.position in c.positions)
def can_manage(actor: Membership, target: Membership) -> bool:
    """Only the top level (owner, co-owners) changes positions, never their own, and only for people strictly below them:
    the owner manages co-owners and staff; co-owners manage staff only."""
    a, t = POSITIONS.get(actor.position, {}), POSITIONS.get(target.position, {})
    return bool(a.get("top")) and actor.user_id != target.user_id and a["rank"] < t.get("rank", -1)
def assignable(actor: Membership, company: Company) -> list[str]:
    """Positions this actor may hand out: the company's offered positions, plus co-owner for the owner only."""
    if not POSITIONS.get(actor.position, {}).get("top"): return []
    return (["co_owner"] if actor.position == "owner" else []) + [k for k in STAFF if k in company.positions]
async def _limits_ok(db: AsyncSession, uid: int, creating: bool) -> None:
    """Per account: creating is limited only by the companies it owns now (MAX_COMPANIES_PER_ACCOUNT, or a higher number in .env would stop at MAX_MEMBERSHIPS); joining by its workplaces.
    Call it with the user's row locked (other_companies_rule), or two requests at once both see room for one more."""
    if creating:
        if await db.scalar(select(func.count()).where(Company.owner_id == uid)) >= MAX_COMPANIES_PER_ACCOUNT: raise FormError(f"Limit reached: an account can own {MAX_COMPANIES_PER_ACCOUNT} compan{'y' if MAX_COMPANIES_PER_ACCOUNT == 1 else 'ies'}. Delete one or hand it over to create another.")
    elif await db.scalar(select(func.count()).where(Membership.user_id == uid)) >= MAX_MEMBERSHIPS: raise FormError(f"Limit reached: {MAX_MEMBERSHIPS} workplaces in total.")
NO_BANK = "Add your bank details in settings first (any country: account number or IBAN, the bank's code): you need them to join or create a company, so you can be paid."
NO_ID = "Add your identity proof in settings first (the ID's number + a photo of it): everyone in a company needs one."
async def missing_kyc(db: AsyncSession, uid: int) -> str | None:  # bank details + identity proof: both needed to join or create a company
    bank, ident = (await db.execute(select(select(BankAccount.user_id).where(BankAccount.user_id == uid).exists(),
                                           select(IdentityProof.user_id).where(IdentityProof.user_id == uid).exists()))).one()
    return None if bank and ident else NO_BANK if not bank else NO_ID
async def other_companies_rule(db: AsyncSession, uid: int, entering: Company | None, lock: bool = True) -> str | None:
    """Why uid can't enter a company (entering=None: creating one) under the "employees may also join other companies" rule, or None.
    A company with the rule off: its staff can't be in any other company, and nobody already in one can join it. Its owner and
    co-owners are exempt (they set the rule). lock: the user's row + their companies are locked, so two joins at once, or a
    settings change mid-join, can't slip past the check."""
    if lock: await db.execute(select(User.id).where(User.id == uid).with_for_update())
    q = select(Company.name, Company.allow_other_companies, Membership.position).join(Membership, Membership.company_id == Company.id).where(Membership.user_id == uid)
    rows = (await db.execute(q.with_for_update(read=True, of=Company) if lock else q)).all()
    if strict := next((name for name, allowed, pos in rows if not allowed and not POSITIONS.get(pos, {}).get("top")), None):
        return f"{strict} doesn't let its employees work at other companies. Leave {strict} first, or ask its owner to change the rule."
    if entering is not None and not entering.allow_other_companies and rows:
        return f"{entering.name} only takes people who don't work at another company. Leave your other workplaces first."
    return None
@router.get("/dashboard")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)  # + the companies that approached you (people search), company joined in: 1 query
    approaches = (await db.scalars(select(Approach).where(Approach.user_id == uid, Approach.dismissed_at.is_(None)).order_by(Approach.created_at.desc())
                                   .options(joinedload(Approach.company, innerjoin=True)))).all()
    return render(request, "dashboard", {"memberships": await _memberships(db, uid), "approaches": approaches, "positions": POSITIONS})
# ---------- create ----------
SINGLE = f"{APP_NAME} is in single company mode: no company can be created while it's on. Only the admin can turn it off."
@router.get("/companies/new")
async def new_company_page(request: Request, db: AsyncSession = Depends(get_db)):
    if await single_company_mode(db, lock=False): flash(request, SINGLE); return RedirectResponse("/dashboard", 303)
    try: await _limits_ok(db, _uid(request), creating=True)  # said up front: the form is long, with two photos to upload
    except FormError as e: flash(request, str(e)); return RedirectResponse("/dashboard", 303)
    return render(request, "company_new", {"positions": POSITIONS, "staff": STAFF, "join_as": JOIN_AS, "account_types": ACCOUNT_TYPES, **SALARY_CHOICES, "commission": COMMISSION_PERCENT})
SALARY_CHOICES = {"currencies": CURRENCIES, "periods": SALARY_PERIODS, "default_currency": DEFAULT_CURRENCY, "registration_types": REGISTRATION_TYPES,  # for _company_fields.html
                  "max_hours": MAX_WORK_HOURS, "hours_every": HOURS_EVERY_DAYS, "min_limit": MIN_EMPLOYEE_LIMIT, "max_limit": MAX_EMPLOYEE_LIMIT, "day_names": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "company_proofs": COMPANY_PROOFS}
async def company_identity(db: AsyncSession, form, company: Company | None) -> dict:
    """The company's verified location + a registration number no other company has, or FormError. The location is checked with
    Geoapify (places.py) unless it's the place already saved; a GSTIN / CIN must belong to a location in India."""
    if not form.location_place_id: raise FormError("Pick the company's location on the map: it's required.")
    if company and form.location_place_id == company.location_place_id:
        place = {"place_id": company.location_place_id, "address": company.location_address, "lat": company.location_lat, "lng": company.location_lng,
                 "country": company.location_country, "timezone": company.timezone}
        if not company.timezone:  # saved before timezones were kept: ask Geoapify once more for it (working hours run on it)
            try: place["timezone"] = (await verify_place(company.location_place_id))["timezone"]
            except PlaceError: pass
    else:
        try: place = await verify_place(form.location_place_id)
        except PlaceError as e: raise FormError(str(e)) from None
    if form.registration_type in ("GSTIN", "CIN") and place["country"] not in (None, "IN"):
        raise FormError(f"A {form.registration_type} belongs to a business in India, but the location you picked is in {place['country']}.")
    taken = select(Company.id).where(Company.registration_type == form.registration_type, Company.registration_number == form.registration_number)
    if await db.scalar(taken.where(Company.id != company.id) if company else taken): raise FormError(f"That {form.registration_type} number is already registered to another company.")
    return place
def company_facts(c: Company, pay: dict) -> dict:  # what a company's change log describes (created) and compares (edited): every field people set
    return {"name": c.name, "what it does": c.description, "hiring": c.hiring, "listed in search": bool(c.listed), "staff may work elsewhere": bool(c.allow_other_companies),
            "website": c.website, "registration": f"{c.registration_type} {c.registration_number}" if c.registration_number else "", "location": c.location_address,
            "exact address": c.exact_address, "working hours": hours_label(c) if c.work_start else "", "time zone": c.timezone, "positions": [label(k) for k in c.positions],
            "minimum salaries": [f"{label(k)} {v:,} {c.salary_currency} a {c.salary_period}" for k, v in sorted(pay.items())], "its own terms": c.terms}
def apply_identity(c: Company, form, place: dict) -> None:  # + working hours, on the location's clock
    c.website, c.registration_type, c.registration_number = form.website, form.registration_type, form.registration_number
    c.location_place_id, c.location_address, c.location_lat, c.location_lng, c.location_country = place["place_id"], place["address"], place["lat"], place["lng"], place["country"]
    c.timezone, c.work_start, c.work_end, c.work_days = place.get("timezone") or c.timezone, form.work_start, form.work_end, form.days_mask()
    c.exact_address = form.exact_address
@router.post("/companies/new")
async def create_company(request: Request, form: CompanyForm = Depends(CompanyForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    unflash_saved(request, form.logo); unflash_saved(request, form.id_proof)  # the outcome is flashed instead of @as_form's "uploaded" notes
    try:
        if await single_company_mode(db): raise FormError(SINGLE)  # (locks the admin's switch until this commits)
        if why := await missing_kyc(db, uid): raise FormError(why)
        if why := await other_companies_rule(db, uid, None): raise FormError(why)  # (locks the user's row: first, so _limits_ok counts after any other request of theirs)
        await _limits_ok(db, uid, creating=True)
        place = await company_identity(db, form, None)  # location verified with Geoapify, registration number unused
        if acct := form.company_account():  # optional: the company's own bank account, checked with the bank when BANK_VERIFY is on
            try: checked = await verify_bank(acct.account_number, acct.routing, acct.holder_name, acct.country)
            except BankCheckError as e: raise FormError(f"Company bank account not saved: {e}") from None
    except FormError: discard_upload(form.logo); discard_upload(form.id_proof); raise  # refused: the uploads aren't kept
    company = Company(name=form.name, owner_id=uid, positions=form.positions, description=form.description, hiring=form.hiring, listed=form.listed,
                      allow_other_companies=form.allow_other_companies, salary_currency=form.salary_currency, salary_period=form.salary_period, max_members=form.max_members,
                      id_proof_type=form.id_proof_type, id_proof_at=utcnow(), terms=form.terms, terms_updated_at=utcnow() if form.terms else None)
    worth_in = form.net_worth_currency or form.salary_currency  # the net worth can be in its own currency (fx.py converts for the guard)
    if form.net_worth is not None: company.net_worth, company.net_worth_currency, company.net_worth_updated_at, company.net_worth_by = form.net_worth, worth_in, utcnow(), request.state.user["username"]
    apply_identity(company, form, place)
    db.add(company)
    await db.flush()  # assigns the uuid4 id (= the join code)
    db.add(Membership(user_id=uid, company_id=company.id, position="owner", terms_version=1, terms_accepted_at=utcnow()))  # the owner wrote them
    db.add_all(CompanySalary(company_id=company.id, position=k, min_amount=v) for k, v in form.salaries().items())  # one per offered position
    if acct: db.add(CompanyBankAccount(company_id=company.id, holder_name=acct.holder_name, bank_name=acct.bank_name, branch=acct.branch, country=acct.country, routing=acct.routing,
                                       swift=acct.swift, bank_address=acct.bank_address, account_type=acct.account_type,
                                       account_enc=seal(f"company-bank:{company.id}", acct.account_number), account_last4=acct.account_number[-4:], updated_by=request.state.user["username"],
                                       verified_at=utcnow() if checked else None, verified_name=checked["name_at_bank"] if checked else ""))
    await update_company_embedding(db, company)  # pgvector: what search + recommendations match against
    if form.sells and form.open_shop: db.add(Shop(company_id=company.id, currency=form.salary_currency, status="open", opened_by=request.state.user["username"][:32], opened_at=utcnow()))  # "do you sell products?" -> the marketplace
    cid = company.id
    gone = lambda: (remove_picture("c", cid), remove_id_photo("c", cid))  # no files left behind for a company that doesn't exist
    try:  # the required logo + identity proof photo, re-encoded before anything is committed
        try: await store_picture(form.logo, "c", cid)
        except PictureError as e: raise FormError(f"Logo: {e}") from None
        try: await store_id_photo(form.id_proof, "c", cid)
        except PictureError as e: raise FormError(f"Identity proof photo: {e}") from None
    except FormError: discard_upload(form.id_proof); gone(); await db.rollback(); raise
    try: await db.commit()
    except IntegrityError:  # someone registered the same number a moment ago
        gone(); await db.rollback()
        raise FormError(f"That {form.registration_type} number is already registered to another company.") from None
    except Exception: gone(); raise
    extra = {"net worth": money(worth_in, form.net_worth) if form.net_worth is not None else "", "identity proof": form.id_proof_type, "shop": "opened" if form.sells and form.open_shop else "",
             "company bank account": f"{acct.holder_name}, {acct.bank_name}, account {acct.account_number} ({acct.account_type}), {acct.routing or ''} {acct.swift or ''}".strip() if acct else ""}
    changelog.company(cid, "company created", changelog.facts(company_facts(company, form.salaries()) | extra))
    changelog.add("u", uid, f"created the company {company.name}")
    worth = f" Net worth: {money(worth_in, form.net_worth)}." if form.net_worth else " Its net worth is 0: nobody can join or be paid until you deposit funds (company dashboard)."
    bank = f" Company account: {acct.bank_name} ending {acct.account_number[-4:]}{' (verified with the bank)' if checked else ''}." if acct else " Add its bank account on the company dashboard to pay salaries."
    if form.sells and form.open_shop:
        flash(request, f"{company.name} created, with its shop open.{worth}{bank} Add your first product: it's live for everyone on {APP_NAME} once it's for sale.", "success")
        return RedirectResponse(f"/companies/{company.id}/shop/products#new", 303)
    flash(request, f"{company.name} created.{worth}{bank} Share its code from your workplaces so people can join." + (" Open its shop any time from the company's shop page." if form.sells else ""), "success")
    return RedirectResponse(f"/workplaces#c-{company.id}", 303)
# ---------- join: code -> confirm -> always joins as JOIN_AS (rookie) ----------
SINGLE_JOIN = f"{APP_NAME} is in single company mode: you're already in its company, so there's no other company to join."
async def solo_member(db: AsyncSession, uid: int) -> bool:  # single company mode and uid is in a company already: joining another is off (user asked)
    return await single_company_mode(db, lock=False) and bool(await db.scalar(select(Membership.company_id).where(Membership.user_id == uid).limit(1)))
@router.get("/companies/join")
async def join_page(request: Request, db: AsyncSession = Depends(get_db)):
    if await solo_member(db, _uid(request)): flash(request, SINGLE_JOIN); return RedirectResponse("/dashboard", 303)
    return render(request, "join")
@router.post("/companies/join")
async def join_lookup(request: Request, form: JoinCodeForm = Depends(JoinCodeForm.as_form), db: AsyncSession = Depends(get_db)):
    if await solo_member(db, _uid(request)): raise FormError(SINGLE_JOIN)
    if not (company := await db.get(Company, uuid.UUID(form.code))): raise FormError("No company has that code.")
    return RedirectResponse(f"/companies/join/{company.id}", 303)
@router.get("/companies/join/{company_id}")
@public  # only so signed-out visitors (e.g. from search) get the login page + a message instead of an error page; nothing is shown to them
async def join_confirm_page(request: Request, company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if not request.state.user:
        listed = await db.scalar(select(Company.name).where(Company.id == company_id, Company.listed))  # name only public (listed) companies
        request.session["_next"] = request.url.path  # straight back here after logging in
        flash(request, f"You need to log in to join {listed or 'this company'}.", "info")
        return RedirectResponse("/login", 303)
    if not (company := await db.get(Company, company_id, options=[joinedload(Company.owner, innerjoin=True), selectinload(Company.salaries)])): raise HTTPException(404, "No company has that code.")
    if await db.get(Membership, (_uid(request), company.id)):
        flash(request, f"You're already in {company.name}.", "info")
        return RedirectResponse(f"/workplaces#c-{company.id}", 303)
    if busy := await on_duty(request, db, _uid(request)): return busy  # staff: no other companies during working hours
    blocked = join_block(company) or await no_room(db, company) or await other_companies_rule(db, _uid(request), company, lock=False) or (
        await missing_kyc(db, _uid(request)))  # shown up front; the POST checks again
    uid = _uid(request)
    offer = await db.get(Approach, (company.id, uid))  # approached by the company: joins straight away
    pending = await db.scalar(select(MemberRequest).where(MemberRequest.company_id == company.id, MemberRequest.user_id == uid, MemberRequest.kind == "join", MemberRequest.status == "pending"))
    enquiry = await db.scalar(select(Chat.id).where(Chat.company_id == company.id, Chat.kind == "enquiry", Chat.direct_key == str(uid)))  # asked them something already: its chat
    return render(request, "join_position", {"company": company, "positions": POSITIONS, "join_as": JOIN_AS, "freelance": FREELANCE, "blocked": blocked, "enquiry": enquiry,
                                             "offered": bool(offer and not offer.dismissed_at), "pending": pending, "standard_terms": STANDARD_TERMS,
                                             "terms_outdated": bool(pending and pending.terms_version != company.terms_version)})  # changed since they asked: accept again
@router.post("/companies/join/{company_id}")
async def join(request: Request, company_id: uuid.UUID, form: JoinForm = Depends(JoinForm.as_form), db: AsyncSession = Depends(get_db)):
    uid, position = _uid(request), form.as_position  # only JOIN_AS, or FREELANCE when the company offers it: never anything above
    company = await db.get(Company, company_id, with_for_update=True)  # row lock: the company can't change or vanish mid-join
    if not company: raise HTTPException(404, "No company has that code.")
    if position not in company.positions: raise FormError(f"{company.name} isn't taking new {label(position)}s right now.")  # Rookie: also a DB constraint
    name = company.name
    if not await db.get(Membership, (uid, company.id)):
        if await solo_member(db, uid): raise FormError(SINGLE_JOIN)  # (single company mode: no second company)
        if why := join_block(company) or await no_room(db, company): raise FormError(why)  # halted, no funds (net worth 0 / not set), or at its employee limit
        if busy := await at_work(db, uid): raise FormError(f"It's working hours at {busy.name}: you can't join other companies until they end.")
        if why := await missing_kyc(db, uid): raise FormError(why)
        if why := await other_companies_rule(db, uid, company): raise FormError(why)
        await _limits_ok(db, uid, creating=False)
        if not form.accept_terms: raise FormError(f"Read {name}'s terms and conditions and tick \"I accept\" to join.")
        if form.terms_version != company.terms_version: raise FormError(f"{name} changed its terms and conditions while you were reading them: read the new version and accept it again.")
        version, accepted = company.terms_version, utcnow()
        offer = await db.get(Approach, (company.id, uid))  # the salary in the offer they're accepting stays with them (if it's in today's currency)
        if not offer or offer.dismissed_at:  # nobody asked them: the owner / a co-owner / a manager has to approve (routers/membership.py)
            asked = await db.scalar(select(MemberRequest).where(MemberRequest.company_id == company_id, MemberRequest.user_id == uid, MemberRequest.kind == "join", MemberRequest.status == "pending"))
            if asked and asked.terms_version != version:  # the terms changed since they asked: this accepts the new ones
                asked.terms_version, asked.terms_accepted_at = version, accepted
                await db.commit()
                flash(request, f"You accepted {name}'s updated terms and conditions: your request can be approved now.", "success")
                return RedirectResponse(f"/companies/join/{company_id}", 303)
            if asked:
                flash(request, f"You've already asked to join {name}: waiting for the owner, a co-owner, a manager or HR.", "info")
                return RedirectResponse(f"/companies/join/{company_id}", 303)
            members = (await db.scalars(select(Membership).where(Membership.company_id == company_id, Membership.position.in_(JOIN_APPROVERS)).options(joinedload(Membership.user, innerjoin=True)))).all()
            r = MemberRequest(company_id=company_id, user_id=uid, kind="join", position=position, note=form.note, terms_version=version, terms_accepted_at=accepted)
            db.add(r)
            await db.flush()
            who = request.state.user["username"]
            await notify(db, [m.user for m in members], company, "warn", f"Join request: {who} wants to join {name} as {label(position)}",
                         f"{who} asked to join {name} as {label(position)}{': ' + form.note if form.note else ''}. Their profile, skills, identity proof and bank details are on the "
                         f"company dashboard: approve or decline it there.", f"/companies/{company_id}/dashboard#requests", f"member-request-new:{r.id}")
            await db.commit()
            flash(request, f"Request sent: the owner, a co-owner, a manager or HR of {name} reviews it (they'll see your profile, skills, identity proof and bank details). You'll be told either way.", "success")
            return RedirectResponse(f"/companies/join/{company_id}", 303)
        agreed = offer.salary_amount if offer.salary_currency == company.salary_currency and offer.salary_period == company.salary_period else None
        db.add(Membership(user_id=uid, company_id=company.id, position=position, salary=agreed, terms_version=version, terms_accepted_at=accepted))
        await back_in_company(db, company.id, uid)
        await db.execute(delete(Approach).where(Approach.user_id == uid, Approach.company_id == company.id))  # their invitation is answered
        try: await db.commit()
        except IntegrityError: await db.rollback()  # a double-submit already joined: same outcome (company is expired now: only company_id below)
        else: flash(request, f"You joined {name} as {label(position)}: they approached you, so no approval was needed." + (" Freelancers aren't monitored: no attendance tracking." if position == FREELANCE else
                                                                           " Your attendance is tracked while you're active here in working hours."), "success")
    else: flash(request, f"You're already in {name}.", "info")
    return RedirectResponse(f"/workplaces#c-{company_id}", 303)
# ---------- workplaces ----------
@router.get("/workplaces")
async def workplaces(request: Request, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    if not (memberships := await _memberships(db, uid, with_salaries=True)):  # the sidebar link is disabled then too; this enforces it server-side
        flash(request, "Join or create a company first.", "info")
        return RedirectResponse("/dashboard", 303)
    rank = lambda m: (POSITIONS.get(m.position, {"rank": 99})["rank"], m.user.username)
    unread = await unread_by_company(db, uid)  # the chat buttons' counts
    cards = [{"company": m.company, "me": m, "members": sorted(m.company.members, key=rank), "is_owner": m.company.owner_id == uid, "unread": unread.get(m.company_id, 0),
              "can_invite": POSITIONS.get(m.position, {}).get("can_invite", False), "assignable": assignable(m, m.company),
              "manageable": {x.user_id for x in m.company.members if can_manage(m, x)}, "can_edit": can_edit(m), "held": held(m.company),
              "problems": [x.user.username for x in m.company.members if not consistent(x, m.company)]} for m in memberships]
    return render(request, "workplaces", {"cards": cards, "positions": POSITIONS, "staff": STAFF, "join_as": JOIN_AS, **SALARY_CHOICES})
# ---------- edit (the MDB modal on workplaces): description, who they're hiring, public listing, positions offered ----------
def can_edit(m: Membership | None) -> bool: return bool(m and POSITIONS.get(m.position, {}).get("top"))  # owner + co-owners
def held(company: Company) -> dict[str, int]:
    """Offered positions that members hold right now -> how many: these can't be removed until those people are moved."""
    counts = {}
    for x in company.members:
        if x.position in STAFF: counts[x.position] = counts.get(x.position, 0) + 1
    return counts
def _rates(minimums: dict[str, int], position: str, agreed: int | None, period: str) -> tuple[int | None, int | None]:  # someone's (per cycle, minimum) under these salaries, as payroll.per_cycle
    base = max((x for x in (minimums.get(position), agreed) if x), default=None)
    return cycle_pay(base, period) if base else None, cycle_pay(minimums[position], period) if position in minimums else None
def reprice(company: Company, minimums: dict[str, int], form) -> bool:
    """The salaries are changing: what's agreed with each person moves to the new currency and period at the day's rate (it's the same pay, not 50,000 of another currency), the amounts
    kept for old cycles follow, and cycles already started stay at today's value (user's rule: new pay starts with the next cycle). Call it before the company's salaries change.
    True when someone's agreed salary was converted."""
    was_cur, was_period, cur, period, moved = company.salary_currency, company.salary_period, form.salary_currency, form.salary_period, False
    if not was_cur: return False
    new, again = form.salaries(), lambda x: convert(x, was_cur, cur)
    for m in company.members:
        old = tuple(again(x) for x in _rates(minimums, m.position, m.salary, was_period))
        if was_cur != cur and m.pay_history: m.pay_history = [{**r, "per": again(r["per"]), "low": again(r["low"])} for r in m.pay_history]
        if m.salary and (was_cur, was_period) != (cur, period): m.salary, moved = max(1, min(pay_in(m.salary, was_cur, was_period, cur, period), MAX_SALARY)), True
        if old != _rates(new, m.position, m.salary, period): keep_pay(company, m, old)
    return moved
@router.post("/companies/{company_id}/settings")
async def company_settings(request: Request, company_id: uuid.UUID, form: CompanySettingsForm = Depends(CompanySettingsForm.as_form), db: AsyncSession = Depends(get_db)):
    company = await db.get(Company, company_id, with_for_update=True, options=[selectinload(Company.members)])  # locked: no position change races this
    # (selectinload, not joinedload: joining the members list into a FOR UPDATE query would lock every membership row and repeat the company per member)
    actor = await db.get(Membership, (_uid(request), company_id)) if company else None
    if not can_edit(actor): raise FormError("Only the owner or a co-owner can edit a company.")  # same answer whether it exists or not
    if company.halted_at: raise FormError(halted_reason(company, "change its details"))
    if not picture("c", company_id): raise FormError(f"Add {company.name}'s logo first (open the company, then logo): every company needs one.")
    if blocked := [k for k in company.positions if k not in form.positions and k in held(company)]:
        raise FormError(f"Can't remove {', '.join(label(k) for k in blocked)}: someone holds it. Move them to another position first.")
    if form.max_members < len(company.members): raise FormError(f"{company.name} already has {len(company.members)} people, so its employee limit can't be lower than that. Remove people first.")
    hours_moved = (form.work_start, form.work_end, form.days_mask()) != (company.work_start, company.work_end, company.work_days)
    if hours_moved and (free := hours_locked(company)): raise FormError(f"{company.name}'s working hours can only change once every {HOURS_EVERY_DAYS} days: the next change can be from {free}. Nothing was saved.")
    place = await company_identity(db, form, company)  # a new location is verified with Geoapify; the registration number must be unused
    minimums = dict((await db.execute(select(CompanySalary.position, CompanySalary.min_amount).where(CompanySalary.company_id == company_id))).all())
    before = company_facts(company, minimums)
    converted = reprice(company, minimums, form)
    added, removed = [k for k in form.positions if k not in company.positions], [k for k in company.positions if k not in form.positions]
    elsewhere = 0  # turning the rule off doesn't remove anyone: staff already in other companies stay, but can't join more
    if company.allow_other_companies and not form.allow_other_companies:
        staff = [m.user_id for m in company.members if not POSITIONS.get(m.position, {}).get("top")]
        elsewhere = await db.scalar(select(func.count(func.distinct(Membership.user_id))).where(Membership.user_id.in_(staff), Membership.company_id != company_id)) if staff else 0
    if terms_changed := form.terms != company.terms:  # a new version: whoever edits accepts it; everyone else is asked to (dashboard), applicants before approval
        company.terms, company.terms_version, company.terms_updated_at = form.terms, company.terms_version + 1, utcnow()
        actor.terms_version, actor.terms_accepted_at = company.terms_version, utcnow()
        others = (await db.scalars(select(User).join(Membership, Membership.user_id == User.id).where(Membership.company_id == company_id, User.id != actor.user_id))).all()
        asking = (await db.scalars(select(User).join(MemberRequest, MemberRequest.user_id == User.id).where(MemberRequest.company_id == company_id, MemberRequest.kind == "join",
                                                                                                             MemberRequest.status == "pending"))).all()
        key = f"terms:{company_id}:{company.terms_version}"
        await notify(db, list(others), company, "warn", f"{company.name} updated its terms and conditions",
                     f"{company.name} changed its terms and conditions (version {company.terms_version}). Read them and accept the new version on its dashboard.", f"/companies/{company_id}/dashboard#terms", key)
        await notify(db, list(asking), company, "warn", f"{company.name} updated its terms and conditions",
                     f"{company.name} changed its terms while you were waiting to join: accept the new version on the join page, or your request can't be approved.", f"/companies/join/{company_id}", key)
    company.description, company.hiring, company.listed, company.positions = form.description, form.hiring, form.listed, form.positions
    company.allow_other_companies, company.salary_currency, company.salary_period = form.allow_other_companies, form.salary_currency, form.salary_period
    apply_identity(company, form, place)
    if hours_moved: company.hours_changed_at = utcnow()  # (the company's row is locked above: two changes at once can't both pass)
    limit_moved, company.max_members = form.max_members != company.max_members, form.max_members
    await db.execute(delete(CompanySalary).where(CompanySalary.company_id == company_id))  # replace: one minimum per offered position
    db.add_all(CompanySalary(company_id=company_id, position=k, min_amount=v) for k, v in form.salaries().items())
    await update_company_embedding(db, company)
    after = company_facts(company, form.salaries())
    try: await db.commit()
    except IntegrityError:
        await db.rollback()
        raise FormError(f"That {form.registration_type} number is already registered to another company.") from None
    changed = " · ".join(filter(None, (changelog.diff(before, after), "employee limit changed" if limit_moved else "")))  # (without the number: managers and HR can download this log, and only the owner and co-owners see it)
    changelog.company(company_id, "company details changed", (changed or "saved again, nothing changed") + (f" · terms are now version {company.terms_version}" if terms_changed else ""))
    changes = [f"+{label(k)}" for k in added] + [f"−{label(k)}" for k in removed]
    rule = "Employees may join other companies." if company.allow_other_companies else "Employees must work only here" + (
        f" ({elsewhere} already work elsewhere: they can stay, but can't join more)." if elsewhere else ".")
    terms = f" Terms and conditions updated (version {company.terms_version}): every member and applicant is asked to accept them." if terms_changed else ""
    terms += f" Salaries agreed with people were converted to {company.salary_currency} per {company.salary_period} at today's rate; new pay starts with each person's next cycle." if converted else ""
    terms += f" Working hours changed: the next change can be from {hours_locked(company)}." if hours_moved else ""
    flash(request, f"{company.name} saved{' (' + ', '.join(changes) + ')' if changes else ''}. {'Listed: anyone can find and join it.' if company.listed else 'Not listed: invite code only.'} {rule}{terms}", "success")
    return RedirectResponse(f"/workplaces#c-{company_id}", 303)
@router.post("/companies/{company_id}/terms/accept")
async def accept_terms(request: Request, company_id: uuid.UUID, form: TermsForm = Depends(TermsForm.as_form), db: AsyncSession = Depends(get_db)):
    """A member accepting the company's current terms (after a change, or having joined before terms existed): recorded with the time."""
    m = await db.get(Membership, (_uid(request), company_id), options=[joinedload(Membership.company, innerjoin=True)])
    if not m: raise FormError("You're not in that company.")
    if not form.accept_terms: raise FormError(f"Tick \"I accept\" to accept {m.company.name}'s terms and conditions.")
    if form.terms_version != m.company.terms_version: raise FormError(f"{m.company.name}'s terms changed again while you were reading them: read the new version.")
    m.terms_version, m.terms_accepted_at, name = form.terms_version, utcnow(), m.company.name
    await db.commit()
    flash(request, f"You accepted {name}'s terms and conditions (version {form.terms_version}).", "success")
    return RedirectResponse(f"/companies/{company_id}/dashboard#terms", 303)
@router.post("/companies/{company_id}/members/{user_id}/position")
async def set_position(request: Request, company_id: uuid.UUID, user_id: int, form: PositionForm = Depends(PositionForm.as_form), db: AsyncSession = Depends(get_db)):
    company = await db.get(Company, company_id, with_for_update=True, options=[selectinload(Company.salaries)])
    actor = await db.get(Membership, (_uid(request), company_id)) if company else None
    target = await db.get(Membership, (user_id, company_id), with_for_update={"of": Membership},  # user joined in; only the membership row is locked
                          options=[joinedload(Membership.user, innerjoin=True)]) if actor else None
    if not (company and actor and target): raise FormError("That person isn't a member of your company.")  # same answer either way
    if company.halted_at: raise FormError(halted_reason(company, "change anyone's position"))
    if not can_manage(actor, target): raise FormError("Only the owner or a co-owner can change positions, and only for people below them.")
    if form.position not in assignable(actor, company): raise FormError(f"You can't give the {label(form.position)} position in {company.name}.")
    if form.position == target.position: raise FormError(f"{target.user.username} is already {label(form.position)}.")
    keep_pay(company, target)  # (user's rule) the new position's pay starts with their next cycle
    if target.position == "co_owner": await db.execute(delete(OwnershipOffer).where(OwnershipOffer.company_id == company_id, OwnershipOffer.to_user_id == user_id))  # an offer only stands for a co-owner
    old, target.position, target.position_since = target.position, form.position, utcnow()
    await db.commit()
    changelog.company(company_id, "position changed", f"{target.user.username}: {label(old)} → {label(form.position)}")
    changelog.add("u", user_id, f"position at {company.name} changed", f"{label(old)} → {label(form.position)}")
    flash(request, f"{target.user.username}: {label(old)} → {label(form.position)}.", "success")
    return RedirectResponse(f"/workplaces#c-{company_id}", 303)
# ---------- a member's full profile: every member of a company can open every colleague's (the CV PDF: owner + co-owners only) ----------
async def _member_for(db: AsyncSession, request: Request, company_id: uuid.UUID, user_id: int, options: list, top_only: bool = False) -> tuple[Membership, Membership]:
    """(viewer, target): the viewer must be in this company (top level for top_only) and the target a member of it; otherwise the same 404."""
    viewer = await db.get(Membership, (_uid(request), company_id))
    allowed = can_edit(viewer) if top_only else viewer is not None
    target = await db.get(Membership, (user_id, company_id), options=options, populate_existing=True) if allowed else None  # populate_existing: loads even if target is the viewer
    if not target: raise HTTPException(404, "That person isn't a member of your company.")
    return viewer, target
@router.get("/companies/{company_id}/members/{user_id}")
async def member_profile(request: Request, company_id: uuid.UUID, user_id: int, db: AsyncSession = Depends(get_db)):
    """Position here, when they joined and for how long, their biodata + CV, and the other companies they work at.
    One query for the membership + company + account + profile (joinedload), one for their other workplaces (+ joined company).
    (Not selectinload(User.memberships): that list includes this same membership, and re-loading it would wipe the joined-in user.)"""
    viewer, m = await _member_for(db, request, company_id, user_id, [joinedload(Membership.company, innerjoin=True), joinedload(Membership.user, innerjoin=True).joinedload(User.profile)])
    others = (await db.scalars(select(Membership).where(Membership.user_id == user_id, Membership.company_id != company_id).order_by(Membership.joined_at)
                               .options(joinedload(Membership.company, innerjoin=True)))).all()
    can_id = viewer.position in (*TOP, "hr") or viewer.user_id == user_id  # identity proofs: the owner, co-owners and HR (and the person)
    can_contact = viewer.position in ("owner", "co_owner", "manager", "hr") or viewer.user_id == user_id  # email + phone: the company's top levels (user asked)
    return render(request, "member", {"m": m, "member": m.user, "profile": m.user.profile, "others": others, "positions": POSITIONS, "can_cv": can_edit(viewer), "can_contact": can_contact, "can_remove": can_manage(viewer, m),
                                      "ident": await db.get(IdentityProof, user_id) if can_id else None, "can_id": can_id, "id_types": ID_TYPES,
                                      "reviews": await review_panel(db, company_id, viewer, user_id),
                                      "assignable": [k for k in assignable(viewer, m.company) if k != m.position] if can_manage(viewer, m) else []})
@router.get("/companies/{company_id}/members/{user_id}/cv")
async def member_cv(request: Request, company_id: uuid.UUID, user_id: int, db: AsyncSession = Depends(get_db)):
    """The member's CV PDF (virus-scanned + checked to be a PDF at upload), as a download only, never shown inline."""
    _, m = await _member_for(db, request, company_id, user_id, [joinedload(Membership.user, innerjoin=True).joinedload(User.profile)], top_only=True)
    p = m.user.profile
    path = (UPLOAD_DIR / p.resume_file).resolve() if p and p.resume_file else None
    if not path or path.parent != UPLOAD_DIR.resolve() or not path.is_file(): raise HTTPException(404, f"{m.user.username} hasn't uploaded a CV.")
    return FileResponse(path, media_type="application/pdf", filename=f"{m.user.username}-cv.pdf", content_disposition_type="attachment", headers={"Cache-Control": "private, no-store"})
@router.post("/companies/{company_id}/leave")
async def leave(request: Request, company_id: uuid.UUID, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """Asking to leave: someone above them (owner / co-owner / manager) approves it, once their salary is settled (routers/membership.py)."""
    uid = _uid(request)
    if not (m := await db.get(Membership, (uid, company_id), options=[joinedload(Membership.company, innerjoin=True).selectinload(Company.members).joinedload(Membership.user, innerjoin=True)])):
        raise FormError("You're not in that company.")
    if m.position == "owner": raise FormError("Owners can't leave their own company; delete it instead.")
    c, link = m.company, f"/companies/{company_id}/dashboard"
    asked = select(MemberRequest.id).where(MemberRequest.company_id == company_id, MemberRequest.user_id == uid, MemberRequest.kind == "exit", MemberRequest.status == "pending")
    if await db.scalar(asked):
        flash(request, f"You've already asked to leave {c.name}: waiting for approval.", "info")
        return RedirectResponse(f"{link}#requests", 303)
    r = MemberRequest(company_id=company_id, user_id=uid, kind="exit")
    db.add(r)
    await db.flush()
    who, name = request.state.user["username"], c.name
    await notify(db, [x.user for x in c.members if can_decide(x, m)], c, "warn", f"Leave request: {who} wants to leave {name}",
                 f"{who} ({label(m.position)}) asked to leave {name}. Settle their salary (every cycle up to today), then approve it on the company dashboard.", f"{link}#requests", f"member-request-new:{r.id}")
    await db.commit()
    changelog.company(company_id, "asked to leave", f"{who} ({label(m.position)})"); changelog.add("u", uid, f"asked to leave {name}")
    flash(request, f"You asked to leave {name}: someone above you approves it once your salary is settled. Until then you're still a member.", "info")
    return RedirectResponse(f"{link}#requests", 303)
# ---------- deleting a company: salaries settled first; with co-owners, every one of them must approve (one decline stops it) ----------
async def _for_deletion(db: AsyncSession, company_id: uuid.UUID) -> Company | None:  # locked, with members + their accounts + salaries
    return await db.get(Company, company_id, with_for_update=True, options=[selectinload(Company.members).joinedload(Membership.user, innerjoin=True), selectinload(Company.salaries)])
async def _who_is_owed(db: AsyncSession, c: Company) -> list[str]:
    payouts = (await db.scalars(select(Payout).where(Payout.company_id == c.id))).all()
    return unsettled(pay_rows(c, c.members, payouts, await approved_leaves(db, [c.id])), c.members)
OPEN_ORDERS = ("placed", "packed", "shipped", "out_for_delivery")  # bought and not delivered yet
async def deletion_block(db: AsyncSession, c: Company) -> str | None:
    """Why c can't be deleted right now, or None. The same for every way of doing it: the company's own button, the co-owners' last vote, its owner deleting their account."""
    if c.halted_at or c.liability_since: return f"{c.name} can't be deleted while it owes its employees more than its net worth: pay them or add funds first."
    if owed := await _who_is_owed(db, c): return f"Settle every salary before deleting {c.name}: {'; '.join(owed)} still due (the current cycle too)."
    if n := await db.scalar(select(func.count()).where(Order.company_id == c.id, Order.status.in_(OPEN_ORDERS))): return f"{c.name} has {n} order{'s' if n != 1 else ''} not delivered yet: deliver or cancel {'them' if n != 1 else 'it'} first, or the buyers are left with nobody to ship or refund."
    if n := await db.scalar(select(func.count()).where(Order.company_id == c.id, Order.status == "delivered", Order.return_requested_at.is_not(None), Order.return_declined_at.is_(None), Order.returned_at.is_(None))):
        return f"{c.name} has {n} return{'s' if n != 1 else ''} to answer first."
    if offer := await db.get(OwnershipOffer, c.id): return f"{c.name} is offered to {next((m.user.username for m in c.members if m.user_id == offer.to_user_id), 'a co-owner')}: withdraw that first."
    return None
async def _delete_now(request: Request, db: AsyncSession, c: Company) -> RedirectResponse:
    name = await erase_company(db, c)
    flash(request, f"{name} was deleted and removed from every member's workplaces; everyone in it has been told.", "success")
    return RedirectResponse("/dashboard", 303)
async def erase_company(db: AsyncSession, c: Company, reason: str = "") -> str:
    """Everyone in it is told (bell + email) that it's gone and what it paid them; then it's deleted (memberships cascade; notices stay).
    reason: the admin deleted it (routers/admin.py), and why: the members are told that instead of "by its owner". Returns its name.
    c must have its members (+ their users) loaded."""
    paid = {}
    for p in (await db.scalars(select(Payout).where(Payout.company_id == c.id).order_by(Payout.cycle))).all():
        s = paid.setdefault(p.user_id, [{}, 0, None]); s[0][p.currency] = s[0].get(p.currency, 0) + p.net; s[1] += 1; s[2] = p.cycle  # net per currency: salaries can be paid in any
    name, cid, rows, texts = c.name, c.id, [], {}
    by = f"by the {APP_NAME} admin. Reason: {reason}" if reason else f"by its owner{' with every co-owner agreeing' if any(x.position == 'co_owner' for x in c.members) else ''}"
    for m in c.members:
        nets, count, last = paid.get(m.user_id, [{}, 0, None])
        money_line = (f"It paid you {count} salar{'y' if count == 1 else 'ies'}, {' + '.join(money(cur, n) for cur, n in nets.items())} net in all (up to cycle {last}); every salary due to you was "
                      f"settled before the deletion." if count else "It owed you no salary when it was deleted.")
        texts[m.user_id] = f"{name} has been deleted {by}. {money_line} Your payslips and receipts stay on your statement."
        rows.append({"user_id": m.user_id, "company_id": cid, "level": "danger", "title": f"{name} has been deleted", "body": texts[m.user_id], "link": "/statements", "key": f"deleted:{cid}"})
    for o in (await db.scalars(select(Order).where(Order.company_id == cid, Order.status.in_(("pending", *OPEN_ORDERS))).with_for_update())).all():  # only when the admin deletes it (deletion_block stops the owner): nobody is left to ship these
        await cancel(db, o, f"{name} closed", APP_NAME, "system")
    new = await notify_many(db, rows)  # everyone in one statement
    mails = [(m.user.email, m.user.username, texts[m.user_id]) for m in c.members if (m.user_id, f"deleted:{cid}") in new]
    calls = (await db.scalars(select(Meeting.id).where(Meeting.company_id == cid))).all()
    chats = (await db.scalars(select(Chat.id).where(Chat.company_id == cid))).all()  # its chats' files go with them
    members = [m.user_id for m in c.members]
    await db.delete(c)  # Postgres ON DELETE CASCADE removes every membership + meeting; payouts + notices keep their own copy (SET NULL)
    await db.commit()
    remove_chat_files(chats)
    changelog.company(cid, "company deleted" + (f" by the admin: {reason}" if reason else ""), f"this change log is kept until {changelog.retire('c', cid)}, then erased", by="admin" if reason else None)
    for u in members: changelog.add("u", u, f"{name} was deleted" + (f" by the admin: {reason}" if reason else ""), by="admin" if reason else None)
    remove_picture("c", cid); remove_id_photo("c", cid); remove_files(calls)  # its logo, identity proof photo and meeting recordings go too
    for to, who, text in mails:
        try: await send_email(to, f"{name} has been deleted", f"Hi {who},\n\n{text}\n\nThis is an automatic message from {APP_NAME}.\n")
        except Exception as e: print(f"deletion email failed: {type(e).__name__}")
    return name
@router.post("/companies/{company_id}/delete")
async def delete_company(request: Request, company_id: uuid.UUID, form: ConfirmForm = Depends(ConfirmForm.as_form), db: AsyncSession = Depends(get_db)):
    company = await _for_deletion(db, company_id)
    if not company or company.owner_id != _uid(request): raise FormError("Only a company's owner can delete it.")  # same answer whether it exists or not
    if form.confirm.strip() != company.name: raise FormError("Type the company's exact name to confirm deleting it.")
    if why := await deletion_block(db, company): raise FormError(why)
    if not (co := [m for m in company.members if m.position == "co_owner"]): return await _delete_now(request, db, company)
    if await db.get(DeletionRequest, company_id): raise FormError(f"Deleting {company.name} is already requested: waiting for the co-owners.")
    name, cid, by = company.name, company.id, request.state.user["username"]
    db.add(DeletionRequest(company_id=cid, requested_by=by, approved_by=[]))
    text = f"{by} (owner) wants to delete {name}. Every co-owner has to approve it on {name}'s dashboard; if any of you declines, it stops."
    got = await notify(db, [m.user for m in co], company, "danger", f"Approve deleting {name}?", text, f"/companies/{cid}/dashboard#danger", f"delete-ask:{cid}:{utcnow():%Y%m%d%H%M%S}")
    mails = [(u.email, u.username) for u in got]
    await db.commit()
    changelog.company(cid, "deletion requested", f"by {by} (owner); every co-owner has to approve")
    for to, who in mails:
        try: await send_email(to, f"Approve deleting {name}?", f"Hi {who},\n\n{text}\n\nThis is an automatic message from {APP_NAME}.\n")
        except Exception as e: print(f"deletion email failed: {type(e).__name__}")
    flash(request, f"Deletion requested: all {len(co)} co-owner{'s' if len(co) != 1 else ''} of {name} must approve it; one decline stops it.", "info")
    return RedirectResponse(f"/companies/{cid}/dashboard#danger", 303)
@router.post("/companies/{company_id}/delete/vote")
async def deletion_vote(request: Request, company_id: uuid.UUID, form: DecisionForm = Depends(DecisionForm.as_form), db: AsyncSession = Depends(get_db)):
    uid, company = _uid(request), await _for_deletion(db, company_id)
    me = next((m for m in company.members if m.user_id == uid), None) if company else None
    if not me or me.position != "co_owner": raise FormError("Only the co-owners vote on deleting a company.")
    if not (ask := await db.get(DeletionRequest, company_id, with_for_update=True)): raise FormError(f"Nobody has asked to delete {company.name}.")
    name, cid, link = company.name, company.id, f"/companies/{company_id}/dashboard#danger"
    if form.decision == "decline":  # one decline stops the whole thing
        await db.delete(ask)
        await notify(db, [m.user for m in company.members if m.position in ("owner", "co_owner")], company, "warn", f"Deleting {name} stopped",
                     f"{me.user.username} (co-owner) declined deleting {name}, so it won't be deleted.", link, f"delete-stop:{cid}:{utcnow():%Y%m%d%H%M%S}")
        await db.commit()
        changelog.company(cid, "deletion declined", f"by {me.user.username} (co-owner): it won't be deleted")
        flash(request, f"You declined: {name} won't be deleted, and its owner has been told.", "info")
        return RedirectResponse(link, 303)
    ask.approved_by = sorted({*ask.approved_by, uid})
    if waiting := [m.user.username for m in company.members if m.position == "co_owner" and m.user_id not in ask.approved_by]:
        await db.commit()
        changelog.company(cid, "deletion approved", f"by {me.user.username} (co-owner); still waiting for {', '.join(waiting)}")
        flash(request, f"You approved deleting {name}. Still waiting for: {', '.join(waiting)}.", "info")
        return RedirectResponse(link, 303)
    if why := await deletion_block(db, company):  # every co-owner agreed: check it all once more, right before
        await db.commit()
        raise FormError(f"Everyone approved, but not yet: {why} Then approve once more to delete {name}.")
    return await _delete_now(request, db, company)
@router.post("/companies/{company_id}/delete/cancel")
async def cancel_deletion(request: Request, company_id: uuid.UUID, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    company = await db.get(Company, company_id)
    if not company or company.owner_id != _uid(request): raise FormError("Only the owner can withdraw a deletion request.")
    if ask := await db.get(DeletionRequest, company_id): await db.delete(ask); await db.commit(); changelog.company(company_id, "deletion request withdrawn")
    flash(request, f"Deletion request for {company.name} withdrawn.", "info")
    return RedirectResponse(f"/companies/{company_id}/dashboard#danger", 303)
