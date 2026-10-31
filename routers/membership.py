"""Joining needs approval from the owner, a co-owner, a manager or HR; leaving from the owner, a co-owner or a manager above the person
(never HR): routers/companies.py files
the requests (MemberRequest), this decides them. A join is approved only if every joining rule still holds (bank details + identity
proof, limits, the "work only here" rule, not halted); leaving only once the person's salary is settled, every cycle up to today paid.
The approver sees the applicant's full details on the company dashboard; the person is told the outcome (bell + email). Someone the
company approached (an open job offer) joins straight away, without a request.
Removing someone (user's rule): the owner or a co-owner, for anyone below them, on the same condition as leaving: every salary due paid.
Handing a company over (user's rule): the owner offers it to a co-owner with their password, and nothing changes until that co-owner accepts with theirs; the old owner stays on as a co-owner."""
import uuid
import changelog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from form import ActionForm, DecisionForm, ReasonForm, TransferAnswerForm, TransferForm
from guard import halted_reason, join_block, no_room, notify
from payroll import approved_leaves, pay_rows, unsettled
from routers.companies import _limits_ok, can_edit, can_manage, label, missing_kyc, other_companies_rule
from routers.leave import JOIN_APPROVERS, can_decide
from routers.settings import _verified
from system import APP_NAME, CYCLE_EVERY, FormError, flash, send_email
from tables import Approach, Company, DeletionRequest, MemberRequest, Membership, OwnershipOffer, Payout, User, back_in_company, get_db, left_company, utcnow
router = APIRouter(tags=["membership"])
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
async def owed_to(db: AsyncSession, c: Company, m: Membership) -> list[str]:
    """What m is still owed by c (empty = settled): every cycle up to today with pay due, after unpaid leave."""
    payouts = (await db.scalars(select(Payout).where(Payout.company_id == c.id, Payout.user_id == m.user_id))).all()
    return unsettled(pay_rows(c, [m], payouts, [l for l in await approved_leaves(db, [c.id]) if l.user_id == m.user_id]), [m])
