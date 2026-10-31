import asyncio, base64, contextlib, hashlib, hmac, ipaddress, os, re, secrets, shutil, smtplib, ssl, threading, time, urllib.parse, urllib.request, uuid
from collections import deque
from email.message import EmailMessage
from datetime import UTC, datetime
from functools import lru_cache
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from markupsafe import Markup
import rcssmin, rjsmin
import redis.asyncio as redis
import audit  # the daily activity log (imports nothing from this app)
import abuse  # spam / limit / security events for the admin console (imports nothing from this app at load time)
from sqlalchemy import URL
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, TimeoutError as PoolTimeout
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocketDisconnect
ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")
err="Error: .env file is missing or invalid. Copy .env.example to .env and edit the values."
# ---------- config (from .env) ----------
APP_NAME = os.getenv("APP_NAME", err)
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", err)
OWNER_NUMBER = os.getenv("OWNER_NUMBER", "")  # the owner's WhatsApp number, for the terms page: like CONTACT_EMAIL, only ever in .env because the code is public
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
REDIS_URL, SESSION_TTL = os.getenv("REDIS_URL",err), int(os.getenv("SESSION_TTL", "86400"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None  # setup_services.ps1 starts Redis with the same one
PLATFORM_ADMINS = {u.strip().lower() for u in os.getenv("PLATFORM_ADMINS", "").split(",") if u.strip()}  # usernames who see the platform's earnings + record settlements; nobody else can take one of these names (tables.username_problem)
CLAMD_HOST, CLAMD_PORT = os.getenv("CLAMD_HOST", err), int(os.getenv("CLAMD_PORT", "3310"))
_PG = {"host": os.getenv("POSTGRES_HOST", err), "port": int(os.getenv("POSTGRES_PORT", err)), "database": os.getenv("POSTGRES_DB", err)}
# Two Postgres logins: the admin (POSTGRES_USER, a superuser) is used only at startup to create tables + the app role; the app itself
# runs as POSTGRES_APP_USER, which can only read/write rows: no DDL, no superuser tricks like COPY ... TO PROGRAM.
ADMIN_DATABASE_URL = URL.create("postgresql+asyncpg", username=os.getenv("POSTGRES_USER"), password=os.getenv("POSTGRES_PASSWORD"), **_PG)
DATABASE_URL = URL.create("postgresql+asyncpg", username=os.getenv("POSTGRES_APP_USER"), password=os.getenv("POSTGRES_APP_PASSWORD"), **_PG)
POSTGRES_SSL = os.getenv("POSTGRES_SSL", "")  # e.g. require / verify-full when the database isn't on this machine
UPLOAD_DIR = ROOT / "uploads"  # uploads live on disk only, saved as "<random hex>_<original name>"
MEDIA_DIR = UPLOAD_DIR / "media"  # re-encoded profile pictures + company logos (media.py), served at /media/
MEDIA_KEY = (os.getenv("MEDIA_KEY") or SECRET_KEY).encode()  # names the picture files (unguessable); changing it hides existing pictures
PROOF_DIR = UPLOAD_DIR / "proofs"  # payout screenshots (re-encoded WebP), never public: only served to the people allowed to see that payout
_BANK_KEY = hmac.new((os.getenv("BANK_KEY") or SECRET_KEY).encode(), b"bank-details", "sha256").digest()  # AES-256 key; changing it makes saved account numbers unreadable
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "25"))
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_MB * 1024 * 1024)  # limit for every request body (uploads included)
BLOCKLIST_FILE = ROOT / os.getenv("IP_BLOCKLIST_FILE", "blocked_ips.txt")
ADMIN_USERNAME, ADMIN_PASSWORD = os.getenv("ADMIN_USERNAME", ""), os.getenv("ADMIN_PASSWORD", "")  # the admin console (routers/admin.py): both set, or it's off
CYCLE_DAYS = int(os.getenv("CYCLE_DAYS", "30"))  # salaries are paid every CYCLE_DAYS days from the day someone joins (user asked: in .env). Changing it renumbers every pay cycle
if not 1 <= CYCLE_DAYS <= 92: raise RuntimeError("CYCLE_DAYS in .env must be a whole number of days from 1 to 92")
CYCLE_EVERY = "every day" if CYCLE_DAYS == 1 else f"every {CYCLE_DAYS} days"  # as the pages say it: "paid every day" / "paid every 30 days"
MAX_COMPANIES_PER_ACCOUNT = int(os.getenv("MAX_COMPANIES_PER_ACCOUNT", "10"))  # companies one account can own at a time (user asked: in .env); deleting one or handing it over frees a slot
if MAX_COMPANIES_PER_ACCOUNT < 1: raise RuntimeError("MAX_COMPANIES_PER_ACCOUNT in .env must be a whole number of at least 1")
OBFUSCATE_ASSETS = os.getenv("OBFUSCATE_ASSETS", "1") != "0"  # 0 = readable class names (still minified), for debugging
ASSET_SALT = os.getenv("ASSET_SALT") or SECRET_KEY  # change it to re-randomize every class name (without logging anyone out)
SMTP_HOST, SMTP_PORT = os.getenv("SMTP_HOST", ""), int(os.getenv("SMTP_PORT", "587"))  # no SMTP_HOST: codes are printed to the console
SMTP_USER, SMTP_PASSWORD = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM") or SMTP_USER
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "console").lower()  # console | twilio
TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM = os.getenv("TWILIO_ACCOUNT_SID", ""), os.getenv("TWILIO_AUTH_TOKEN", ""), os.getenv("TWILIO_FROM", "")
DEFAULT_PHONE_REGION = os.getenv("DEFAULT_PHONE_REGION") or None  # e.g. IN or US: lets people type numbers without +country code
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]  # Host headers the app answers to
VLLM_URL, VLLM_MODEL = os.getenv("VLLM_URL", "").rstrip("/"), os.getenv("VLLM_MODEL", "gemma-3-12b")  # the local AI (vLLM on the graphics card, one model for everything: grow.py); empty URL = off
VLLM_PARALLEL, VLLM_MAX_LEN = int(os.getenv("VLLM_PARALLEL", "8")), int(os.getenv("VLLM_MAX_LEN", "5120"))  # answers at once; the longest prompt + answer, in tokens (setup_services.ps1 starts vLLM with the same two)
# Geoapify (company locations, places.py): the browser key draws the map tiles + searches addresses in the location dialog (restrict it
# to your site's address in Geoapify's dashboard); the server key verifies the picked place with the Place Details API. One key can do both.
GEOAPIFY_API_KEY = os.getenv("GEOAPIFY_API_KEY", "")
GEOAPIFY_SERVER_KEY = os.getenv("GEOAPIFY_SERVER_KEY", "") or GEOAPIFY_API_KEY
GEOAPIFY_DETAILS_URL = os.getenv("GEOAPIFY_DETAILS_URL", "https://api.geoapify.com/v2/place-details")
# Bank account verification, switched on / off by hand in .env (BANK_VERIFY=1 / 0; off by default): when on, every bank account (people's +
# companies') is checked with the bank through Cashfree's Bank Account Verification API (bankcheck.py) before it's saved, and payouts
# need verified accounts. Off: accounts are saved unverified and nothing is sent anywhere.
BANK_VERIFY = os.getenv("BANK_VERIFY", "0") == "1"
# Identity number verification, also switched by hand (ID_VERIFY=1 / 0; off by default): when on, PAN, Indian driving licences and voter IDs
# are checked online with Cashfree's Verification Suite (idcheck.py); every ID's format + check digit is always checked locally (form.py).
ID_VERIFY = os.getenv("ID_VERIFY", "0") == "1"
CASHFREE_CLIENT_ID, CASHFREE_CLIENT_SECRET = os.getenv("CASHFREE_CLIENT_ID", ""), os.getenv("CASHFREE_CLIENT_SECRET", "")
CASHFREE_BASE_URL = (os.getenv("CASHFREE_BASE_URL") or ("https://sandbox.cashfree.com" if os.getenv("CASHFREE_ENV", "production") == "sandbox" else "https://api.cashfree.com") + "/verification").rstrip("/")
AI_STATUS = {"up": False, "ready": False, "detail": "checking the AI…"}  # refreshed every 15 s by grow.watch_ai(); the sidebar's light + AI buttons read it
AI_ASSIST = os.getenv("AI_ASSIST", "vector").lower()  # instruction bar: "vector" (pgvector, offline) or "claude" (Claude reads it; falls back to vector)
reddisdb = redis.from_url(REDIS_URL, password=REDIS_PASSWORD, decode_responses=True)  # holds login sessions only
templates = Jinja2Templates(directory=ROOT / "templates")
# ---------- passwords (scrypt, random salt, stored as "salt$hash") ----------
def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    return salt.hex() + "$" + hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()
def verify_password(password: str, stored: str) -> bool:
    return hmac.compare_digest(hash_password(password, bytes.fromhex(stored.split("$")[0])), stored)
