"""Chats inside a company: anyone in a company can talk to anyone in it, one to one or in a named group, the owner included. Who can read:
the people in the chat, and the company's owner, co-owners, managers and HR (tables.CHAT_READERS), who can open every chat in their
company, read-only unless they're in it. That's said on every chat page, on /chats and in the standard terms accepted before joining.
Personal chats (no company, readable only by the people in them, shown as normal chats; company chats are labelled "company chat · <company>"):
"personal" = one to one with a friend or anyone you share a company with; "pgroup" = a named personal group of friends (routers/friends.py),
where any member can add their own friends. Blocking someone stops personal messages between you. The chat drawer (base.html + app.js)
lists every chat, live.
Enquiries: anyone can contact a company before joining it ("enquiry": one chat per person and company, direct_key = who asked). The
company's owner, co-owners and HR are in it (ENQUIRY_STAFF; one who comes later is added when they open it) and it's a company chat like
any other (its readers can read it); who asked needn't be in the company, and sees only that one chat of it.
Messages are encrypted at rest (AES-256-GCM, bound to their chat). Leaving the company ends access to its chats; a deleted account's
messages stay readable for MESSAGE_RETENTION_DAYS (30), then are erased (tables.purge_expired_messages). New messages reach open tabs
over the presence WebSocket as a nudge only ({"t": "chat", chat, from, title}): the text is fetched here, access-checked.
Names: everyone is shown by their current username (read live from users, so a rename shows at once) with the name on their identity
proof beside it (✓ when verified); a rename or a new identity proof is also pushed to colleagues' open tabs ({"t": "name"}: tell_names)."""
import asyncio, hmac, json, re, shutil, time, uuid
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from assist import rate_limited, user_context
import changelog, spam
from form import ActionForm, ChatFileForm, ChatMembersForm, ChatMessageForm, DirectChatForm, ForwardForm, GroupChatForm, PersonalChatForm, PersonalGroupForm
from presence import hub
from routers.friends import are_friends, blocked_between, friend_ids
from system import APP_NAME, UPLOAD_DIR, FormError, chat_dir, chat_file, flash, render, seal, unflash_saved, unseal
from tables import CHAT_READERS, MESSAGE_RETENTION_DAYS, POSITIONS, Chat, ChatMember, ChatMessage, Company, IdentityProof, Membership, User, get_db, seen_by, utcnow
router = APIRouter(tags=["chats"])
SHOWN = 200  # messages on a chat page (the newest)
GROUPS = ("group", "pgroup")  # named chats of several people: a company's, a personal one
ENQUIRY_STAFF = ("owner", "co_owner", "hr")  # who answers a company's enquiries
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
# a message's sealed body: plain text, or (a file / forwarded) ENVELOPE + {"text", "file": {name, size, type}, "fwd": "who it came from"}
ENVELOPE = "\x1e"
def parts_of(m: ChatMessage) -> dict:
    raw = unseal(f"chat:{m.chat_id}", m.body_enc)
    if raw is None: return {"text": "[this message can't be decrypted]"}
    if not raw.startswith(ENVELOPE): return {"text": raw}
    try: return json.loads(raw[1:])
    except ValueError: return {"text": raw[1:]}
def text_of(m: ChatMessage) -> str: p = parts_of(m); return p.get("text") or (f"📎 {p['file']['name']}" if p.get("file") else "")
def pack(text: str, file: dict | None = None, fwd: str = "") -> str: return ENVELOPE + json.dumps({"text": text, "file": file, "fwd": fwd}, ensure_ascii=False) if file or fwd else text
# files sent in chats: on disk (system.chat_dir / chat_file: one folder per chat, HMAC names); nothing about them in Postgres but the sealed message
IMAGES = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))
def sniff(path: Path) -> str:  # by the bytes, never the name: only real PNG / JPEG / GIF / WebP pictures are ever shown inline
    with path.open("rb") as f: head = f.read(16)
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP": return "image/webp"
    return next((t for magic, t in IMAGES if head.startswith(magic)), "application/pdf" if head.startswith(b"%PDF") else "application/octet-stream")
def file_json(m: ChatMessage, f: dict | None) -> dict | None:
    return {"name": f["name"], "size": f["size"], "image": f["type"].startswith("image/"), "url": f"/chats/{m.chat_id}/files/{m.id}"} if f else None
async def access(db: AsyncSession, uid: int, chat_id: int) -> tuple[Chat, Membership | None, bool]:
    """(chat, your membership of its company, you're in the chat). The same 404 unless you're in the chat and still in its company, or
    you're its company's owner / co-owner / manager / HR (they can read every chat there). A personal chat (no company): only the two
    people in it, ever (membership None)."""
    chat = await db.get(Chat, chat_id)
    if chat and chat.company_id is None:
        if not await db.get(ChatMember, (chat_id, uid)): raise HTTPException(404, "There's no such chat.")
        return chat, None, True
    if chat and chat.kind == "enquiry" and chat.direct_key == str(uid):  # who asked: in it whether or not they're in the company
        if not await db.get(ChatMember, (chat_id, uid)): raise HTTPException(404, "There's no such chat.")
        return chat, await db.get(Membership, (uid, chat.company_id)), True
    mine = await db.get(Membership, (uid, chat.company_id)) if chat else None
    inside = bool(mine and await db.get(ChatMember, (chat_id, uid)))
    if not chat or not mine or not (inside or mine.position in CHAT_READERS): raise HTTPException(404, "There's no such chat.")
    if chat.kind == "enquiry" and not inside and mine.position in ENQUIRY_STAFF:  # owner / co-owner / HR since it started: they answer it too
        db.add(ChatMember(chat_id=chat_id, user_id=uid, last_read_at=utcnow())); await db.commit(); inside = True
    return chat, mine, inside
