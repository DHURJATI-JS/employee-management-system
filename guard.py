"""The payroll guard: the part of the AI employee management that watches each company's money against what its people have earned.
Every GUARD_S (and right after a payout or a net worth change) it works out each company's employee liability (payroll.owed: every
finished unpaid cycle in full + the current cycle's working hours so far) and compares it with the company's net worth, both in one
currency: the net worth's (fx.py converts the salaries' currency at the day's rate, so a net worth in INR covers salaries in USD):
- liability > net worth: a timestamped warning to the owner + co-owners (notification + email) with the deadline;
- still unresolved GRACE_S (24 h) later: the company's operations halt (no joining, offers, edits, promotions, deletion, owner /
  co-owner payouts or attendance tracking) and every employee is alerted by notification + email; paying staff + adding funds still work;
- covered again: the warning clears, operations resume, and whoever was alerted is told.
It also reminds the payers (owner, co-owners, managers, HR) when someone's 30-day cycle ends unpaid, once per person and cycle.
Deterministic rules, not a language model: amounts and deadlines have to be exact. Notices land in the bell (tables.Notice) + email."""
import asyncio, os
from datetime import datetime, timedelta
from sqlalchemy import ARRAY, Integer, String, any_, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import joinedload, selectinload
import fx
from fx import convert
from payroll import approved_leaves, local, money, pay_rows, tz_name, worth_cur
from system import APP_NAME, send_email
from tables import PAYERS, TOP, Company, Membership, Notice, Payout, Session, User, utcnow
GUARD_S, GRACE_S = int(os.getenv("GUARD_S", "300")), int(os.getenv("GUARD_GRACE_S", str(24 * 3600)))  # tests shorten both
BATCH = 20  # companies checked (and locked) at a time
def stamp(c, when: datetime) -> str: return f"{local(c, when):%d %b %Y, %H:%M} ({tz_name(c)})"
def halted_reason(c, doing: str) -> str:  # the error shown when a halted company tries something it can't
    return f"{c.name}'s operations are halted: what its employees have earned is more than its net worth. It can't {doing} until it pays its employees or adds funds."
def no_funds(c, doing: str = "take on new people") -> str | None:  # net worth 0 (or never set): nobody new joins a company that can't pay them
    if c.net_worth: return None
    return f"{c.name} can't {doing} right now: its net worth is {'0' if c.net_worth == 0 else 'not set'}. The owner or a co-owner has to deposit funds first."
def join_block(c) -> str | None: return halted_reason(c, "take on new people") if c.halted_at else no_funds(c)  # why nobody can join c now (None = they can)
async def no_room(db, c) -> str | None:  # c is at its employee limit, in words for anyone: never the number, only its owner and co-owners see that. Whoever really joins holds c's row locked, so the next one counts after them
    if c.max_members and await db.scalar(select(func.count()).select_from(Membership).where(Membership.company_id == c.id)) >= c.max_members: return f"{c.name} isn't taking new people right now."
async def notify(db, users: list[User], c, level: str, title: str, body: str, link: str, key: str) -> list[User]:
    """One notice per user, once per key: returns who got a new one (they're the ones emailed)."""
    if not users: return []
    stmt = insert(Notice).values([{"user_id": u.id, "company_id": c.id, "level": level, "title": title[:160], "body": body[:1000], "link": link, "key": key, "created_at": utcnow()} for u in users])
    new = set((await db.execute(stmt.on_conflict_do_nothing(constraint="notices_once").returning(Notice.user_id))).scalars().all())
    return [u for u in users if u.id in new]
async def notify_many(db, rows: list[dict]) -> set[tuple[int, str]]:
    """Many notices, each with its own text, in one statement (per 2000 rows): rows of user_id, company_id, level, title, body, link, key. Once per key, like notify(); returns the (user id, key) of the new ones."""
    new, now = set(), utcnow()
    for i in range(0, len(rows), 2000):
        stmt = insert(Notice).values([{**r, "title": r["title"][:160], "body": r["body"][:1000], "created_at": now} for r in rows[i:i + 2000]])
        new.update(tuple(x) for x in (await db.execute(stmt.on_conflict_do_nothing(constraint="notices_once").returning(Notice.user_id, Notice.key))).all())
    return new