async def password_matches(password: str, stored: str) -> bool: return await asyncio.to_thread(verify_password, password, stored)  # on a thread: scrypt is slow on purpose, and on the main loop every login froze the whole site for it (the load test)
async def new_password_hash(password: str) -> str: return await asyncio.to_thread(hash_password, password)
DUMMY_HASH = hash_password(secrets.token_hex())  # checked when a username doesn't exist, so response time doesn't reveal valid usernames
# ---------- flash messages (stored in the signed session cookie) ----------
class FormError(Exception):
    """Raise from a form endpoint: browsers get flashed + redirected back, APIs get a 400."""
def flash(request: Request, message: str, category: str = "error") -> None:
    request.session["_flashes"] = [*request.session.get("_flashes", []), [category, message]]
def unflash_saved(request: Request, saved: str) -> None:  # drop @as_form's generic "uploaded and saved as X" note when the route flashes the real outcome
    request.session["_flashes"] = [f for f in request.session.get("_flashes", []) if not f[1].endswith(f"saved as {saved}.")]
def get_flashed_messages(request: Request) -> list[list[str]]:  # in the visitor's language (i18n.prepare translated them before the page)
    import i18n
    lang = i18n.lang_of(request)
    return [[c, i18n.flash_text(m, lang)] for c, m in request.session.pop("_flashes", [])]
# ---------- CSRF (one token per session; every @as_form form checks it) ----------
def csrf_token(request: Request) -> str:
    return request.session.setdefault("_csrf", secrets.token_urlsafe(32))
def csrf_input(request: Request) -> Markup:
    return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token(request)}">')
def verify_csrf(request: Request, token: str) -> None:
    if not token or not hmac.compare_digest(token.encode(), request.session.get("_csrf", "").encode()):
        raise FormError("Form expired or invalid (CSRF check failed). Please try again.")
# ---------- verification codes + email/SMS delivery (Python's smtplib for email; SMS needs a gateway: Twilio) ----------
CODE_TTL_MIN, CODE_ATTEMPTS, RESEND_COOLDOWN_S, MAX_SENDS, SIGNUPS_PER_IP_HOUR = 10, 5, 60, 5, 5
def new_code() -> str: return f"{secrets.randbelow(10**6):06d}"  # 6 digits from the OS's secure random source
BLACKLIST_KEY = (os.getenv("BLACKLIST_KEY") or SECRET_KEY).encode()  # keys the credential blacklist; changing it forgets every entry
def identity_digest(kind: str, value: str) -> str:  # "email"/"phone"/"username" + value -> the keyed hash the blacklist stores (never the value itself)
    return hmac.new(BLACKLIST_KEY, f"{kind}:{value.strip().lower()}".encode(), "sha256").hexdigest()
def seal(context: str, value: str) -> bytes:  # AES-256-GCM with a fresh nonce; context (e.g. "bank:42") is authenticated, so a row can't be moved to another user
    nonce = os.urandom(12)
    return nonce + AESGCM(_BANK_KEY).encrypt(nonce, value.encode(), context.encode())
def unseal(context: str, blob: bytes) -> str | None:  # None if it was tampered with, moved, or sealed under another key
    try: return AESGCM(_BANK_KEY).decrypt(blob[:12], blob[12:], context.encode()).decode()
    except (InvalidTag, ValueError): return None
def code_hash(code: str) -> str: return hmac.new(SECRET_KEY.encode(), b"verify-code:" + code.encode(), "sha256").hexdigest()  # keyed: can't be brute-forced from a DB dump
def mask_email(email: str) -> str: local, _, domain = email.partition("@"); return local[:1] + "•" * max(len(local) - 1, 2) + "@" + domain
def mask_phone(phone: str) -> str: return phone[:3] + "•" * max(len(phone) - 5, 2) + phone[-2:]
def delivery_modes() -> dict: return {"email": f"SMTP {SMTP_HOST}:{SMTP_PORT}" if SMTP_HOST else "console", "sms": SMS_PROVIDER}
def _smtp_send(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_FROM, to, subject
    msg.set_content(body)
    ctx = ssl.create_default_context()  # verified TLS only
    with (smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=20) if SMTP_PORT == 465 else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)) as s:
        if SMTP_PORT != 465: s.starttls(context=ctx)
        if SMTP_USER: s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)
def _twilio_send(to: str, body: str) -> None:
    auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    data = urllib.parse.urlencode({"To": to, "From": TWILIO_FROM, "Body": body}).encode()
    urllib.request.urlopen(urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json", data,
                                                  {"Authorization": f"Basic {auth}"}), timeout=20).close()
async def send_email(to: str, subject: str, body: str) -> None:
    if not SMTP_HOST: return print(f"\n[DEV EMAIL -> {to}] {subject}\n{body}\n", flush=True)  # set SMTP_* in .env to really send
    try: await run_in_threadpool(_smtp_send, to, subject, body)
    except (OSError, smtplib.SMTPException) as e:
        print(f"email to {to} failed: {e!r}")
        raise FormError("Couldn't send the verification email. Check the address and try again.") from None
async def send_sms(to: str, body: str) -> None:
    if SMS_PROVIDER != "twilio": return print(f"\n[DEV SMS -> {to}] {body}\n", flush=True)  # set SMS_PROVIDER=twilio + TWILIO_* to really send
    try: await run_in_threadpool(_twilio_send, to, body)
    except OSError as e:  # urllib's HTTPError/URLError are OSErrors
        print(f"SMS to {to} failed: {e!r}")
        raise FormError("Couldn't send the SMS code. Check the number and try again.") from None