async def _people(db: AsyncSession, ids) -> dict[int, list[tuple[int, str]]]:  # chat id -> [(user id, username)] of who's in it
    out: dict[int, list] = {}
    for cid, u, name in (await db.execute(select(ChatMember.chat_id, User.id, User.username).join(User, User.id == ChatMember.user_id)
                                          .where(ChatMember.chat_id.in_(list(ids))).order_by(User.username))).all() if ids else []: out.setdefault(cid, []).append((u, name))
    return out
async def names(db: AsyncSession, uids) -> dict[int, dict]:
    """{user id: {"username" (current: a rename shows everywhere at once), "real" (the name on their identity proof), "verified"}}."""
    rows = (await db.execute(select(User.id, User.username, IdentityProof.name_on_id, IdentityProof.verified_at).outerjoin(IdentityProof, IdentityProof.user_id == User.id)
                             .where(User.id.in_([u for u in set(uids) if u])))).all() if uids else []
    return {u: {"username": n, "real": real or "", "verified": bool(v)} for u, n, real, v in rows}
def who(m: ChatMessage, known: dict[int, dict]) -> dict:  # a message's sender as shown: current username + real name, or the kept name of a deleted account
    if m.sender_id and (k := known.get(m.sender_id)): return {"user": m.sender_id, "username": k["username"], "real": k["real"], "verified": k["verified"], "gone": False}
    return {"user": 0, "username": m.sender_name, "real": "", "verified": False, "gone": True}
def name_of(chat: Chat, people: list[tuple[int, str]], uid: int, company: str | None = None) -> str:  # a group's title, the other person in a direct / personal chat
    if chat.kind in GROUPS: return chat.title
    if chat.kind == "enquiry" and chat.direct_key == str(uid): return company or chat.title  # an enquiry: who asked sees the company's name (its current one when given)
    if chat.kind == "enquiry": return next((n for u, n in people if str(u) == chat.direct_key), "deleted account")  # the company sees who asked
    return next((n for u, n in people if u != uid), "deleted account")
def other_of(chat: Chat, people: list[tuple[int, str]], uid: int) -> int | None:  # the one person a chat is with (shown with their real name), if it's with one
    if chat.kind == "enquiry": return next((u for u, _ in people if str(u) == chat.direct_key and u != uid), None)
    return next((u for u, _ in people if u != uid), None) if chat.kind not in GROUPS else None
def _zone(request: Request):
    try: return ZoneInfo(request.state.user.get("tz") or "UTC")  # on the viewer's clock, like the pages (payroll.at)
    except (ZoneInfoNotFoundError, ValueError): return ZoneInfo("UTC")
def message_json(m: ChatMessage, known: dict, uid: int, zone) -> dict:  # one message for the live views (chat page, the chat drawer): text (+ its file, where it came from)
    b, p = who(m, known), parts_of(m)
    return {"id": m.id, **b, "who": b["username"] + (" (deleted account)" if b["gone"] else ""), "mine": m.sender_id == uid, "text": p.get("text", ""),
            "file": file_json(m, p.get("file")), "fwd": p.get("fwd", ""), "at": f"{m.created_at.replace(tzinfo=UTC).astimezone(zone):%d %b, %H:%M %Z}"}