def _paydays(c: Company, rows: dict, cur, link: str) -> list[dict]:  # payday: a cycle ended unpaid -> whoever can pay it is reminded, once per person and cycle. Only the latest one, saying how many older ones wait too: with 1-day cycles a backlog would be a notice per payer, member and cycle (the load test: 241,174 at once)
    payers, out = [m.user_id for m in c.members if m.position in PAYERS], []
    for m in c.members:
        r = rows[m.user_id]
        due = [d for d in r["due"] if d[0] < r["cycle"] and r["terms"][d[0]]["suggest"]]  # (the current cycle isn't over yet; nothing to pay when it was all leave, or for owners and co-owners, unless they had a salary then)
        if not due: continue
        k, first, last = due[-1]
        older = f" {len(due) - 1} earlier cycle{'s are' if len(due) > 2 else ' is'} unpaid too." if len(due) > 1 else ""
        text = f"{m.user.username}'s salary for cycle {k} ({first:%d %b} – {last:%d %b %Y}) at {c.name} is due: the cycle ended on {last:%d %b}. Calculated: {cur(r['terms'][k]['suggest'])}.{older}"
        out += [{"user_id": p, "company_id": c.id, "level": "warn", "title": f"Payday: pay {m.user.username} (cycle {k}, {c.name})", "body": text, "link": link, "key": f"remind:{c.id}:{m.user_id}:{k}"} for p in payers if p != m.user_id]
    return out
async def _remind(db, companies: list[Company], rows: list[dict]) -> list[tuple[str, str, str]]:
    """Every company's payday reminders at once (with 1-day cycles there can be thousands, each sweep): those already sent are left out in one query, the rest go in
    one statement, and whoever got a new one gets one email per company."""
    if not rows: return []
    sent = (await db.execute(select(Notice.user_id, Notice.key).where(Notice.user_id == any_(cast(sorted({r["user_id"] for r in rows}), ARRAY(Integer))), Notice.key == any_(cast(sorted({r["key"] for r in rows}), ARRAY(String)))))).all()
    have, due = {tuple(x) for x in sent}, {}
    fresh = [r for r in rows if (r["user_id"], r["key"]) not in have]
    new = await notify_many(db, fresh)
    for r in fresh:
        if (r["user_id"], r["key"]) in new: due.setdefault((r["company_id"], r["user_id"]), []).append(r["body"])
    names, people = {c.id: c.name for c in companies}, {m.user_id: m.user for c in companies for m in c.members}
    return [(people[u].email, f"Payday at {names[cid]}: {len(texts)} salar{'y' if len(texts) == 1 else 'ies'} due",
             f"Hi {people[u].username},\n\n" + "\n".join("- " + t for t in texts) + f"\n\nRecord them on {names[cid]}'s dashboard in {APP_NAME}.\n\nThis is an automatic message from {APP_NAME}'s payroll guard.\n") for (cid, u), texts in due.items()]