# ---------- front-end build: assets/*.css|js -> static/dist/<name>.<hash>.min.css|js (minified, class names randomized) ----------
# Every "hx-*" name (classes, ids, CSS variables, keyframes) becomes "x" + HMAC(ASSET_SALT, name): random-looking, stable across
# restarts and workers, impossible to map back without the salt. MDB/Bootstrap/Font Awesome classes are left alone (their CSS/JS
# need them). The readable sources in assets/ are never served; only static/dist/ is. Rebuilt automatically when a source changes.
ASSET_SRC, ASSET_OUT = ROOT / "assets", ROOT / "static" / "dist"
_HX = re.compile(r"(?:(?<=--)|(?<![\w-]))hx-[a-z0-9-]+")  # hx-name, also inside --hx-var
_ATTR = re.compile(r'\b(class|id)="([^"]*)"')
def _hx_name(token: str) -> str: return "x" + hmac.new(ASSET_SALT.encode(), token.encode(), "sha256").hexdigest()[:9]
def obfuscate(code: str) -> str: return _HX.sub(lambda m: _hx_name(m.group()), code) if OBFUSCATE_ASSETS else code
@lru_cache(maxsize=8192)
def _attr_value(value: str) -> str: return obfuscate(value)  # (a page repeats the same few class lists thousands of times: one HMAC each was 7 ms a page)
def obfuscate_html(html: str) -> str:  # only class/id attributes, so page text (usernames, file names...) is never touched
    return _ATTR.sub(lambda m: f'{m.group(1)}="{_attr_value(m.group(2))}"', html) if OBFUSCATE_ASSETS else html
_assets: dict = {"stamp": None, "urls": {}, "next": 0.0}
def build_assets() -> dict:
    """Build static/dist from assets/ if any source changed since the last build (looked at every few seconds, not on each of a page's calls). Returns {"css/app.css": "/static/dist/app.<hash>.min.css", ...}."""
    if _assets["urls"] and time.monotonic() < _assets["next"]: return _assets["urls"]
    _assets["next"] = time.monotonic() + 3
    sources = sorted(p for p in ASSET_SRC.rglob("*") if p.suffix in (".css", ".js"))
    stamp = (OBFUSCATE_ASSETS, *((p.as_posix(), p.stat().st_mtime_ns) for p in sources))
    if stamp == _assets["stamp"]: return _assets["urls"]
    ASSET_OUT.mkdir(parents=True, exist_ok=True)
    urls = {}
    for p in sources:
        code = obfuscate(p.read_text(encoding="utf-8"))
        code = rcssmin.cssmin(code) if p.suffix == ".css" else rjsmin.jsmin(code)
        name = f"{p.stem}.{hashlib.sha256(code.encode()).hexdigest()[:10]}.min{p.suffix}"  # content hash: safe to cache forever
        if not (ASSET_OUT / name).exists():
            (tmp := ASSET_OUT / (name + ".tmp")).write_text(code, encoding="utf-8")
            os.replace(tmp, ASSET_OUT / name)  # atomic, so a request never gets a half-written file
        urls[p.relative_to(ASSET_SRC).as_posix()] = f"/static/dist/{name}"
    for old in ASSET_OUT.iterdir():  # drop previous builds
        if f"/static/dist/{old.name}" not in urls.values(): old.unlink(missing_ok=True)
    _assets.update(stamp=stamp, urls=urls)
    return urls
def asset(path: str) -> str: return build_assets()[path]  # in templates: {{ asset('css/app.css') }}
# ---------- templates ----------
TEMPLATE_GLOBALS = {"app_name": APP_NAME, "contact_email": CONTACT_EMAIL, "max_upload_mb": f"{MAX_UPLOAD_MB:g}",  # same for every page and request; add more site-wide values here
                    "geoapify_key": GEOAPIFY_API_KEY, "cycle_days": CYCLE_DAYS, "cycle_every": CYCLE_EVERY}  # the browser map key (public by nature: restrict it to your site's address at Geoapify); the pay cycle
def maps_link(lat: float | None, lng: float | None) -> str:  # {{ maps_link(c.location_lat, c.location_lng) }}: the spot on OpenStreetMap (no account or token needed)
    return f"https://www.openstreetmap.org/?{urlencode({'mlat': f'{lat:.6f}', 'mlon': f'{lng:.6f}'})}#map=17/{lat:.6f}/{lng:.6f}" if lat is not None and lng is not None else ""
def initials(name: str) -> str:  # "test_amy" -> "TA", "alex" -> "AL"
    parts = [p for p in re.split(r"[\W_]+", name) if p]
    return ((parts[0][0] + parts[1][0]) if len(parts) > 1 else name[:2]).upper()
def static(path: str) -> str: return "/static/" + path.lstrip("/")  # root-relative on purpose: url_for() builds absolute URLs from the Host header
def media_file(kind: str, key) -> Path:  # kind "u" (user id) / "c" (company id) -> its picture file; the name is an HMAC, nothing is stored in Postgres
    return MEDIA_DIR / (hmac.new(MEDIA_KEY, f"{kind}:{key}".encode(), "sha256").hexdigest()[:32] + ".webp")
def proof_file(record_id: int, kind: str = "proof") -> Path: return PROOF_DIR / (hmac.new(MEDIA_KEY, f"{kind}:{record_id}".encode(), "sha256").hexdigest()[:32] + ".webp")  # "proof" payout / "deposit"; nothing in Postgres
def id_file(kind: str, key) -> Path: return PROOF_DIR / (hmac.new(MEDIA_KEY, f"identity:{kind}:{key}".encode(), "sha256").hexdigest()[:32] + ".webp")  # "u" person / "c" company: their identity proof photo
def chat_dir(chat_id: int) -> Path: return UPLOAD_DIR / "chat" / hmac.new(MEDIA_KEY, f"chat-dir:{chat_id}".encode(), "sha256").hexdigest()[:24]  # files sent in one chat
def chat_file(chat_id: int, msg_id: int) -> Path: return chat_dir(chat_id) / hmac.new(MEDIA_KEY, f"chat-file:{chat_id}:{msg_id}".encode(), "sha256").hexdigest()[:32]
def remove_chat_files(chat_ids) -> None:  # chats deleted (a company's, an emptied one): the files sent in them go too
    for c in chat_ids: shutil.rmtree(chat_dir(c), ignore_errors=True)
def ai_dir(user_id: int) -> Path: return UPLOAD_DIR / "ai" / hmac.new(MEDIA_KEY, f"ai-dir:{user_id}".encode(), "sha256").hexdigest()[:24]  # pictures someone showed the Deminic AI
def ai_file(user_id: int, msg_id: int) -> Path: return ai_dir(user_id) / (hmac.new(MEDIA_KEY, f"ai-file:{user_id}:{msg_id}".encode(), "sha256").hexdigest()[:32] + ".webp")
def picture(kind: str, key) -> str | None:  # in templates: {{ picture('u', user_id) }} / {{ picture('c', company.id) }}; None = no picture yet
    p = media_file(kind, key)
    try: return f"/media/{p.name}?v={p.stat().st_mtime_ns // 1_000_000}"  # ?v= changes on every new upload, so caches never show an old one
    except OSError: return None
