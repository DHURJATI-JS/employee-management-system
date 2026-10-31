"""Company dashboard, salary payouts and transaction statements.
- /companies/<id>/dashboard: every member sees who's online (live over /ws/presence) and the working hours; the owner, co-owners,
  managers and HR also see attendance, each member's 30-day pay cycles (what's due), the pay buttons and the company's statement.
- Recording a payout (owner / co-owner / manager / HR; HR can't pay themselves): the gross salary, TDS, the account it went to (must
  match the employee's saved bank details), when, the bank's reference and a screenshot are all required and typed in by hand. It can
  be in any supported currency: the gross is converted into the salary currency (the day's rate) for the minimum and the clearance.
  The employee gets a notification that opens their statement on that payment, and both sides get the payslip by email.
- /statements: your own payments received; every line opens to the full payslip + the screenshot. The company's statement (on its
  dashboard) works the same way and is only shown to the four ranks above. Screenshots + payslips are only served to those people.
- Deposits (owner / co-owners, with their password): money paid into the company, recorded like a payout (amount, where it came from,
  the company account it was credited to, when, reference, screenshot) and added to the net worth; on the company's statement as money in."""
import uuid
import changelog, companyai, completion
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from bankcheck import BankCheckError, unchecked_why, verify_bank
from form import CompanyBankForm, CompanyProofForm, DepositForm, NetWorthForm, PayoutForm
from guard import halted_reason, notify, stamp, sweep, GRACE_S
from media import PictureError, discard_upload, remove_proof, store_id_photo, store_proof
from payroll import (approved_leaves, attendance, company_tz, cycle_attendance, cycle_dates, current_cycle, day_seconds, deposit_slip, hm, hours_label, in_hours, money,
                     pay_rows, slip, slip_text, start_day, terms, today, tz_name, unsettled, working_days, worth_cur)
from fx import convert, rate, rate_text
from presence import hub
from routers.chats import unread_by_company
from routers.tasks import review_counts
from routers.leave import JOIN_APPROVERS, can_decide, span
from routers.settings import _verified
from system import APP_NAME, BANK_VERIFY, FormError, flash, id_file, proof_file, render, same_text, seal, send_email, unflash_saved, unseal
from tables import (ACCOUNT_TYPES, COMPANY_PROOFS, CURRENCIES, DEFAULT_CURRENCY, FREELANCE, ID_TYPES, MAX_NET_WORTH, PAYERS, POSITIONS, SELF_PAY, STANDARD_TERMS, TOP, BankAccount, Company, CompanyBankAccount, DeletionRequest,
                    Deposit, IdentityProof, LeaveRequest, MemberRequest, Membership, OwnershipOffer, Payout, Profile, Task, TaskAssignee, User, get_db, monitored, utcnow)
router = APIRouter(tags=["payroll"])
label = lambda key: POSITIONS.get(key, {"label": key})["label"]
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
def may_pay(payer: Membership, target: Membership) -> bool:  # owner / co-owner / manager / HR; paying yourself only for the first three
    return payer.position in PAYERS and (payer.user_id != target.user_id or payer.position in SELF_PAY)
def totals(payouts) -> list[tuple[str, int, int, int]]:  # (currency, gross, tds, net) summed per currency
    sums: dict[str, list[int]] = {}
    for p in payouts:
        s = sums.setdefault(p.currency, [0, 0, 0]); s[0] += p.gross; s[1] += p.tds; s[2] += p.net
    return [(cur, *s) for cur, s in sums.items()]
def deposited(deposits) -> list[tuple[str, int, int]]:  # (currency, total, how many) per currency
    sums: dict[str, list[int]] = {}
    for d in deposits:
        s = sums.setdefault(d.currency, [0, 0]); s[0] += d.amount; s[1] += 1
    return [(cur, *s) for cur, s in sums.items()]
def lines(payouts, deposits) -> list[tuple[str, object]]:  # a company's statement: payouts ("out") and deposits ("in"), newest first
    return sorted([*(("out", p) for p in payouts), *(("in", d) for d in deposits)], key=lambda t: (t[1].transferred_at, t[1].created_at), reverse=True)
