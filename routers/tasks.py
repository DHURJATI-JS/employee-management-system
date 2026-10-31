"""Tasks and peer reviews inside a company.
Tasks: the owner, co-owners, managers and HR (tables.TASKERS) give tasks to people ranked below them: several at once, each with a title,
a description, a deadline (on the company's clock) and any number of assignees. Assignees are told by notification + email and mark a
task done (the assigner is told); whoever gave it, or the owner / a co-owner, can cancel it (assignees are told).
Reviews: anyone in a company can like or dislike anyone else in it, always with a reason. One active review per person; to change your
mind you withdraw it ("unlike" / "undislike"): no flipping, and at most MAX_REVIEWS ever per person (withdrawn ones count). The person
reviewed sees the counts and the reasons, not who wrote them; the owner, co-owners, managers and HR see who did."""
import re, uuid
import companyai
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from assist import rate_limited
from form import ActionForm, ReviewForm, TASK_BATCH, TaskForm
from guard import notify, notify_many
from meet import DURATIONS, WHISPER, calendar_meetings, company_meetings, my_meetings
from payroll import company_tz, day_seconds, hm, hours_label, in_hours, local, start_day, today, tz_name, works_on
from regions import holiday_on, holidays_between, region_name
from system import APP_NAME, FormError, flash, render, safe_referer, send_email
from tables import MAX_REVIEWS, POSITIONS, TASKERS, TOP, Attendance, Company, LeaveRequest, Membership, Review, Task, TaskAssignee, User, get_db, monitored, utcnow
router = APIRouter(tags=["tasks"])
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
def viewer(request: Request) -> ZoneInfo:  # the viewer's time zone (their region's, chosen at signup / in settings), UTC until they pick one
    try: return ZoneInfo(request.state.user.get("tz") or "UTC")
    except (ZoneInfoNotFoundError, ValueError): return ZoneInfo("UTC")
rank = lambda position: POSITIONS.get(position, {"rank": 99})["rank"]
def can_assign(me: Membership, position: str) -> bool: return me.position in TASKERS and rank(position) > rank(me.position)  # people ranked below you
async def _company(db: AsyncSession, uid: int, company_id: uuid.UUID) -> Membership:
    m = await db.get(Membership, (uid, company_id), options=[joinedload(Membership.company, innerjoin=True).selectinload(Company.members).joinedload(Membership.user, innerjoin=True)])
    if not m: raise HTTPException(404, "You're not in that company.")
    return m
def when(c: Company, t: Task) -> str: return f"{local(c, t.deadline):%d %b %Y, %H:%M} ({tz_name(c)})"
def open_day(c: Company, day: date) -> bool: return works_on(c, day) and not holiday_on(c.location_country, day)  # a working day that isn't a public holiday there
def day_mark(seconds: int, is_open: bool, on_leave: bool, is_today: bool, full: int) -> str:
    """One person's attendance on one day, for the /tasks calendar and the company's attendance page: "full" (90% of its working hours or more), "part", "absent", "leave" (approved),
    "today" (so far), or "" = nothing to mark (a day off or a public holiday, and nothing worked)."""
    if not seconds and not is_open: return ""
    return "today" if is_today else "leave" if on_leave and not seconds else "full" if full and seconds >= full * 0.9 else "part" if seconds else "absent"
def closed(c: Company) -> str | None:
    """Why tasks can't be given at c right now (None = they can): only in working hours, on a working day that isn't a public holiday."""
    now = datetime.now(company_tz(c))
    if h := holiday_on(c.location_country, now.date()): return f"Today is {h}, a public holiday at {c.name}: tasks can't be given on holidays."
    if not works_on(c, now.date()): return f"Today isn't a working day at {c.name} ({hours_label(c)}): tasks can only be given on working days."
    if not in_hours(c): return f"It's outside {c.name}'s working hours ({hours_label(c)}, {tz_name(c)}): tasks can only be given during them."
    return None
def next_open(c: Company) -> str:  # when tasks can be given again: the next working-hours start on an open day (within 3 weeks)
    now = datetime.now(company_tz(c))
    for i in range(22):
        day = now.date() + timedelta(days=i)
        start = datetime.combine(day, c.work_start, now.tzinfo)
        if open_day(c, day) and (start > now or in_hours(c)): return f"{max(start, now):%a %d %b, %H:%M} ({tz_name(c)})"
    return "not in the next 3 weeks"
