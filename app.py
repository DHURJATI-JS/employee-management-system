import asyncio, contextlib, importlib, importlib.metadata, os, pkgutil, re, socket, subprocess, sys
from contextlib import asynccontextmanager
HERE = os.path.dirname(os.path.abspath(__file__))
def run_setup(*flags: str) -> int: return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(HERE, "setup_services.ps1"), *flags], cwd=HERE).returncode
def has_package(name: str) -> bool:
    try: importlib.metadata.version(name); return True
    except importlib.metadata.PackageNotFoundError: return False
def missing_packages() -> list[str]:  # what myapp/pyproject.toml asks for and this Python doesn't have. Standard library only: it runs before anything else can be imported
    try: import tomllib
    except ModuleNotFoundError: return ["a newer Python"]
    with open(os.path.join(HERE, "myapp", "pyproject.toml"), "rb") as f: return [n for n in (re.match(r"[\w.-]+", d)[0] for d in tomllib.load(f)["project"]["dependencies"]) if not has_package(n)]
if __name__ == "__main__":  # one click: a fresh copy, or the wrong Python. setup_services.ps1 installs what's missing and runs the app itself. Only here: uvicorn's reloader imports this file too, and it mustn't check on every reload
    gone, ours = missing_packages(), os.path.normcase(sys.prefix) == os.path.normcase(os.path.join(HERE, "myapp", ".venv"))
    if gone and ours and os.getenv("DEMINIC_SYNCED"): sys.exit(f"Still missing after installing them: {', '.join(gone)}")
    if gone or not os.path.exists(os.path.join(HERE, ".env")): sys.exit(run_setup("-Start", *(["-Sync"] if gone and ours else [])))  # (-Sync only for our own environment: the wrong Python doesn't need it rebuilt)
from fastapi import APIRouter, Depends, FastAPI, Request
import redis as redis_sync
from sqlalchemy import text
import uvicorn
import routers
from decoraters import cache, public
from system import API_DOCS, APP_NAME, CLAMD_HOST, CLAMD_PORT, DATABASE_URL, REDIS_PASSWORD, REDIS_URL, csrf_token, reddisdb, render, require_login, setup_system
from tables import MESSAGE_RETENTION_DAYS, Session, engine, purge_expired_messages, setup_database, single_company_mode
from assist import backfill_embeddings, seed_examples
from grow import check_ai, watch_ai
from presence import hub
import guard
import meet
import abuse, audit, changelog, companyai, i18n, shop, shopai
async def conn_scalars(sql: str) -> list:
    async with engine.connect() as conn: return list((await conn.execute(text(sql))).scalars().all())
from system import AI_ASSIST, AI_STATUS
async def maintenance() -> None:  # hourly: erase deleted accounts' message history + change logs once their retention period is over
    while True:
        try:
            if n := await purge_expired_messages(): print(f"Purged {n} archived messages of deleted accounts (past {MESSAGE_RETENTION_DAYS} days)")
            if n := await asyncio.to_thread(changelog.purge): print(f"Erased {n} change logs of deleted accounts / companies (past {changelog.KEEP_DAYS} days)")
        except Exception as e: print(f"maintenance: {type(e).__name__}: {e}")  # e.g. Postgres briefly down: try again next hour
        await asyncio.sleep(3600)
async def site_switches() -> None:  # the admin's switches (single company mode), re-read every 10 s: the templates' copy stays right even if another process flipped one
    while True:
        await asyncio.sleep(10)
        with contextlib.suppress(Exception):
            async with Session() as db: await single_company_mode(db, lock=False)