fetching = lambda request: request.headers.get("x-requested-with") == "fetch"  # the chat drawer (app.js): JSON answers instead of redirects
@router.get("/chats")
async def chats_page(request: Request, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    memberships = await user_context(db, uid)  # companies + their members (for "new chat")
    ids = [m.company_id for m in memberships]
    readable = [m.company_id for m in memberships if m.position in CHAT_READERS]
    mine = (await db.execute(select(Chat, ChatMember).join(ChatMember, (ChatMember.chat_id == Chat.id) & (ChatMember.user_id == uid))
                             .where(Chat.company_id.in_(ids)).order_by(Chat.last_message_at.desc()))).all() if ids else []
    others = (await db.scalars(select(Chat).where(Chat.company_id.in_(readable), ~select(ChatMember.chat_id).where(ChatMember.chat_id == Chat.id, ChatMember.user_id == uid).exists())
                               .order_by(Chat.last_message_at.desc()).limit(200))).all() if readable else []
    personal = (await db.scalars(select(Chat).join(ChatMember, (ChatMember.chat_id == Chat.id) & (ChatMember.user_id == uid)).where(Chat.company_id.is_(None)).order_by(Chat.last_message_at.desc()))).all()
    asked = (await db.scalars(select(Chat).join(ChatMember, (ChatMember.chat_id == Chat.id) & (ChatMember.user_id == uid))  # your enquiries to companies you're not in
                              .where(Chat.kind == "enquiry", Chat.direct_key == str(uid), Chat.company_id.not_in(ids or [uuid.UUID(int=0)])).order_by(Chat.last_message_at.desc()))).all()
    asked_to = dict((await db.execute(select(Company.id, Company.name).where(Company.id.in_({c.company_id for c in asked})))).all()) if asked else {}
    friends = await friend_ids(db, uid)
    all_ids = [c.id for c, _ in mine] + [c.id for c in others] + [c.id for c in personal] + [c.id for c in asked]
    people = await _people(db, all_ids)
    last = {m.chat_id: m for m in (await db.scalars(select(ChatMessage).distinct(ChatMessage.chat_id).where(ChatMessage.chat_id.in_(all_ids), seen_by(uid))
                                                     .order_by(ChatMessage.chat_id, ChatMessage.id.desc()))).all()} if all_ids else {}
    unread = dict((await db.execute(select(ChatMessage.chat_id, func.count()).join(ChatMember, (ChatMember.chat_id == ChatMessage.chat_id) & (ChatMember.user_id == uid))
                                    .where(ChatMessage.sender_id.is_distinct_from(uid), ChatMessage.created_at > func.coalesce(ChatMember.last_read_at, ChatMember.added_at))
                                    .group_by(ChatMessage.chat_id))).all()) if mine or personal or asked else {}
    known = await names(db, {u for p in people.values() for u, _ in p} | {x.user_id for m in memberships for x in m.company.members} | {m.sender_id for m in last.values()} | set(friends))
    other = lambda c: other_of(c, people.get(c.id, []), uid)  # the other person in a direct / personal chat, who asked in an enquiry
    row = lambda c, co=None: {"chat": c, "name": name_of(c, people.get(c.id, []), uid, co), "other": other(c), "people": people.get(c.id, []), "last": last.get(c.id),
                     "by": who(last[c.id], known) if c.id in last else None, "preview": text_of(last[c.id])[:90] if c.id in last else "", "unread": unread.get(c.id, 0)}
    companies = [{"m": m, "chats": [row(c) for c, _ in mine if c.company_id == m.company_id], "others": [row(c) for c in others if c.company_id == m.company_id],
                  "colleagues": sorted((x for x in m.company.members if x.user_id != uid), key=lambda x: x.user.username)} for m in memberships]
    contacts = {x.user_id: x.user.username for m in memberships for x in m.company.members if x.user_id != uid} | {f: known[f]["username"] for f in friends if f in known}
    return render(request, "chats", {"companies": companies, "personal": [row(c) for c in personal], "contacts": sorted(contacts.items(), key=lambda x: x[1].lower()),  # who you can message personally
                                     "asked": [row(c, asked_to.get(c.company_id)) for c in asked],
                                     "friends": sorted(((f, known[f]["username"]) for f in friends if f in known), key=lambda x: x[1].lower()), "positions": POSITIONS,
                                     "readers": CHAT_READERS, "retention": MESSAGE_RETENTION_DAYS, "known": known})
@router.get("/chats/{chat_id}")
async def chat_page(request: Request, chat_id: int, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    chat, mine, inside = await access(db, uid, chat_id)
    company = await db.get(Company, chat.company_id) if chat.company_id else None  # None: a personal chat
    people = (await _people(db, [chat_id])).get(chat_id, [])
    messages = list(reversed((await db.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id, seen_by(uid)).order_by(ChatMessage.id.desc()).limit(SHOWN))).all()))
    staff = (await db.execute(select(Membership.user_id, User.username, Membership.position).join(User, User.id == Membership.user_id).where(Membership.company_id == chat.company_id)
                              .order_by(User.username))).all() if company else []
    if inside:  # read up to now
        me = await db.get(ChatMember, (chat_id, uid)); me.last_read_at = utcnow(); await db.commit()
    can_add = (chat.kind == "group" and (chat.created_by_id == uid or mine.position in CHAT_READERS)) or (chat.kind == "pgroup" and inside)  # a personal group: any member adds their friends
    pals = await friend_ids(db, uid) if chat.kind == "pgroup" and inside else []
    known = await names(db, {u for u, _ in people} | {m.sender_id for m in messages} | {u for u, _, _ in staff} | set(pals))
    other = other_of(chat, people, uid)
    inside_ids = {x for x, _ in people}
    addable = ([(u, n) for u, n, p in staff if u not in inside_ids] if chat.kind == "group" else [(f, known[f]["username"]) for f in pals if f not in inside_ids and f in known]) if can_add else []
    parts = {m.id: parts_of(m) for m in messages}
    return render(request, "chat", {"chat": chat, "company": company, "name": name_of(chat, people, uid, company.name if company else None), "other": other, "people": people, "inside": inside, "me": mine,
                                    "msgs": [{"m": m, "text": parts[m.id].get("text", ""), "file": file_json(m, parts[m.id].get("file")), "fwd": parts[m.id].get("fwd", ""), "by": who(m, known)} for m in messages],
                                    "more": len(messages) == SHOWN, "known": known,
                                    "readers": [(u, n) for u, n, p in staff if p in CHAT_READERS],  # (not "messages": base.html's toasts use that name)
                                    "addable": addable, "positions": POSITIONS,
                                    "retention": MESSAGE_RETENTION_DAYS, "last_id": messages[-1].id if messages else 0, "uid": uid})