def deadline_problem(c: Company, due: datetime, people: list[User]) -> str | None:  # due: naive, on the company's clock
    if h := holiday_on(c.location_country, due.date()): return f"the deadline is on {h}, a public holiday at {c.name}"
    if not works_on(c, due.date()): return f"the deadline is on a day off at {c.name} (it works {hours_label(c)})"
    if not c.work_start <= due.time() <= c.work_end: return f"the deadline is outside {c.name}'s working hours ({c.work_start:%H:%M}–{c.work_end:%H:%M} {tz_name(c)})"
    for u in people:
        if h := holiday_on(u.region, due.date()): return f"the deadline is on {h}, a national holiday for {u.username} ({region_name(u.region)})"
    return None
@router.get("/tasks")
async def my_tasks(request: Request, db: AsyncSession = Depends(get_db)):
    """Your tasks in every company (open first, by deadline), and the companies where you give tasks."""
    uid = _uid(request)
    rows = (await db.execute(select(Task, TaskAssignee, Company).join(TaskAssignee, (TaskAssignee.task_id == Task.id) & (TaskAssignee.user_id == uid)).join(Company, Company.id == Task.company_id)
                             .join(Membership, (Membership.company_id == Task.company_id) & (Membership.user_id == uid))  # companies you're still in: a task of one you left isn't yours to do
                             .where(Task.status == "open").order_by(TaskAssignee.done_at.is_not(None), Task.deadline).limit(200))).all()
    giving = (await db.execute(select(Company.id, Company.name, Membership.position).join(Membership, Membership.company_id == Company.id)
                               .where(Membership.user_id == uid, Membership.position.in_(TASKERS)).order_by(Company.name))).all()
    region = await db.scalar(select(User.region).where(User.id == uid))
    tracked = any(monitored(p) for p in (await db.scalars(select(Membership.position).where(Membership.user_id == uid))).all())  # attendance on the calendar
    return render(request, "tasks", {"rows": [{"t": t, "a": a, "c": c, "when": when(c, t), "late": not a.done_at and t.deadline < utcnow()} for t, a, c in rows], "giving": giving, "tracked": tracked,
                                     "positions": POSITIONS, "region": region, "meetings": await my_meetings(db, uid)})  # TASKERS also schedule meetings (MEET_HOSTS)
