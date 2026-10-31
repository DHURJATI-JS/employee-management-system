"""Change logs (user asked, 2026-10-30): every change, kept as encrypted files on disk (nothing in Postgres): one per company, one per person.
- A company's file: every change in it (settings, people, positions, payroll, tasks, meetings, chats, the shop...) and its members' own profile
  + document changes, with full ID and bank numbers (the user's choice). Its owner, co-owners, managers and HR (PAYERS) download it any time,
  with their password (routers/changes.py).
- A person's file: every change to their account, profile and documents (full numbers too), and what they did themselves; downloadable by
  them, with their password. Kept while the account exists; once it's deleted, an erase marker dates it KEEP_DAYS (30) on and purge() (hourly,
  app.py) erases it then. A deleted company's file goes the same way.
Each line is one change, sealed on its own (AES-256-GCM, system.seal, bound to its file): unreadable without the key, and a line can't be
moved to another file. While a request is served its changes wait (begin / finish, from system._session_guard): a refused form logs
nothing. A request that changes something without saying what (no explicit line) still leaves one: the route's name + what it acted on.
Routes that know more (profile, documents, bank, company settings, payroll, products, orders, friends) write what changed, old -> new.
One background task appends the lines (run(), like audit.py), so a request never waits on the disk."""
import asyncio, base64, contextvars, hmac, json, threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
import audit
from system import MEDIA_KEY, seal, unseal
CHANGE_DIR = audit.LOG_DIR / "changes"
KEEP_DAYS = 30  # after the account / company is deleted
_held: contextvars.ContextVar = contextvars.ContextVar("changelog_held", default=None)  # the request being served: {"actor", "lines", "refused", "closed"}
_queue: list[tuple[str, str, str]] = []  # (kind, key, the change as JSON), waiting for the writer
_wake = asyncio.Event()
SKIP = {"send", "add_to_cart", "update_cart", "assist", "find", "login", "logout", "register", "verify", "resend", "ask", "start_review", "company_review",
        "upload", "cashfree_hook", "accept_terms_page", "ai_send", "ai_clear", "forward", "send_file", "link_preview", "enquire",
        "forgot", "forgot_verify", "forgot_resend", "best_ask", "ai_review"}  # not changes (messages, the cart, searches, logging in, the private AI chat, asking for a recovery code, asking the company AI); a new enquiry + a password reset log themselves
def _stem(kind: str, key) -> str: return kind + "-" + hmac.new(MEDIA_KEY, f"changes:{kind}:{key}".encode(), "sha256").hexdigest()[:32]  # unguessable, like pictures
def file_of(kind: str, key) -> Path: return CHANGE_DIR / f"{_stem(kind, key)}.log"
def _context(kind: str, key) -> str: return f"changes:{kind}:{key}"
def who(user) -> str: return audit.who(user) if isinstance(user, dict) else (user or "system")
def actor() -> str:  # who's acting now: the request's user ("guest" for a signed-out one, e.g. a courier's link), else "system"
    h = _held.get()
    return who(h["actor"]) if h and h.get("actor") else ("guest" if h else "system")
def _show(v) -> str:
    if v in (None, "", [], (), set()): return "—"
    if isinstance(v, bool): return "yes" if v else "no"
    if isinstance(v, (list, tuple, set)): return ", ".join(map(str, v))
    return str(v)
def diff(old: dict, new: dict) -> str:  # "headline: Backend dev → Senior backend dev · skills: python → python, docker" (only what changed)
    return " · ".join(f"{k}: {_show(old.get(k))} → {_show(v)}" for k, v in new.items() if _show(old.get(k)) != _show(v))
def facts(d: dict) -> str: return " · ".join(f"{k}: {_show(v)}" for k, v in d.items())  # every field, for something new
def add(kind: str, key, what: str, detail: str = "", by: str | None = None) -> None:
    """One change in kind "c" (a company, key = its id) or "u" (a person, key = their user id)."""
    rec = json.dumps({"at": datetime.now(UTC).isoformat(timespec="milliseconds"), "by": by or actor(), "what": what[:300], "detail": detail[:8000]}, ensure_ascii=False)
    h = _held.get()
    if h is not None and not h["closed"]: h["lines"].append((kind, str(key), rec)); return  # written once the request has succeeded
    _queue.append((kind, str(key), rec)); _wake.set()
def company(cid, what: str, detail: str = "", by: str | None = None) -> None: add("c", cid, what, detail, by)
async def person(db, uid: int, username: str, what: str, detail: str = "", by: str | None = None, companies=None) -> None:
    """A change to someone's account / profile / documents: their own file, and each company they're in (its leaders see members' changes)."""
    from tables import Membership
    add("u", uid, what, detail, by)
    for cid in companies if companies is not None else (await db.scalars(__import__("sqlalchemy").select(Membership.company_id).where(Membership.user_id == uid))).all():
        add("c", cid, f"{username}: {what}", detail, by)
# ---------- serving a request (system._session_guard) ----------
def begin(user) -> contextvars.Token: return _held.set({"actor": user, "lines": [], "refused": None, "closed": False})
def refused(why: str) -> None:  # the request was turned down (system._error): what it held is dropped
    if (h := _held.get()) is not None: h["refused"] = why or "refused"
RESOLVE = {"chat_id": ("chats", "company_id"), "meeting_id": ("meetings", "company_id"), "leave_id": ("leave_requests", "company_id"), "request_id": ("member_requests", "company_id"),
           "task_id": ("tasks", "company_id"), "review_id": ("reviews", "company_id"), "oid": ("orders", "company_id"), "pid": ("products", "company_id")}  # constants, never user input
