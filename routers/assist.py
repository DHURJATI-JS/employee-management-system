"""The instruction bar (assist.py) + the Deminic AI's private chat (deminic.py). What the bar understands (pgvector, or Claude when set) runs
as before; what it can't place goes to the Deminic AI when the local AI is up: the page you were on opens its chat, the answer arrives
live, and if the AI could round it off to a command the bar understands, the answer carries a button for it."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
import deminic
from assist import examples, interpret, rate_limited
from form import ActionForm, AIForm, AssistForm
from media import discard_upload
from routers.chats import _zone, fetching
from system import AI_STATUS, FormError, ai_file, flash, render, safe_referer, unflash_saved
from tables import AIMessage, get_db
router = APIRouter(tags=["assist"])  # logged-in only: the bar lives in the sidebar and on the dashboard
@router.get("/assist")
async def assist_page(request: Request): return RedirectResponse("/dashboard", 303)  # so "go back" from a confirmation lands somewhere useful
@router.post("/assist")
async def assist(request: Request, form: AssistForm = Depends(AssistForm.as_form), db: AsyncSession = Depends(get_db)):
    user = request.state.user
    if rate_limited(user["user_id"]): raise FormError("That's a lot of instructions in a short time; wait a few minutes.")
    plan = await interpret(db, user, form.q.strip())
    if plan.get("unknown") and AI_STATUS.get("ready"):  # the bar can't place it: the Deminic AI takes it (privately), live in its chat
        try:
            await deminic.ask(db, int(user["user_id"]), form.q.strip())
            request.session["open_ai"] = True
            flash(request, "» Deminic is answering that: watch it write in its chat (bottom right).", "info")
        except FormError as e: flash(request, f"» {plan['flash']} (Deminic AI: {e})", "error")  # one question at a time, busy, or spam protection
        return RedirectResponse(safe_referer(request, "/dashboard"), 303)  # back where they were: every page has the drawer
    how = f"via {plan['engine']}" + (f", distance {plan['distance']:.2f}" if plan.get("distance") is not None else "")
    if plan["kind"] == "confirm": return render(request, "assist_confirm", {"plan": plan, "said": form.q.strip(), "how": how})
    if plan["flash"]: flash(request, f"» {plan['flash']}", plan["category"])
    return RedirectResponse(plan["url"], 303)
# ---------- the Deminic AI's private chat (the chat drawer's pinned "Deminic AI") ----------
@router.get("/deminic/messages")
async def ai_messages(request: Request, db: AsyncSession = Depends(get_db)):
    uid = int(request.state.user["user_id"])
    await deminic.settle(db, uid)  # a lost job (a restart) shows as failed, not "thinking" for ever
    rows = (await db.scalars(select(AIMessage).where(AIMessage.user_id == uid).order_by(AIMessage.id.desc()).limit(80))).all()[::-1]
    zone = _zone(request)
    return {"messages": [deminic.as_json(m, zone) for m in rows], "ready": bool(AI_STATUS.get("ready")), "detail": AI_STATUS.get("detail", ""), "examples": examples()[:4]}
@router.post("/deminic/messages")
async def ai_send(request: Request, form: AIForm = Depends(AIForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = int(request.state.user["user_id"])
    if form.image: unflash_saved(request, form.image)
    if not AI_STATUS.get("ready"):
        if form.image: discard_upload(form.image)
        raise FormError(f"The local AI is offline right now ({AI_STATUS.get('detail', 'not running')}): try again in a moment.")
    try: aid = await deminic.ask(db, uid, form.text, form.web, form.image)  # one at a time + spam protection (deminic.ask -> spam.check_ai); web: search the web; image: a picture to look at
    except FormError:
        if form.image: discard_upload(form.image)  # refused before it was used: the raw upload goes
        raise
    return {"id": aid} if fetching(request) else RedirectResponse(safe_referer(request, "/dashboard"), 303)
@router.get("/deminic/images/{message_id}")
async def ai_image(request: Request, message_id: int, db: AsyncSession = Depends(get_db)):
    """A picture someone showed the Deminic AI: only ever to them (the same 404 for anyone else)."""
    uid = int(request.state.user["user_id"])
    m = await db.get(AIMessage, message_id)
    if not m or m.user_id != uid or m.role != "me" or not (path := ai_file(uid, message_id)).is_file(): raise HTTPException(404, "That picture isn't there any more.")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
@router.post("/deminic/clear")
async def ai_clear(request: Request, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = int(request.state.user["user_id"])
    await db.execute(delete(AIMessage).where(AIMessage.user_id == uid))
    await db.commit()
    deminic.forget(uid)  # the pictures it was shown go too
    return {"ok": True} if fetching(request) else RedirectResponse(safe_referer(request, "/dashboard"), 303)