@router.get("/tasks/calendar")
async def calendar(request: Request, month: str = "", db: AsyncSession = Depends(get_db)):
    """One month of your calendar (JSON, loaded by app.js as you scroll, either way): the tasks you were given or gave that run through it
    (from the day given to the deadline, on each company's clock), with your region's national holidays."""
    uid = _uid(request)
    if not (m := re.fullmatch(r"(\d{4})-(\d{2})", month)) or not (2000 <= int(m[1]) <= 2100 and 1 <= int(m[2]) <= 12): raise HTTPException(400, "month must be YYYY-MM (2000-2100)")
    first = date(int(m[1]), int(m[2]), 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    lo, hi = datetime.combine(first - timedelta(days=1), time.min), datetime.combine(last + timedelta(days=2), time.min)  # UTC with a day's margin: exact local dates below
    rows = (await db.execute(select(Task, Company).join(Company, Company.id == Task.company_id).join(Membership, (Membership.company_id == Task.company_id) & (Membership.user_id == uid))  # companies you're still in
                             .where(Task.status == "open", Task.created_at < hi, Task.deadline >= lo, or_(Task.id.in_(select(TaskAssignee.task_id).where(TaskAssignee.user_id == uid)), Task.created_by_id == uid))
                             .order_by(Task.created_at).limit(500))).all()
    who: dict[int, list[TaskAssignee]] = {}
    for a in (await db.scalars(select(TaskAssignee).where(TaskAssignee.task_id.in_([t.id for t, _ in rows])))).all() if rows else []: who.setdefault(a.task_id, []).append(a)
    region, now, out, zone = request.state.user.get("region"), utcnow(), [], viewer(request)
    mine_at = lambda d: d.replace(tzinfo=UTC).astimezone(zone)  # on the viewer's clock (their region's time zone)
    for t, c in rows:
        s, e = mine_at(t.created_at).date(), mine_at(t.deadline).date()
        if e < first or s > last: continue
        people = who.get(t.id, [])
        mine = next((a for a in people if a.user_id == uid), None)
        out.append({"id": t.id, "title": t.title, "company": c.name, "start": s.isoformat(), "end": e.isoformat(), "due": f"{mine_at(t.deadline):%d %b, %H:%M %Z}",
                    "mine": mine is not None, "done": bool(mine and mine.done_at), "gave": t.created_by_id == uid, "progress": f"{sum(1 for a in people if a.done_at)}/{len(people)}",
                    "late": t.deadline < now and not (mine and mine.done_at), "url": f"/companies/{c.id}/tasks#task-{t.id}"})
    meets = []  # the video meetings you're invited to: one day each, with their time on your clock
    for m, cname in await calendar_meetings(db, uid, lo, hi):
        s, e = mine_at(m.starts_at), mine_at(m.ends_at)
        if first <= s.date() <= last: meets.append({"id": m.id, "title": m.title, "company": cname, "start": s.date().isoformat(), "end": s.date().isoformat(), "time": f"{s:%H:%M}–{e:%H:%M %Z}",
                                                     "status": m.status, "url": f"/meetings/{m.id}"})
    seats = [(s, c) for s, c in (await db.execute(select(Membership, Company).join(Company, Company.id == Membership.company_id).where(Membership.user_id == uid))).all() if monitored(s.position)]
    return {"month": f"{first:%Y-%m}", "label": f"{first:%B %Y}", "first": first.isoformat(), "last": last.isoformat(), "tasks": out, "meetings": meets, "region": region_name(region),
            "holidays": {d.isoformat(): n for d, n in holidays_between(region, first, last).items()}, "attendance": await attendance_days(db, uid, seats, first, last), "tracked": len(seats)}
async def attendance_days(db: AsyncSession, uid: int, seats, first: date, last: date) -> dict[str, list[dict]]:
    """Your attendance on the calendar (user asked), per day (each company's own days): every working day since you joined each company
    where yours is tracked (staff, not owners / co-owners / freelancers) up to today: "full" (90% of its working hours or more), "part",
    "absent", "leave" (approved), "today" (so far). Public holidays and days off are left alone unless you worked."""
    if not seats: return {}
    ids, out = [c.id for _, c in seats], {}
    secs = {(cid, d): s for cid, d, s in (await db.execute(select(Attendance.company_id, Attendance.day, Attendance.active_seconds)
                                                            .where(Attendance.user_id == uid, Attendance.company_id.in_(ids), Attendance.day.between(first, last)))).all()}
    leave = (await db.scalars(select(LeaveRequest).where(LeaveRequest.user_id == uid, LeaveRequest.company_id.in_(ids), LeaveRequest.status == "approved",
                                                         LeaveRequest.start_day <= last, LeaveRequest.end_day >= first))).all()
    for m, c in seats:
        now, full, joined = today(c), day_seconds(c), start_day(c, m.joined_at)
        for i in range((last - first).days + 1):
            d = first + timedelta(days=i)
            if d < joined or d > now: continue
            s, off = secs.get((c.id, d), 0), any(l.company_id == c.id and l.start_day <= d <= l.end_day for l in leave)
            if not (status := day_mark(s, open_day(c, d), off, d == now, full)): continue
            out.setdefault(d.isoformat(), []).append({"company": c.name, "status": status, "worked": hm(s), "of": hm(full), "url": f"/companies/{c.id}/dashboard"})
    return out
@router.get("/companies/{company_id}/tasks")
async def company_tasks(request: Request, company_id: uuid.UUID, title: str = "", to: int = 0, ai: int = 0, db: AsyncSession = Depends(get_db)):  # title + to: pre-filled from "who's best for this task?"; ai: "the AI picks" already chosen (the dashboard's button)
    uid = _uid(request)
    me = await _company(db, uid, company_id)
    c, tasker = me.company, me.position in TASKERS
    q = select(Task).where(Task.company_id == company_id).order_by(Task.status, Task.deadline.desc()).limit(150)
    tasks = (await db.scalars(q if tasker else q.join(TaskAssignee, (TaskAssignee.task_id == Task.id) & (TaskAssignee.user_id == uid)))).all()
    who: dict[int, list] = {}
    for a, name in (await db.execute(select(TaskAssignee, User.username).join(User, User.id == TaskAssignee.user_id).where(TaskAssignee.task_id.in_([t.id for t in tasks])))).all() if tasks else []:
        who.setdefault(a.task_id, []).append((a, name))
    people = sorted((m for m in c.members if can_assign(me, m.position)), key=lambda m: (rank(m.position), m.user.username))
    now = utcnow()
    shut = closed(c) if tasker else None
    colleagues = sorted((m for m in c.members if m.user_id != uid), key=lambda m: (rank(m.position), m.user.username))  # meetings: anyone in the company can be invited
    return render(request, "company_tasks", {"c": c, "me": me, "tasker": tasker, "people": people, "positions": POSITIONS, "batch": range(TASK_BATCH), "tz": tz_name(c), "prefill": {"title": " ".join(title.split())[:120], "to": to, "ai": ai},
                                             "meet": await company_meetings(db, me), "colleagues": colleagues, "durations": DURATIONS, "captions": WHISPER,
                                             "closed": shut, "next_open": next_open(c) if shut else "", "hours": hours_label(c),
                                             "now_local": datetime.now(company_tz(c)).strftime("%Y-%m-%dT%H:%M"), "top": me.position in TOP,
                                             "tasks": [{"t": t, "who": who.get(t.id, []), "when": when(c, t), "late": t.status == "open" and t.deadline < now,
                                                        "mine": next((a for a, _ in who.get(t.id, []) if a.user_id == uid), None)} for t in tasks]})
@router.post("/companies/{company_id}/tasks")
async def give_tasks(request: Request, company_id: uuid.UUID, form: TaskForm = Depends(TaskForm.as_form), db: AsyncSession = Depends(get_db)):
    """One or more tasks at once, each to any number of people ranked below you: notification + one email per person listing theirs."""
    uid = _uid(request)
    me = await _company(db, uid, company_id)
    c = me.company
    if me.position not in TASKERS: raise FormError(f"Only {c.name}'s owner, co-owners, managers and HR can give tasks.")
    if why := closed(c): raise FormError(f"{why} Next chance: {next_open(c)}.")
    if rate_limited(f"tasks:{uid}", 30, 600): raise FormError("That's a lot of tasks in a short time: wait a few minutes.")
    allowed, tz, now = {m.user_id: m for m in c.members if can_assign(me, m.position)}, company_tz(c), datetime.now(UTC)
    batch, picked, sig = form.tasks(), {}, None
    for i, t in batch:
        if t["ai"]:  # (user asked) the AI picks who, by what the task needs (companyai.pick): the boxes ticked don't count then
            if not t["deadline"] or t["deadline"].replace(tzinfo=tz) <= now: raise FormError(f"Task {i} ({t['title']}): the deadline has already passed ({tz_name(c)} time).")
            sig = sig or await companyai.signals(db, c)  # once, however many tasks the AI picks for
            if not (best := await companyai.pick(db, c, list(allowed.values()), t["title"], t["description"], t["deadline"], t["ai"], sig)): raise FormError(f"Task {i} ({t['title']}): the AI found nobody free on that day: everyone below you is on leave or on a holiday.")
            t["to"], picked[i] = sorted(m.user_id for m, _ in best), {m.user_id: why for m, why in best}
        if bad := [x for x in t["to"] if x not in allowed]: raise FormError(f"Task {i} ({t['title']}): you can only give tasks to people in {c.name} ranked below you ({POSITIONS[me.position]['label']}).")
        if t["deadline"].replace(tzinfo=tz) <= now: raise FormError(f"Task {i} ({t['title']}): the deadline has already passed ({tz_name(c)} time).")
        if why := deadline_problem(c, t["deadline"], [allowed[x].user for x in t["to"]]): raise FormError(f"Task {i} ({t['title']}): {why}. Pick another deadline.")
    made, by_person, notes = [Task(company_id=company_id, title=t["title"], description=t["description"], deadline=t["deadline"].replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None),
                                   created_by_id=uid, created_by_name=me.user.username) for _, t in batch], {}, []
    db.add_all(made)
    await db.flush()  # one statement for all of them: their ids
    for (i, t), task in zip(batch, made):
        db.add_all(TaskAssignee(task_id=task.id, user_id=x) for x in t["to"])
        due, people = when(c, task), [allowed[x].user for x in t["to"]]
        body = f"{me.user.username} gave you a task at {c.name}, due {due}: {task.title}." + (f" {task.description[:600]}" if task.description else "") + (" The company AI picked you for it." if i in picked else "")
        notes += [{"user_id": u.id, "company_id": c.id, "level": "info", "title": f"New task: {task.title} ({c.name})", "body": body, "link": f"/companies/{company_id}/tasks#task-{task.id}", "key": f"task:{task.id}"} for u in people]
        for u in people: by_person.setdefault(u.id, (u, []))[1].append(f"- {task.title} (due {due})" + (f"\n  {task.description}" if task.description else ""))
    await notify_many(db, notes)
    names, cname = sorted({allowed[x].user.username for _, t in batch for x in t["to"]}), c.name
    await db.commit()
    for u, lines in by_person.values():
        try: await send_email(u.email, f"{len(lines)} new task{'s' if len(lines) != 1 else ''} at {cname}", f"Hi {u.username},\n\n{me.user.username} gave you "
                              f"{'a task' if len(lines) == 1 else str(len(lines)) + ' tasks'} at {cname}:\n\n" + "\n".join(lines) + f"\n\nSee them and mark them done under Tasks in {APP_NAME}.\n")
        except Exception as e: print(f"task email failed: {type(e).__name__}")
    chose = " ".join(f"Task {i}: the AI picked " + "; ".join(f"{allowed[u].user.username} ({why})" for u, why in who.items()) + "." for i, who in picked.items())
    flash(request, f"{len(made)} task{'s' if len(made) != 1 else ''} given to {', '.join(names)}: they've been notified and emailed." + (f" {chose}" if chose else ""), "success")
    return RedirectResponse(f"/companies/{company_id}/tasks", 303)