async def finish(token, request, status: int) -> None:
    """After the response: the request's changes are written if it succeeded; one that changed something but said nothing gets one line."""
    h = _held.get()
    _held.reset(token)
    if not h: return
    h["closed"] = True  # tasks it started log straight to the queue from now on
    if h["refused"] or status >= 400: return
    if h["lines"]: _queue.extend(h["lines"]); _wake.set(); return
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"): return
    name = getattr(request.scope.get("endpoint"), "__name__", "")
    if not name or name in SKIP: return
    params = dict(request.scope.get("path_params") or {})
    about = ", ".join(f"{k.removesuffix('_id')} {v}" for k, v in params.items() if k not in ("company_id",))
    what, by = name.replace("_", " ") + (f" ({about})" if about else ""), who(h["actor"]) if h["actor"] else "guest"
    cid = params.get("company_id")
    if not cid:
        for k, (table, col) in RESOLVE.items():
            if k in params and str(params[k]).isdigit():
                from sqlalchemy import text
                from tables import Session
                try:
                    async with Session() as db: cid = await db.scalar(text(f"SELECT {col} FROM {table} WHERE id = :i"), {"i": int(params[k])})
                except Exception: cid = None
                break
    rec = lambda: json.dumps({"at": datetime.now(UTC).isoformat(timespec="milliseconds"), "by": by, "what": what, "detail": ""}, ensure_ascii=False)
    if cid: _queue.append(("c", str(cid), rec()))
    elif h["actor"]: _queue.append(("u", str(h["actor"]["user_id"]), rec()))  # their own doing, outside any company (orders, addresses, friends...)
    _wake.set()
# ---------- reading (downloads) ----------
def entries(kind: str, key) -> list[dict]:  # oldest first; a line that won't decrypt (tampered, another key) is skipped
    f, out = file_of(kind, key), []
    if not f.is_file(): return out
    for line in f.read_text(encoding="ascii", errors="ignore").splitlines():
        try: blob = base64.b64decode(line, validate=True)
        except ValueError: continue
        if (raw := unseal(_context(kind, key), blob)) is not None:
            try: out.append(json.loads(raw))
            except ValueError: pass
    return sorted(out, key=lambda r: r.get("at", ""))
def as_text(kind: str, key, title: str, zone, by: str) -> str:
    rows = entries(kind, key)
    local = lambda iso: datetime.fromisoformat(iso).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")
    head = [f"{title} · {len(rows)} change{'' if len(rows) == 1 else 's'} · downloaded {datetime.now(zone):%Y-%m-%d %H:%M %Z} by {by}",
            f"Oldest first, times on {zone.key}. Full ID and bank numbers are included: keep this file somewhere safe.", "-" * 100]
    return "\n".join(head + [" | ".join([local(r["at"]), r.get("by", "?"), r.get("what", "")] + ([r["detail"]] if r.get("detail") else [])) for r in rows]) + "\n"
# ---------- deleted accounts / companies: kept KEEP_DAYS more ----------
def retire(kind: str, key) -> str:
    """The account / company is gone: its file is erased KEEP_DAYS from now (an empty marker beside it holds the date). Returns that date."""
    when = (datetime.now(UTC) + timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
    CHANGE_DIR.mkdir(parents=True, exist_ok=True)
    (CHANGE_DIR / f"{_stem(kind, key)}.erase-{when}").touch()
    return f"{when[:4]}-{when[4:6]}-{when[6:]}"
def purge() -> int:  # hourly (app.py): erase the files whose date has come
    n, today = 0, datetime.now(UTC).strftime("%Y%m%d")
    for marker in CHANGE_DIR.glob("*.erase-*") if CHANGE_DIR.is_dir() else []:
        stem, _, day = marker.name.partition(".erase-")
        if day <= today:
            (CHANGE_DIR / f"{stem}.log").unlink(missing_ok=True); marker.unlink(missing_ok=True); n += 1
    return n
def remove(kind: str, key) -> None:  # tests' cleanup only: the file + its marker, now
    for f in CHANGE_DIR.glob(f"{_stem(kind, key)}.*") if CHANGE_DIR.is_dir() else []: f.unlink(missing_ok=True)
# ---------- the writer ----------
_lock = threading.Lock()  # the writer (a thread) and flush() never append at the same time
def _write(batch: list[tuple[str, str, str]]) -> None:
    CHANGE_DIR.mkdir(parents=True, exist_ok=True)
    files: dict[tuple[str, str], list[str]] = {}
    for kind, key, rec in batch: files.setdefault((kind, key), []).append(base64.b64encode(seal(_context(kind, key), rec)).decode())
    with _lock:
        for (kind, key), rows in files.items():
            with file_of(kind, key).open("a", encoding="ascii") as f: f.write("\n".join(rows) + "\n")
async def run() -> None:  # started by app.py: appends queued changes every half second
    while True:
        await _wake.wait(); _wake.clear()
        await asyncio.sleep(0.5)
        batch, _queue[:] = list(_queue), []
        if not batch: continue
        try: await asyncio.to_thread(_write, batch)
        except OSError as e: _queue[:0] = batch; print(f"change log: {e}")  # kept for the next try
def flush() -> None:  # at shutdown, and before a download (what's queued is in the file)
    if _queue: batch, _queue[:] = list(_queue), []; _write(batch)