@router.get("/companies/{company_id}/dashboard")
async def company_dashboard(request: Request, company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)  # the company + its members + their accounts + salaries: 3 queries; then payouts, bank details, attendance: 1 each
    me = await db.get(Membership, (uid, company_id), options=[joinedload(Membership.company, innerjoin=True).options(
        selectinload(Company.members).joinedload(Membership.user, innerjoin=True), selectinload(Company.salaries))])
    if not me: raise HTTPException(404, "You're not a member of that company.")
    c, ledger = me.company, me.position in PAYERS
    members = sorted(c.members, key=lambda m: (POSITIONS.get(m.position, {"rank": 99})["rank"], m.user.username))
    payouts = (await db.scalars(select(Payout).where(Payout.company_id == company_id, *(() if ledger else (Payout.user_id == uid,)))
                                .order_by(Payout.transferred_at.desc(), Payout.id.desc()))).all()
    deposits = (await db.scalars(select(Deposit).where(Deposit.company_id == company_id))).all() if ledger else []  # money in: on the statement with the payouts
    banks ={b.user_id: b for b in (await db.scalars(select(BankAccount).where(BankAccount.user_id.in_([m.user_id for m in members])))).all()} if ledger else {}
    accounts = {k: unseal(f"bank:{k}", b.account_enc) for k, b in banks.items()} if ledger else {}  # payers need the full number to send the money
    leaves = (await db.scalars(select(LeaveRequest).where(LeaveRequest.company_id == company_id, *(() if ledger or me.position in ("owner", "co_owner", "manager") else (LeaveRequest.user_id == uid,)))
                               .order_by(LeaveRequest.created_at.desc()).limit(80).options(joinedload(LeaveRequest.user, innerjoin=True)))).all()
    pay = pay_rows(c, members if ledger else [me], payouts if ledger else [p for p in payouts if p.user_id == uid], [l for l in leaves if l.status == "approved"])
    now = today(c)
    # joining / leaving requests: approvers see them all, with each applicant's profile, skills, identity proof and bank details (masked); others only their own
    approver = me.position in JOIN_APPROVERS  # sees the requests; leaving is only decided by someone above (can_exit), never HR
    reqs = (await db.scalars(select(MemberRequest).where(MemberRequest.company_id == company_id, MemberRequest.status == "pending", *(() if approver else (MemberRequest.user_id == uid,)))
                             .order_by(MemberRequest.created_at).options(joinedload(MemberRequest.user, innerjoin=True)))).all()
    who = [r.user_id for r in reqs if r.kind == "join"] if approver else []
    profiles = {p.user_id: p for p in (await db.scalars(select(Profile).where(Profile.user_id.in_(who)))).all()} if who else {}
    idents = {p.user_id: p for p in (await db.scalars(select(IdentityProof).where(IdentityProof.user_id.in_(who)))).all()} if who else {}
    abanks = {b.user_id: b for b in (await db.scalars(select(BankAccount).where(BankAccount.user_id.in_(who)))).all()} if who else {}
    elsewhere: dict[int, list[str]] = {}
    for u_id, cname in ((await db.execute(select(Membership.user_id, Company.name).join(Company, Company.id == Membership.company_id).where(Membership.user_id.in_(who), Company.listed))).all() if who else []):
        elsewhere.setdefault(u_id, []).append(cname)
    cbank = await db.get(CompanyBankAccount, company_id) if ledger else None
    offer = await db.get(OwnershipOffer, company_id) if me.position in TOP else None  # the owner offered the company to a co-owner: for them to answer
    return render(request, "company_dashboard", {
        "done": completion.of_company(c, cbank, len(members)) if me.position in TOP else None,  # the setup ring: for the owner + co-owners, who can finish it
        "c": c, "me": me, "members": members, "ledger": ledger, "positions": POSITIONS, "freelance": FREELANCE, "payouts": payouts, "totals": totals(payouts),
        "lines": lines(payouts, deposits) if ledger else [], "deposited": deposited(deposits), "currencies": CURRENCIES, "default_currency": DEFAULT_CURRENCY, "standard_terms": STANDARD_TERMS,
        "chat_unread": (await unread_by_company(db, uid)).get(company_id, 0), "rv": await review_counts(db, company_id),
        "tasks_open": await db.scalar(select(func.count()).select_from(TaskAssignee).join(Task, Task.id == TaskAssignee.task_id)
                                      .where(TaskAssignee.user_id == uid, TaskAssignee.done_at.is_(None), Task.status == "open", Task.company_id == company_id)),
        "pay": pay, "att": await attendance(db, c, members if ledger else [me]), "banks": banks, "accounts": accounts,
        "cbank": cbank, "held": sum(r["owed"] for r in pay.values()), "top": me.position in TOP,
        "deadline": stamp(c, c.liability_since + timedelta(seconds=GRACE_S)) if c.liability_since else "", "since": stamp(c, c.liability_since) if c.liability_since else "",
        "halted_since": stamp(c, c.halted_at) if c.halted_at else "", "account_types": ACCOUNT_TYPES, "company_proofs": COMPANY_PROOFS, "bank_verify": BANK_VERIFY,
        "leaves": leaves, "by_id": {m.user_id: m for m in members}, "can_decide": lambda l: can_decide(me, next((m for m in members if m.user_id == l.user_id), None)),
        "leave_days": lambda l: working_days(c, l.start_day, l.end_day), "span": span, "deletion": await db.get(DeletionRequest, company_id),
        "co_owners": [m for m in members if m.position == "co_owner"], "offer": offer,
        "reqs": reqs, "approver": approver, "profiles": profiles, "idents": idents, "abanks": abanks, "elsewhere": elsewhere, "id_types": ID_TYPES,
        "can_exit": lambda r: can_decide(me, next((m for m in members if m.user_id == r.user_id), None)),
        "contacts": members if (lead := me.position in ("owner", "co_owner", "manager", "hr")) else [m for m in members if m.position in ("owner", "co_owner", "manager", "hr")],  # the top levels: everyone's email + phone (user asked); the rest: theirs
        "lead": lead,
        "owing": {u: [(k, r["terms"][k]["suggest"]) for k, *_ in r["due"] if (r["terms"][k]["suggest"] or 0) > 0] for u, r in pay.items()},  # salary still due, per member
        "status": {m.user_id: "untracked" if m.position == FREELANCE else hub.status(m.user_id) for m in members}, "may_pay": lambda t: may_pay(me, t),
        "monitored": monitored, "hours": hours_label(c), "tz": tz_name(c), "working_now": in_hours(c), "day_seconds": day_seconds(c), "hm": hm,  # money: the template global (+ the viewer's currency)
        "coai": await companyai.panel(db, c, me) if ledger else None,  # the company AI (owner, co-owners, managers, HR): each member's signals + its promotion calls (not "ai": base.html sets that)
        "slip": slip, "now_local": datetime.now(company_tz(c)).strftime("%Y-%m-%dT%H:%M"), "today": now})