async def _task(db: AsyncSession, task_id: int) -> tuple[Task, Company]:
    t = await db.get(Task, task_id)
    c = await db.get(Company, t.company_id) if t else None
    if not t or not c: raise HTTPException(404, "There's no such task.")
    return t, c
@router.post("/tasks/{task_id}/{action}")
async def task_action(request: Request, task_id: int, action: str, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """done / reopen (the assignee), cancel (whoever gave it, or the owner / a co-owner)."""
    uid = _uid(request)
    t, c = await _task(db, task_id)
    back = "/tasks" if safe_referer(request, "").split("?")[0] == "/tasks" else f"/companies/{c.id}/tasks"  # one of two known pages: never a URL from the request
    if action in ("done", "reopen"):
        a = await db.get(TaskAssignee, (task_id, uid))
        if not a or not await db.get(Membership, (uid, c.id)): raise HTTPException(404, "There's no such task.")
        if t.status != "open": raise FormError("That task was cancelled.")
        a.done_at = utcnow() if action == "done" else None
        if action == "done" and t.created_by_id:
            giver = await db.get(User, t.created_by_id)
            if giver: await notify(db, [giver], c, "info", f"Task done: {t.title} ({c.name})", f"{request.state.user['username']} marked “{t.title}” done.", f"/companies/{c.id}/tasks#task-{t.id}", f"task-done:{t.id}:{uid}")
        await db.commit()
        flash(request, f"“{t.title}” {'marked done' if action == 'done' else 'reopened'}.", "success")
    elif action == "cancel":
        me = await db.get(Membership, (uid, c.id))
        if not me or not (t.created_by_id == uid or me.position in TOP): raise FormError("Only whoever gave this task, or the owner or a co-owner, can cancel it.")
        if t.status != "open": raise FormError("That task is already cancelled.")
        t.status = "cancelled"
        people = (await db.scalars(select(User).join(TaskAssignee, TaskAssignee.user_id == User.id).where(TaskAssignee.task_id == task_id))).all()
        await notify(db, list(people), c, "warn", f"Task cancelled: {t.title} ({c.name})", f"{request.state.user['username']} cancelled “{t.title}”: you don't need to do it.", f"/companies/{c.id}/tasks", f"task-cancel:{t.id}")
        await db.commit()
        flash(request, f"“{t.title}” cancelled: everyone it was for has been told.", "info")
    else: raise HTTPException(404, "There's no such action.")
    return RedirectResponse(f"{back}#task-{task_id}", 303)
# ---------- peer reviews: like / dislike with a reason ----------
async def review_panel(db: AsyncSession, company_id: uuid.UUID, viewer: Membership, subject_id: int) -> dict:
    """For a member's page: active likes / dislikes, your active review of them, how many you have left, and the reasons (to them without
    names; with names to the owner, co-owners, managers and HR)."""
    active = (await db.execute(select(Review, User.username).join(User, User.id == Review.reviewer_id)
                               .where(Review.company_id == company_id, Review.subject_id == subject_id, Review.withdrawn_at.is_(None)).order_by(Review.created_at.desc()))).all()
    used = await db.scalar(select(func.count()).where(Review.company_id == company_id, Review.reviewer_id == viewer.user_id, Review.subject_id == subject_id))
    sees, names = viewer.user_id == subject_id or viewer.position in TASKERS, viewer.position in TASKERS and viewer.user_id != subject_id  # your own page never names who wrote about you, whatever your position
    return {"likes": sum(r.kind == "like" for r, _ in active), "dislikes": sum(r.kind == "dislike" for r, _ in active), "max": MAX_REVIEWS,
            "mine": next((r for r, _ in active if r.reviewer_id == viewer.user_id), None), "left": MAX_REVIEWS - (used or 0), "self": viewer.user_id == subject_id,
            "reasons": [{"r": r, "by": n if names else None} for r, n in active] if sees else [], "names": names}
async def review_counts(db: AsyncSession, company_id: uuid.UUID) -> dict[int, tuple[int, int]]:  # member -> (likes, dislikes), active ones
    rows = (await db.execute(select(Review.subject_id, func.count().filter(Review.kind == "like"), func.count().filter(Review.kind == "dislike"))
                             .where(Review.company_id == company_id, Review.withdrawn_at.is_(None)).group_by(Review.subject_id))).all()
    return {s: (l, d) for s, l, d in rows}
@router.post("/companies/{company_id}/members/{user_id}/review")
async def review(request: Request, company_id: uuid.UUID, user_id: int, form: ReviewForm = Depends(ReviewForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    me, them = await db.get(Membership, (uid, company_id)), await db.get(Membership, (user_id, company_id), options=[joinedload(Membership.user, innerjoin=True), joinedload(Membership.company, innerjoin=True)])
    if not me or not them: raise FormError("You can only review people in a company you're both in.")
    if uid == user_id: raise FormError("You can't review yourself.")
    name, cname, link = them.user.username, them.company.name, f"/companies/{company_id}/members/{user_id}#reviews"
    mine = await db.scalar(select(Review).where(Review.company_id == company_id, Review.reviewer_id == uid, Review.subject_id == user_id, Review.withdrawn_at.is_(None)))
    if mine: raise FormError(f"You've already {mine.kind}d {name}: withdraw that first if you've changed your mind.")
    used = await db.scalar(select(func.count()).where(Review.company_id == company_id, Review.reviewer_id == uid, Review.subject_id == user_id))
    if used >= MAX_REVIEWS: raise FormError(f"You've used both of your {MAX_REVIEWS} reviews of {name} at {cname}.")
    if rate_limited(f"review:{uid}", 20, 3600): raise FormError("That's a lot of reviews in a short time: try again later.")
    r = Review(company_id=company_id, reviewer_id=uid, subject_id=user_id, kind=form.kind, reason=form.reason)
    db.add(r)
    await db.flush()
    await notify(db, [them.user], them.company, "info", f"A colleague {form.kind}d your work ({cname})", f"Someone at {cname} {form.kind}d your work: “{form.reason}”", link, f"review:{r.id}")
    await db.commit()
    left = MAX_REVIEWS - used - 1
    flash(request, f"You {form.kind}d {name}. {'You can withdraw it if you change your mind' + (f' ({left} review left after that).' if left else ' (it was your last one).')}", "success")
    return RedirectResponse(link, 303)
@router.post("/reviews/{review_id}/withdraw")
async def withdraw(request: Request, review_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """Unlike / undislike: only your own, active review. It still counts toward the MAX_REVIEWS you can give that person."""
    uid, r = _uid(request), await db.get(Review, review_id)
    if not r or r.reviewer_id != uid: raise HTTPException(404, "There's no such review.")
    if r.withdrawn_at: raise FormError("You've already withdrawn that.")
    r.withdrawn_at, kind, link = utcnow(), r.kind, f"/companies/{r.company_id}/members/{r.subject_id}#reviews"
    left = MAX_REVIEWS - await db.scalar(select(func.count()).where(Review.company_id == r.company_id, Review.reviewer_id == uid, Review.subject_id == r.subject_id))
    await db.commit()
    flash(request, f"{'Unliked' if kind == 'like' else 'Undisliked'}. " + (f"You can review them {left} more time{'s' if left != 1 else ''}." if left else "You've used both of your reviews of them."), "info")
    return RedirectResponse(link, 303)
