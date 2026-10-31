import hashlib, inspect, time
from functools import wraps
from typing import Annotated
from fastapi import File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile  # what FastAPI actually passes in (fastapi.UploadFile subclasses it)
from system import AI_STATUS, UPLOAD_DIR, FormError, admin_session, build_assets, csrf_token, first_send, flash, save_file, scan_file, verify_csrf, wants_html
KW = inspect.Parameter.KEYWORD_ONLY
class _SavedFileMarker:
    def __repr__(self): return "SavedFile"
_SAVED = _SavedFileMarker()
SavedFile = Annotated[str, _SAVED]  # form field type for <input type="file">: @as_form scans + saves the upload, the field then holds the saved file name
def as_form(cls):
    """Add `cls.as_form` for `Depends(Model.as_form)`: reads the fields from form data, checks csrf_token, and for every
    SavedFile field virus-scans + saves the upload (flash "saved" on success; on failure FormError -> flash + redirect back)."""
    fields = cls.model_fields.items()
    if bad := [n for n, f in fields if f.annotation in (UploadFile, StarletteUploadFile)]: raise TypeError(f"{cls.__name__}.{bad[0]}: use SavedFile, not UploadFile, for file inputs")
    files = [n for n, f in fields if _SAVED in f.metadata]
    params = [inspect.Parameter("request", KW, annotation=Request), inspect.Parameter("csrf_token", KW, annotation=str, default=Form(""))]
    params += [inspect.Parameter(n, KW, annotation=UploadFile | None, default=File(None)) if n in files  # required check is done below
               else inspect.Parameter(n, KW, annotation=f.annotation, default=Form(... if f.is_required() else f.default)) for n, f in fields]
    async def _as_form(request: Request, csrf_token: str, **data):
        verify_csrf(request, csrf_token)
        uploads = {}
        for n in files:
            if isinstance(up := data.get(n), StarletteUploadFile) and up.filename: uploads[n] = data[n] = up  # real file chosen
            elif cls.model_fields[n].is_required(): raise FormError(f"{n}: no file selected.")
            else: data.pop(n, None)  # optional and empty: use the field's default
        try: form = cls(**{**data, **{n: up.filename for n, up in uploads.items()}})  # validate the other fields before saving anything
        except ValidationError as e: raise RequestValidationError(e.errors())  # 422 instead of a 500
        if cls.ONCE:  # the same form from the same person at the same address, moments apart, makes one thing (a slow page clicked again, 8 copies at once: the browser's own guard only covers 3 s and one tab)
            key = hashlib.sha256(repr((request.cookies.get("sid"), request.url.path, sorted(data.items(), key=lambda kv: kv[0]))).encode()).hexdigest()
            if not first_send(key, cls.ONCE): raise FormError("That was just sent: there's no need to send it twice.")
            request.state.sent_key = key
        kept: list[str] = []
        try:
            for n, up in uploads.items():
                await scan_file(up)
                setattr(form, n, saved := await save_file(up))
                kept.append(saved)
                if wants_html(request): flash(request, f"{up.filename} uploaded and saved as {saved}.", "success")
        except Exception:  # a later file failed its scan / save: the ones already saved aren't kept either
            for name in kept: (UPLOAD_DIR / name).unlink(missing_ok=True)
            raise
        return form
    _as_form.__signature__ = inspect.Signature(params)
    cls.as_form = _as_form
    return cls
def public(endpoint):
    """Mark an endpoint as reachable without logging in (system.require_login skips it). Every other endpoint requires a login."""
    endpoint.public = True
    return endpoint
def guest_only(endpoint):
    """Login / signup / account recovery pages and their forms: someone already logged in is sent to their dashboard instead, and a
    browser signed in to the admin console to the console (user asked: no user logins there). Put it under @public."""
    @wraps(endpoint)
    async def wrapper(*args, **kwargs):
        request = kwargs.get("request") or next(a for a in args if isinstance(a, Request))
        if user := getattr(request.state, "user", None):
            flash(request, f"You're already logged in as {user['username']}. Log out first to use another account.", "info")
            return RedirectResponse("/dashboard", 303)
        if await admin_session(request):
            flash(request, "You're signed in to the admin console. Sign out of it first to log in, sign up or recover a user id or password.", "info")
            return RedirectResponse("/admin", 303)
        return await endpoint(*args, **kwargs)
    return wrapper
def _single_company() -> bool:
    from tables import SITE
    return SITE["single_company"]
def _lang_version(request: Request) -> int:  # a cached page is redone once more of its language has been translated
    import i18n
    return i18n._version.get(i18n.lang_of(request), 0)
class PageCache:
    """In-process page cache, Flask-Caching style: put @cache.memoize(timeout=60) under @router.get / @public on a page endpoint
    that returns render(...). Lives in this process's memory, not Redis (Redis holds login sessions only).
    - one cached copy per page + query string + logged-in user (anonymous visitors share one); only 200 HTML responses are kept
    - pages with a pending flash message are never cached or served from cache (flashes are one-off and per person)
    - the per-request CSP nonce and the session's CSRF token are cut out before storing and filled back in on every hit,
      so no visitor ever receives someone else's token, and scripts/forms keep working
    - entries also expire when the built CSS/JS changes, so cached pages never point at deleted asset files"""
    NONCE, CSRF = "\x00nonce\x00", "\x00csrf\x00"  # placeholders; NUL can't come out of a template
    def __init__(self, max_entries: int = 512): self.store, self.max_entries = {}, max_entries
    def clear(self) -> None: self.store.clear()
    def memoize(self, timeout: int = 60):
        def decorator(endpoint):
            @wraps(endpoint)
            async def wrapper(*args, **kwargs):
                request = kwargs.get("request") or next(a for a in args if isinstance(a, Request))
                if request.session.get("_flashes"): return await endpoint(*args, **kwargs)
                user = getattr(request.state, "user", None) or {}
                key = (request.url.netloc, endpoint.__module__, endpoint.__qualname__, request.url.path, request.url.query,  # host first: no cross-host reuse
                       tuple(sorted(user.items())), tuple(build_assets().values()), AI_STATUS["ready"],  # + the AI status: the sidebar shows it
                       request.cookies.get("hx_lang", ""), _lang_version(request), _single_company())  # + the page's language (i18n.py), its translations so far, and the admin's switch (the sidebar locks show it)
                now = time.monotonic()
                if (hit := self.store.get(key)) and hit[0] > now:
                    return HTMLResponse(hit[1].replace(self.NONCE, request.state.nonce).replace(self.CSRF, csrf_token(request)), hit[2], headers={"X-Cache": "HIT"})
                resp = await endpoint(*args, **kwargs)
                if isinstance(resp, HTMLResponse) and resp.status_code == 200:
                    html = bytes(resp.body).decode().replace(request.state.nonce, self.NONCE)
                    if token := request.session.get("_csrf"): html = html.replace(token, self.CSRF)
                    if len(self.store) >= self.max_entries: self._prune(now)
                    self.store[key] = (now + timeout, html, resp.status_code)
                    resp.headers["X-Cache"] = "MISS"
                return resp
            return wrapper
        return decorator
    def _prune(self, now: float) -> None:
        for k in [k for k, (expires, *_) in self.store.items() if expires <= now]: del self.store[k]
        while len(self.store) >= self.max_entries: del self.store[next(iter(self.store))]  # still full: drop the oldest
cache = PageCache()