@router.post("/companies/{company_id}/payouts/{user_id}")
async def record_payout(request: Request, company_id: uuid.UUID, user_id: int, form: PayoutForm = Depends(PayoutForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    unflash_saved(request, form.proof)  # the outcome is flashed instead of the generic "uploaded" note
    try:
        c = await db.get(Company, company_id, with_for_update=True, options=[selectinload(Company.members).joinedload(Membership.user, innerjoin=True), selectinload(Company.salaries)])
        # ^ the company row is locked: one payout at a time per company, so two at once can't both spend the same net worth
        payer = next((m for m in c.members if m.user_id == uid), None) if c else None
        if not payer or payer.position not in PAYERS: raise FormError("Only the owner, a co-owner, a manager or HR can record salary payments.")  # same answer whether it exists or not
        if not (target := next((m for m in c.members if m.user_id == user_id), None)): raise FormError("That person isn't a member of this company.")
        name = target.user.username
        if not may_pay(payer, target): raise FormError("HR can't record their own salary: ask the owner, a co-owner or a manager.")
        if c.halted_at and target.position in TOP: raise FormError(halted_reason(c, "pay its owners or co-owners"))
        if not c.salary_currency: raise FormError(f"Set {c.name}'s salaries first (workplaces → edit), then pay them.")
        if not (source := await db.get(CompanyBankAccount, company_id)): raise FormError(f"Add {c.name}'s own bank account first (company dashboard → company account): salaries are paid from it.")
        if c.net_worth is None: raise FormError(f"Set {c.name}'s net worth first (company dashboard → company account): every payment is checked against it.")
        if not c.id_proof_at: raise FormError(f"Upload {c.name}'s identity proof first (company dashboard → identity proof).")
        rows = pay_rows(c, c.members, (await db.scalars(select(Payout).where(Payout.company_id == company_id))).all(), await approved_leaves(db, [company_id]))
        if not (bank := await db.get(BankAccount, user_id)): raise FormError(f"{name} hasn't added bank details yet: they have to add them in settings before they can be paid.")
        if (account := unseal(f"bank:{user_id}", bank.account_enc)) is None: raise FormError(f"{name}'s saved bank details can't be read any more: ask them to enter them again in settings.")
        if BANK_VERIFY and not bank.verified_at: raise FormError(f"{name}'s bank account isn't verified with the bank yet: ask them to save it again in settings (it's checked then).")
        if BANK_VERIFY and not source.verified_at: raise FormError(f"{c.name}'s own bank account isn't verified with the bank yet: save it again (company dashboard → company account).")
        if not same_text(form.account_number, account): raise FormError(f"That account number isn't {name}'s ({bank.bank_name}, ending {bank.account_last4}): check where the money went.")
        if form.cycle > (n := current_cycle(c, target.joined_at)): raise FormError(f"Cycle {form.cycle} hasn't started yet: {name} is in cycle {n}.")
        t = rows[user_id]["terms"].get(form.cycle) or terms(c, target, form.cycle, rows[user_id]["leaves"])  # after approved (unpaid) leave
        first, last = t["first"], t["last"]
        cur, sc = form.currency or c.salary_currency, c.salary_currency  # paid in any currency (user asked); the minimum + the clearance are in the salaries' one
        counted = convert(form.gross, cur, sc)  # at the day's rate (fx.py)
        rated = f" {money(cur, form.gross)} is {money(sc, counted)} at today's rate ({rate_text(cur, sc)})." if cur != sc else ""
        if (minimum := t["minimum"]) and counted < minimum:
            raise FormError(f"The gross salary can't be below {c.name}'s minimum for {label(t['position'])}: {money(sc, minimum)} for cycle {form.cycle}"
                            + (f" ({t['leave']} day{'s' if t['leave'] != 1 else ''} of unpaid leave already taken off)." if t["leave"] else ".") + rated)
        if (salary := t["suggest"]) is not None and counted > salary and (cur == sc or convert(form.gross - 1, cur, sc) >= salary):  # user's rule: nobody records more than the cycle's salary (only whole units can be typed in another currency: the first one past it is the exact salary)
            raise FormError(f"The gross salary can't be more than {name}'s salary for cycle {form.cycle}: {money(sc, salary)}" + (f" ({t['leave']} day{'s' if t['leave'] != 1 else ''} of unpaid leave already taken off)." if t["leave"] else ".") + rated)
        if counted > (cap := rows[user_id]["max"]):  # the clearance: net worth - what everyone else's working hours cost and isn't paid yet (in the salaries' currency)
            wc, worth = worth_cur(c), convert(c.net_worth, worth_cur(c), sc)
            raise FormError(f"That's more than {c.name} can pay out now. Clearance: {money(sc, max(cap, 0))} = net worth {money(sc, worth)}"
                            f"{f' ({money(wc, c.net_worth)})' if wc != sc else ''} − {money(sc, worth - cap)} that the others' working hours cost and isn't paid yet." + rated)
        when = form.transferred_at.replace(tzinfo=company_tz(c))  # typed on the company's clock
        if when > datetime.now(UTC) + timedelta(minutes=5): raise FormError("The transfer time is in the future: enter when the money was actually sent.")
        if when.date() < start_day(c, target.joined_at): raise FormError(f"That transfer is dated before {name} joined {c.name}.")
        if form.cycle in rows[user_id]["paid"]: raise FormError(f"{name}'s cycle {form.cycle} is already paid.")
        if await db.scalar(select(Payout.id).where(Payout.company_id == company_id, Payout.reference == form.reference)): raise FormError(f"Reference {form.reference} is already on another payment.")
    except FormError: discard_upload(form.proof); raise  # refused: the screenshot isn't kept
    if monitored(target.position):
        days, secs = await cycle_attendance(db, c, user_id, first, last)
        presence = f"{days} of {working_days(c, first, min(last, today(c)))} working days present, {hm(secs)} active in working hours"
    else: presence = f"not monitored ({label(target.position)})"
    if t["leave"]: presence += f"; unpaid leave: {t['leave']} working day{'s' if t['leave'] != 1 else ''} ({money(c.salary_currency, t['deduction'])} off the calculated salary)"
    p = Payout(company_id=company_id, user_id=user_id, paid_by_id=uid, company_name=c.name, employee_name=name, position=t["position"], paid_by_name=payer.user.username,
               paid_by_position=payer.position, cycle=form.cycle, period_start=first, period_end=last, currency=cur, gross=form.gross, tds=form.tds, net=form.gross - form.tds,
               salary_currency=sc, counted=counted, fx_rate=rate(cur, sc).quantize(Decimal("1e-10")) if cur != sc else None, holder_name=bank.holder_name, bank_name=bank.bank_name, routing=bank.routing, bank_country=bank.country, swift=bank.swift, account_last4=bank.account_last4,
               company_code=str(company_id), transferred_at=when.astimezone(UTC).replace(tzinfo=None), timezone=tz_name(c), reference=form.reference, attendance=presence[:300],
               from_bank=source.bank_name, from_last4=source.account_last4)
    db.add(p)
    c.net_worth = max(0, c.net_worth - convert(form.gross, cur, worth_cur(c)))  # the money (net + TDS) leaves the company, in the net worth's currency
    try: await db.flush()  # its id names the screenshot file
    except Exception: discard_upload(form.proof); raise
    try: await store_proof(form.proof, p.id)
    except PictureError as e:
        await db.rollback()
        raise FormError(f"Transfer screenshot: {e}") from None
    owner = await db.get(User, c.owner_id)
    to_employee, to_company = target.user.email, {e for e in (owner.email if owner else None, payer.user.email) if e and e != target.user.email}
    try: await db.commit()
    except IntegrityError:  # paid / reference used a moment ago by someone else
        remove_proof(p.id); await db.rollback()
        raise FormError("That cycle was just paid, or that reference is already on another payment.") from None
    paid = (f"{p.employee_name}, cycle {p.cycle} ({p.period_start:%d %b} – {p.period_end:%d %b %Y}) · gross {money(p.currency, p.gross)} · TDS {money(p.currency, p.tds)} · net {money(p.currency, p.net)}"
            + (f" (gross = {money(sc, counted)} in the salary currency at {rate_text(cur, sc, p.fx_rate)})" if cur != sc else "") + f" · to {p.holder_name}, {p.bank_name} account {getattr(form, 'account_number', '') or '••••' + p.account_last4} · from {p.from_bank} ••••{p.from_last4} · reference {p.reference} · recorded by {p.paid_by_name}")
    changelog.company(company_id, "salary paid", paid); changelog.add("u", user_id, f"salary received from {p.company_name}", paid)
    text = slip_text(p)
    for to, subject, intro in [(to_employee, f"Salary credited: {money(p.currency, p.net)} from {p.company_name}",
                                f"Hi {p.employee_name},\n\n{p.company_name} has paid your salary for cycle {p.cycle}: {money(p.currency, p.net)} was sent to your "
                                f"{p.bank_name} account ending {p.account_last4}.\n\nOpen Statements in {APP_NAME} to see this payment with the transfer screenshot.")] + [
                               (to, f"Salary paid: {p.employee_name}, {money(p.currency, p.net)} ({p.company_name})",
                                f"{p.paid_by_name} recorded {p.employee_name}'s salary for cycle {p.cycle} at {p.company_name}: {money(p.currency, p.net)} net.\n\n"
                                f"It's on {p.company_name}'s transaction statement (company dashboard in {APP_NAME}) with the transfer screenshot.") for to in sorted(to_company)]:
        try: await send_email(to, subject, f"{intro}\n\n{text}\n\nThis is an automatic message from {APP_NAME}.\n")
        except Exception as e: print(f"payslip email failed: {type(e).__name__}")  # never undo a recorded payment for it
    await sweep(company_id)  # paying people can resolve a liability warning (or show the company now can't cover the rest)
    flash(request, f"Payment recorded: {money(p.currency, p.net)} to {p.employee_name} for cycle {p.cycle}" + (f" (gross {money(cur, p.gross)} = {money(sc, counted)} in the salary currency)" if cur != sc else "")
          + ". They've been notified and both sides got the payslip by email.", "success")
    return RedirectResponse(f"/companies/{company_id}/dashboard#pay-{p.id}", 303)
@router.get("/statements")
async def statements(request: Request, db: AsyncSession = Depends(get_db)):
    """Your transaction statement: every salary payment you've received, newest first, and, for the companies you own or co-own,
    every payment they've made (the company's statement, as on its dashboard). Opening it marks your received ones read (the bell)."""
    uid = _uid(request)
    order = (Payout.transferred_at.desc(), Payout.id.desc())
    payouts = (await db.scalars(select(Payout).where(Payout.user_id == uid).order_by(*order))).all()
    tops = (await db.execute(select(Company.id, Company.name).join(Membership, Membership.company_id == Company.id)
                             .where(Membership.user_id == uid, Membership.position.in_(TOP)).order_by(Company.name))).all()
    paid_out = (await db.scalars(select(Payout).where(Payout.company_id.in_([c for c, _ in tops])).order_by(*order))).all() if tops else []
    paid_in = (await db.scalars(select(Deposit).where(Deposit.company_id.in_([c for c, _ in tops])))).all() if tops else []
    companies = [{"id": c, "name": name, "payouts": (out := [p for p in paid_out if p.company_id == c]), "deposited": deposited(ins := [d for d in paid_in if d.company_id == c]),
                  "lines": lines(out, ins)} for c, name in tops]
    if new := {p.id for p in payouts if p.seen_at is None}:
        await db.execute(update(Payout).where(Payout.user_id == uid, Payout.seen_at.is_(None)).values(seen_at=utcnow()))
        await db.commit()
        request.state.user["unread"] = max(0, request.state.user["unread"] - len(new))
    return render(request, "statements", {"payouts": payouts, "new": new, "totals": totals(payouts), "companies": [dict(c, totals=totals(c["payouts"])) for c in companies]})
async def _payout_for(db: AsyncSession, request: Request, payout_id: int) -> Payout:
    """The payout, if you're the one it paid or you're the owner / a co-owner / a manager / HR of its company; else the same 404."""
    uid, p = _uid(request), await db.get(Payout, payout_id)
    if p and p.user_id != uid:
        m = await db.get(Membership, (uid, p.company_id)) if p.company_id else None
        if not (m and m.position in PAYERS): p = None
    if not p: raise HTTPException(404, "There's no such payment on your statements.")
    return p
@router.get("/payouts/{payout_id}/proof")
async def payout_proof(request: Request, payout_id: int, db: AsyncSession = Depends(get_db)):
    """The transfer screenshot (re-encoded WebP, never public)."""
    path = proof_file((await _payout_for(db, request, payout_id)).id)
    if not path.is_file(): raise HTTPException(404, "The screenshot of this payment is missing.")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"})
@router.get("/payouts/{payout_id}/slip")
async def payslip(request: Request, payout_id: int, db: AsyncSession = Depends(get_db)):
    p = await _payout_for(db, request, payout_id)
    return render(request, "payslip", {"p": p, "lines": slip(p)})
# ---------- identity proofs: photos are never public ----------
@router.post("/companies/{company_id}/identity")
async def company_proof(request: Request, company_id: uuid.UUID, form: CompanyProofForm = Depends(CompanyProofForm.as_form), db: AsyncSession = Depends(get_db)):
    """Add / replace the company's identity proof photo (owner / co-owners)."""
    unflash_saved(request, form.file)
    try: c = await _top_company(db, request, company_id, "identity proof")
    except FormError: discard_upload(form.file); raise
    try: await store_id_photo(form.file, "c", company_id)
    except PictureError as e: raise FormError(f"Identity proof photo: {e}") from None
    was, (c.id_proof_type, c.id_proof_at, name) = c.id_proof_type, (form.id_proof_type, utcnow(), c.name)
    await db.commit()
    changelog.company(company_id, "identity proof uploaded", f"{COMPANY_PROOFS.get(was, was) + ' → ' if was else ''}{COMPANY_PROOFS[form.id_proof_type]} · new photo")
    flash(request, f"{name}'s identity proof ({COMPANY_PROOFS[form.id_proof_type]}) saved.", "success")
    return RedirectResponse(f"/companies/{company_id}/dashboard#finance", 303)
def _photo(path) -> FileResponse:
    if not path.is_file(): raise HTTPException(404, "No identity proof photo has been uploaded.")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"})
@router.get("/identity/u/{user_id}/photo")
async def person_id_photo(request: Request, user_id: int, db: AsyncSession = Depends(get_db)):
    """A person's ID photo: for them, the owner, co-owners and HR of a company they're both in, and the owner, co-owners, managers and HR
    of a company they've asked to join (to review the request); anyone else gets the same 404."""
    uid = _uid(request)
    if uid != user_id:
        mine, theirs = Membership.__table__.alias(), Membership.__table__.alias()
        shared = await db.scalar(select(mine.c.company_id).join(theirs, theirs.c.company_id == mine.c.company_id)
                                 .where(mine.c.user_id == uid, mine.c.position.in_((*TOP, "hr")), theirs.c.user_id == user_id).limit(1))
        reviewing = shared or await db.scalar(select(MemberRequest.id).join(Membership, Membership.company_id == MemberRequest.company_id)
                                              .where(Membership.user_id == uid, Membership.position.in_(JOIN_APPROVERS), MemberRequest.user_id == user_id,
                                                     MemberRequest.kind == "join", MemberRequest.status == "pending").limit(1))
        if not reviewing: raise HTTPException(404, "No identity proof photo has been uploaded.")
    return _photo(id_file("u", user_id))
@router.get("/identity/c/{company_id}/photo")
async def company_id_photo(request: Request, company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    m = await db.get(Membership, (_uid(request), company_id))
    if not m or m.position not in PAYERS: raise HTTPException(404, "No identity proof photo has been uploaded.")
    return _photo(id_file("c", company_id))
# ---------- the company's own account + net worth (owner / co-owners, with their password) ----------
async def _top_company(db: AsyncSession, request: Request, company_id: uuid.UUID, what: str = "bank account or net worth") -> Company:
    m = await db.get(Membership, (_uid(request), company_id))
    if not m or m.position not in TOP: raise FormError(f"Only the owner or a co-owner can change the company's {what}.")  # same answer either way
    return await db.get(Company, company_id, with_for_update=True)  # locked: a payout can't take its gross off the net worth at the same moment
@router.post("/companies/{company_id}/bank")
async def company_bank(request: Request, company_id: uuid.UUID, form: CompanyBankForm = Depends(CompanyBankForm.as_form), db: AsyncSession = Depends(get_db)):
    """The account salaries are paid from: encrypted like personal accounts (bound to the company id); payslips show it as "paid from"."""
    c = await _top_company(db, request, company_id)
    account = await _verified(db, request, form.password)
    name = c.name
    try: checked = await verify_bank(form.account_number, form.routing, form.holder_name, form.country)  # with the bank, when BANK_VERIFY is on (Indian banks)
    except BankCheckError as e: raise FormError(f"Company account not saved: {e}") from None
    bank = await db.get(CompanyBankAccount, company_id) or CompanyBankAccount(company_id=company_id)
    new = bank.account_last4 is None
    facts = lambda b, number: {"holder": b.holder_name, "bank": b.bank_name, "branch": b.branch, "country": b.country, "account type": b.account_type, "account number": number,
                               "bank code": b.routing, "SWIFT / BIC": b.swift, "bank address": b.bank_address}
    before = {} if new else facts(bank, unseal(f"company-bank:{company_id}", bank.account_enc) or f"••••{bank.account_last4}")  # full numbers in the change log (user's choice)
    bank.holder_name, bank.bank_name, bank.branch, bank.account_type = form.holder_name, form.bank_name, form.branch, form.account_type
    bank.country, bank.routing, bank.swift, bank.bank_address = form.country, form.routing, form.swift, form.bank_address  # any country (banks.py)
    bank.account_enc, bank.account_last4, bank.updated_by = seal(f"company-bank:{company_id}", form.account_number), form.account_number[-4:], account.username
    bank.verified_at, bank.verified_name = (utcnow(), checked["name_at_bank"]) if checked else (None, "")
    after = facts(bank, form.account_number)
    db.add(bank)
    await db.commit()
    changelog.company(company_id, "company bank account " + ("added" if new else "changed"), (changelog.facts(after) if new else changelog.diff(before, after) or "saved again")
                      + (f" · verified with the bank: {checked['name_at_bank']}" if checked else ""))
    flash(request, f"{name}'s bank account {'saved' if new else 'updated'}: {form.bank_name} ending {form.account_number[-4:]}" + (f", verified with the bank ({checked['name_at_bank']})" if checked else f" (not verified: {unchecked_why(form.country)})") + ". Salaries are paid from it.", "success")
    return RedirectResponse(f"/companies/{company_id}/dashboard#finance", 303)
@router.post("/companies/{company_id}/net-worth")
async def company_net_worth(request: Request, company_id: uuid.UUID, form: NetWorthForm = Depends(NetWorthForm.as_form), db: AsyncSession = Depends(get_db)):
    """The funds the company has: every payment is capped by it (the clearance) and takes its gross off it; the payroll guard compares it
    with what the employees have earned."""
    c = await _top_company(db, request, company_id)
    account = await _verified(db, request, form.password)
    was = money(worth_cur(c), c.net_worth) if c.net_worth is not None else "—"
    c.net_worth, c.net_worth_currency, c.net_worth_updated_at, c.net_worth_by = form.net_worth, form.net_worth_currency, utcnow(), account.username  # any currency: the guard converts
    name, cur = c.name, form.net_worth_currency
    await db.commit()
    changelog.company(company_id, "net worth set", f"{was} → {money(cur, form.net_worth)}")
    await sweep(company_id)  # raises or clears a liability warning (and lifts a halt) right away
    flash(request, f"{name}'s net worth is now {money(cur, form.net_worth)}.", "success")
    return RedirectResponse(f"/companies/{company_id}/dashboard#finance", 303)
@router.post("/companies/{company_id}/deposits")
async def record_deposit(request: Request, company_id: uuid.UUID, form: DepositForm = Depends(DepositForm.as_form), db: AsyncSession = Depends(get_db)):
    """Money paid into the company (owner / co-owners, with their password): the amount, where it came from, the company account it
    was credited to (must match the saved one), when, the bank's reference and a screenshot. Added to the net worth, on the company's
    statement with the screenshot; the other owners are told, and the payroll guard re-checks (a deposit can end a warning or a halt)."""
    uid = _uid(request)
    unflash_saved(request, form.proof)
    try:
        c = await _top_company(db, request, company_id, "funds")
        account = await _verified(db, request, form.password)
        if not c.salary_currency: raise FormError(f"Set {c.name}'s salaries first (workplaces → edit): deposits are in the same currency.")
        if not (dest := await db.get(CompanyBankAccount, company_id)): raise FormError(f"Add {c.name}'s own bank account first (company account): deposits are recorded into it.")
        if (number := unseal(f"company-bank:{company_id}", dest.account_enc)) is None: raise FormError(f"{c.name}'s saved bank account can't be read any more: save it again (company account).")
        if not same_text(form.account_number, number): raise FormError(f"That isn't {c.name}'s account ({dest.bank_name}, ending {dest.account_last4}): only money credited to the company account can be recorded.")
        when = form.transferred_at.replace(tzinfo=company_tz(c))  # typed on the company's clock
        if when > datetime.now(UTC) + timedelta(minutes=5): raise FormError("That time is in the future: enter when the money actually arrived.")
        if when.astimezone(UTC).replace(tzinfo=None) < c.created_at - timedelta(days=1): raise FormError(f"That's before {c.name} was created here: its net worth covers what it had by then.")
        wc = worth_cur(c)  # added in the net worth's currency, converted at the day's rate (fx.py)
        if (credited := convert(form.amount, form.currency, wc)) < 1: raise FormError(f"{money(form.currency, form.amount)} is less than 1 {wc} once converted: too small to add.")
        if (after := (c.net_worth or 0) + credited) > MAX_NET_WORTH: raise FormError(f"That would take the net worth over the maximum ({money(wc, MAX_NET_WORTH)}).")
        if await db.scalar(select(Deposit.id).where(Deposit.company_id == company_id, Deposit.reference == form.reference)): raise FormError(f"Reference {form.reference} is already on another deposit.")
    except FormError: discard_upload(form.proof); raise  # refused: the screenshot isn't kept
    me = await db.get(Membership, (uid, company_id))
    d = Deposit(company_id=company_id, by_id=uid, company_name=c.name, company_code=str(company_id), by_name=account.username, by_position=me.position, currency=form.currency,
                amount=form.amount, worth_currency=wc, credited=credited, source=form.source, to_bank=dest.bank_name, to_last4=dest.account_last4,
                transferred_at=when.astimezone(UTC).replace(tzinfo=None), timezone=tz_name(c), reference=form.reference, note=form.note, net_worth_after=after)
    db.add(d)
    c.net_worth, c.net_worth_currency, c.net_worth_updated_at, c.net_worth_by = after, wc, utcnow(), account.username  # the money is the company's now
    try: await db.flush()  # its id names the screenshot file
    except Exception: discard_upload(form.proof); raise
    try: await store_proof(form.proof, d.id, "deposit")
    except PictureError as e:
        await db.rollback()
        raise FormError(f"Deposit screenshot: {e}") from None
    others = (await db.scalars(select(User).join(Membership, Membership.user_id == User.id).where(Membership.company_id == company_id, Membership.position.in_(TOP), User.id != uid))).all()
    title, cname, amt, link = f"Funds deposited: {money(d.currency, d.amount)} into {c.name}", c.name, money(d.currency, d.amount), f"/companies/{company_id}/dashboard#dep-{d.id}"
    added = f" ({money(wc, credited)} once converted)" if wc != d.currency else ""
    text = f"{d.by_name} recorded {amt}{added} from {d.source} into {cname}'s {d.to_bank} account ••••{d.to_last4} (ref {d.reference}). Net worth is now {money(wc, after)}."
    got = await notify(db, others, c, "info", title, text, link, f"deposit:{d.id}")
    try: await db.commit()
    except IntegrityError:  # that reference was used a moment ago
        remove_proof(d.id, "deposit"); await db.rollback()
        raise FormError("That reference is already on another deposit.") from None
    receipt = "\n".join(f"{k} : {v}" for k, v in deposit_slip(d))
    changelog.company(company_id, "deposit recorded", f"{amt}{added} from {d.source} into {d.to_bank} account {form.account_number} · reference {d.reference} · net worth now {money(wc, after)}"
                      + (f" · note: {d.note}" if d.note else ""))
    for to in sorted({u.email for u in got} | {account.email}):
        try: await send_email(to, title, f"{text}\n\nRECEIPT\n{receipt}\n\nIt's on {cname}'s transaction statement (company dashboard in {APP_NAME}) with the screenshot.\n\nThis is an automatic message from {APP_NAME}.\n")
        except Exception as e: print(f"deposit email failed: {type(e).__name__}")  # never undo a recorded deposit for it
    await sweep(company_id)  # more funds: a liability warning (or a halt) may clear
    flash(request, f"Deposit recorded: {amt}{added} into {cname}. Net worth is now {money(wc, after)}.", "success")
    return RedirectResponse(f"/companies/{company_id}/dashboard#dep-{d.id}", 303)
@router.get("/deposits/{deposit_id}/proof")
async def deposit_proof(request: Request, deposit_id: int, db: AsyncSession = Depends(get_db)):
    """A deposit's screenshot: for the owner, co-owners, managers and HR of its company (who see the statement) and whoever recorded it."""
    uid, d = _uid(request), await db.get(Deposit, deposit_id)
    if d and d.by_id != uid:
        m = await db.get(Membership, (uid, d.company_id)) if d.company_id else None
        if not (m and m.position in PAYERS): d = None
    if not d or not (path := proof_file(d.id, "deposit")).is_file(): raise HTTPException(404, "There's no such deposit on your statements.")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, no-store", "Content-Disposition": "inline"})
