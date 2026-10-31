"""Video meetings in a company (meet.py runs the live part). The owner, co-owners, managers and HR (tables.MEET_HOSTS) schedule one for
the members they pick (or everyone), inside working hours on a working day that isn't a holiday, never overlapping another: a company
holds one call at a time. Or they start one now. Invitees get a notification + an email, a reminder 10 minutes before, and a nudge on
every open tab when it starts. Only invited members can join (/meetings/<id>/room); afterwards they, and the company's owner,
co-owners, managers and HR, can open its record (/meetings/<id>): the recordings, the AI's notes, the captions and the chat.
The room page is the only one allowed the camera, microphone and screen capture (system.py's Permissions-Policy)."""
import json, time, uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from assist import rate_limited
from decoraters import public
from form import ActionForm, MeetingForm
from guard import notify
from meet import (DURATIONS, MAX_PARTS, NOT_INVITED, WHISPER, access, clash, line_view, notes_of, people_of, recording_file, recordings, remove_files, room_shut, rooms,
                  slot_problem)
from payroll import company_tz, local, tz_name
from presence import hub
from routers.chats import names
from routers.tasks import next_open, viewer
from system import AI_STATUS, APP_NAME, SESSION_COOKIE, FormError, _session_key, attachment, client_ip, flash, ip_blocked, own_origin, render, send_email
import audit
from tables import MEET_HOSTS, MEET_READERS, MESSAGE_RETENTION_DAYS, POSITIONS, TOP, Company, Meeting, MeetingInvite, MeetingLine, Membership, User, get_db, utcnow
router = APIRouter(tags=["meetings"])
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
async def _meeting(db: AsyncSession, uid: int, meeting_id: int):
    try: return await access(db, uid, meeting_id)
    except LookupError as e: raise HTTPException(404, str(e)) from None
def _span(c: Company, m: Meeting) -> str: return f"{local(c, m.starts_at):%a %d %b %Y, %H:%M}–{local(c, m.ends_at):%H:%M} ({tz_name(c)})"
@router.post("/companies/{company_id}/meetings")
async def schedule(request: Request, company_id: uuid.UUID, form: MeetingForm = Depends(MeetingForm.as_form), db: AsyncSession = Depends(get_db)):
    """Schedule a meeting (or start it now): the slot must be in working hours on an open day, free of other meetings, and not on a
    national holiday of anyone invited. Invitees are notified + emailed (their own clock in the email)."""
    uid = _uid(request)
    me = await db.get(Membership, (uid, company_id), options=[joinedload(Membership.user, innerjoin=True),
                                                             joinedload(Membership.company, innerjoin=True).selectinload(Company.members).joinedload(Membership.user, innerjoin=True)])
    if not me: raise HTTPException(404, "You're not in that company.")
    c = me.company
    if me.position not in MEET_HOSTS: raise FormError(f"Only {c.name}'s owner, co-owners, managers and HR can schedule meetings.")
    if rate_limited(f"meet-new:{uid}", 20, 3600): raise FormError("That's a lot of meetings in a short time: try again later.")
    members = {m.user_id: m.user for m in c.members}
    picked = (set(members) if form.everyone else set(form.invite) & set(members)) - {uid}
    if not picked: raise FormError(f"Invite at least one other person from {c.name} (or tick everyone).")
    tz = company_tz(c)
    now = datetime.now(tz).replace(tzinfo=None, second=0, microsecond=0)
    start = now if form.now else form.starts.replace(tzinfo=None, second=0, microsecond=0)
    if not form.now and start < now: raise FormError(f"That start time has already passed ({tz_name(c)} time).")
    if start > now + timedelta(days=366): raise FormError("Schedule it within the next year.")
    if why := slot_problem(c, start, form.minutes, [members[u] for u in picked | {uid}]):
        raise FormError(f"Can't {'start it now' if form.now else 'schedule it'}: {why}." + (f" Next chance: {next_open(c)}." if form.now else " Pick another time."))
    s = start.replace(tzinfo=tz).astimezone(UTC).replace(tzinfo=None)
    e = s + timedelta(minutes=form.minutes)
    await db.execute(select(Company.id).where(Company.id == company_id).with_for_update())  # one booking at a time per company: two can't take the same slot
    if other := await clash(db, company_id, s, e):
        raise FormError(f"That overlaps “{other.title}” ({local(c, other.starts_at):%a %d %b, %H:%M}–{local(c, other.ends_at):%H:%M} {tz_name(c)}): a company holds one call at a time. Pick another time.")
    m = Meeting(company_id=company_id, title=form.title, agenda=form.agenda, starts_at=s, ends_at=e, created_by_id=uid, created_by_name=me.user.username, reminded_at=utcnow() if form.now else None)
    db.add(m)
    await db.flush()
    db.add_all(MeetingInvite(meeting_id=m.id, user_id=u) for u in picked | {uid})
    people, span, by, mid, cname, title = [members[u] for u in picked], _span(c, m), me.user.username, m.id, c.name, m.title
    await notify(db, people, c, "info", f"{'Meeting now' if form.now else 'Meeting'}: {title} ({cname})", f"{by} invited you to “{title}” at {cname}, {span}."
                 + (f" Agenda: {form.agenda[:500]}" if form.agenda else "") + (" It's starting now: join it from Tasks." if form.now else " Join it from Tasks when it starts."), f"/meetings/{mid}", f"meet:{mid}")
    mails = [(u.email, u.username, u.timezone) for u in people]
    await db.commit()
    for to, who, zone in mails:
        mine = f"\nOn your clock: {theirs(s, zone):%a %d %b, %H:%M}–{theirs(e, zone):%H:%M %Z}." if zone and zone != tz_name(c) and theirs(s, zone) else ""
        try: await send_email(to, f"{'Meeting now' if form.now else 'Meeting'}: {title} ({cname})", f"Hi {who},\n\n{by} invited you to a video meeting at {cname}: “{title}”, {span}.{mine}\n"
                              + (f"\nAgenda:\n{form.agenda}\n" if form.agenda else "") + f"\nJoin it under Tasks in {APP_NAME} (the room opens 10 minutes before the start). "
                              "It may be recorded, captioned and summarised by the local AI; the people invited and the company's owner, co-owners, managers and HR can open its record afterwards.\n")
        except Exception as ex: print(f"meeting email failed: {type(ex).__name__}")
    if form.now: return RedirectResponse(f"/meetings/{mid}/room", 303)
    flash(request, f"“{title}” scheduled for {span}: {len(people)} {'person' if len(people) == 1 else 'people'} invited, notified and emailed.", "success")
    return RedirectResponse(f"/companies/{company_id}/tasks#meetings", 303)
