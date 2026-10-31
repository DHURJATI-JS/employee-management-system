"""The activity log: one plain-text file per day in logs/ (logs/2026-10-30.txt), one line per thing done, like
  2026-10-30 18:40:12.345 +0530 | user 12 alex (Alex Morgan) | 127.0.0.1 | shop | POST /shop/checkout | checkout | 303 | 84 ms
when (the server's clock, with its UTC offset), who (user id, username, and the name on their identity proof once they've added one;
"guest" before logging in, "system" for background jobs), from where (IP), which section, what (method + path + the route's name), the
outcome and how long it took. Every page and API call (system._session_guard), log-ins, sockets (presence, meetings) and what background
jobs do on their own (the AI giving tasks, payments settling, meetings ending). Secrets never get in: no request bodies, no cookies, the
values of token / code / password / secret / key query parameters redacted, long random path segments (delivery links) shortened.
Written by one background task (run(), started by app.py), so a request never waits on the disk."""
import asyncio, os, re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode
LOG_DIR = Path(__file__).parent / os.getenv("AUDIT_LOG_DIR", "logs")
SECRET_KEYS = re.compile(r"(?i)token|code|pass|secret|key|signature|otp")
RANDOM_PART = re.compile(r"/[A-Za-z0-9_-]{24,}(?=/|$)")  # a delivery link's token etc.; uuids (36 chars with hyphens) stay readable
UUID = re.compile(r"^/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_lines: list[str] = []
_wake = asyncio.Event()
def who(user: dict | None) -> str:  # "user 12 alex (Alex Morgan)" / "guest"
    if not user: return "guest"
    real = (user.get("real") or "").strip()
    return f"user {user.get('user_id')} {user.get('username', '?')}" + (f" ({real})" if real else "")
def clean_path(path: str, query: str = "") -> str:
    path = RANDOM_PART.sub(lambda m: m.group() if UUID.match(m.group()) else m.group()[:9] + "…", path)
    if query: path += "?" + urlencode([(k, "…" if SECRET_KEYS.search(k) else v[:60]) for k, v in parse_qsl(query, keep_blank_values=True)][:12])
    return path[:300]
def section(path: str) -> str:  # the app's area, from the path: /companies/<id>/shop/... -> shop, /shop/... -> shop, /chats -> chats ...
    parts = [p for p in path.split("/") if p]
    if not parts: return "home"
    if parts[0] == "companies" and len(parts) >= 3 and parts[2] in ("shop", "tasks", "dashboard", "meetings", "members", "leave-requests", "payouts", "deposits", "bank"): return {"dashboard": "company"}.get(parts[2], parts[2])
    return {"ws": f"socket:{parts[1] if len(parts) > 1 else ''}", "register": "auth", "login": "auth", "logout": "auth"}.get(parts[0], parts[0])
def log(user: dict | None | str, where: str, what: str, outcome: str = "", ip: str = "", ms: float | None = None) -> None:
    """Queue one line (the writer task appends it to today's file). user: the session's user dict, or "system"."""
    now = datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond // 1000:03d} " + now.strftime("%z")
    person = user if isinstance(user, str) else who(user)
    fields = [stamp, person, ip or "-", where, what.replace("\n", " ")[:400]] + ([outcome] if outcome else []) + ([f"{ms:.0f} ms"] if ms is not None else [])
    _lines.append(" | ".join(fields))
    if len(_lines) > 50_000: del _lines[:10_000]  # the disk is gone for long: keep the newest
    _wake.set()
def _write(lines: list[str]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[str]] = {}
    for line in lines: by_day.setdefault(line[:10], []).append(line)
    for day, rows in by_day.items():
        with (LOG_DIR / f"{day}.txt").open("a", encoding="utf-8") as f: f.write("\n".join(rows) + "\n")
async def run() -> None:  # started by app.py's lifespan: appends queued lines every half second
    while True:
        await _wake.wait(); _wake.clear()
        await asyncio.sleep(0.5)
        batch, _lines[:] = list(_lines), []
        if not batch: continue
        try: await asyncio.to_thread(_write, batch)
        except OSError as e: _lines[:0] = batch; print(f"activity log: {e}")  # kept for the next try
def flush() -> None:  # at shutdown: whatever is still queued
    if _lines: _write(list(_lines)); _lines.clear()