@router.post("/member-requests/{request_id}/decide")
async def decide(request: Request, request_id: int, form: DecisionForm = Depends(DecisionForm.as_form), db: AsyncSession = Depends(get_db)):
    uid, r = _uid(request), await db.get(MemberRequest, request_id, with_for_update=True)
    approver = await db.get(Membership, (uid, r.company_id), options=[joinedload(Membership.user, innerjoin=True)]) if r else None
    target = await db.get(Membership, (r.user_id, r.company_id), options=[joinedload(Membership.user, innerjoin=True)]) if r and r.kind == "exit" else None
    allowed = r and approver and approver.user_id != r.user_id and (approver.position in JOIN_APPROVERS if r.kind == "join" else can_decide(approver, target))
    if not allowed: raise FormError("Joining is decided by the owner, a co-owner, a manager or HR; leaving by the owner, a co-owner or a manager above them.")  # same answer either way
    if r.status != "pending": raise FormError(f"That request was already {r.status}.")
    c = await db.get(Company, r.company_id, with_for_update=True, options=[selectinload(Company.salaries)])
    person = target.user if target else await db.get(User, r.user_id)
    name, email, cname, cid, by, link = person.username, person.email, c.name, c.id, approver.user.username, f"/companies/{c.id}/dashboard"
    what = "join" if r.kind == "join" else "leave"
    if form.decision == "approve" and r.kind == "join":
        if not await db.get(Membership, (r.user_id, cid)):
            if why := join_block(c): raise FormError(why)  # halted, or no funds (net worth 0 / not set)
            if await no_room(db, c):  # the limit's number is for the owner and co-owners only: a manager or HR approving this hears who to ask
                raise FormError(f"{cname} has reached its employee limit of {c.max_members}: raise it in the company's details, or someone has to leave first." if can_edit(approver) else f"{cname} can't take on anyone else right now: ask the owner or a co-owner.")
            if r.position not in c.positions: raise FormError(f"{cname} doesn't offer {label(r.position)} any more: decline the request.")
            if await missing_kyc(db, r.user_id): raise FormError(f"{name} can't be taken on yet: their bank details or identity proof are missing (they removed them after asking).")
            if r.terms_version != c.terms_version: raise FormError(f"{name} accepted an older version of {cname}'s terms and conditions: they've been asked to accept the current one on the join page first.")
            if why := await other_companies_rule(db, r.user_id, c): raise FormError(f"{name} can't join under the company rules: {why}")
            await _limits_ok(db, r.user_id, creating=False)
            db.add(Membership(user_id=r.user_id, company_id=cid, position=r.position, terms_version=r.terms_version, terms_accepted_at=r.terms_accepted_at))  # salary cycle 1 starts today
            await back_in_company(db, cid, r.user_id)
            await db.execute(delete(Approach).where(Approach.user_id == r.user_id, Approach.company_id == cid))
        text = f"{by} approved your request: you're in {cname} as {label(r.position)}. Your salary is counted from today, paid {CYCLE_EVERY}."
    elif form.decision == "approve":  # leaving: only once everything they earned is paid
        if target.position == "owner": raise FormError("The owner can't leave: delete the company instead.")
        if owed := await owed_to(db, c, target):
            cycles = "; ".join(o.split(" (", 1)[-1].rstrip(")") for o in owed)
            raise FormError(f"{name} still has salary due ({cycles}, the current cycle too): pay it from the payroll first, then approve their leaving.")
        await db.delete(target)
        await left_company(db, cid, r.user_id)
        text = f"{by} approved your request to leave {cname}: every salary due to you was paid first, and your payslips stay on your statement."
    else: text = f"{by} declined your request to {what} {cname}."
    r.status, r.decided_by, r.decided_at = ("approved" if form.decision == "approve" else "declined"), by, utcnow()
    got = await notify(db, [person], c, "info" if r.status == "approved" else "warn", f"Request to {what} {cname} {r.status}", text, link if r.status == "approved" and r.kind == "join" else "/dashboard",
                       f"member-request:{r.id}")
    await db.commit()
    if got:
        try: await send_email(email, f"Your request to {what} {cname} was {r.status}", f"Hi {name},\n\n{text}\n\nThis is an automatic message from {APP_NAME}.\n")
        except Exception as e: print(f"membership email failed: {type(e).__name__}")
    flash(request, f"{name}'s request to {what} {r.status}." + (f" {name} is now {label(r.position)} here." if r.status == "approved" and r.kind == "join" else ""), "success")
    return RedirectResponse(f"{link}#requests", 303)