def tenure(since: datetime) -> str:  # naive-UTC timestamp -> "less than a day" / "12 days" / "3 months" / "1 year 2 months"; in templates: {{ m.joined_at|tenure }}
    days = (datetime.now(UTC).replace(tzinfo=None) - since).days
    years, months = divmod(days * 12 // 365, 12)
    if days < 1: return "less than a day"
    if not years and not months: return f"{days} day{'s' if days != 1 else ''}"
    return " ".join(p for p in (f"{years} year{'s' if years != 1 else ''}" if years else "", f"{months} month{'s' if months != 1 else ''}" if months else "") if p)
templates.env.globals.update(TEMPLATE_GLOBALS, csrf_input=csrf_input, get_flashed_messages=get_flashed_messages, asset=asset, initials=initials, static=static, picture=picture,
                             ai_status=lambda: AI_STATUS, maps_link=maps_link)  # {{ ai_status().ready }}: is the local AI (vLLM + model) available right now
templates.env.filters["tenure"] = tenure
import banks; banks.register(templates.env)  # {{ bank_line(b) }} + the bank-country picker (banks.py imports nothing from here)
def age(born) -> int:  # {{ user.dob|age }}: whole years old today
    today = datetime.now(UTC).date()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
templates.env.filters["age"] = age
def render(request: Request, template: str, context: dict | None = None, status_code: int = 200, **extra) -> HTMLResponse:
    """Render templates/<template> (".html" optional). Every page gets app_name (+ TEMPLATE_GLOBALS), request, user (Redis session
    or None), path and year, plus any custom values: render(request, "home", {"stats": stats}) or render(request, "home", stats=stats).
    Class/id names in the output are randomized to match the built CSS/JS, and every script/style tag gets the CSP nonce."""
    import i18n  # (imports this module: here, not at the top)
    name = template if template.endswith(".html") else template + ".html"
    open_ai = "session" in request.scope and request.session.pop("open_ai", False)  # the bar handed something to the Deminic AI: the page opens its chat (app.js)
    lang = i18n.lang_of(request)  # the page's language: its own compiled templates (i18n.py)
    ctx = {"request": request, "user": getattr(request.state, "user", None), "path": request.url.path, "year": datetime.now().year, "open_ai": open_ai, "lang": lang,
           "rtl": lang in i18n.RTL, "languages": i18n.LANGUAGES, **(context or {}), **extra}
    return HTMLResponse(add_nonce(obfuscate_html(i18n.env_for(lang).get_template(name).render(ctx)), request), status_code)
# ---------- login sessions (Redis "session:<sha256 of cookie>" -> {user_id, username, login_id}, written only after a successful login) ----------
# One login per user at a time: every login writes a fresh uuid4 login_id to users.login_id (Postgres) AND to the Redis session, and
# deletes the user's previous Redis session (indexed at "session_of:<user_id>"). Every request then checks the two ids match.
SESSION_COOKIE = "sid"
class LoginRequired(Exception): pass
def _session_key(sid: str) -> str: return "session:" + hashlib.sha256(sid.encode()).hexdigest()
def _session_index(user_id) -> str: return f"session_of:{user_id}"
async def start_session(request: Request, response: Response, user_id: int, username: str, login_id: uuid.UUID) -> None:
    """Call after the new login_id is committed to users.login_id. Cuts off this user's older session (any device) immediately:
    its data is wiped and replaced by a bare "revoked" marker, so that device can be told why on its next request."""
    if own := request.cookies.get(SESSION_COOKIE): await reddisdb.delete(_session_key(own))  # this browser's own old session
    if (other := await reddisdb.get(_session_index(user_id))) and other != (own and _session_key(own)):
        async with reddisdb.pipeline(transaction=True) as p:  # atomic: no moment where the old session still works
            await p.delete(other).hset(other, mapping={"revoked": "elsewhere"}).expire(other, SESSION_TTL).execute()
    sid = secrets.token_urlsafe(32)
    session_key = _session_key(sid)
    await reddisdb.hset(session_key, mapping={"user_id": user_id, "username": username, "login_id": str(login_id)})
    await reddisdb.expire(session_key, SESSION_TTL)
    await reddisdb.set(_session_index(user_id), session_key, ex=SESSION_TTL)
    response.set_cookie(SESSION_COOKIE, sid, max_age=SESSION_TTL, httponly=True, samesite="lax", secure=request.url.scheme == "https")
    request.session.pop("_csrf", None)  # fresh CSRF token after login
async def end_session(request: Request, response: Response) -> None:
    if sid := request.cookies.get(SESSION_COOKIE):
        key = _session_key(sid)
        if user_id := await reddisdb.hget(key, "user_id"):
            if await reddisdb.get(_session_index(user_id)) == key: await reddisdb.delete(_session_index(user_id))
        await reddisdb.delete(key)
    response.delete_cookie(SESSION_COOKIE)
async def load_user(request: Request) -> dict | None:
    """The logged-in user ({"user_id", "username", "companies"}) or None, cached on request.state.user. The Redis session is
    cross-checked against Postgres on every request: the account must still exist AND its login_id must equal the session's
    (compared in constant time), otherwise the session is destroyed on the spot and request.state.session_revoked says why."""
    if not hasattr(request.state, "user"):
        user, sid = None, request.cookies.get(SESSION_COOKIE)
        session: dict = (await reddisdb.hgetall(_session_key(sid)) if sid else None) or {}  # decode_responses=True: str keys/values
        if sid and session.get("revoked"):  # cut off by a newer login on another device
            await reddisdb.delete(_session_key(sid))
            request.state.session_revoked = session["revoked"]
        elif sid and session:
            from tables import account_summary  # imported here because tables.py imports this module
            try: summary = await account_summary(int(session["user_id"]))
            except (KeyError, ValueError): summary = None  # malformed session
            current, stored = str((summary or {}).get("login_id") or ""), str(session.get("login_id") or "")
            if summary and summary.get("banned"): summary, current = None, "banned"  # banned by the admin (routers/admin.py): out at once
            if summary and current and hmac.compare_digest(current.encode(), stored.encode()):  # picture / bare: for the "add a picture / logo" reminders
                user = {"user_id": session["user_id"], "username": summary["username"], "companies": summary["companies"], "unread": summary["unread"],
                        "picture": picture("u", session["user_id"]), "bare": tuple((cid, name) for cid, name, *_ in summary["top"] if not picture("c", cid)),
                        "unpaid": tuple((cid, name) for cid, name, paid, _ in summary["top"] if not paid),  # companies of yours with no minimum salaries yet
                        "unproved": tuple((cid, name) for cid, name, _, proved in summary["top"] if not proved),  # ... with no identity proof yet
                        "monitored": summary["monitored"], "bank": summary["bank"], "identity": summary["identity"],  # attendance tracked; bank + identity proof added
                        "chats": summary.get("chats", 0), "tasks": summary.get("tasks", 0), "live": summary.get("live", 0),  # unread chat messages, tasks to do, meetings on now (sidebar)
                        "region": summary.get("region"), "tz": summary.get("timezone"),  # times + money are shown in these (payroll.at / money)
                        "real": summary.get("real") or "", "friend_requests": summary.get("friend_requests", 0),  # the name on their identity proof (activity log); friend requests waiting
                        "lang": summary.get("language") or ""}  # the UI language they picked (i18n.py)
            else:
                await reddisdb.delete(_session_key(sid))
                request.state.session_revoked = "banned" if current == "banned" else "elsewhere" if summary else "deleted"
        request.state.user = user
    return request.state.user
async def require_login(request: HTTPConnection) -> None:
    """Global dependency (set in app.py): runs before every endpoint except @public ones, puts the Redis session in request.state.user.
    HTTPConnection, so it serves WebSocket routes too (routers/presence.py)."""
    user = await load_user(request)
    if wants_html(request) and request.scope["type"] == "http":
        import i18n
        await i18n.prepare(request)  # flash messages waiting for this page: into its language first
    if getattr(request.state, "session_revoked", None) == "elsewhere" and wants_html(request):
        flash(request, "You were signed out because your account was logged in on another device.", "info")
    if getattr(request.state, "session_revoked", None) == "banned" and wants_html(request):
        flash(request, f"You have been banned from {APP_NAME} by the admin: you were signed out. Contact the admin at {CONTACT_EMAIL}.", "error")
    if not user and not getattr(getattr(request.scope.get("route"), "endpoint", None), "public", False): raise LoginRequired()
# ---------- IP blocklist (blocked_ips.txt: one IP or CIDR range per line, "#" comments) ----------
# The txt file is parsed into hash sets only when it changes (each request costs one stat() to notice edits, no restart needed).
# Lookups are set membership: an exact IP is one O(1) check; CIDR ranges are grouped by prefix length, so a range check is one
# O(1) lookup per distinct prefix length in the file (at most 33 for IPv4), however many entries the file has.
_blocklist: dict = {"mtime": None, "count": 0, "ips": set(), "ranges": {}}  # ranges: {(ip version, prefix length): {network as int}}
def block_entry(text: str) -> str:
    """One IP or range the way the blocklist keeps it: ::ffff:1.2.3.4 is 1.2.3.4 written another way, a range of one address is that address. ValueError if it's neither."""
    net = ipaddress.ip_network(text.strip(), strict=False)
    if v4 := getattr(net.network_address, "ipv4_mapped", None): net = ipaddress.ip_network(f"{v4}/{max(net.prefixlen - 96, 0)}", strict=False)
    return str(net.network_address) if net.num_addresses == 1 else str(net)
def blocks(entry: str, ip: str) -> bool:  # does this blocklist entry (an IP or a range) cover that address?
    try: net, a = ipaddress.ip_network(block_entry(entry)), ipaddress.ip_address(ip)
    except ValueError: return False
    a = getattr(a, "ipv4_mapped", None) or a
    return a.version == net.version and a in net
def _load_blocklist() -> dict:
    try: mtime = BLOCKLIST_FILE.stat().st_mtime_ns
    except OSError:  # no file: nothing is blocked
        _blocklist.update(mtime=None, count=0, ips=set(), ranges={})
        return _blocklist
    if mtime != _blocklist["mtime"]:
        ips, ranges, count = set(), {}, 0
        with BLOCKLIST_FILE.open(encoding="utf-8-sig") as f:
            for n, line in enumerate(f, 1):
                if not (entry := line.split("#", 1)[0].strip()): continue
                try: net = ipaddress.ip_network(block_entry(entry))
                except ValueError:
                    print(f"{BLOCKLIST_FILE.name}:{n}: not an IP or CIDR range, ignored: {entry!r}")
                    continue
                count += 1
                if net.num_addresses == 1: ips.add(net.network_address)
                else: ranges.setdefault((net.version, net.prefixlen), set()).add(int(net.network_address))
        _blocklist.update(mtime=mtime, count=count, ips=ips, ranges=ranges)
    return _blocklist
def client_ip(request: Request) -> str: return request.client.host if request.client else ""  # behind a proxy, run uvicorn with --proxy-headers
ADMIN_COOKIE = "hx_admin"  # the admin console's session (routers/admin.py): a random token -> Redis "admin:<sha256>" = the IP it signed in from
def admin_key(token: str) -> str: return "admin:" + hashlib.sha256(token.encode()).hexdigest()
async def admin_session(request: Request) -> bool:  # this browser is signed in to the admin console (from that IP): login, signup + recovery are closed (decoraters.guest_only)
    token = request.cookies.get(ADMIN_COOKIE)
    return bool(token) and (await reddisdb.get(admin_key(token))) == client_ip(request)
_ban_lock = threading.Lock()
def blocked_list() -> list[tuple[str, str]]:  # (entry, its comment) for each IP / range in the file: the admin console's list
    try: lines = BLOCKLIST_FILE.read_text(encoding="utf-8-sig").splitlines()
    except OSError: return []
    return [(e.strip(), c.strip()) for e, _, c in (l.partition("#") for l in lines) if e.strip()]
def block_ip(ip: str, why: str) -> bool:
    """Add an IP or a range to the blocklist file (the admin console, or 5 wrong admin logins); it applies on the next request. False if it's listed already."""
    entry = block_entry(ip)  # ValueError if it's neither
    with _ban_lock:
        if any(e == entry for e, _ in blocked_list()): return False
        text = BLOCKLIST_FILE.read_text(encoding="utf-8-sig") if BLOCKLIST_FILE.exists() else ""
        why = " ".join(why.split()).replace("#", "")[:160]
        BLOCKLIST_FILE.write_text(text + ("" if not text or text.endswith("\n") else "\n") + f"{entry}  # {why} · {datetime.now():%Y-%m-%d %H:%M}\n", encoding="utf-8")
    return True
def unblock_ip(entry: str) -> bool:  # take one IP / range off the list (its line goes); False if it wasn't there
    with _ban_lock:
        if not BLOCKLIST_FILE.exists(): return False
        lines = BLOCKLIST_FILE.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        keep = [l for l in lines if l.partition("#")[0].strip() != entry.strip()]
        if len(keep) == len(lines): return False
        BLOCKLIST_FILE.write_text("".join(keep), encoding="utf-8")
    return True
# ---------- request rate limit: every page / API request (not static files), per logged-in user, else per IP ----------
RATE_LIMIT = os.getenv("RATE_LIMIT", "60/minute")  # "<count>/<second|minute|hour>"
class Limiter:
    """A sliding-window limiter in this process's memory (Redis holds login sessions only). limiter.limit(RATE_LIMIT) -> (count,
    seconds); limiter.hit(key, ...) -> how many seconds to wait (0 = allowed, and counted)."""
    UNITS = {"second": 1, "minute": 60, "hour": 3600}
    def __init__(self): self.hits: dict[str, deque] = {}
    def limit(self, rate: str) -> tuple[int, float]:
        n, _, unit = rate.partition("/")
        return int(n), float(self.UNITS[unit.strip().rstrip("s")])
    def hit(self, key: str, count: int, window: float) -> float:
        now, q = time.monotonic(), self.hits.setdefault(key, deque())
        while q and now - q[0] >= window: q.popleft()
        if len(q) >= count: return window - (now - q[0])
        q.append(now)
        if len(self.hits) > 50_000: self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < window}  # forget idle keys
        return 0.0
