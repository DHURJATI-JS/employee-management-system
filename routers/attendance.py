"""A company's attendance (user asked), opened from its dashboard: one month, one row per person, one mark per day, by the rules of the /tasks calendar
(routers/tasks.day_mark). Its owner, co-owners, managers and HR see everyone who's tracked, anyone else only themselves (as on the dashboard). Owners, co-owners
and freelancers aren't tracked (tables.monitored), so they have no row. Two queries on top of the company's members, however long the list."""
import uuid
from calendar import monthrange
from datetime import date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from payroll import approved_leaves, day_seconds, hm, hours_label, start_day, today, tz_name
from routers.tasks import day_mark, open_day
from system import render
from tables import PAYERS, POSITIONS, Attendance, Company, Membership, get_db, monitored
router = APIRouter(tags=["attendance"])
SIGN = {"full": "✓", "part": "◐", "absent": "✗", "leave": "☾", "today": "●"}  # the calendar's marks (app.js ATT)
SAYS = {"full": "full day", "part": "part of the day", "absent": "absent", "leave": "on leave", "today": "today so far"}
def _month(text: str, now: date, earliest: date) -> date:  # "2026-10" -> its first day, between the first month anyone here was tracked and this one (anything else: this month)
    try: first = date(int(text[:4]), int(text[5:7]), 1)
    except ValueError: first = now.replace(day=1)
    return max(min(first, now.replace(day=1)), earliest)
def _row(m: Membership, days: list[date], joined: date, secs: dict, leaves: list, opens: dict, now: date, full: int) -> dict:
    cells, worked, present, expected, absent, leave = [], 0, 0, 0, 0, 0
    for d in days:
        s = secs.get((m.user_id, d), 0)
        if d < joined or d > now: cells.append({"mark": "", "off": False, "title": "", "now": d == now}); continue
        mark = day_mark(s, opens[d], any(l.start_day <= d <= l.end_day for l in leaves), d == now, full)
        title = f"{d:%a %d %b}: " + ("day off or holiday" if not mark else f"{SAYS[mark]}, {hm(s)} of {hm(full)}" if s or mark == "today" else SAYS[mark])
        cells.append({"mark": mark, "sign": SIGN.get(mark, ""), "off": not mark, "title": title, "now": d == now})
        worked += s; present += s > 0; absent += mark == "absent"; leave += mark == "leave"
        expected += mark in ("full", "part", "absent") or (mark == "today" and s > 0)
    return {"m": m, "cells": cells, "present": present, "expected": expected, "absent": absent, "leave": leave, "worked": hm(worked)}
@router.get("/companies/{company_id}/attendance")
async def company_attendance(request: Request, company_id: uuid.UUID, month: str = "", db: AsyncSession = Depends(get_db)):
    uid = int(request.state.user["user_id"])
    me = await db.get(Membership, (uid, company_id), options=[joinedload(Membership.company, innerjoin=True).selectinload(Company.members).joinedload(Membership.user, innerjoin=True)])
    if not me: raise HTTPException(404, "You're not a member of that company.")
    c, everyone = me.company, me.position in PAYERS
    now, full = today(c), day_seconds(c)
    people = sorted((m for m in c.members if monitored(m.position) and (everyone or m.user_id == uid)), key=lambda m: (POSITIONS.get(m.position, {"rank": 99})["rank"], m.user.username))
    joined = {m.user_id: start_day(c, m.joined_at) for m in people}
    this, earliest = now.replace(day=1), min(joined.values(), default=now).replace(day=1)
    first = _month(month, now, earliest)
    days = [first + timedelta(days=i) for i in range(monthrange(first.year, first.month)[1])]
    secs = {(u, d): s for u, d, s in (await db.execute(select(Attendance.user_id, Attendance.day, Attendance.active_seconds).where(Attendance.company_id == c.id, Attendance.user_id.in_(joined), Attendance.day.between(first, days[-1])))).all()} if joined else {}
    away: dict[int, list] = {}
    for l in await approved_leaves(db, [c.id]): away.setdefault(l.user_id, []).append(l)
    opens = {d: open_day(c, d) for d in days}
    older, newer = (first - timedelta(days=1)).replace(day=1) if first > earliest else None, days[-1] + timedelta(days=1) if first < this else None
    return render(request, "company_attendance", {"c": c, "everyone": everyone, "days": days, "opens": opens, "today_is": now, "positions": POSITIONS, "hours": hours_label(c), "tz": tz_name(c), "full": hm(full),
                                                  "rows": [_row(m, days, joined[m.user_id], secs, away.get(m.user_id, []), opens, now, full) for m in people], "label": f"{first:%B %Y}", "older": older, "newer": newer})
