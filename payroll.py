"""Working hours, attendance and salary cycles. Everything runs on the company's own clock (its location's timezone from Geoapify):
working hours are at most MAX_WORK_HOURS on the days it picks, and salary is paid in CYCLE_DAYS cycles counted from the day each
member joined ("from day one"): cycle 1 = joining day + 29 days, cycle 2 the next 30 days, and so on. Payouts (routers/payroll.py)
record one cycle each; the amounts are typed in by the payer, and the suggestion here is just the starting value."""
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from jinja2 import pass_context
from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from banks import bank_line
from fx import convert, rate_text
from payrules import tax_label
from system import templates
from tables import CYCLE_DAYS, FREELANCE, HOURS_EVERY_DAYS, POSITIONS, REGIONS, TOP, Attendance, Company, LeaveRequest, Membership, Payout, monitored, utcnow
def tax_line(currency: str) -> str:  # the payslip line for the money withheld: "TDS deducted" (India), "PAYE + NI" (UK), "Tax withheld" (US) …
    label = tax_label(currency)
    return "TDS deducted" if label == "TDS" else label[0].upper() + label[1:]
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
def company_tz(c) -> ZoneInfo:  # c: a Company (or anything with timezone + location_country)
    for name in (c.timezone, "Asia/Kolkata" if c.location_country == "IN" else None, "UTC"):  # India has one timezone; otherwise UTC until the location gives one
        if name:
            try: return ZoneInfo(name)
            except (ValueError, ZoneInfoNotFoundError): pass
    return ZoneInfo("UTC")
def tz_name(c) -> str: return company_tz(c).key
def local(c, utc_naive: datetime) -> datetime: return utc_naive.replace(tzinfo=UTC).astimezone(company_tz(c))  # a stored naive-UTC time, on the company's clock
def today(c) -> date: return datetime.now(company_tz(c)).date()
def works_on(c, day: date) -> bool: return bool(c.work_days >> day.weekday() & 1)
def in_hours(c, when: datetime | None = None) -> bool:
    """Is it working time at company c right now (or at `when`, an aware datetime)? The window is [work_start, work_end) on a working day."""
    now = (when or datetime.now(UTC)).astimezone(company_tz(c))
    return works_on(c, now.date()) and c.work_start <= now.time().replace(tzinfo=None) < c.work_end
def days_label(mask: int) -> str:  # 31 -> "Mon–Fri", 21 -> "Mon, Wed, Fri"
    days = [i for i in range(7) if mask >> i & 1]
    if len(days) > 2 and days == list(range(days[0], days[-1] + 1)): return f"{DAY_NAMES[days[0]]}–{DAY_NAMES[days[-1]]}"
    return ", ".join(DAY_NAMES[i] for i in days)
def hours_label(c) -> str: return f"{c.work_start:%H:%M}–{c.work_end:%H:%M} {days_label(c.work_days)}"
def day_seconds(c) -> int: return (c.work_end.hour * 60 + c.work_end.minute - c.work_start.hour * 60 - c.work_start.minute) * 60
def hours_locked(c) -> str:  # when working hours can change again, on the company's clock; "" = they can change now (user's rule: once every HOURS_EVERY_DAYS)
    free = c.hours_changed_at + timedelta(days=HOURS_EVERY_DAYS) if c.hours_changed_at else None
    return f"{local(c, free):%d %b %Y, %H:%M} ({tz_name(c)})" if free and free > utcnow() else ""
