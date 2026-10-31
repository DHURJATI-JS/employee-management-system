"""Leave: anyone but the owner asks for leave on their company's dashboard; someone ranked above them among the owner, co-owners and
managers approves or declines it (never their own). Approved leave is unpaid: payroll.terms takes its working days off that cycle's
salary, its minimum and the employee liability. Both sides are told (the bell; the decision by email too)."""
import uuid
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from assist import rate_limited
from form import ActionForm, DecisionForm, LeaveForm
from guard import notify, sweep
from payroll import start_day, working_days
from system import APP_NAME, FormError, flash, send_email
from tables import POSITIONS, Company, LeaveRequest, Membership, get_db, utcnow
router = APIRouter(tags=["leave"])
APPROVERS = ("owner", "co_owner", "manager")  # leave, and leaving the company (when ranked above the person)
JOIN_APPROVERS = (*APPROVERS, "hr")  # joining the company: HR can approve that too (routers/membership.py)
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
def can_decide(approver: Membership | None, requester: Membership | None) -> bool:  # ranked above them, and never your own request
    return bool(approver and requester and approver.position in APPROVERS and approver.user_id != requester.user_id
                and POSITIONS[approver.position]["rank"] < POSITIONS.get(requester.position, {"rank": 99})["rank"])
def span(l) -> str: return f"{l.start_day:%d %b}" + (f" – {l.end_day:%d %b %Y}" if l.end_day != l.start_day else f" {l.start_day:%Y}")
@router.post("/companies/{company_id}/leave-requests")  # (not /leave: that's leaving the company, routers/companies.py)
async def request_leave(request: Request, company_id: uuid.UUID, form: LeaveForm = Depends(LeaveForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    m = await db.get(Membership, (uid, company_id), options=[joinedload(Membership.company, innerjoin=True).selectinload(Company.members).joinedload(Membership.user, innerjoin=True)])
    if not m: raise FormError("You're not in that company.")
    if m.position == "owner": raise FormError("The owner doesn't need anyone's approval for leave.")
    c = m.company
    if form.start_day < start_day(c, m.joined_at): raise FormError("That leave starts before you joined.")
    if not (days := working_days(c, form.start_day, form.end_day)): raise FormError("Those are all days off already: there are no working days in that leave.")
    clash = select(LeaveRequest.id).where(LeaveRequest.company_id == company_id, LeaveRequest.user_id == uid, LeaveRequest.status.in_(("pending", "approved")),
                                          LeaveRequest.start_day <= form.end_day, LeaveRequest.end_day >= form.start_day)
    if await db.scalar(clash): raise FormError("You've already asked for (or been given) leave on some of those days.")
    if rate_limited(f"leave:{uid}", 20, 3600): raise FormError("That's a lot of leave requests; try again later.")
    lr = LeaveRequest(company_id=company_id, user_id=uid, start_day=form.start_day, end_day=form.end_day, reason=form.reason)
    db.add(lr)
    await db.flush()
    name, cname = request.state.user["username"], c.name
    await notify(db, [x.user for x in c.members if can_decide(x, m)], c, "warn", f"Leave request: {name}, {days} working day{'s' if days != 1 else ''} ({cname})",
                 f"{name} asks for leave {span(lr)} ({days} working day{'s' if days != 1 else ''}, unpaid){': ' + form.reason if form.reason else ''}. Approve or decline it on {cname}'s dashboard.",
                 f"/companies/{company_id}/dashboard#leave", f"leave:{lr.id}")
    await db.commit()
    flash(request, f"Leave requested: {span(lr)}, {days} working day{'s' if days != 1 else ''}. The owner, a co-owner or a manager above you decides; approved leave is unpaid.", "success")
    return RedirectResponse(f"/companies/{company_id}/dashboard#leave", 303)
@router.post("/leave/{leave_id}/decide")
async def decide_leave(request: Request, leave_id: int, form: DecisionForm = Depends(DecisionForm.as_form), db: AsyncSession = Depends(get_db)):
    lr = await db.get(LeaveRequest, leave_id, with_for_update=True)
    approver = await db.get(Membership, (_uid(request), lr.company_id), options=[joinedload(Membership.user, innerjoin=True)]) if lr else None
    requester = await db.get(Membership, (lr.user_id, lr.company_id), options=[joinedload(Membership.user, innerjoin=True), joinedload(Membership.company, innerjoin=True)]) if approver else None
    if not can_decide(approver, requester): raise FormError("Only someone ranked above them (the owner, a co-owner or a manager) can decide this leave.")  # same answer either way
    if lr.status != "pending": raise FormError(f"That leave request was already {lr.status}.")
    ok, c = form.decision == "approve", requester.company
    lr.status, lr.decided_by, lr.decided_at = ("approved" if ok else "declined"), approver.user.username, utcnow()
    days, cid, who, email, by = working_days(c, lr.start_day, lr.end_day), c.id, requester.user.username, requester.user.email, approver.user.username
    text = (f"Your leave {span(lr)} at {c.name} ({days} working day{'s' if days != 1 else ''}) was {lr.status} by {by}."
            + (f" It's unpaid: those days come off that cycle's salary." if ok else ""))
    got = await notify(db, [requester.user], c, "info" if ok else "warn", f"Leave {lr.status}: {span(lr)} ({c.name})", text, f"/companies/{cid}/dashboard#leave", f"leave-decided:{lr.id}")
    await db.commit()
    if got:
        try: await send_email(email, f"Your leave was {lr.status} ({c.name})", f"Hi {who},\n\n{text}\n\nThis is an automatic message from {APP_NAME}.\n")
        except Exception as e: print(f"leave email failed: {type(e).__name__}")
    if ok: await sweep(cid)  # less pay owed: a liability warning may clear
    flash(request, f"{who}'s leave {span(lr)} {lr.status}." + (f" {days} unpaid working day{'s' if days != 1 else ''}: the payroll takes them off." if ok else ""), "success")
    return RedirectResponse(f"/companies/{cid}/dashboard#leave", 303)
@router.post("/leave/{leave_id}/cancel")
async def cancel_leave(request: Request, leave_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    lr = await db.get(LeaveRequest, leave_id, with_for_update=True)
    if not lr or lr.user_id != _uid(request): raise FormError("That's not your leave request.")
    if lr.status != "pending": raise FormError(f"That leave request was already {lr.status}: ask the owner if it needs changing.")
    lr.status, cid = "cancelled", lr.company_id
    await db.commit()
    flash(request, "Leave request cancelled.", "info")
    return RedirectResponse(f"/companies/{cid}/dashboard#leave", 303)