limiter = Limiter()
RATE, PICTURE_RATE = limiter.limit(RATE_LIMIT), (600, 60.0)  # shop pictures have their own allowance: one page of products is dozens of them, and they used to eat the page limit (a first look at three pages got refused)
_sent: dict[str, float] = {}  # forms with ONCE (form.py) just sent: key -> until when. In this process's memory, like the limits above
def first_send(key: str, window: float) -> bool:  # True = go on. No await in here: 8 copies of a form arriving together can't both get through
    now = time.monotonic()
    if len(_sent) > 20_000:
        for k in [k for k, until in _sent.items() if until < now]: del _sent[k]
    if _sent.get(key, 0.0) > now: return False
    _sent[key] = now + window
    return True
def ip_blocked(request: Request) -> bool:
    try: ip = ipaddress.ip_address(client_ip(request))
    except ValueError: return False
    ip = getattr(ip, "ipv4_mapped", None) or ip  # ::ffff:1.2.3.4 -> 1.2.3.4
    bl = _load_blocklist()
    if ip in bl["ips"]: return True  # exact IP: O(1)
    n, bits = int(ip), ip.max_prefixlen
    return any((n >> (bits - p) << (bits - p)) in nets for (v, p), nets in bl["ranges"].items() if v == ip.version)
# ---------- request size limit (every body, so every upload) ----------
class BodyLimit:
    """ASGI middleware: a body over MAX_UPLOAD_BYTES (by Content-Length, or counted while it streams in) raises HTTPException(413)
    when the app reads it, so the normal error handling flashes it on the form. After a rejection the unread rest of the body is
    drained (up to a cap) before the response goes out, so the browser actually receives the redirect instead of a dropped
    connection. Only then: other responses (e.g. static files) read receive() themselves to watch for disconnects."""
    def __init__(self, app, max_bytes: int = MAX_UPLOAD_BYTES): self.app, self.max_bytes = app, max_bytes
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http": return await self.app(scope, receive, send)
        declared = next((int(v) for k, v in scope["headers"] if k == b"content-length" and v.isdigit()), 0)
        state = {"read": 0, "done": False, "rejected": False}
        def too_big():
            state["rejected"] = True
            return HTTPException(413, f"File too large: uploads are limited to {MAX_UPLOAD_MB:g} MB.")
        async def limited_receive():
            if declared > self.max_bytes: raise too_big()
            msg = await receive()
            if msg["type"] == "http.request":
                state["read"] += len(msg.get("body", b""))
                state["done"] = not msg.get("more_body", False)
                if state["read"] > self.max_bytes: raise too_big()
            return msg
        async def drain_then_send(message):
            if message["type"] == "http.response.start" and state["rejected"] and not state["done"]:
                drained = 0
                while not state["done"] and drained < 4 * self.max_bytes:
                    msg = await receive()
                    if msg["type"] != "http.request": break
                    drained += len(msg.get("body", b""))
                    state["done"] = not msg.get("more_body", False)
            await send(message)
        await self.app(scope, limited_receive, drain_then_send)