@asynccontextmanager
async def lifespan(app: FastAPI):
    await setup_database()  # as the admin: tables, migrations, least-privilege app role (fails fast if Postgres is down)
    async with engine.connect() as conn: await conn.execute(text("SELECT 1"))  # and the app role can actually log in
    print(f"Instruction bar: {await seed_examples()} example phrasings embedded in pgvector; engine = {AI_ASSIST}")
    print(f"Company search: {await backfill_embeddings()} companies/profiles given a vector")
    AI_STATUS.update(await asyncio.to_thread(check_ai))  # optional: down = a red light on the sidebar's AI finder link + AI reviews disabled, everything else works
    print(f"AI reviews: {AI_STATUS['detail']}")
    watcher = asyncio.create_task(watch_ai())  # re-checked every 15 s
    upkeep = asyncio.create_task(maintenance())
    presence = asyncio.create_task(hub.run())  # live presence + attendance credits (presence.py)
    payroll_guard = asyncio.create_task(guard.run())  # liability warnings, halts, payday reminders (guard.py)
    meet.WHISPER.update(await asyncio.to_thread(meet.check_whisper))  # optional: down = meetings without captions
    print(f"Meeting captions: {meet.WHISPER['detail']}")
    meetings, meeting_ai = asyncio.create_task(meet.rooms.run()), asyncio.create_task(meet.ai.run())  # rooms, reminders, captions' English + notes (meet.py)
    shop_clock, activity = asyncio.create_task(shopai.run()), asyncio.create_task(audit.run())  # payments, the AI's shop tasks, competitor tracking (shopai.py); the activity log (audit.py)
    changes = asyncio.create_task(changelog.run())  # every change, per company + per person, encrypted (changelog.py)
    abuses = asyncio.create_task(abuse.run())  # spam strikes / mutes / rate limits / wrong admin logins -> Postgres, for the admin console (abuse.py)
    in_use = {p.stem for p in i18n.LOCALES.glob("*.json")} | set((await conn_scalars("SELECT DISTINCT language FROM users WHERE language IS NOT NULL")))
    print(f"Languages: {', '.join(sorted(i18n.LANGUAGES))} (translator {i18n.TRANSLATOR}); {sum(i18n.queue_all(l) for l in in_use if l in i18n.LANGUAGES)} strings queued for {', '.join(sorted(in_use)) or 'none yet'}")
    translator = asyncio.create_task(i18n.run())  # the pages' words in other languages, in the background (i18n.py + the libretranslate container)
    async with Session() as db: print(f"Single company mode: {'on' if await single_company_mode(db, lock=False) else 'off'} (the admin console's switch)")
    switches = asyncio.create_task(site_switches())
    promotions = asyncio.create_task(companyai.run())  # the company AI's daily promotion reviews, for companies that turned them on (companyai.py)
    print(f"Shop: payments {shop.pay_mode()}" + (" (TEST MODE: no money moves; set PAYMENTS=cashfree for real payments)" if shop.pay_mode() == "test" else "") + f", platform commission {shop.COMMISSION_PERCENT}%")
    print(f"Activity log: {audit.LOG_DIR} (one .txt file per day) · change logs: {changelog.CHANGE_DIR} (encrypted, per company + per person)")
    yield
    watcher.cancel(); upkeep.cancel(); presence.cancel(); payroll_guard.cancel(); meetings.cancel(); meeting_ai.cancel(); shop_clock.cancel(); activity.cancel(); changes.cancel(); abuses.cancel(); translator.cancel(); switches.cancel(); promotions.cancel()
    with contextlib.suppress(Exception): audit.flush()
    with contextlib.suppress(Exception): changelog.flush()
    with contextlib.suppress(Exception): hub.credit(); await hub.flush()  # attendance not written yet
    await reddisdb.aclose()
    await engine.dispose()
app = FastAPI(title=APP_NAME, lifespan=lifespan, dependencies=[Depends(require_login)], docs_url=None, redoc_url=None, openapi_url="/openapi.json" if API_DOCS else None)  # every endpoint checks the login, except @public ones; /docs + /redoc only with API_DOCS=1 (setup_system re-serves them with CSP nonces)
setup_system(app)
for m in pkgutil.iter_modules(routers.__path__):  # auto-include every routers/*.py that defines `router`
    if isinstance(r := getattr(importlib.import_module(f"routers.{m.name}"), "router", None), APIRouter): app.include_router(r)
@app.get("/")
@public
@cache.memoize(timeout=60)
async def home(request: Request): return render(request, "home")  # public intro page
@app.get("/csrf")
@public
async def csrf(request: Request): return {"csrf_token": csrf_token(request)}  # for clients without an HTML form page
def services_up() -> bool:
    try:
        for addr in ((CLAMD_HOST, CLAMD_PORT), (DATABASE_URL.host, DATABASE_URL.port)): socket.create_connection(addr, timeout=1).close()
        return bool(redis_sync.from_url(REDIS_URL, password=REDIS_PASSWORD, socket_connect_timeout=1).ping())
    except (redis_sync.RedisError, OSError): return False
def ensure_services() -> bool:
    if services_up(): return True
    print("Redis/ClamAV/Postgres not reachable, running setup_services.ps1...")
    return run_setup() == 0
if __name__ == "__main__":
    if ensure_services(): uvicorn.run("app:app", host="127.0.0.1", port=int(os.getenv("PORT", "8000")), reload=True, timeout_keep_alive=30)  # (the default 5 s closes connections just as a browser reuses them: the load test saw those resets)
    else: print("Service setup failed, exiting.")