def theirs(utc_naive: datetime, zone: str) -> datetime | None:  # a stored time on someone's own clock (their region's time zone), None if it's not a valid one
    try: return utc_naive.replace(tzinfo=UTC).astimezone(ZoneInfo(zone))
    except (ZoneInfoNotFoundError, ValueError): return None
@router.get("/meetings/{meeting_id}")
async def meeting_page(request: Request, meeting_id: int, db: AsyncSession = Depends(get_db)):
    """The meeting: when, who, join (when the room is open), and afterwards its record: recordings, notes, captions + chat."""
    uid = _uid(request)
    m, c, mine, inv = await _meeting(db, uid, meeting_id)
    people = (await people_of(db, [m.id])).get(m.id, [])
    lines = (await db.scalars(select(MeetingLine).where(MeetingLine.meeting_id == m.id).order_by(MeetingLine.id).limit(3000))).all()
    known = await names(db, {x.speaker_id for x in lines} | {u for u, _, _ in people})
    room = rooms.rooms.get(m.id)
    return render(request, "meeting", {"m": m, "c": c, "me": mine, "invited": bool(inv), "people": people, "known": known, "lines": [(x, line_view(x, known)) for x in lines],
                                       "notes": notes_of(m), "parts": recordings(m.id), "shut": room_shut(m), "in_room": len(room.peers) if room else 0, "tz": tz_name(c),
                                       "can_manage": m.created_by_id == uid or mine.position in TOP, "readers": MEET_READERS, "positions": POSITIONS, "span": _span(c, m),
                                       "retention": MESSAGE_RETENTION_DAYS, "captions": WHISPER, "recording": bool(room and room.recorder)})