# ---------- salary cycles ----------
LOOK_BACK = max(24, 730 // CYCLE_DAYS)  # how far back unpaid cycles still count: two years, in days and not in cycles (24 cycles were 2 years at 30 days, but only 24 days with 1-day cycles: older pay fell out of what's owed)
def start_day(c, joined_at: datetime) -> date: return local(c, joined_at).date()  # day one, on the company's calendar
def cycle_dates(c, joined_at: datetime, n: int) -> tuple[date, date]:
    first = start_day(c, joined_at) + timedelta(days=CYCLE_DAYS * (n - 1))
    return first, first + timedelta(days=CYCLE_DAYS - 1)
def current_cycle(c, joined_at: datetime) -> int: return max(1, (today(c) - start_day(c, joined_at)).days // CYCLE_DAYS + 1)
def working_days(c, first: date, last: date) -> int: return sum(works_on(c, first + timedelta(days=i)) for i in range((last - first).days + 1)) if last >= first else 0
def per_cycle(c, position: str, agreed: int | None = None) -> int | None:
    """The salary for one 30-day cycle: the position's minimum (or the salary agreed in their offer, whichever is higher), a yearly
    salary divided by 12. None when there's nothing to go on (owners / co-owners, or no salaries set yet)."""
    base = max((x for x in (c.salary_of().get(position), agreed) if x), default=None)
    return cycle_pay(base, c.salary_period) if base and c.salary_currency else None
def cycle_pay(base: int, period: str) -> int: return round((base if period == "month" else base / 12) * CYCLE_DAYS / 30)  # a month's pay per 30-day cycle; shorter cycles (CYCLE_DAYS in .env, e.g. 1 = every day) pay their share
def minimum_per_cycle(c, position: str) -> int | None: return per_cycle(c, position) if c.salary_of().get(position) else None
def keep_pay(c, m, rates: tuple | None = None) -> None:
    """m's pay is about to change (a new position, new minimum salaries, another currency). User's rule: new pay starts with the next cycle, so every cycle already started stays
    at what they have now: a promotion can't reprice old cycles, and becoming a co-owner (no salary) doesn't wipe what they earned. Call it before the change (c's salaries
    loaded). rates: their (per cycle, minimum) when the caller has them."""
    per, low = rates or (per_cycle(c, m.position, m.salary), minimum_per_cycle(c, m.position))
    m.pay_history = [*(m.pay_history or []), {"until": cycle_dates(c, m.joined_at, current_cycle(c, m.joined_at))[1].isoformat(), "per": per, "low": low, "position": m.position}]
def pay_of(m, first: date, now: tuple) -> tuple:  # (per cycle, minimum, position) of the cycle that starts on `first`: what they had when it started, else now = what they have today
    for r in m.pay_history or ():
        if first <= date.fromisoformat(r["until"]): return r["per"], r["low"], r["position"]
    return now
def worked_fraction(c, first: date, last: date, now: datetime) -> float:
    """How much of a cycle's working hours have gone by at `now` (company time): whole working days before today, plus today's hours so far."""
    total = working_days(c, first, last) * day_seconds(c)
    if now.date() > last or not total: return 1.0 if now.date() > last else 0.0
    done = working_days(c, first, now.date() - timedelta(days=1)) * day_seconds(c)
    if works_on(c, now.date()) and now.date() >= first:
        done += max(0, min((now - datetime.combine(now.date(), c.work_start, now.tzinfo)).total_seconds(), day_seconds(c)))
    return min(1.0, done / total)
def leave_days(c, leaves, first: date, last: date) -> int:
    """Working days in [first, last] covered by approved leave (each day once, however the requests overlap)."""
    days: set[date] = set()
    for l in leaves:
        lo, hi = max(l.start_day, first), min(l.end_day, last)
        days.update(lo + timedelta(days=i) for i in range((hi - lo).days + 1))
    return sum(works_on(c, d) for d in days)
def terms(c, m, k: int, leaves=(), rates: tuple | None = None) -> dict:
    """Cycle k for member m: its dates, the approved leave in it (unpaid: day rate = the cycle's salary / its working days), and the
    salary + minimum after that deduction, as they stood when the cycle started (pay_of). suggest / minimum are None for people without a set salary.
    rates: their (per cycle, minimum, position) today when the caller has them: with 1-day cycles this runs dozens of times per person, per page."""
    first, last = cycle_dates(c, m.joined_at, k)
    per, low, position = pay_of(m, first, rates or (per_cycle(c, m.position, m.salary), minimum_per_cycle(c, m.position), m.position))
    wd = working_days(c, first, last)
    ld = leave_days(c, leaves, first, last) if leaves else 0
    cut = lambda x: round(x * min(ld, wd) / wd) if x and wd else 0
    return {"first": first, "last": last, "leave": ld, "per": per, "position": position, "deduction": cut(per), "suggest": per - cut(per) if per else None, "minimum": low - cut(low) if low else None}
def owed(c, m, paid_cycles, leaves=(), known: dict | None = None) -> int:
    """The cost of m's working hours not paid yet: every finished unpaid cycle in full (their salary can't go below it) minus approved
    leave, plus the current cycle's working hours so far minus the leave taken so far. 0 for people without a set salary (owner, co-owners),
    unless they had one in a cycle not paid yet (a co-owner who was staff). known: the terms of their unpaid cycles when the caller has them."""
    if not m.pay_history and not per_cycle(c, m.position, m.salary): return 0
    n, now, total = current_cycle(c, m.joined_at), datetime.now(company_tz(c)), 0
    for k in range(max(1, n - LOOK_BACK), n + 1):
        if k in paid_cycles: continue
        t = known[k] if known else terms(c, m, k, leaves)
        if not (per := t["per"]): continue
        if k < n: total += t["suggest"]
        else:
            wd = working_days(c, t["first"], t["last"])
            gone = leave_days(c, leaves, t["first"], min(t["last"], now.date())) if leaves else 0
            total += max(0, round(per * worked_fraction(c, t["first"], t["last"], now)) - (round(per * gone / wd) if wd else 0))
    return total
def worth_cur(c) -> str | None: return c.net_worth_currency or c.salary_currency  # the net worth's currency (what the guard compares in)
def pay_rows(c, members, payouts: list[Payout], leaves=()) -> dict[int, dict]:
    """Per member (user id): {"cycle": current cycle, "dates": its (first, last) day, "due": [(cycle, first, last)] unpaid up to the
    current one (oldest first), "overdue": how many of those already ended, "paid": {cycle: payout}, "suggest": per-cycle salary,
    "minimum": the least a payout may be, "terms": {due cycle: payroll.terms (after unpaid leave)}, "owed", "max" (the clearance)}.
    leaves: the company's approved LeaveRequests. Someone who left and came back counts cycles from 1 again, so payouts of their earlier time here don't count: only those
    recorded since they joined, or for a cycle starting on or after that day."""
    paid: dict[int, dict] = {}
    since = {m.user_id: m for m in members}
    for p in payouts:
        if (m := since.get(p.user_id)) and (p.created_at >= m.joined_at or p.period_start >= start_day(c, m.joined_at)): paid.setdefault(p.user_id, {})[p.cycle] = p
    off: dict[int, list] = {}
    for l in leaves: off.setdefault(l.user_id, []).append(l)
    out = {}
    for m in members:
        n, mine, gone = current_cycle(c, m.joined_at), paid.get(m.user_id, {}), off.get(m.user_id, [])
        due = [(k, *cycle_dates(c, m.joined_at, k)) for k in range(max(1, n - LOOK_BACK), n + 1) if k not in mine]  # the last 2 years at most
        rates = per_cycle(c, m.position, m.salary), minimum_per_cycle(c, m.position), m.position
        known = {k: terms(c, m, k, gone, rates) for k, *_ in due}
        out[m.user_id] = {"cycle": n, "dates": cycle_dates(c, m.joined_at, n), "paid": mine, "due": due, "overdue": sum(k < n for k, *_ in due),
                          "suggest": rates[0], "minimum": rates[1], "owed": owed(c, m, mine, gone, known), "terms": known, "leaves": gone}
    funds, everyone = None if c.net_worth is None else convert(c.net_worth, worth_cur(c), c.salary_currency), sum(r["owed"] for r in out.values())
    for r in out.values(): r["max"] = None if funds is None else funds - (everyone - r["owed"])  # the clearance, the most one payment may be: the net worth (in the salaries' currency) minus what everyone else's working hours cost and isn't paid yet, so paying one person, the owner included, never spends what the others have earned. None = no net worth set yet
    return out
async def at_work(db: AsyncSession, uid: int):
    """The company whose working hours are on right now where uid is staff (not the owner, a co-owner or a freelancer), or None. During
    them, they can't look at or search other companies (routers/discover.py, the join pages). A halted company doesn't count."""
    rows = (await db.scalars(select(Company).join(Membership, Membership.company_id == Company.id)
                             .where(Membership.user_id == uid, Membership.position.not_in((*TOP, FREELANCE)), Company.halted_at.is_(None)))).all()
    return next((c for c in rows if in_hours(c)), None)
async def approved_leaves(db: AsyncSession, company_ids) -> list[LeaveRequest]:
    return list((await db.scalars(select(LeaveRequest).where(LeaveRequest.company_id.in_(list(company_ids)), LeaveRequest.status == "approved"))).all())
def unsettled(rows: dict[int, dict], members) -> list[str]:
    """Who still has salary due (any unpaid cycle up to the current one with something to pay): a company can't be deleted until it's none."""
    names = {m.user_id: m.user.username for m in members}
    return [f"{names[uid]} (cycle {', '.join(str(k) for k, *_ in r['due'] if (r['terms'][k]['suggest'] or 0) > 0)})" for uid, r in rows.items()
            if any((r["terms"][k]["suggest"] or 0) > 0 for k, *_ in r["due"])]
def transferred(p: Payout) -> datetime: return p.transferred_at.replace(tzinfo=UTC).astimezone(ZoneInfo(p.timezone))  # as it was typed (company time)
# ---------- attendance ----------
async def attendance(db: AsyncSession, c, members) -> dict[int, dict]:
    """Per monitored member: {"today": seconds active today, "days": days present this cycle, "seconds": active this cycle,
    "workdays": working days so far this cycle}. One query for the whole company."""
    tracked = [m for m in members if monitored(m.position)]
    if not tracked: return {}
    ranges = {m.user_id: cycle_dates(c, m.joined_at, current_cycle(c, m.joined_at)) for m in tracked}
    rows = (await db.execute(select(Attendance.user_id, Attendance.day, Attendance.active_seconds)
                             .where(Attendance.company_id == c.id, Attendance.user_id.in_(ranges), Attendance.day >= min(r[0] for r in ranges.values())))).all()
    now = today(c)
    out = {uid: {"today": 0, "days": 0, "seconds": 0, "workdays": working_days(c, first, min(last, now))} for uid, (first, last) in ranges.items()}
    for uid, day, secs in rows:
        first, last = ranges[uid]
        if day == now: out[uid]["today"] = secs
        if first <= day <= last and secs: out[uid]["days"] += 1; out[uid]["seconds"] += secs
    return out
async def cycle_attendance(db: AsyncSession, c, user_id: int, first: date, last: date) -> tuple[int, int]:
    """(days present, seconds active) for one member in one cycle: printed on their payslip."""
    rows = (await db.execute(select(Attendance.active_seconds).where(Attendance.company_id == c.id, Attendance.user_id == user_id,
                                                                    Attendance.day.between(first, last), Attendance.active_seconds > 0))).scalars().all()
    return len(rows), sum(rows)
def hm(seconds: int) -> str: return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
# ---------- payslip (the same lines in the emails, the statements and the printable slip) ----------
def money(currency: str, amount: int) -> str: return f"{currency} {amount:,}"
def slip(p: Payout) -> list[tuple[str, str]]:
    when, sc = transferred(p), p.salary_currency or p.currency  # sc: the company's salary currency then (its country's rules name the deduction)
    label = lambda k: POSITIONS.get(k, {"label": k})["label"]
    return [("Payslip no.", f"{p.id:06d}"), ("Company", p.company_name), *([("Company ID", p.company_code)] if p.company_code else []), ("Employee", f"{p.employee_name} ({label(p.position)})"),
            ("Pay period", f"cycle {p.cycle}: {p.period_start:%d %b %Y} – {p.period_end:%d %b %Y}"),
            ("Gross salary", money(p.currency, p.gross)), (tax_line(sc), money(p.currency, p.tds)), ("Net paid", money(p.currency, p.net)),
            *([("Counted as", f"{money(sc, p.counted)} gross in {sc}, the salary currency" + (f", at {rate_text(p.currency, sc, p.fx_rate)}" if p.fx_rate else ""))] if sc != p.currency else []),
            ("Credited to", f"{bank_line(p)}, holder {p.holder_name}"),
            *([("Paid from", f"{p.company_name}'s {p.from_bank} account ••••{p.from_last4}")] if p.from_last4 else []),
            ("Transferred", f"{when:%d %b %Y, %H:%M} ({p.timezone})"), ("Reference", p.reference),
            *([("Attendance", p.attendance)] if p.attendance else []),
            ("Recorded by", f"{p.paid_by_name} ({label(p.paid_by_position)}) on {p.created_at:%d %b %Y, %H:%M} UTC")]
def deposit_slip(d) -> list[tuple[str, str]]:  # a Deposit: the receipt lines (statements, emails)
    label = POSITIONS.get(d.by_position, {"label": d.by_position})["label"]
    wc = d.worth_currency or d.currency
    return [("Deposit no.", f"D{d.id:06d}"), ("Company", d.company_name), ("Company ID", d.company_code), ("Amount", money(d.currency, d.amount)), ("From", d.source),
            *([("Added to net worth", f"{money(wc, d.credited)} (converted from {d.currency} at the day's rate)")] if wc != d.currency else []),
            ("Credited to", f"{d.company_name}'s {d.to_bank} account ••••{d.to_last4}"), ("Received", f"{transferred(d):%d %b %Y, %H:%M} ({d.timezone})"), ("Reference", d.reference),
            *([("Note", d.note)] if d.note else []), ("Net worth after", money(wc, d.net_worth_after)),
            ("Recorded by", f"{d.by_name} ({label}) on {d.created_at:%d %b %Y, %H:%M} UTC")]
# ---------- shown in the viewer's region: their time zone and currency (request.state.user["tz"] / ["region"], chosen at signup or in settings) ----------
def viewer_zone(ctx) -> ZoneInfo:
    try: return ZoneInfo((ctx.get("user") or {}).get("tz") or "UTC")
    except (ZoneInfoNotFoundError, ValueError): return ZoneInfo("UTC")
@pass_context
def at(ctx, when, fmt: str = "%d %b %Y, %H:%M") -> str:  # {{ at(m.created_at) }}: a stored time (naive = UTC) on the viewer's clock, with its zone when it has a time of day
    if when is None: return ""
    mine = (when if when.tzinfo else when.replace(tzinfo=UTC)).astimezone(viewer_zone(ctx))
    return mine.strftime(fmt) + (f" {mine.tzname()}" if "%H" in fmt else "")
@pass_context
def money_view(ctx, currency: str, amount) -> str:  # {{ money('USD', 160) }}: + "≈ INR 15,247" in the viewer's region currency when it's another one
    shown = money(currency, amount)
    mine = REGIONS.get((ctx.get("user") or {}).get("region") or "", (None, None))[1]
    if not mine or not currency or mine == currency or amount is None: return shown
    return Markup(f'{escape(shown)} <small class="hx-fx" title="in your region\'s currency, at today\'s rate">≈ {escape(money(mine, convert(amount, currency, mine)))}</small>')
@pass_context
def your_hours(ctx, c) -> str:  # " · your time 03:30–11:30" when the viewer's clock isn't the company's ("" otherwise)
    zone, here = viewer_zone(ctx), company_tz(c)
    if (ctx.get("user") or {}).get("tz") in (None, str(here)): return ""
    day = today(c)
    s, e = (datetime.combine(day, t, here).astimezone(zone) for t in (c.work_start, c.work_end))
    shift = " (next day)" if s.date() > day else " (day before)" if s.date() < day else ""
    return f" · your time {s:%H:%M}–{e:%H:%M} {s.tzname()}{shift}"
@pass_context
def at_span(ctx, start, end) -> str:  # {{ at_span(m.starts_at, m.ends_at) }}: "Fri 30 Oct, 10:00–10:30 IST" on the viewer's clock (both stored naive UTC)
    s, e = (x.replace(tzinfo=UTC).astimezone(viewer_zone(ctx)) for x in (start, end))
    return f"{s:%a %d %b, %H:%M}–{e:%H:%M}" + (f" (ends {e:%a %d %b})" if e.date() != s.date() else "") + f" {e.tzname()}"
templates.env.globals.update(work_hours=hours_label, tz_name=tz_name, money=money_view, slip=slip, deposit_slip=deposit_slip, transferred=transferred, worth_cur=worth_cur,
                             at=at, at_span=at_span, your_hours=your_hours, hours_locked=hours_locked)  # money in templates: + the viewer's currency; Python texts (emails, flashes) use plain money()  # {{ work_hours(c) }}, {{ money('INR', 5000) }} …
def slip_text(p: Payout) -> str:
    lines = slip(p)
    width = max(len(k) for k, _ in lines)
    rule = "-" * 60
    return "\n".join([f"PAYSLIP · {p.company_name}", rule, *(f"{k.ljust(width)} : {v}" for k, v in lines), rule,
                      f"Net amount credited: {money(p.currency, p.net)} (gross {money(p.currency, p.gross)} - {tax_label(p.salary_currency or p.currency)} {money(p.currency, p.tds)})"])