@router.get("/chats/{chat_id}/since/{after}")
async def chat_since(request: Request, chat_id: int, after: int, db: AsyncSession = Depends(get_db)):
    """Messages newer than `after` (JSON) for the open chat page, after a WebSocket nudge: same access check as the page."""
    uid = _uid(request)
    chat, mine, inside = await access(db, uid, chat_id)
    rows = (await db.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id, ChatMessage.id > after, seen_by(uid)).order_by(ChatMessage.id).limit(100))).all()
    known = await names(db, {m.sender_id for m in rows})
    if inside and rows: me = await db.get(ChatMember, (chat_id, uid)); me.last_read_at = utcnow(); await db.commit()
    zone = _zone(request)
    return {"messages": [message_json(m, known, uid, zone) for m in rows]}
# ---------- the chat drawer (base.html + app.js): every chat of yours, company + personal, live ----------
@router.get("/chats/drawer/list")
async def drawer_list(request: Request, db: AsyncSession = Depends(get_db)):
    """Your chats, newest first: company chats carry their company ("company chat · Acme"), personal ones don't. + the people you can
    message (anyone you share a company with), each with the companies you share."""
    uid = _uid(request)
    memberships = await user_context(db, uid)
    mine_ids = [m.company_id for m in memberships]
    rows = (await db.scalars(select(Chat).join(ChatMember, (ChatMember.chat_id == Chat.id) & (ChatMember.user_id == uid))
                             .where((Chat.company_id.is_(None)) | (Chat.company_id.in_(mine_ids or [uuid.UUID(int=0)])) | ((Chat.kind == "enquiry") & (Chat.direct_key == str(uid))))  # + your enquiries
                             .order_by(Chat.last_message_at.desc()).limit(80))).all()
    ids = [c.id for c in rows]
    people = await _people(db, ids)
    last = {m.chat_id: m for m in (await db.scalars(select(ChatMessage).distinct(ChatMessage.chat_id).where(ChatMessage.chat_id.in_(ids), seen_by(uid)).order_by(ChatMessage.chat_id, ChatMessage.id.desc()))).all()} if ids else {}
    unread = dict((await db.execute(select(ChatMessage.chat_id, func.count()).join(ChatMember, (ChatMember.chat_id == ChatMessage.chat_id) & (ChatMember.user_id == uid))
                                    .where(ChatMessage.chat_id.in_(ids), ChatMessage.sender_id.is_distinct_from(uid), ChatMessage.created_at > func.coalesce(ChatMember.last_read_at, ChatMember.added_at))
                                    .group_by(ChatMessage.chat_id))).all()) if ids else {}
    companies = {m.company_id: m.company.name for m in memberships}
    if outside := {c.company_id for c in rows if c.company_id and c.company_id not in companies}:  # companies you asked something but aren't in
        companies |= dict((await db.execute(select(Company.id, Company.name).where(Company.id.in_(outside)))).all())
    contacts: dict[int, dict] = {}
    for m in memberships:
        for x in m.company.members:
            if x.user_id != uid: contacts.setdefault(x.user_id, {"user": x.user_id, "companies": [], "friend": False})["companies"].append({"id": str(m.company_id), "name": m.company.name})
    for f in await friend_ids(db, uid): contacts.setdefault(f, {"user": f, "companies": [], "friend": True})["friend"] = True  # friends: message them wherever they work
    known = await names(db, {u for p in people.values() for u, _ in p} | set(contacts) | {m.sender_id for m in last.values()})
    zone, out = _zone(request), []
    today = datetime.now(zone).date()
    when = lambda t: f"{(t := t.replace(tzinfo=UTC).astimezone(zone)):%H:%M}" if t.date() == today else f"{t:%d %b}"  # today: the time, else the day
    for c in rows:
        other = other_of(c, people.get(c.id, []), uid)
        k = known.get(other) if other else None
        m = last.get(c.id)
        out.append({"id": c.id, "kind": c.kind, "company": companies.get(c.company_id) if c.company_id else None, "title": (k or {}).get("username") or name_of(c, people.get(c.id, []), uid, companies.get(c.company_id)),
                    "other": other or 0, "real": (k or {}).get("real", ""), "verified": bool(k and k.get("verified")), "members": len(people.get(c.id, [])), "unread": unread.get(c.id, 0),
                    "last": {"who": "you" if m.sender_id == uid else who(m, known)["username"], "text": text_of(m)[:90], "at": when(m.created_at)} if m else None})
    return {"chats": out, "unread": sum(unread.values()),
            "contacts": sorted(({**v, "name": known.get(u, {}).get("username", "?"), "real": known.get(u, {}).get("real", ""), "verified": bool(known.get(u, {}).get("verified"))} for u, v in contacts.items()), key=lambda x: x["name"].lower())}
