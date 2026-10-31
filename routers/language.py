"""The language switcher (user asked; i18n.py): anyone, logged in or not, picks a language from the header. It's kept in a cookie, and on the
account when logged in (so it follows them to other devices). Picking one queues every page's words for it: the parts not translated yet
show in English for a few minutes."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
import i18n
from decoraters import public
from form import LanguageForm
from system import flash, safe_referer
from tables import User, get_db
router = APIRouter(tags=["language"])
@router.post("/language")
@public
async def set_language(request: Request, form: LanguageForm = Depends(LanguageForm.as_form), db: AsyncSession = Depends(get_db)):
    if user := getattr(request.state, "user", None):
        account = await db.get(User, int(user["user_id"]))
        account.language = None if form.lang == "en" else form.lang
        await db.commit()
    waiting = i18n.queue_all(form.lang)
    if waiting: flash(request, "Language changed. The parts not translated yet show in English for a few minutes, while this server translates them.", "info")  # (no language name in it: the translator "translated" हिन्दी into English)
    resp = RedirectResponse(safe_referer(request, "/"), 303)
    resp.set_cookie(i18n.COOKIE, form.lang, max_age=365 * 86400, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")
    return resp
