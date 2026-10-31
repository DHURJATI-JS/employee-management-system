"""/ws/presence: one WebSocket per open tab of a logged-in user (app.js). Messages are tiny JSON: {"t": "state", "active": bool} when the
tab starts / stops being used, and {"t": "watch", "company": id} from a company dashboard (members only). The hub (presence.py) does
the rest. Checked before the socket is accepted: a valid login session (the same Redis + Postgres check as every page), the IP
blocklist, and the Origin header (only this site's own pages can open it: no cross-site WebSocket hijacking). Oversized or too
frequent messages close it."""
import json, time
from fastapi import APIRouter, WebSocket
from decoraters import public
from presence import hub
from system import SESSION_COOKIE, _session_key, client_ip, ip_blocked, own_origin
import audit
router = APIRouter(tags=["presence"])
MAX_MESSAGE, MAX_PER_MINUTE = 200, 60  # the page sends a state change at most every few seconds + a heartbeat every 30 s
@router.websocket("/ws/presence")
@public  # require_login still runs and loads the session; without one the socket is refused here (close code 4401)
async def presence_socket(ws: WebSocket):
    user = getattr(ws.state, "user", None)
    if not user or ip_blocked(ws) or not own_origin(ws):
        return await ws.close(code=4401 if not user else 4403)
    uid, recent = int(user["user_id"]), []
    await ws.accept()
    audit.log(user, "socket:presence", "connected", ip=client_ip(ws))
    try:
        await hub.join(ws, uid, _session_key(ws.cookies.get(SESSION_COOKIE, "")))
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect": break
            now = time.monotonic()
            recent = [t for t in recent if now - t < 60] + [now]
            raw = message.get("text") or ""
            if not raw or len(raw) > MAX_MESSAGE or len(recent) > MAX_PER_MINUTE: return await ws.close(code=4429)
            try: msg = json.loads(raw)
            except ValueError: continue
            if not isinstance(msg, dict): continue
            if msg.get("t") == "state":
                if (on := msg.get("active") is True) != hub.sockets.get(uid, {}).get(ws): audit.log(user, "socket:presence", "in use" if on else "idle", "other apps (system-wide)" if msg.get("system") is True else "this tab", client_ip(ws))
                await hub.set_active(ws, uid, on)
            elif msg.get("t") == "watch": await hub.watch(ws, uid, msg.get("company"))
    finally:
        await hub.leave(ws, uid)
        audit.log(user, "socket:presence", "disconnected", ip=client_ip(ws))