# ---------- security headers (Talisman-style CSP: nonce-only, so a script/style runs only if it carries this request's nonce) ----------
MDB_STYLE_HASHES = ("'sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=' "  # the two <style> tags mdb.umd.min.js (9.3.0) injects itself:
                    "'sha256-3ITP0qhJJYBulKb1omgiT3qOK6k0iB3rMDhGfpM8b7c='")  # an empty one + its autofill-detection keyframes
YOUTUBE = "img-src 'self' data: https://i.ytimg.com; frame-src https://www.youtube-nocookie.com"  # chats: a YouTube link's thumbnail, and its player (privacy mode) once clicked
CSP = (f"default-src 'self'; script-src 'nonce-{{nonce}}'; style-src 'nonce-{{nonce}}' {MDB_STYLE_HASHES}; {YOUTUBE}; font-src 'self'; "
       "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
DOCS_CSP = ("default-src 'self'; script-src 'nonce-{nonce}'; "  # /docs and /redoc: their CDN + inline scripts carry the nonce too
            "style-src 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "  # ReDoc injects <style> at runtime
            "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; worker-src blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
DOCS_PATHS, API_DOCS = ("/docs", "/redoc"), os.getenv("API_DOCS", "") == "1"
# Pages with the location dialog: the same nonce-only policy (Leaflet is served from /static and loads with this request's nonce),
# plus exactly two Geoapify hosts: map tile images, and the address search API. Every other page keeps the strict policy above.
# Only when GEOAPIFY_API_KEY is set.
MAPS_CSP = (f"default-src 'self'; script-src 'nonce-{{nonce}}'; style-src 'nonce-{{nonce}}' {MDB_STYLE_HASHES}; img-src 'self' data: https://maps.geoapify.com https://i.ytimg.com; font-src 'self'; "
            "connect-src 'self' https://api.geoapify.com; frame-src https://www.youtube-nocookie.com; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
MAPS_PATHS = ("/companies/new", "/workplaces")
_NONCE_TARGETS = re.compile(r'<(?:script|style)\b(?![^>]*\snonce=)|<link\b(?=[^>]*\brel="stylesheet")(?![^>]*\snonce=)', re.I)
def add_nonce(html: str, request: Request) -> str:
    """Put this request's CSP nonce on every <script>, <style> and stylesheet <link>. Jinja autoescaping turns any "<" in page
    content into "&lt;", so only real tags from our templates can ever match (user input can't get a nonce)."""
    if not (nonce := getattr(request.state, "nonce", None)): return html
    return _NONCE_TARGETS.sub(lambda m: f'{m.group(0)} nonce="{nonce}"', html)
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                    "Referrer-Policy": "strict-origin-when-cross-origin", "Permissions-Policy": "camera=(), microphone=(), display-capture=(), geolocation=()"}
CALL_PAGE = re.compile(r"/meetings/\d+/room")  # the meeting room (routers/meetings.py) is the only page allowed the camera, microphone and screen capture
CALL_PERMISSIONS = "camera=(self), microphone=(self), display-capture=(self), geolocation=()"
def same_text(a: str, b: str) -> bool:  # compared in constant time, whatever the text: compare_digest itself raises on non-ASCII strings, so a junk token was a crash (the fuzz test)
    return hmac.compare_digest(a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass"))
def attachment(name: str) -> str:  # a Content-Disposition header for any file name: a header can only carry Latin-1, so a name in Hindi or Arabic was a crash. filename* (RFC 5987) holds the real one, filename a plain copy for old clients
    plain = re.sub(r"[^A-Za-z0-9 ._-]", "_", name).strip() or "download"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"
def safe_referer(request: Request, fallback: str = "/") -> str:
    """The Referer as a local path if it's a page on this site (same host and port), else fallback. Never sends anyone off-site."""
    try: ref = urlsplit(request.headers.get("referer", ""))
    except ValueError: return fallback  # "http://[bad": not an address at all
    if ref.netloc != request.url.netloc or not ref.path: return fallback
    return ref.path + (f"?{ref.query}" if ref.query else "")
def own_origin(ws) -> bool:  # a WebSocket opened by one of this site's own pages (no cross-site hijacking)
    try: return urlsplit(ws.headers.get("origin", "")).netloc == ws.headers.get("host")
    except ValueError: return False
def back(request: Request) -> RedirectResponse: return RedirectResponse(safe_referer(request, request.url.path), 303)
def wants_html(request: Request) -> bool: return "text/html" in request.headers.get("accept", "")
# ---------- error pages ----------
ERROR_MESSAGES = {  # used when an error has no specific message of its own ({path} and {method} are filled in)
    400: "The request couldn't be processed.", 401: "You need to log in to see this page.", 403: "You don't have access to this page.",
    404: "There's nothing at {path}. The link may be broken, or the page was moved.", 405: "{method} isn't allowed on {path}.",
    413: "That upload is too large.", 422: "The request had missing or invalid data.", 429: "Too many requests. Wait a moment and try again.",
    500: "Something broke on our side. Try again in a moment.", 503: "A service this page depends on is down. Try again in a moment.",
}
async def error_page(request: Request, status: int, message: str | None = None) -> Response:
    """Render templates/error.html with {"errmessage": msg, "status", "title", "back_url"}. back_url is the Referer when it's one of
    our pages (and not this same failing URL), else "/". Falls back to plain text if the page itself can't be rendered."""
    try: phrase = HTTPStatus(status).phrase
    except ValueError: phrase = "Error"
    msg = message if message and message != phrase else ERROR_MESSAGES.get(status, phrase).format(path=request.url.path, method=request.method)
    back_url = safe_referer(request)
    if back_url == request.url.path: back_url = "/"
    with contextlib.suppress(Exception): await load_user(request)  # so the navbar shows the right user even on 404s
    try: return render(request, "error", {"errmessage": msg, "status": status, "title": phrase.lower(), "back_url": back_url}, status_code=status)
    except Exception: return PlainTextResponse(f"{status} {phrase}: {msg}", status)
def setup_system(app: FastAPI) -> None:
    """Static files, sessions, security headers and error handling: browsers get the error page (or, for a form posted from one of
    our pages, a flash message back on that form); API clients get JSON."""
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")  # a mount, so no login needed (CSS/JS for the login page)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")  # only re-encoded .webp pictures ever land here (media.py)
    # middleware runs outermost-last-added: security headers -> IP blocklist -> trusted hosts -> session guard -> sessions -> body size limit -> app
    app.add_middleware(BodyLimit)
    app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
    @app.middleware("http")
    async def _session_guard(request: Request, call_next):
        """Runs on every page/API request right after the IP blocklist and before anything renders: loads the session and checks
        its login_id against Postgres (one login per user; see load_user). Static files skip it (no database hit for CSS/JS)."""
        if "\x00" in request.url.path or b"%00" in request.scope.get("query_string", b""): return PlainTextResponse("Bad request.", 400)  # a NUL byte: never valid here, and Postgres can't store it (a crash otherwise)
        quiet = request.url.path.startswith(("/static/", "/media/", "/shop/media/"))  # files: no session check, no activity log line
        started, ip = time.monotonic(), client_ip(request)
        if not request.url.path.startswith(("/static/", "/media/")):
            with contextlib.suppress(redis.RedisError, OperationalError, PoolTimeout, OSError): await load_user(request)  # down or swamped: the route retries -> 503 page
            user = getattr(request.state, "user", None)  # the rate limit: per user id when logged in, per IP when not
            who = f"user:{user['user_id']}" if user else f"ip:{ip}"
            if wait := limiter.hit(f"pictures:{who}", *PICTURE_RATE) if quiet else limiter.hit(who, *RATE):
                why = f"Too many requests: at most {RATE_LIMIT.replace('/', ' a ')}. Try again in {int(wait) + 1} seconds."
                resp = await error_page(request, 429, why) if wants_html(request) else JSONResponse({"detail": why}, 429)
                resp.headers["Retry-After"] = str(int(wait) + 1)
                audit.log(user, audit.section(request.url.path), f"{request.method} {audit.clean_path(request.url.path, request.url.query)}", "429 rate-limited", ip)
                abuse.record("rate-limit", user and int(user["user_id"]), ip, f"{request.method} {request.url.path}"[:300], every=60)  # the admin console's repeat offenders
                abuse.refused(ip, user and user["user_id"], request.url.path); abuse.seen(ip, user and user["user_id"], request.url.path, True)  # enough refusals = a limiter overload: every incoming IP is logged then
                return resp
        abuse.seen(ip, (getattr(request.state, "user", None) or {}).get("user_id"), request.url.path)  # (a no-op unless the limiter is overloaded; static files count too)
        import changelog  # (imports this module: here, not at the top)
        held, status = changelog.begin(getattr(request.state, "user", None)), 500  # the change logs: what this request changes is written once it has succeeded
        try:
            resp = await call_next(request); status = resp.status_code
        finally: await changelog.finish(held, request, status)
        if getattr(request.state, "session_revoked", None): resp.delete_cookie(SESSION_COOKIE)  # the dead session's cookie goes too
        if not quiet:  # the activity log (audit.py): who did what, where, how it went
            endpoint = getattr(request.scope.get("endpoint"), "__name__", "")
            audit.log(getattr(request.state, "user", None), audit.section(request.url.path), f"{request.method} {audit.clean_path(request.url.path, request.url.query)}" + (f" | {endpoint}" if endpoint else ""),
                      str(resp.status_code), client_ip(request), (time.monotonic() - started) * 1000)
        return resp
    # a request whose Host header isn't in ALLOWED_HOSTS gets a 400 before the session check or any route (no Host-header cache poisoning)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    print(f"IP blocklist: {_load_blocklist()['count']} entries from {BLOCKLIST_FILE}")
    print(f"Assets built ({'classes randomized' if OBFUSCATE_ASSETS else 'readable class names'}): {', '.join(build_assets().values())}")
    print(f"Verification codes: email via {delivery_modes()['email']}, SMS via {delivery_modes()['sms']}" + (" (console = printed here, dev only)" if "console" in delivery_modes().values() else ""))
    @app.middleware("http")
    async def _ip_blocklist(request: Request, call_next):  # every request; a blocked IP only ever gets /blocked (and the CSS/JS it uses)
        if request.url.path == "/blocked" or request.url.path.startswith("/static/") or not ip_blocked(request): return await call_next(request)
        return RedirectResponse("/blocked", 303) if wants_html(request) else JSONResponse({"detail": "Your IP address is blocked"}, 403)
    async def _blocked_page(request: Request):
        if not ip_blocked(request): return RedirectResponse("/", 303)
        return render(request, "blocked", {"ip": client_ip(request)}, status_code=403)
    _blocked_page.public = True  # same as @public (decoraters.py imports this module, so it can't be imported here)
    app.add_api_route("/blocked", _blocked_page, methods=["GET"], include_in_schema=False)
    # API docs: off unless API_DOCS=1 in .env (they'd give anyone a map of every route, the admin's included). FastAPI's own pages, re-served so their <script>/<link> tags get the nonce too (app.py turns the built-in ones off)
    async def _swagger(request: Request): return HTMLResponse(add_nonce(get_swagger_ui_html(openapi_url=app.openapi_url, title=f"{app.title} · docs").body.decode(), request))
    async def _redoc(request: Request): return HTMLResponse(add_nonce(get_redoc_html(openapi_url=app.openapi_url, title=f"{app.title} · redoc").body.decode(), request))
    for path, page in zip(DOCS_PATHS, (_swagger, _redoc)) if API_DOCS else ():
        page.public = True
        app.add_api_route(path, page, methods=["GET"], include_in_schema=False)
    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        request.state.nonce = secrets.token_urlsafe(16)
        resp = await call_next(request)
        policy = DOCS_CSP if request.url.path in DOCS_PATHS else MAPS_CSP if GEOAPIFY_API_KEY and request.url.path in MAPS_PATHS else CSP
        resp.headers["Content-Security-Policy"] = policy.format(nonce=request.state.nonce)
        resp.headers.update(SECURITY_HEADERS)
        if CALL_PAGE.fullmatch(request.url.path): resp.headers["Permissions-Policy"] = CALL_PERMISSIONS
        if request.url.scheme == "https": resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path.startswith(("/static/dist/", "/media/")) and resp.status_code == 200: resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # hashed / versioned names
        return resp
    async def _login_required(request: Request, exc: Exception):
        if not wants_html(request): return JSONResponse({"detail": "Not logged in"}, 401)
        q = request.url.query
        if request.method == "GET": request.session["_next"] = request.url.path + (f"?{q}" if q and len(q) <= 500 else "")  # come back here after logging in, with the page's search or filter
        return await error_page(request, 401)
    async def _error(request: Request, exc: Exception):  # FormError, RequestValidationError, HTTPException (incl. 404/405 from routing)
        import changelog
        changelog.refused(type(exc).__name__)  # turned down: nothing it held goes into the change logs
        _sent.pop(getattr(request.state, "sent_key", ""), None)  # a refused form can be sent again at once, corrected or not
        if not wants_html(request):
            if isinstance(exc, RequestValidationError): return await request_validation_exception_handler(request, exc)
            if isinstance(exc, HTTPException): return await http_exception_handler(request, exc)
            return JSONResponse({"detail": str(exc)}, 400)
        status = exc.status_code if isinstance(exc, HTTPException) else 422 if isinstance(exc, RequestValidationError) else 400
        if isinstance(exc, RequestValidationError):  # field name = last non-index part of loc (list items report ("positions", 0)); whole-form checks have none
            field = lambda e: next((f"{p}: " for p in reversed(e["loc"]) if not isinstance(p, int)), "")
            msgs = [field(e) + e["msg"].removeprefix("Value error, ") for e in exc.errors()]
        else: msgs = [str(exc.detail if isinstance(exc, HTTPException) else exc)]
        if request.method != "GET" and status < 500 and safe_referer(request, ""):  # a form posted from one of our pages: flash on that form
            for m in msgs: flash(request, m)
            return back(request)
        resp = await error_page(request, status, "; ".join(msgs))
        resp.headers.update(getattr(exc, "headers", None) or {})  # e.g. Allow on 405
        return resp
    async def _unavailable(request: Request, exc: Exception):  # Redis / Postgres down, or the server swamped
        if request.scope["type"] == "websocket":  # (the same on a live socket) it can't be sent a page: closed properly, 1013 = try again later, and app.js reconnects by itself
            with contextlib.suppress(RuntimeError, OSError, WebSocketDisconnect): await request.close(code=1013)
            return None
        resp = await error_page(request, 503) if wants_html(request) else JSONResponse({"detail": "Service unavailable"}, 503)
        resp.headers["Retry-After"] = "30"
        return resp
    async def _crash(request: Request, exc: Exception):  # anything unhandled; Starlette still logs the traceback
        return await error_page(request, 500) if wants_html(request) else JSONResponse({"detail": "Internal Server Error"}, 500)
    async def _twice(request: Request, exc: IntegrityError):  # a unique rule of the database said no: the same thing sent twice at once (a double click), and the first one won. Other broken rules stay real errors
        if "UniqueViolation" not in type(exc.orig).__name__: raise exc
        return await _error(request, FormError("That was just done: there's no need to do it twice."))
    async def _refused(request: Request, exc: DBAPIError):  # what the database itself turned down
        kinds = {c.__name__ for c in type(exc.orig.__cause__).__mro__} if exc.orig is not None and exc.orig.__cause__ else set()
        if "DataError" in kinds: return await _error(request, HTTPException(404, "There's no such thing here.") if request.method == "GET" else FormError("One of those values is out of range: check it and try again."))  # a value it can't even hold (a made-up id of 99999999999): nothing can match it
        if kinds & {"LockNotAvailableError", "QueryCanceledError"}: return await _unavailable(request, exc)  # a lock or a query timed out, the server is that busy: try again, not a crash
        raise exc
    app.add_exception_handler(LoginRequired, _login_required)
    app.add_exception_handler(IntegrityError, _twice)
    app.add_exception_handler(DBAPIError, _refused)
    for exc_type in (FormError, RequestValidationError, HTTPException): app.add_exception_handler(exc_type, _error)
    for exc_type in (redis.RedisError, OperationalError, PoolTimeout, ConnectionError): app.add_exception_handler(exc_type, _unavailable)  # (PoolTimeout: every database connection busy for 30 s: the load test at 500 people)
    app.add_exception_handler(Exception, _crash)
# ---------- antivirus (ClamAV clamd, INSTREAM protocol; fails closed) ----------
async def scan_file(file: UploadFile) -> None:
    """Stream the upload to clamd. Raises FormError if infected or if the scan can't run."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(CLAMD_HOST, CLAMD_PORT), 5)
        try:
            writer.write(b"zINSTREAM\0")
            await file.seek(0)
            while chunk := await file.read(64 * 1024):
                writer.write(len(chunk).to_bytes(4, "big") + chunk)
                await writer.drain()
            writer.write(b"\0\0\0\0")
            await writer.drain()
            result = (await asyncio.wait_for(reader.read(), 120)).rstrip(b"\0").decode()
        finally: writer.close()
    except (OSError, asyncio.TimeoutError):
        raise FormError("Virus scanner is unavailable or the file is too large to scan. Please try again.")
    await file.seek(0)
    if result.endswith("FOUND"): raise FormError(f"Upload rejected: virus detected ({result.removeprefix('stream: ').removesuffix(' FOUND')}).")
    if not result.endswith("OK"): raise FormError(f"Could not scan the file ({result}).")
# ---------- saving uploads (disk only) ----------
MAX_NAME = 100
async def save_file(file: UploadFile) -> str:
    """Save the upload to UPLOAD_DIR and return the exact file name it was saved as. Raises FormError (flash + redirect back) on failure."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", file.filename or "file")  # no path parts or chars Windows rejects
    stem, dot, ext = name.rpartition(".")
    if len(name) > MAX_NAME: name = f"{stem[:MAX_NAME - len(ext[:20]) - 1]}.{ext[:20]}" if dot else name[:MAX_NAME]  # a long name is cut, its extension stays: a disk takes 255 characters at most, and the random prefix counts
    name = uuid.uuid4().hex + "_" + name
    try:
        UPLOAD_DIR.mkdir(exist_ok=True)
        await file.seek(0)
        with (UPLOAD_DIR / name).open("wb") as out: await run_in_threadpool(shutil.copyfileobj, file.file, out)
    except OSError:
        with contextlib.suppress(OSError): (UPLOAD_DIR / name).unlink(missing_ok=True)  # don't leave a half-written file
        raise FormError("File cannot be saved. Please try again.") from None
    return name
