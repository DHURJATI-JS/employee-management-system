"""Friends (user asked): ask anyone on the app by their username; they accept or decline. Friends chat privately, one to one (a personal
chat) or in personal groups (routers/chats.py): no company can read those. Company chats are never touched by any of this. Blocking someone
ends the friendship and stops their friend requests and personal messages. A request rings the other person's bell and reaches their open
tabs live ({"t": "friend"}); requests, friendships and their endings go into both people's change logs (changelog.py)."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
import changelog
from assist import rate_limited
from form import ActionForm, FriendRequestForm
from presence import hub
from system import FormError, flash, render
from tables import Chat, ChatMember, FriendBlock, Friendship, IdentityProof, Notice, User, get_db, utcnow
router = APIRouter(tags=["friends"])
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
def pair(a: int, b: int) -> tuple[int, int]: return (a, b) if a < b else (b, a)
async def are_friends(db: AsyncSession, a: int, b: int) -> bool:
    f = await db.get(Friendship, pair(a, b))
    return bool(f and f.status == "friends")
async def blocked_between(db: AsyncSession, a: int, b: int) -> bool:  # either one blocked the other
    return bool(await db.scalar(select(func.count()).select_from(FriendBlock).where(or_((FriendBlock.user_id == a) & (FriendBlock.blocked_id == b), (FriendBlock.user_id == b) & (FriendBlock.blocked_id == a)))))
async def friend_ids(db: AsyncSession, uid: int) -> list[int]:
    rows = (await db.execute(select(Friendship.low_id, Friendship.high_id).where(Friendship.status == "friends", or_(Friendship.low_id == uid, Friendship.high_id == uid)))).all()
    return [b if a == uid else a for a, b in rows]
async def _ring(db: AsyncSession, to: int, title: str, body: str, key: str) -> None:  # the bell (a notice without a company) + their open tabs
    await db.execute(insert(Notice).values(user_id=to, company_id=None, level="info", title=title[:160], body=body[:1000], link="/friends", key=key[:120], created_at=utcnow())
                     .on_conflict_do_nothing(constraint="notices_once"))
@router.get("/friends")
async def friends_page(request: Request, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    rows = (await db.scalars(select(Friendship).where(or_(Friendship.low_id == uid, Friendship.high_id == uid)).order_by(Friendship.since.desc().nulls_last(), Friendship.created_at.desc()))).all()
    blocks = (await db.scalars(select(FriendBlock).where(FriendBlock.user_id == uid).order_by(FriendBlock.created_at.desc()))).all()
    other = lambda f: f.high_id if f.low_id == uid else f.low_id
    ids = {other(f) for f in rows} | {b.blocked_id for b in blocks}
    people = {u: {"username": n, "real": r or "", "verified": bool(v)} for u, n, r, v in (await db.execute(
        select(User.id, User.username, IdentityProof.name_on_id, IdentityProof.verified_at).outerjoin(IdentityProof, IdentityProof.user_id == User.id).where(User.id.in_(ids)))).all()} if ids else {}
    chats = dict((await db.execute(select(Chat.direct_key, Chat.id).where(Chat.kind == "personal", Chat.direct_key.in_([f"{f.low_id}:{f.high_id}" for f in rows])))).all()) if rows else {}
    view = lambda f: {"id": other(f), **people.get(other(f), {"username": "?", "real": "", "verified": False}), "since": f.since, "at": f.created_at, "chat": chats.get(f"{f.low_id}:{f.high_id}")}
    return render(request, "friends", {"friends": [view(f) for f in rows if f.status == "friends"], "incoming": [view(f) for f in rows if f.status == "pending" and f.requested_by != uid],
                                       "outgoing": [view(f) for f in rows if f.status == "pending" and f.requested_by == uid],
                                       "blocked": [{"id": b.blocked_id, **people.get(b.blocked_id, {"username": "?", "real": "", "verified": False})} for b in blocks]})
@router.post("/friends/request")
async def send_request(request: Request, form: FriendRequestForm = Depends(FriendRequestForm.as_form), db: AsyncSession = Depends(get_db)):
    uid, me = _uid(request), request.state.user["username"]
    if rate_limited(f"friend-request:{uid}", 30, 3600): raise FormError("That's a lot of friend requests in an hour: try again later.")
    them = await db.scalar(select(User).where(func.lower(User.username) == form.username.lower()))
    if not them: raise FormError(f"There's no account called {form.username}.")
    if them.id == uid: raise FormError("That's you: you can't be your own friend here.")
    if await blocked_between(db, uid, them.id): raise FormError(f"You can't send {them.username} a friend request.")  # either side blocked: the same answer
    f = await db.get(Friendship, pair(uid, them.id), with_for_update=True)
    if f and f.status == "friends": raise FormError(f"You and {them.username} are already friends.")
    if f and f.requested_by == uid: raise FormError(f"You've already asked {them.username}: waiting for them to accept.")
    name = them.username
    if f:  # they asked you first: asking back = accepting
        f.status, f.since = "friends", utcnow()
        await _ring(db, them.id, f"{me} accepted your friend request", f"You and {me} are friends now: chat privately from the chat button or your friends page.", f"friend-yes:{f.low_id}:{f.high_id}:{utcnow():%Y%m%d%H%M%S}")
        await db.commit()
        await hub.tell([them.id], {"t": "friend", "kind": "accepted", "from": me})
        changelog.add("u", uid, f"now friends with {name}"); changelog.add("u", them.id, f"now friends with {me}")
        flash(request, f"{name} had asked you too: you're friends now.", "success")
        return RedirectResponse("/friends", 303)
    low, high = pair(uid, them.id)
    db.add(Friendship(low_id=low, high_id=high, requested_by=uid, status="pending", created_at=utcnow()))
    await _ring(db, them.id, f"{me} sent you a friend request", f"{me} would like to be friends. Accept to chat privately (one to one or in groups), or decline.", f"friend-ask:{low}:{high}:{utcnow():%Y%m%d%H%M%S}")
    await db.commit()
    await hub.tell([them.id], {"t": "friend", "kind": "request", "from": me})
    changelog.add("u", uid, f"sent a friend request to {name}"); changelog.add("u", them.id, f"friend request from {me}")
    flash(request, f"Friend request sent to {name}.", "success")
    return RedirectResponse("/friends", 303)
async def _row(db: AsyncSession, uid: int, other: int) -> tuple[Friendship | None, str]:
    return await db.get(Friendship, pair(uid, other), with_for_update=True), (await db.scalar(select(User.username).where(User.id == other))) or "that account"
@router.post("/friends/{user_id}/accept")
async def accept(request: Request, user_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    uid, me = _uid(request), request.state.user["username"]
    f, name = await _row(db, uid, user_id)
    if not f or f.status != "pending" or f.requested_by == uid: raise FormError(f"There's no friend request from {name} to accept.")
    f.status, f.since = "friends", utcnow()
    await _ring(db, user_id, f"{me} accepted your friend request", f"You and {me} are friends now: chat privately from the chat button or your friends page.", f"friend-yes:{f.low_id}:{f.high_id}:{utcnow():%Y%m%d%H%M%S}")
    await db.commit()
    await hub.tell([user_id], {"t": "friend", "kind": "accepted", "from": me})
    changelog.add("u", uid, f"accepted {name}'s friend request"); changelog.add("u", user_id, f"{me} accepted your friend request")
    flash(request, f"You and {name} are friends now.", "success")
    return RedirectResponse("/friends", 303)
@router.post("/friends/{user_id}/decline")
async def decline(request: Request, user_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    return await _end(request, db, user_id, lambda f, uid: f.status == "pending" and f.requested_by != uid, "declined {name}'s friend request", "Declined: {name} isn't told.")
@router.post("/friends/{user_id}/cancel")
async def cancel(request: Request, user_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    return await _end(request, db, user_id, lambda f, uid: f.status == "pending" and f.requested_by == uid, "withdrew the friend request to {name}", "Request to {name} withdrawn.")
@router.post("/friends/{user_id}/remove")
async def unfriend(request: Request, user_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    return await _end(request, db, user_id, lambda f, uid: f.status == "friends", "unfriended {name}", "You and {name} aren't friends any more. Your chat history stays.")
async def _end(request: Request, db: AsyncSession, user_id: int, allowed, log: str, said: str) -> RedirectResponse:
    uid = _uid(request)
    f, name = await _row(db, uid, user_id)
    if not f or not allowed(f, uid): raise FormError(f"Nothing to change with {name}.")
    await db.delete(f)
    await db.commit()
    changelog.add("u", uid, log.format(name=name))
    if "unfriended" in log: changelog.add("u", user_id, f"{request.state.user['username']} ended the friendship")
    flash(request, said.format(name=name), "info")
    return RedirectResponse("/friends", 303)
@router.post("/friends/{user_id}/block")
async def block(request: Request, user_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """No friend requests or personal messages from them any more (either way); a friendship or request between you ends. Company chats stay."""
    uid = _uid(request)
    if user_id == uid: raise FormError("You can't block yourself.")
    name = await db.scalar(select(User.username).where(User.id == user_id))
    if not name: raise FormError("There's no such account.")
    await db.execute(delete(Friendship).where(Friendship.low_id == pair(uid, user_id)[0], Friendship.high_id == pair(uid, user_id)[1]))
    await db.execute(insert(FriendBlock).values(user_id=uid, blocked_id=user_id, created_at=utcnow()).on_conflict_do_nothing())
    await db.commit()
    changelog.add("u", uid, f"blocked {name}")
    flash(request, f"{name} is blocked: no friend requests or personal messages from them. Company chats aren't affected.", "info")
    return RedirectResponse("/friends", 303)
@router.post("/friends/{user_id}/unblock")
async def unblock(request: Request, user_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    name = await db.scalar(select(User.username).where(User.id == user_id)) or "that account"
    if not (await db.execute(delete(FriendBlock).where(FriendBlock.user_id == uid, FriendBlock.blocked_id == user_id))).rowcount: raise FormError(f"{name} isn't blocked.")
    await db.commit()
    changelog.add("u", uid, f"unblocked {name}")
    flash(request, f"{name} is unblocked.", "info")
    return RedirectResponse("/friends", 303)