@router.get("/chats/{chat_id}/recent")
async def recent(request: Request, chat_id: int, db: AsyncSession = Depends(get_db)):
    """The drawer opens a chat: its newest 60 messages (oldest first), who can read it, and it's read up to now."""
    uid = _uid(request)
    chat, mine, inside = await access(db, uid, chat_id)
    rows = list(reversed((await db.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id, seen_by(uid)).order_by(ChatMessage.id.desc()).limit(60))).all()))
    people = (await _people(db, [chat_id])).get(chat_id, [])
    known = await names(db, {m.sender_id for m in rows} | {u for u, _ in people})
    company = await db.get(Company, chat.company_id) if chat.company_id else None
    if inside: me = await db.get(ChatMember, (chat_id, uid)); me.last_read_at = utcnow(); await db.commit()
    other = other_of(chat, people, uid)
    zone = _zone(request)
    readers = (f"the people in it and {company.name}'s owner, co-owners, managers and HR" if company else
               f"only the {len(people)} people in this personal group: no company can read it" if chat.kind == "pgroup" else "only the two of you: a personal chat no company can read")
    return {"chat": {"id": chat.id, "kind": chat.kind, "title": known.get(other, {}).get("username") if other else name_of(chat, people, uid, company.name if company else None), "other": other or 0,
                     "real": known.get(other, {}).get("real", "") if other else "", "company": company.name if company else None, "inside": inside, "people": [known.get(u, {}).get("username", n) for u, n in people],
                     "readers": readers},
            "messages": [message_json(m, known, uid, zone) for m in rows], "last": rows[-1].id if rows else 0}
async def unread_by_company(db: AsyncSession, uid: int) -> dict[uuid.UUID, int]:  # unread chat messages per company (workplaces cards, company dashboards)
    return dict((await db.execute(select(Chat.company_id, func.count(ChatMessage.id)).join(ChatMessage, ChatMessage.chat_id == Chat.id)
                                  .join(ChatMember, (ChatMember.chat_id == Chat.id) & (ChatMember.user_id == uid))
                                  .where(ChatMessage.sender_id.is_distinct_from(uid), ChatMessage.created_at > func.coalesce(ChatMember.last_read_at, ChatMember.added_at))
                                  .group_by(Chat.company_id))).all())
async def tell_names(db: AsyncSession, user_id: int) -> None:
    """Someone's username or identity name changed: every colleague's open tabs update it at once (chat pages, chat lists)."""
    colleagues = (await db.scalars(select(Membership.user_id).where(Membership.company_id.in_(select(Membership.company_id).where(Membership.user_id == user_id))).distinct())).all()
    k = (await names(db, [user_id])).get(user_id)
    if k: await hub.tell(colleagues, {"t": "name", "user": user_id, **k})