@router.get("/meetings/{meeting_id}/room")
async def room_page(request: Request, meeting_id: int, db: AsyncSession = Depends(get_db)):
    """The call itself: camera / mic check, then the room (meet.js)."""
    uid = _uid(request)
    m, c, mine, inv = await _meeting(db, uid, meeting_id)
    back = RedirectResponse(f"/meetings/{meeting_id}", 303)
    if not inv: flash(request, f"You can open this meeting's record (as {c.name}'s {POSITIONS[mine.position]['label']}), but only the people invited can join it.", "info"); return back
    if why := room_shut(m): flash(request, why, "info"); return back
    if other := await db.scalar(select(Meeting.title).where(Meeting.company_id == c.id, Meeting.status == "live", Meeting.id != m.id)):
        flash(request, f"“{other}” is live at {c.name} right now, and a company can only have one call at a time: join that one, or wait for it to end."); return back
    people = (await people_of(db, [m.id])).get(m.id, [])
    return render(request, "meeting_room", {"m": m, "c": c, "people": people, "host": mine.position in MEET_HOSTS or m.created_by_id == uid, "span": _span(c, m), "captions": WHISPER,
                                            "english": AI_STATUS, "readers": MEET_READERS, "positions": POSITIONS})
@router.get("/meetings/{meeting_id}/recording/{part}")
async def recording(request: Request, meeting_id: int, part: int, download: bool = False, db: AsyncSession = Depends(get_db)):
    """One recording of the meeting (WebM): only for the people who can open its record."""
    m, c, _, _ = await _meeting(db, _uid(request), meeting_id)
    if not 1 <= part <= MAX_PARTS or not (path := recording_file(meeting_id, part)).is_file(): raise HTTPException(404, "There's no such recording.")
    return FileResponse(path, media_type="video/webm", filename=f"{c.name} - {m.title} - recording {part}.webm", content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, no-store"})
@router.get("/meetings/{meeting_id}/transcript")
async def transcript(request: Request, meeting_id: int, db: AsyncSession = Depends(get_db)):
    """The meeting's notes, captions and chat as a text file, on your clock."""
    m, c, _, _ = await _meeting(db, _uid(request), meeting_id)
    lines = (await db.scalars(select(MeetingLine).where(MeetingLine.meeting_id == m.id).order_by(MeetingLine.id).limit(20000))).all()
    known, zone = await names(db, {x.speaker_id for x in lines}), viewer(request)
    notes, out = notes_of(m), [f"{m.title} · {c.name}", _span(c, m), f"Scheduled by {m.created_by_name}" + (f"\nAgenda: {m.agenda}" if m.agenda else ""), ""]
    if notes:
        out += ["NOTES (written by the local AI from the captions and chat: check them)", notes["summary"], *(f"- {p}" for p in notes["points"])]
        out += ["Decisions:", *(f"- {d}" for d in notes["decisions"])] if notes["decisions"] else []
        out += ["Action items:", *(f"- {a['who']}: {a['what']}" for a in notes["actions"])] if notes["actions"] else []
        out.append("")
    out.append("CAPTIONS (Whisper, machine transcript) AND CHAT")
    for x in lines:
        v = line_view(x, known)
        who = v["name"] + (f" ({v['real']})" if v["real"] else "") + (" (deleted account)" if v["gone"] else "")
        said = v["text"] if x.kind == "chat" or x.lang == "en" else f"{v['en'] or '[not translated]'}\n      [{v['language']}] {v['text']}"
        out.append(f"[{x.at.replace(tzinfo=UTC).astimezone(zone):%H:%M:%S %Z}] {'chat · ' if x.kind == 'chat' else ''}{who}: {said}")
    name = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in f"{c.name} - {m.title}")[:80]
    return PlainTextResponse("\n".join(out) + "\n", headers={"Content-Disposition": attachment(f"{name}.txt"), "Cache-Control": "private, no-store"})