@router.post("/member-requests/{request_id}/cancel")
async def cancel(request: Request, request_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    r = await db.get(MemberRequest, request_id, with_for_update=True)
    if not r or r.user_id != _uid(request): raise FormError("That's not your request.")
    if r.status != "pending": raise FormError(f"That request was already {r.status}.")
    r.status, cid, kind = "cancelled", r.company_id, r.kind
    await db.commit()
    flash(request, "Request withdrawn.", "info")
    return RedirectResponse(f"/companies/join/{cid}" if kind == "join" else f"/companies/{cid}/dashboard", 303)
# ---------- removing someone from a company ----------
@router.post("/companies/{company_id}/members/{user_id}/remove")
async def remove_member(request: Request, company_id: uuid.UUID, user_id: int, form: ReasonForm = Depends(ReasonForm.as_form), db: AsyncSession = Depends(get_db)):
    c = await db.get(Company, company_id, with_for_update=True, options=[selectinload(Company.salaries)])
    actor = await db.get(Membership, (_uid(request), company_id), options=[joinedload(Membership.user, innerjoin=True)]) if c else None
    target = await db.get(Membership, (user_id, company_id), with_for_update={"of": Membership}, options=[joinedload(Membership.user, innerjoin=True)]) if actor else None
    if not (c and actor and target): raise FormError("That person isn't a member of your company.")  # same answer either way
    if not can_manage(actor, target): raise FormError("Only the owner or a co-owner can remove someone, and only people below them.")
    if c.halted_at: raise FormError(halted_reason(c, "remove anyone"))
    name, email, cname, by = target.user.username, target.user.email, c.name, actor.user.username
    if owed := await owed_to(db, c, target):
        cycles = "; ".join(o.split(" (", 1)[-1].rstrip(")") for o in owed)
        raise FormError(f"{name} still has salary due ({cycles}, the current cycle too): pay it from the payroll first, then remove them.")
    text, person = f"{by} removed you from {cname}{': ' + form.reason if form.reason else ''}. Every salary due to you was paid first, and your payslips stay on your statement.", target.user
    await db.delete(target)
    await left_company(db, company_id, user_id)
    await db.execute(update(MemberRequest).where(MemberRequest.company_id == company_id, MemberRequest.user_id == user_id, MemberRequest.kind == "exit", MemberRequest.status == "pending").values(status="approved", decided_by=by, decided_at=utcnow()))
    got = await notify(db, [person], c, "warn", f"You were removed from {cname}", text, "/statements", f"removed:{company_id}:{user_id}:{utcnow():%Y%m%d%H%M%S}")
    await db.commit()
    changelog.company(company_id, "member removed", f"{name}, by {by}" + (f": {form.reason}" if form.reason else "")); changelog.add("u", user_id, f"removed from {cname}", f"by {by}" + (f": {form.reason}" if form.reason else ""))
    if got:
        try: await send_email(email, f"You were removed from {cname}", f"Hi {name},\n\n{text}\n\nThis is an automatic message from {APP_NAME}.\n")
        except Exception as e: print(f"membership email failed: {type(e).__name__}")
    flash(request, f"{name} is no longer in {cname}: they've been told.", "success")
    return RedirectResponse(f"/companies/{company_id}/dashboard", 303)
# ---------- handing a company over ----------
def _danger(company_id) -> RedirectResponse: return RedirectResponse(f"/companies/{company_id}/dashboard#danger", 303)
async def _bell(db: AsyncSession, who: User, c: Company, level: str, title: str, text: str, key: str) -> tuple:  # one person's bell; give what it returns to _mail after the commit
    await notify(db, [who], c, level, title, text, f"/companies/{c.id}/dashboard#danger", key)
    return who.email, title, f"Hi {who.username},\n\n{text}\n\nThis is an automatic message from {APP_NAME}.\n"
async def _mail(to: str, title: str, body: str) -> None:
    try: await send_email(to, title, body)
    except Exception as e: print(f"ownership email failed: {type(e).__name__}")
@router.post("/companies/{company_id}/transfer")
async def offer_ownership(request: Request, company_id: uuid.UUID, form: TransferForm = Depends(TransferForm.as_form), db: AsyncSession = Depends(get_db)):
    uid, c = _uid(request), await db.get(Company, company_id, with_for_update=True)
    if not c or c.owner_id != uid: raise FormError("Only a company's owner can hand it over.")  # same answer either way
    await _verified(db, request, form.password)
    if c.halted_at: raise FormError(halted_reason(c, "change hands"))
    if await db.get(DeletionRequest, company_id): raise FormError(f"You've asked to delete {c.name}: withdraw that first.")
    if await db.get(OwnershipOffer, company_id): raise FormError(f"{c.name} is already on offer: withdraw that first.")
    target = await db.get(Membership, (form.user_id, company_id), options=[joinedload(Membership.user, innerjoin=True)])
    if not target or target.position != "co_owner": raise FormError(f"{c.name} can only go to one of its co-owners: appoint them first.")
    name, by, cname = target.user.username, request.state.user["username"], c.name
    db.add(OwnershipOffer(company_id=company_id, to_user_id=form.user_id, by_name=by))
    mail = await _bell(db, target.user, c, "warn", f"{by} is offering you {cname}", f"{by} wants to hand {cname} over to you. If you accept you become its owner, with everything that comes with it (its people's pay first), and {by} stays on as a co-owner. Nothing changes until you answer on its dashboard.", f"offer:{company_id}:{utcnow():%Y%m%d%H%M%S}")
    await db.commit()
    await _mail(*mail)
    changelog.company(company_id, "ownership offered", f"to {name}, by {by}: nothing changes until they accept")
    flash(request, f"Offered to {name}: nothing changes until they accept.", "info")
    return _danger(company_id)
@router.post("/companies/{company_id}/transfer/cancel")
async def withdraw_ownership(request: Request, company_id: uuid.UUID, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    c = await db.get(Company, company_id, with_for_update=True)
    if not c or c.owner_id != _uid(request): raise FormError("Only a company's owner can withdraw an offer.")
    if offer := await db.get(OwnershipOffer, company_id):
        who = await db.get(User, offer.to_user_id)
        await db.delete(offer)
        mail = await _bell(db, who, c, "info", f"{c.name}'s offer was withdrawn", f"{request.state.user['username']} withdrew the offer of {c.name}: nothing has changed.", f"offer-off:{company_id}:{utcnow():%Y%m%d%H%M%S}")
        await db.commit()
        await _mail(*mail)
        changelog.company(company_id, "ownership offer withdrawn")
    flash(request, "Offer withdrawn.", "info")
    return _danger(company_id)
@router.post("/companies/{company_id}/transfer/answer")
async def answer_ownership(request: Request, company_id: uuid.UUID, form: TransferAnswerForm = Depends(TransferAnswerForm.as_form), db: AsyncSession = Depends(get_db)):
    uid, c = _uid(request), await db.get(Company, company_id, with_for_update=True)
    offer = await db.get(OwnershipOffer, company_id, with_for_update=True) if c else None
    if not offer or offer.to_user_id != uid: raise FormError("That offer isn't open any more.")  # same answer either way
    owner = await db.get(User, c.owner_id)
    me, cname, name = await db.get(Membership, (uid, company_id)), c.name, request.state.user["username"]
    if form.decision == "decline":
        await db.delete(offer)
        mail = await _bell(db, owner, c, "info", f"{name} declined {cname}", f"{name} declined your offer of {cname}: nothing has changed.", f"offer-no:{company_id}:{utcnow():%Y%m%d%H%M%S}")
        await db.commit()
        await _mail(*mail)
        changelog.company(company_id, "ownership offer declined", f"by {name}")
        flash(request, f"Declined: {cname} stays with {owner.username}.", "info")
        return _danger(company_id)
    await _verified(db, request, form.password)  # (a wrong one stops here: the offer stands)
    if c.halted_at: raise FormError(halted_reason(c, "change hands"))
    if not me or me.position != "co_owner": raise FormError(f"Only a co-owner of {cname} can take it over.")
    old = await db.get(Membership, (c.owner_id, company_id))
    now = utcnow()
    old.position, old.position_since = "co_owner", now
    await db.flush()  # first: one owner per company, also in the database (company_members_one_owner)
    me.position, me.position_since, c.owner_id = "owner", now, uid
    await db.delete(offer)
    everyone = (await db.scalars(select(User).join(Membership, Membership.user_id == User.id).where(Membership.company_id == company_id))).all()
    title, text = f"{name} now owns {cname}", f"{owner.username} handed {cname} over to {name}, who is its owner now. {owner.username} stays on as a co-owner."
    await notify(db, list(everyone), c, "info", title, text, f"/companies/{company_id}/dashboard", f"owned:{company_id}:{now:%Y%m%d%H%M%S}")
    await db.commit()
    await _mail(owner.email, title, f"Hi {owner.username},\n\n{text}\n\nThis is an automatic message from {APP_NAME}.\n")
    changelog.company(company_id, "ownership transferred", f"{owner.username} → {name}; {owner.username} stays on as a co-owner")
    flash(request, f"{cname} is yours now: you're its owner, and {owner.username} stays on as a co-owner.", "success")
    return _danger(company_id)
