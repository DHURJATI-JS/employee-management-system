"""The outside services' keys (user asked: everything depends on the key, and on the key working). They live in .env, which git never sees, and reach the app through here: read from the
file whenever it changes, so a new key works at once (load_dotenv keeps what the server started with), with os.environ behind it for tests. The admin console pastes a key, the service
is tried with it, and only a working key is saved (routers/admin.py). Nothing ever shows a key again: only whether it's set."""
import os
from pathlib import Path
from dotenv import dotenv_values, set_key
ENV_FILE = Path(__file__).parent / ".env"
_seen: dict = {"mtime": None, "values": {}}
def _file() -> dict:
    try: mtime = ENV_FILE.stat().st_mtime_ns
    except OSError: return {}
    if mtime != _seen["mtime"]: _seen.update(mtime=mtime, values=dotenv_values(ENV_FILE, interpolate=False))
    return _seen["values"]
def key(name: str) -> str: return str(_file().get(name) or os.getenv(name, "")).strip()
def save_key(name: str, value: str) -> None:
    set_key(ENV_FILE, name, value, quote_mode="always")
    os.environ[name] = value
    _seen["mtime"] = None
def cashfree_pg_url(live: bool | None = None) -> str:  # payments: real or sandbox, as .env says unless asked
    live = key("CASHFREE_PG_ENV") == "production" if live is None else live
    return (key("CASHFREE_PG_URL") or ("https://api.cashfree.com/pg" if live else "https://sandbox.cashfree.com/pg")).rstrip("/")
def cashfree_check_url(live: bool | None = None) -> str:  # bank + ID checks
    live = key("CASHFREE_ENV") != "sandbox" if live is None else live
    return (key("CASHFREE_BASE_URL") or ("https://api.cashfree.com" if live else "https://sandbox.cashfree.com") + "/verification").rstrip("/")