async def _check(db, c: Company, payouts: list[Payout], leaves, now: datetime) -> tuple[list[tuple[str, str, str]], list[dict]]:
    rows, cur, link = await asyncio.to_thread(pay_rows, c, c.members, payouts, leaves), (lambda x: money(c.salary_currency, x)), f"/companies/{c.id}/dashboard"  # (a thread: with 1-day cycles this is real work, and the site keeps answering meanwhile)
    wc = worth_cur(c)  # everything is compared in the net worth's currency (fx.py converts the salaries' one)
    worth = lambda x: money(wc, x)
    liability = sum(r["owed"] for r in rows.values())
    due = convert(liability, c.salary_currency, wc)  # the liability in the net worth's currency
    both = lambda x: cur(x) + (f" (≈ {worth(convert(x, c.salary_currency, wc))})" if wc != c.salary_currency else "")
    top = [m.user for m in c.members if m.position in TOP]
    staff = [m.user for m in c.members if m.position not in TOP]
    mails: list[tuple[str, str, str]] = []
    mail = lambda users, subject, text: mails.extend((u.email, subject, f"Hi {u.username},\n\n{text}\n\nThis is an automatic message from {APP_NAME}'s payroll guard.\n") for u in users)
    short = c.net_worth is not None and due > c.net_worth
    owed_text = both(liability)  # the liability in the salaries' currency (+ converted); the net worth in its own
    if short and not c.liability_since:  # a new warning, timestamped now
        c.liability_since, deadline = now, now + timedelta(seconds=GRACE_S)
        text = (f"{c.name}'s employee liability is {owed_text}: what its people's working hours have earned and haven't been paid. Its net worth is "
                f"{worth(c.net_worth)}, {worth(due - c.net_worth)} short. Warning issued {stamp(c, now)}. Pay your employees or add funds (deposit funds on the company dashboard) "
                f"before {stamp(c, deadline)}, or all of {c.name}'s operations halt to protect them, and every employee is alerted.")
        mail(await notify(db, top, c, "danger", f"Warning: {c.name}'s employee liability is over its net worth", text, link, f"liability:{c.id}:{now:%Y%m%d%H%M%S}"),
             f"Warning: {c.name} can't cover what its employees have earned", text)
    elif short and not c.halted_at and now - c.liability_since >= timedelta(seconds=GRACE_S):  # 24 h, still unresolved: halt
        c.halted_at, key = now, f"halt:{c.id}:{c.liability_since:%Y%m%d%H%M%S}"
        warned = stamp(c, c.liability_since)
        to_staff = (f"{c.name} hasn't covered what its employees have earned since {warned} (liability {owed_text}, net worth {worth(c.net_worth)}), so {APP_NAME} "
                    f"has halted its operations to protect you: it can't take on people, make offers, change positions, pay its owners or be deleted, and attendance "
                    f"isn't tracked, until it pays its employees or adds funds. What you've earned is still owed to you: check your statement, and talk to its owner or HR.")
        to_top = (f"The warning from {warned} wasn't resolved within 24 hours: {c.name}'s operations are halted (no joining, offers, edits, promotions, deletion, owner "
                  f"payouts or attendance) and every employee has been alerted. Liability {owed_text}, net worth {worth(c.net_worth)}. Pay your employees or add funds to resume.")
        mail(await notify(db, staff, c, "danger", f"{c.name} has halted operations to protect its employees", to_staff, "/statements", key), f"Alert: {c.name} has halted operations", to_staff)
        mail(await notify(db, top, c, "danger", f"{c.name}'s operations are halted", to_top, link, key), f"{c.name}'s operations are halted", to_top)
    elif not short and c.liability_since:  # covered again
        since, was_halted, key = c.liability_since, c.halted_at, f"resolved:{c.id}:{c.liability_since:%Y%m%d%H%M%S}"
        c.liability_since = c.halted_at = None
        text = f"{c.name}'s employee liability ({owed_text}) is covered by its net worth ({worth(c.net_worth)}) again: the warning from {stamp(c, since)} is resolved" + (" and operations have resumed." if was_halted else ".")
        mail(await notify(db, top, c, "info", f"{c.name}: employee liability covered", text, link, key), f"{c.name}: employee liability covered", text)
        if was_halted: mail(await notify(db, staff, c, "info", f"{c.name} has resumed operations", text, "/statements", key), f"{c.name} has resumed operations", text)
    return mails, _paydays(c, rows, cur, link)
async def _batch(ids: list) -> list[tuple[str, str, str]]:
    """A few companies in one short transaction. FOR NO KEY UPDATE, not FOR UPDATE: it still keeps two guards or a payout apart, but a company's orders and tasks can carry on (their foreign keys only need a key share lock).
    The load test found this: one lock over every company for a whole sweep had orders and tasks time out, all over the site."""
    async with Session() as db:
        q = select(Company).where(Company.id.in_(ids), Company.salary_currency.is_not(None)).options(selectinload(Company.members).joinedload(Membership.user, innerjoin=True), selectinload(Company.salaries))
        companies = (await db.scalars(q.with_for_update(of=Company, skip_locked=True, key_share=True))).all()
        if not companies: return []
        paid, off, now, mails, paydays = {}, {}, utcnow(), [], []
        for p in (await db.scalars(select(Payout).where(Payout.company_id.in_([c.id for c in companies])))).all(): paid.setdefault(p.company_id, []).append(p)
        for l in await approved_leaves(db, [c.id for c in companies]): off.setdefault(l.company_id, []).append(l)
        for c in companies:
            got, due = await _check(db, c, paid.get(c.id, []), off.get(c.id, []), now)  # (a warning, a halt or a recovery: rare, one notice each)
            mails += got; paydays += due
        mails += await _remind(db, companies, paydays)
        await db.commit()
    return mails
async def sweep(company_id=None) -> None:
    """Check every company with salaries (or just one), BATCH at a time: companies being paid right now are skipped this round (their rows are locked)."""
    async with Session() as db: ids = [company_id] if company_id else list((await db.scalars(select(Company.id))).all())
    mails = []
    for i in range(0, len(ids), BATCH): mails += await _batch(ids[i:i + BATCH])
    for to, subject, body in mails:
        try: await send_email(to, subject, body)
        except Exception as e: print(f"guard email failed: {type(e).__name__}")
async def run() -> None:
    await fx.start()  # the exchange rates every comparison uses
    while True:
        await asyncio.sleep(GUARD_S)
        try: await fx.refresh(); await sweep()
        except Exception as e: print(f"payroll guard: {type(e).__name__}: {e}")