@router.post("/chats/{chat_id}/messages")
async def send(request: Request, chat_id: int, form: ChatMessageForm = Depends(ChatMessageForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    chat, mine, inside = await access(db, uid, chat_id)
    if not inside: raise FormError("You can read this chat (as the company's " + POSITIONS[mine.position]["label"] + "), but only the people in it can write.")
    people = (await _people(db, [chat_id])).get(chat_id, [])
    if chat.kind == "personal" and (other := next((u for u, _ in people if u != uid), None)) and await blocked_between(db, uid, other):
        raise FormError("You can't send personal messages to this person any more (one of you blocked the other).")
    spam.check_chat(uid, chat_id, form.body)
    now, me = utcnow(), request.state.user["username"]
    msg = ChatMessage(chat_id=chat_id, sender_id=uid, sender_name=me, body_enc=seal(f"chat:{chat_id}", form.body), created_at=now)
    db.add(msg)
    chat.last_message_at = now
    (await db.get(ChatMember, (chat_id, uid))).last_read_at = now
    title = name_of(chat, people, uid) if chat.kind in GROUPS else me
    company = (await db.get(Company, chat.company_id)).name if chat.company_id else None
    await db.commit()
    await hub.tell([u for u, _ in people if u != uid], {"t": "chat", "chat": chat_id, "from": me, "title": title, "company": company, "kind": chat.kind})  # a nudge: the text is fetched, access-checked
    if fetching(request): return {"ok": True, "id": msg.id}
    return RedirectResponse(f"/chats/{chat_id}#end", 303)
async def _writable(db: AsyncSession, request: Request, chat_id: int) -> tuple[Chat, list[tuple[int, str]]]:  # you're in it, and nobody blocked anybody (personal); then spam.check_chat
    uid = _uid(request)
    chat, mine, inside = await access(db, uid, chat_id)
    if not inside: raise FormError("Only the people in this chat can write in it.")
    people = (await _people(db, [chat_id])).get(chat_id, [])
    if chat.kind == "personal" and (other := next((u for u, _ in people if u != uid), None)) and await blocked_between(db, uid, other):
        raise FormError("You can't send personal messages to this person any more (one of you blocked the other).")
    return chat, people
async def _post(db: AsyncSession, request: Request, chat: Chat, people, body: str, file_from: Path | None = None) -> int:  # a new message (+ its file moved / copied in), then the nudge
    uid, me, now = _uid(request), request.state.user["username"], utcnow()
    msg = ChatMessage(chat_id=chat.id, sender_id=uid, sender_name=me, body_enc=seal(f"chat:{chat.id}", body), created_at=now)
    db.add(msg); await db.flush()
    if file_from:
        chat_dir(chat.id).mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread((shutil.move if file_from.parent == UPLOAD_DIR else shutil.copyfile), file_from, chat_file(chat.id, msg.id))
    chat.last_message_at = now
    (await db.get(ChatMember, (chat.id, uid))).last_read_at = now
    company = (await db.get(Company, chat.company_id)).name if chat.company_id else None
    mid = msg.id
    await db.commit()
    await hub.tell([u for u, _ in people if u != uid], {"t": "chat", "chat": chat.id, "from": me, "title": name_of(chat, people, uid) if chat.kind in GROUPS else me, "company": company, "kind": chat.kind})
    return mid
@router.post("/chats/{chat_id}/files")
async def send_file(request: Request, chat_id: int, form: ChatFileForm = Depends(ChatFileForm.as_form), db: AsyncSession = Depends(get_db)):
    """A file in a chat (virus-scanned by @as_form, any type, up to the upload limit), with an optional line of text. Pictures show in the
    chat; anything else is a download."""
    unflash_saved(request, form.file)
    saved = UPLOAD_DIR / form.file
    try: chat, people = await _writable(db, request, chat_id); spam.check_chat(_uid(request), chat_id, form.body.strip(), "file")
    except FormError: saved.unlink(missing_ok=True); raise
    meta = {"name": form.file.split("_", 1)[-1][:120] or "file", "size": saved.stat().st_size, "type": await asyncio.to_thread(sniff, saved)}
    mid = await _post(db, request, chat, people, pack(form.body.strip(), meta), saved)
    return {"ok": True, "id": mid} if fetching(request) else RedirectResponse(f"/chats/{chat_id}#end", 303)
@router.get("/chats/{chat_id}/files/{message_id}")
async def get_file(request: Request, chat_id: int, message_id: int, db: AsyncSession = Depends(get_db)):
    await access(db, _uid(request), chat_id)  # whoever can read the chat
    m = await db.scalar(select(ChatMessage).where(ChatMessage.id == message_id, ChatMessage.chat_id == chat_id, seen_by(_uid(request))))
    f = parts_of(m).get("file") if m else None
    if not f or not (path := chat_file(chat_id, message_id)).is_file(): raise HTTPException(404, "That file isn't there any more.")
    image = f["type"].startswith("image/")  # pictures (by their bytes) inline; everything else only ever downloads, as bytes
    return FileResponse(path, media_type=f["type"] if image else "application/octet-stream", filename=f["name"], content_disposition_type="inline" if image else "attachment",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
@router.post("/chats/{chat_id}/forward")
async def forward(request: Request, chat_id: int, form: ForwardForm = Depends(ForwardForm.as_form), db: AsyncSession = Depends(get_db)):
    """Forward a message you can read (a chat's, with its file) or one of the Deminic AI's answers to you, into a chat you're in."""
    chat, people = await _writable(db, request, chat_id)
    uid = _uid(request)
    src_file = None
    if form.message_id:
        src = await db.scalar(select(ChatMessage).where(ChatMessage.id == form.message_id, seen_by(uid)))
        if not src: raise HTTPException(404, "That message isn't there any more.")
        await access(db, uid, src.chat_id)  # the same 404 if you can't read it
        p = parts_of(src)
        text, f, fwd = p.get("text", ""), p.get("file"), p.get("fwd") or who(src, await names(db, [src.sender_id]))["username"]
        if f and (path := chat_file(src.chat_id, src.id)).is_file(): src_file = path
        elif f: f = None
    elif form.ai_id:
        import deminic  # (deminic -> meet -> this module: imported when used)
        from tables import AIMessage
        a = await db.get(AIMessage, form.ai_id)
        if not a or a.user_id != uid or a.role != "ai": raise HTTPException(404, "That answer isn't there any more.")
        text, f, fwd = deminic.body(a).get("text", ""), None, f"{APP_NAME} AI"
    else: raise FormError("Pick a message to forward.")
    spam.check_chat(uid, chat_id, text, "forward")  # the same message into chat after chat: spam
    mid = await _post(db, request, chat, people, pack(text, f, fwd), src_file)
    return {"ok": True, "id": mid} if fetching(request) else RedirectResponse(f"/chats/{chat_id}#end", 303)
_previews: dict[str, tuple[float, dict]] = {}  # url -> (when, preview): a day, in this process
@router.get("/chats/preview/link")
async def link_preview(request: Request, url: str):
    """A link's title + description for its card in a chat (YouTube links are shown by the page itself). Fetched like competitor pages
    (shopai.fetch_page): public web addresses only, 2 MB, 12 s; nothing but text comes back."""
    if not re.fullmatch(r"https?://[^\s<>\"]{4,500}", url): raise HTTPException(400, "Not a web link.")
    if (hit := _previews.get(url)) and time.time() - hit[0] < 86400: return hit[1]
    if rate_limited(f"preview:{_uid(request)}", 30, 60): raise HTTPException(429, "Too many link previews: wait a minute.")
    from shopai import FetchError, fetch_page
    try: final, html = await asyncio.to_thread(fetch_page, url, 600_000)
    except FetchError as e: raise HTTPException(422, str(e)) from None
    except Exception: raise HTTPException(422, "That page couldn't be read.") from None
    meta = lambda prop: (m := re.search(rf'<meta[^>]+(?:property|name)=["\'](?:og:)?{prop}["\'][^>]*content=["\']([^"\']{{1,400}})', html, re.I)) and unescape(m.group(1)).strip()
    title = meta("title") or ((m := re.search(r"<title[^>]*>([^<]{1,300})", html, re.I)) and unescape(m.group(1)).strip()) or ""
    out = {"url": final, "site": meta("site_name") or re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*$", r"\1", final)), "title": " ".join(title.split())[:200],
           "description": " ".join((meta("description") or "").split())[:300]}
    if len(_previews) > 2000: _previews.clear()
    _previews[url] = (time.time(), out)
    return out
async def _colleagues(db: AsyncSession, company_id: uuid.UUID, uids) -> set[int]:  # which of these are in the company
    return set((await db.scalars(select(Membership.user_id).where(Membership.company_id == company_id, Membership.user_id.in_(list(uids))))).all()) if uids else set()
@router.post("/chats/direct")
async def direct(request: Request, form: DirectChatForm = Depends(DirectChatForm.as_form), db: AsyncSession = Depends(get_db)):
    """Open the one-to-one chat with someone in the same company (started the first time)."""
    uid = _uid(request)
    if form.user_id == uid: raise FormError("That's you: pick someone else.")
    if len(await _colleagues(db, form.company_id, [uid, form.user_id])) < 2: raise FormError("You can only chat with people in the same company.")
    key = f"{min(uid, form.user_id)}:{max(uid, form.user_id)}"
    find = select(Chat.id).where(Chat.company_id == form.company_id, Chat.kind == "direct", Chat.direct_key == key)
    if not (cid := await db.scalar(find)):
        spam.check_new_chat(uid)
        chat = Chat(company_id=form.company_id, kind="direct", direct_key=key, created_by_id=uid)
        db.add(chat)
        try:
            await db.flush()
            db.add_all([ChatMember(chat_id=chat.id, user_id=uid), ChatMember(chat_id=chat.id, user_id=form.user_id)])
            cid = chat.id
            await db.commit()
        except IntegrityError:  # started from both sides at once
            await db.rollback()
            cid = await db.scalar(find)
    return {"id": cid} if fetching(request) else RedirectResponse(f"/chats/{cid}#end", 303)
@router.post("/chats/personal")
async def personal(request: Request, form: PersonalChatForm = Depends(PersonalChatForm.as_form), db: AsyncSession = Depends(get_db)):
    """A personal one-to-one chat (no company: shown as a normal message, readable only by the two of you). With a friend or anyone you
    share a company with, unless one of you blocked the other; once it exists it stays yours, wherever you both work."""
    uid = _uid(request)
    if form.user_id == uid: raise FormError("That's you: pick someone else.")
    if await blocked_between(db, uid, form.user_id): raise FormError("You can't message this person privately (one of you blocked the other).")
    key = f"{min(uid, form.user_id)}:{max(uid, form.user_id)}"
    find = select(Chat.id).where(Chat.kind == "personal", Chat.direct_key == key)
    if not (cid := await db.scalar(find)):
        shared = await db.scalar(select(func.count()).select_from(Membership).where(Membership.user_id == form.user_id, Membership.company_id.in_(select(Membership.company_id).where(Membership.user_id == uid))))
        if not shared and not await are_friends(db, uid, form.user_id): raise FormError("You can message your friends and the people you work with: send them a friend request first.")
        spam.check_new_chat(uid)
        chat = Chat(company_id=None, kind="personal", direct_key=key, created_by_id=uid)
        db.add(chat)
        try:
            await db.flush()
            db.add_all([ChatMember(chat_id=chat.id, user_id=uid), ChatMember(chat_id=chat.id, user_id=form.user_id)])
            cid = chat.id
            await db.commit()
        except IntegrityError: await db.rollback(); cid = await db.scalar(find)
    return {"id": cid} if fetching(request) else RedirectResponse(f"/chats/{cid}#end", 303)
@router.post("/companies/{company_id}/enquire")
async def enquire(request: Request, company_id: uuid.UUID, form: ChatMessageForm = Depends(ChatMessageForm.as_form), db: AsyncSession = Depends(get_db)):
    """Contact a company before joining it (its join page, its search card): your question opens your enquiry chat with it (one per person
    and company, started the first time), where its owner, co-owners and HR answer. They're told on the bell and live."""
    uid, me = _uid(request), request.state.user["username"]
    if not (company := await db.get(Company, company_id)): raise HTTPException(404, "No company has that code.")
    if await db.get(Membership, (uid, company_id)): raise FormError(f"You're in {company.name}: talk to its people in its chats.")
    find = select(Chat).where(Chat.company_id == company_id, Chat.kind == "enquiry", Chat.direct_key == str(uid))
    new = not (chat := await db.scalar(find))
    spam.check_chat(uid, chat.id if chat else f"enquiry:{company_id}", form.body)
    if new:
        if rate_limited(f"enquire:{uid}", 10, 86400): raise FormError("That's a lot of companies to contact in a day: try again tomorrow.")
        spam.check_new_chat(uid)
        staff = (await db.scalars(select(User).join(Membership, Membership.user_id == User.id).where(Membership.company_id == company_id, Membership.position.in_(ENQUIRY_STAFF)))).all()
        chat = Chat(company_id=company_id, kind="enquiry", direct_key=str(uid), title=company.name[:60], created_by_id=uid)
        db.add(chat)
        try:
            await db.flush()
            db.add_all(ChatMember(chat_id=chat.id, user_id=u) for u in {uid, *(s.id for s in staff)})
            await db.commit()
        except IntegrityError: await db.rollback(); chat, new = await db.scalar(find), False  # sent twice at once: the other one started it
    elif not await db.get(ChatMember, (chat.id, uid)): db.add(ChatMember(chat_id=chat.id, user_id=uid)); await db.commit()
    people = (await _people(db, [chat.id])).get(chat.id, [])
    await _post(db, request, chat, people, form.body)
    if new:
        from guard import notify  # (guard -> payroll -> …: imported when used)
        await notify(db, list(staff), company, "info", f"New enquiry from {me}", f"{me} contacted {company.name} before joining. Answer in the enquiry chat: the owner, co-owners and HR are in it.",
                     f"/chats/{chat.id}#end", f"enquiry:{chat.id}")
        await db.commit()
        changelog.company(company_id, "enquiry received", f"from {me} (an enquiry chat with the owner, co-owners and HR)")
        changelog.add("u", uid, f"sent an enquiry to {company.name}")
    if fetching(request): return {"id": chat.id}
    if new: flash(request, f"Sent to {company.name}: its owner, co-owners and HR answer here (it's a company chat: its managers can read it too).", "success")
    return RedirectResponse(f"/chats/{chat.id}#end", 303)
@router.post("/chats/group")
async def group(request: Request, form: GroupChatForm = Depends(GroupChatForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    if not await db.get(Membership, (uid, form.company_id)): raise FormError("You can only start a group in a company you're in.")
    picked = await _colleagues(db, form.company_id, set(form.members) - {uid})
    if not picked: raise FormError("Pick at least one person from the company for the group.")
    spam.check_new_chat(uid)
    chat = Chat(company_id=form.company_id, kind="group", title=form.title, created_by_id=uid)
    db.add(chat)
    await db.flush()
    db.add_all(ChatMember(chat_id=chat.id, user_id=u) for u in {uid, *picked})
    cid = chat.id
    await db.commit()
    flash(request, f"Group “{form.title}” started with {len(picked)} other{'s' if len(picked) != 1 else ''}.", "success")
    return RedirectResponse(f"/chats/{cid}#end", 303)
@router.post("/chats/pgroup")
async def personal_group(request: Request, form: PersonalGroupForm = Depends(PersonalGroupForm.as_form), db: AsyncSession = Depends(get_db)):
    """A personal group chat (no company: only the people in it can read it), started with friends of yours."""
    uid = _uid(request)
    picked = set(form.members) & set(await friend_ids(db, uid))
    if not picked: raise FormError("Pick at least one friend for the group (friends page: send requests first).")
    spam.check_new_chat(uid)
    chat = Chat(company_id=None, kind="pgroup", title=form.title, created_by_id=uid)
    db.add(chat)
    await db.flush()
    db.add_all(ChatMember(chat_id=chat.id, user_id=u) for u in {uid, *picked})
    cid, names_in = chat.id, (await db.scalars(select(User.username).where(User.id.in_(picked)).order_by(User.username))).all()
    await db.commit()
    changelog.add("u", uid, f"started the personal group “{form.title}”", f"with {', '.join(names_in)}")
    await hub.tell(list(picked), {"t": "chat", "chat": cid, "from": request.state.user["username"], "title": form.title, "company": None})
    if fetching(request): return {"id": cid}
    flash(request, f"Personal group “{form.title}” started with {len(picked)} friend{'s' if len(picked) != 1 else ''}: only the people in it can read it.", "success")
    return RedirectResponse(f"/chats/{cid}#end", 303)
@router.post("/chats/{chat_id}/members")
async def add_members(request: Request, chat_id: int, form: ChatMembersForm = Depends(ChatMembersForm.as_form), db: AsyncSession = Depends(get_db)):
    """Add people to a group: a company's (whoever started it, or the company's owner / co-owners / managers / HR: people from the company), a
    personal one (any member: friends of theirs)."""
    uid = _uid(request)
    chat, mine, inside = await access(db, uid, chat_id)
    already = {u for u, _ in (await _people(db, [chat_id])).get(chat_id, [])}
    if chat.kind == "pgroup":
        if not inside: raise FormError("Only the people in this group can add to it.")
        new = (set(form.members) - already) & set(await friend_ids(db, uid))
        if not new: raise FormError("Pick friends of yours who aren't in the group yet.")
    else:
        if chat.kind != "group" or not (chat.created_by_id == uid or mine.position in CHAT_READERS): raise FormError("Only whoever started this group (or the company's owner, a co-owner, a manager or HR) can add people.")
        new = await _colleagues(db, chat.company_id, set(form.members) - already)
        if not new: raise FormError("Pick people from the company who aren't in the group yet.")
    db.add_all(ChatMember(chat_id=chat_id, user_id=u) for u in new)
    await db.commit()
    flash(request, f"Added {len(new)} {'person' if len(new) == 1 else 'people'} to the group: they see its whole history.", "success")
    return RedirectResponse(f"/chats/{chat_id}#end", 303)
@router.post("/chats/{chat_id}/leave")
async def leave(request: Request, chat_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    chat, mine, inside = await access(db, uid, chat_id)
    if chat.kind not in GROUPS or not inside: raise FormError("You can only leave a group you're in.")
    await db.execute(delete(ChatMember).where(ChatMember.chat_id == chat_id, ChatMember.user_id == uid))
    await db.commit()
    flash(request, f"You left “{chat.title}”.", "info")
    return RedirectResponse("/chats", 303)