@router.post("/meetings/{meeting_id}/cancel")
async def cancel(request: Request, meeting_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    m, c, mine, _ = await _meeting(db, uid, meeting_id)
    if not (m.created_by_id == uid or mine.position in TOP): raise FormError("Only whoever scheduled it, or the owner or a co-owner, can cancel it.")
    if m.status != "scheduled" or m.id in rooms.rooms: raise FormError("Only a meeting nobody is in can be cancelled: end it from the call instead.")
    m.status = "cancelled"
    people = list((await db.scalars(select(User).join(MeetingInvite, MeetingInvite.user_id == User.id).where(MeetingInvite.meeting_id == m.id, User.id != uid))).all())
    span, title, cid = _span(c, m), m.title, c.id
    await notify(db, people, c, "warn", f"Meeting cancelled: {title} ({c.name})", f"{request.state.user['username']} cancelled “{title}” ({span}).", f"/companies/{cid}/tasks#meetings", f"meet-cancel:{m.id}")
    await db.commit()
    flash(request, f"“{title}” cancelled: everyone invited has been told.", "info")
    return RedirectResponse(f"/companies/{cid}/tasks#meetings", 303)
@router.post("/meetings/{meeting_id}/delete")
async def delete_meeting(request: Request, meeting_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """Delete a past meeting's whole record: recordings, notes, captions, chat."""
    uid = _uid(request)
    m, c, mine, _ = await _meeting(db, uid, meeting_id)
    if not (m.created_by_id == uid or mine.position in TOP): raise FormError("Only whoever scheduled it, or the owner or a co-owner, can delete its record.")
    if m.status not in ("ended", "cancelled") or m.id in rooms.rooms: raise FormError("Only a meeting that's over can be deleted.")
    title, cid = m.title, c.id
    await db.delete(m)
    await db.commit()
    remove_files([meeting_id])
    flash(request, f"“{title}” deleted, with its recordings, notes, captions and chat.", "info")
    return RedirectResponse(f"/companies/{cid}/tasks#meetings", 303)
# ---------- the call's socket ----------
MAX_TEXT, MAX_CLIP, MAX_PIECE = 64 * 1024, 1024 * 1024, 8 * 1024 * 1024  # a WebRTC offer is a few KB; a speech clip a few dozen; a recording piece a few hundred
@router.websocket("/ws/meet/{meeting_id}")
@public  # require_login still runs and loads the session; without one the socket is refused here (close code 4401)
async def meet_socket(ws: WebSocket, meeting_id: int):
    """Checked before it's accepted: a valid login session, the IP blocklist and the Origin header (only this site's own pages can open
    it); then meet.Rooms.enter checks the invitation. Text frames are JSON (WebRTC setup, state, chat, recording on/off, end), binary
    frames are a speech clip (1) or a piece of the recording (2). Too big or too many closes it."""
    user = getattr(ws.state, "user", None)
    if not user or ip_blocked(ws) or not own_origin(ws): return await ws.close(code=4401 if not user else 4403)
    if not 0 < meeting_id < 2**31: return await ws.close(code=4404)  # no meeting has a number like that (the database couldn't even look it up)
    await ws.accept()
    seated = await rooms.enter(ws, user, meeting_id)
    if isinstance(seated, str):
        audit.log(user, "socket:meet", f"refused from meeting {meeting_id}", seated[:120], client_ip(ws))
        await ws.send_json({"t": "refused", "detail": seated})
        return await ws.close(code=4404)
    room, peer = seated
    audit.log(user, "socket:meet", f"joined meeting {meeting_id} ({room.title} · {room.company})", ip=client_ip(ws))
    uid, texts, blobs = int(user["user_id"]), deque(), deque()
    await hub.join(ws, uid, _session_key(ws.cookies.get(SESSION_COOKIE, "")))  # in a call = using the app (attendance), and logging out closes it
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect": break
            now = time.monotonic()
            if (data := message.get("bytes")) is not None:
                while blobs and now - blobs[0] > 60: blobs.popleft()
                blobs.append(now)
                if len(blobs) > 120 or len(data) > (MAX_CLIP if data[:1] == b"\x01" else MAX_PIECE): return await ws.close(code=4429)
                if data[:1] == b"\x01": rooms.later(rooms.speech(room, peer, data[1:]))  # Whisper takes a moment: never hold up the rest of this socket
                else: await rooms.on_bytes(room, peer, data)  # recording pieces: in order
                continue
            while texts and now - texts[0] > 60: texts.popleft()
            texts.append(now)
            raw = message.get("text") or ""
            if not raw or len(raw) > MAX_TEXT or len(texts) > 1500: return await ws.close(code=4429)
            try: msg = json.loads(raw)
            except ValueError: continue
            if isinstance(msg, dict): await rooms.on_text(room, peer, msg)
    finally:
        await rooms.leave(room, peer)
        await hub.leave(ws, uid)
        audit.log(user, "socket:meet", f"left meeting {meeting_id}", ip=client_ip(ws))
