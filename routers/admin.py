"""The admin console (user asked). Signs in with the user id + password in .env (ADMIN_USERNAME / ADMIN_PASSWORD), only while logged out
of every account; TRIES wrong tries from one IP address and that address is banned from the whole site (the form says so, with the
tries left). Its session: a random cookie -> Redis, ADMIN_TTL, that IP only; while it's live, that browser can't log in, sign up or recover
an account (decoraters.guest_only). The admin sees the server's resources, the
repeat offenders of the spam + rate limits (abuse.py) with the local AI's warnings, and can: ban an account with a clear reason (it can't
log in, the login page says why; it's out of every company and its own companies are deleted, as when an account is deleted, but its
chats stay), lift a ban, block / unblock IP addresses, and delete a company with a clear reason (its members are told why). Every admin
action is in the activity log (audit.py) as "admin" and in the change logs of whoever it touched."""
import asyncio, csv, hmac, io, os, secrets, shutil, time
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
import abuse, audit, changelog
from decoraters import public
from form import ActionForm, AdminIPForm, AdminLoginForm, AdminReasonForm, AdminSwitchForm
from presence import hub
from routers.companies import erase_company
from system import (ADMIN_COOKIE, ADMIN_PASSWORD, ADMIN_USERNAME, APP_NAME, CONTACT_EMAIL, ROOT, UPLOAD_DIR, FormError, admin_key, admin_session, block_entry, block_ip, blocked_list, blocks, client_ip, flash,
                    reddisdb, render, send_email, templates, unblock_ip)
from tables import SITE, AbuseEvent, AIWarning, Company, Friendship, Membership, OverloadIP, SiteSetting, User, get_db, left_company, single_company_mode, utcnow
router = APIRouter(tags=["admin"])
COOKIE, ADMIN_TTL, TRIES = ADMIN_COOKIE, 7200, 5
def _bytes(n) -> str:  # {{ n|bytes }}: 1.4 GB
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB": return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
def _span(seconds) -> str: s = int(seconds or 0); return f"{s // 86400}d {s % 86400 // 3600}h" if s >= 86400 else f"{s // 3600}h {s % 3600 // 60}m"  # {{ s|span }}: 2h 14m
templates.env.filters.update(bytes=_bytes, span=_span)
def configured() -> bool: return bool(ADMIN_USERNAME and ADMIN_PASSWORD)
_key = admin_key
async def is_admin(request: Request) -> bool: return not getattr(request.state, "user", None) and await admin_session(request)  # a live admin session, from its IP, nobody logged in as a user
async def _gate(request: Request) -> RedirectResponse | None:  # every console page + action: None = go on
    if not configured(): raise HTTPException(404, "There's no admin console here.")
    if getattr(request.state, "user", None):
        flash(request, "Log out of your account first: the admin signs in separately, while logged out.", "info")
        return RedirectResponse("/dashboard", 303)
    if not await is_admin(request): return RedirectResponse("/admin/login", 303)
    return None
async def _fails(db: AsyncSession, ip: str) -> int:  # wrong admin sign-ins from this IP since its last right one (in the last day)
    since = await db.scalar(select(func.max(AbuseEvent.at)).where(AbuseEvent.ip == ip, AbuseEvent.kind == "admin-ok")) or (utcnow() - timedelta(days=1))
    return await db.scalar(select(func.count()).select_from(AbuseEvent).where(AbuseEvent.ip == ip, AbuseEvent.kind == "admin-login", AbuseEvent.at > max(since, utcnow() - timedelta(days=1))))
# ---------- signing in / out ----------
@router.get("/admin/login")
@public
async def admin_login_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not configured(): raise HTTPException(404, "There's no admin console here.")
    if getattr(request.state, "user", None):
        flash(request, "Log out of your account first: the admin signs in separately, while logged out.", "info")
        return RedirectResponse("/dashboard", 303)
    if await is_admin(request): return RedirectResponse("/admin", 303)
    return render(request, "admin_login", {"left": TRIES - await _fails(db, client_ip(request)), "tries": TRIES, "ip": client_ip(request)})
@router.post("/admin/login")
@public
async def admin_login(request: Request, form: AdminLoginForm = Depends(AdminLoginForm.as_form), db: AsyncSession = Depends(get_db)):
    if not configured(): raise HTTPException(404, "There's no admin console here.")
    if getattr(request.state, "user", None): raise FormError("Log out of your account first: the admin signs in separately, while logged out.")
    ip = client_ip(request)
    right = hmac.compare_digest(form.username.encode(), ADMIN_USERNAME.encode()) & hmac.compare_digest(form.password.encode(), ADMIN_PASSWORD.encode())  # both always compared
    if not right:
        db.add(AbuseEvent(kind="admin-login", ip=ip, detail=f"wrong admin sign-in as “{form.username[:40]}”")); await db.commit()
        abuse.alarm()  # the AI reviews the events again soon
        fails = await _fails(db, ip)
        audit.log(None, "admin", f"wrong admin sign-in ({fails}/{TRIES})", "401", ip)
        if fails >= TRIES:
            block_ip(ip, f"{TRIES} wrong admin sign-ins")
            db.add(AbuseEvent(kind="ip-banned", ip=ip, detail=f"{TRIES} wrong admin sign-ins")); await db.commit()
            audit.log(None, "admin", f"IP {ip} banned after {TRIES} wrong admin sign-ins", "403", ip)
            return RedirectResponse("/blocked", 303)
        left = TRIES - fails
        raise FormError(f"Wrong admin user id or password. {left} {'try' if left == 1 else 'tries'} left: after {TRIES} wrong tries this IP address ({ip}) is banned from {APP_NAME}.")
    token = secrets.token_urlsafe(32)
    await reddisdb.set(_key(token), ip, ex=ADMIN_TTL)
    db.add(AbuseEvent(kind="admin-ok", ip=ip, detail="admin signed in")); await db.commit()
    audit.log("admin", "admin", "admin signed in", "ok", ip)
    resp = RedirectResponse("/admin", 303)
    resp.set_cookie(COOKIE, token, max_age=ADMIN_TTL, httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/")  # path /: the login + signup pages see it and stay closed
    return resp
@router.post("/admin/logout")
@public
async def admin_logout(request: Request, _: ActionForm = Depends(ActionForm.as_form)):
    if token := request.cookies.get(COOKIE): await reddisdb.delete(_key(token))
    audit.log("admin", "admin", "admin signed out", "ok", client_ip(request))
    resp = RedirectResponse("/admin/login", 303)
    resp.delete_cookie(COOKIE, path="/"); resp.delete_cookie(COOKIE, path="/admin")  # (/admin: sessions from before it moved to /)
    return resp
# ---------- the console ----------
_sizes: dict[str, tuple[float, int]] = {}
def _dir_size(path) -> int:  # bytes under a folder (cached 5 minutes: a big uploads folder takes a moment to walk)
    if (hit := _sizes.get(str(path))) and time.time() - hit[0] < 300: return hit[1]
    total, stack = 0, [str(path)]
    while stack:
        try:
            for e in os.scandir(stack.pop()):
                if e.is_dir(follow_symlinks=False): stack.append(e.path)
                elif e.is_file(follow_symlinks=False): total += e.stat(follow_symlinks=False).st_size
        except OSError: continue
    _sizes[str(path)] = (time.time(), total)
    return total
def _machine() -> dict:  # (a thread) the server's CPU, memory, disk, this process
    import psutil
    vm, du, me = psutil.virtual_memory(), shutil.disk_usage(ROOT), psutil.Process()
    return {"cpu": psutil.cpu_percent(interval=0.4), "cores": psutil.cpu_count(), "ram_used": vm.used, "ram_total": vm.total, "ram_pct": vm.percent,
            "disk_used": du.used, "disk_total": du.total, "disk_pct": round(du.used / du.total * 100, 1), "app_ram": me.memory_info().rss, "app_threads": me.num_threads(),
            "app_up": time.time() - me.create_time(), "uploads": _dir_size(UPLOAD_DIR), "logs": _dir_size(ROOT / "logs")}
def _services() -> dict:  # (a thread) the local AI's model, the translator
    import json, urllib.request
    from system import VLLM_URL
    out = {"models": [], "translator": False}
    try: out["models"] = [m.get("id", "") for m in json.loads(urllib.request.urlopen(VLLM_URL + "/v1/models", timeout=3).read()).get("data", [])]
    except (OSError, ValueError): pass
    try: out["translator"] = urllib.request.urlopen("http://127.0.0.1:5000/languages", timeout=3).status == 200
    except (OSError, ValueError): pass
    return out
async def usage(db: AsyncSession) -> dict:
    from meet import ai
    machine, services = await asyncio.gather(asyncio.to_thread(_machine), asyncio.to_thread(_services))
    mem = await reddisdb.info("memory")
    return {**machine, **services, "db": await db.scalar(text("SELECT pg_database_size(current_database())")), "redis": mem.get("used_memory", 0),
            "online": len(hub.sockets), "ai_queue": len(ai.captions) + len(ai.urgent) + len(ai.later), "users": await db.scalar(select(func.count()).select_from(User)),
            "companies": await db.scalar(select(func.count()).select_from(Company)), "banned": await db.scalar(select(func.count()).select_from(User).where(User.banned_at.is_not(None)))}
WEIGHT = {"chat-mute": 6, "ai-mute": 6, "shopai-mute": 6, "chat-strike": 2, "ai-strike": 2, "shopai-strike": 2, "rate-limit": 1, "admin-login": 4, "ip-banned": 10, "banned": 10}
async def offenders(db: AsyncSession, days: int = 7) -> list[dict]:
    """Who keeps going past the limits (the last `days`): per account (or IP when nobody's logged in), counts per kind, newest first by weight."""
    rows = (await db.execute(select(AbuseEvent.user_id, AbuseEvent.ip, AbuseEvent.kind, func.count(), func.max(AbuseEvent.at), func.max(AbuseEvent.detail))
                             .where(AbuseEvent.at > utcnow() - timedelta(days=days), AbuseEvent.kind.not_in(("admin-ok", "overload", "overload-end")))  # (overloads: their own section)
                             .group_by(AbuseEvent.user_id, AbuseEvent.ip, AbuseEvent.kind))).all()
    who: dict[str, dict] = {}
    for uid, ip, kind, n, last, detail in rows:
        w = who.setdefault(f"u{uid}" if uid else f"ip{ip}", {"user": uid, "ip": "" if uid else ip, "ips": set(), "kinds": {}, "last": last, "score": 0, "detail": detail})
        if ip: w["ips"].add(ip)
        w["kinds"][kind] = w["kinds"].get(kind, 0) + n; w["score"] += WEIGHT.get(kind, 1) * n
        if last > w["last"]: w["last"], w["detail"] = last, detail
    names = dict((await db.execute(select(User.id, User.username).where(User.id.in_([w["user"] for w in who.values() if w["user"]])))).all()) if who else {}
    banned = [e for e, _ in blocked_list()]
    for w in who.values(): w["name"], w["ip_banned"] = names.get(w["user"], ""), bool(w["ip"] and any(blocks(e, w["ip"]) for e in banned))  # (a range covers it too)
    return sorted(who.values(), key=lambda w: (-w["score"], w["last"]))[:40]
_KEY = "%Y%m%d%H%M%S%f"  # an overload's start as it's named in links (?overload=… and its .csv)
async def overloads(db: AsyncSession, pick: str = "") -> dict:
    """The request limiter's overloads (abuse.py: every incoming IP was counted during each), newest first, and the IPs of the one picked
    (the newest by default), busiest first. The .csv has every one of them."""
    rows = (await db.execute(select(OverloadIP.started_at, func.count(), func.sum(OverloadIP.requests), func.sum(OverloadIP.refused), func.max(OverloadIP.last_seen))
                             .group_by(OverloadIP.started_at).order_by(OverloadIP.started_at.desc()).limit(10))).all()
    runs = [{"since": s, "ips": n, "requests": r, "refused": f, "last": l, "key": f"{s:{_KEY}}"} for s, n, r, f, l in rows]
    cur = next((x for x in runs if x["key"] == pick), runs[0] if runs else None)
    ips = (await db.scalars(select(OverloadIP).where(OverloadIP.started_at == cur["since"]).order_by(OverloadIP.requests.desc(), OverloadIP.ip).limit(100))).all() if cur else []
    names = dict((await db.execute(select(User.id, User.username).where(User.id.in_({i.user_id for i in ips if i.user_id})))).all()) if ips else {}
    entries = [e for e, _ in blocked_list()]
    return {"runs": runs, "cur": cur, "ips": ips, "names": names, "banned": {i.ip for i in ips if any(blocks(e, i.ip) for e in entries)}, "active": abuse.overload["since"], "at": abuse.OVERLOAD_AT, "hold": abuse.HOLD_TEXT}
@router.get("/admin")
@public
async def console(request: Request, q: str = "", overload: str = "", db: AsyncSession = Depends(get_db)):
    if r := await _gate(request): return r
    q = " ".join(q.split())[:60]
    found = (await db.scalars(select(User).where(or_(User.username.ilike(f"%{q}%"), User.email.ilike(f"%{q}%"))).order_by(User.username).limit(20))).all() if q else []
    companies = (await db.scalars(select(Company).where(Company.name.ilike(f"%{q}%")).order_by(Company.name).limit(20).options(joinedload(Company.owner)))).all() if q else []
    on, n = await single_company_mode(db, lock=False), await db.scalar(select(func.count()).select_from(Company))
    return render(request, "admin", {"use": await usage(db), "offenders": await offenders(db), "ov": await overloads(db, overload[:24]), "q": q, "found": found, "companies": companies, "ips": blocked_list(),
                                     "mode": {"on": on, "companies": n, "blocked": not on and n > 1},  # the single company switch: off and 2+ companies = can't be turned on
                                     "warnings": (await db.scalars(select(AIWarning).order_by(AIWarning.id.desc()).limit(12))).all(),
                                     "banned": (await db.scalars(select(User).where(User.banned_at.is_not(None)).order_by(User.banned_at.desc()).limit(50))).all()})
@router.get("/admin/overloads/{key}.csv")
@public
async def overload_csv(request: Request, key: str, db: AsyncSession = Depends(get_db)):
    """Every IP address seen during one limiter overload, as a spreadsheet (UTC times)."""
    if r := await _gate(request): return r
    try: since = datetime.strptime(key, _KEY)
    except ValueError: raise HTTPException(404, "There's no such overload.") from None
    rows = (await db.scalars(select(OverloadIP).where(OverloadIP.started_at == since).order_by(OverloadIP.requests.desc(), OverloadIP.ip))).all()
    if not rows: raise HTTPException(404, "There's no such overload.")
    cell = lambda v: "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v  # no spreadsheet formulas from a request's path
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["ip", "requests", "refused", "first_seen_utc", "last_seen_utc", "last_user_id", "first_path"])
    for o in rows: w.writerow([o.ip, o.requests, o.refused, f"{o.first_seen:%Y-%m-%d %H:%M:%S}", f"{o.last_seen:%Y-%m-%d %H:%M:%S}", o.user_id or "", cell(o.path)])
    audit.log("admin", "admin", f"downloaded the IPs of the limiter overload of {since:%d %b %Y %H:%M} UTC ({len(rows)})", "ok", client_ip(request))
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="overload-{key[:14]}.csv"', "Cache-Control": "no-store"})
# ---------- actions (each with a clear reason where someone is affected) ----------
@router.post("/admin/single-company")
@public
async def admin_single_company(request: Request, form: AdminSwitchForm = Depends(AdminSwitchForm.as_form), db: AsyncSession = Depends(get_db)):
    """Single company mode (user asked): no company can be created while it's on. It turns on only while one company or none exists."""
    if r := await _gate(request): return r
    row = await db.get(SiteSetting, "single_company", with_for_update=True) or SiteSetting(key="single_company")  # locked: a company being created right now finishes first
    if form.on and (n := await db.scalar(select(func.count()).select_from(Company))) > 1:
        raise FormError(f"Single company mode can't be turned on: {n} companies exist. It needs one company or none, so delete the others first (find them with the search).")
    row.value, row.updated_at = "on" if form.on else "off", utcnow()
    db.add(row); await db.commit()
    SITE["single_company"] = form.on  # the templates' copy
    audit.log("admin", "admin", f"single company mode turned {row.value}", "ok", client_ip(request))
    flash(request, "Single company mode is on: nobody can create a company now." if form.on else "Single company mode is off: companies can be created again.", "success")
    return RedirectResponse("/admin#mode", 303)
async def ban_user(db: AsyncSession, u: User, reason: str, ip: str) -> list[str]:
    """As when an account is deleted: out of every company, its own companies deleted (their members told why), friends gone, logged out
    everywhere; but the account row stays, so its chat messages keep their sender and are never erased. Returns the companies deleted."""
    owned = (await db.scalars(select(Company).where(Company.owner_id == u.id).options(selectinload(Company.members).joinedload(Membership.user)))).all()
    gone = [await erase_company(db, c, f"its owner {u.username} was banned: {reason}") for c in owned]
    left = (await db.scalars(select(Membership.company_id).where(Membership.user_id == u.id))).all()
    await db.execute(delete(Membership).where(Membership.user_id == u.id))
    for cid in left: await left_company(db, cid, u.id)
    await db.execute(delete(Friendship).where(or_(Friendship.low_id == u.id, Friendship.high_id == u.id)))
    u.banned_at, u.ban_reason, u.banned_by, u.login_id = utcnow(), reason, "admin", None  # login_id: every session ends at once
    db.add(AbuseEvent(kind="banned", user_id=u.id, ip=ip, detail=reason[:300]))
    await db.commit()
    await hub.kick(u.id)  # their open tabs lose the live connection at once
    changelog.add("u", u.id, "banned by the admin", reason, by="admin")
    for cid in left: changelog.company(cid, f"{u.username} was banned by the {APP_NAME} admin and is no longer in the company", reason, by="admin")
    try: await send_email(u.email, f"Your {APP_NAME} account has been banned", f"Hi {u.username},\n\nYour {APP_NAME} account has been banned by the admin.\nReason: {reason}\n\n"
                          f"You can't log in any more. If you think this is a mistake, contact the admin at {CONTACT_EMAIL}.\n")
    except Exception as e: print(f"ban email: {type(e).__name__}")
    return gone
@router.post("/admin/users/{user_id}/ban")
@public
async def admin_ban(request: Request, user_id: int, form: AdminReasonForm = Depends(AdminReasonForm.as_form), db: AsyncSession = Depends(get_db)):
    if r := await _gate(request): return r
    if not (u := await db.get(User, user_id)): raise HTTPException(404, "No such account.")
    if u.banned_at: raise FormError(f"{u.username} is banned already.")
    gone = await ban_user(db, u, form.reason, client_ip(request))
    audit.log("admin", "admin", f"banned {u.username} (id {u.id}): {form.reason}" + (f"; deleted {', '.join(gone)}" if gone else ""), "ok", client_ip(request))
    flash(request, f"{u.username} is banned: logged out, out of every company{', and ' + ', '.join(gone) + ' deleted' if gone else ''}. Their chats stay.", "success")
    return RedirectResponse("/admin#banned", 303)
@router.post("/admin/users/{user_id}/unban")
@public
async def admin_unban(request: Request, user_id: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    if r := await _gate(request): return r
    if not (u := await db.get(User, user_id)) or not u.banned_at: raise FormError("That account isn't banned.")
    u.banned_at, u.ban_reason, u.banned_by = None, "", ""
    db.add(AbuseEvent(kind="unbanned", user_id=u.id, ip=client_ip(request), detail="ban lifted")); await db.commit()
    changelog.add("u", u.id, "ban lifted by the admin", by="admin")
    audit.log("admin", "admin", f"lifted the ban on {u.username} (id {u.id})", "ok", client_ip(request))
    flash(request, f"{u.username} can log in again (their old companies aren't given back).", "success")
    return RedirectResponse("/admin#banned", 303)
@router.post("/admin/ips/ban")
@public
async def admin_ip_ban(request: Request, form: AdminIPForm = Depends(AdminIPForm.as_form), db: AsyncSession = Depends(get_db)):
    if r := await _gate(request): return r
    if len(" ".join(form.reason.split())) < 10: raise FormError("Give a clear reason (10 characters or more).")
    try: entry = block_entry(form.ip)  # one address, or a range (10.0.0.0/8), however it's written
    except ValueError: raise FormError("That isn't an IP address or a range like 10.0.0.0/8.") from None
    if blocks(entry, client_ip(request)): raise FormError(f"{entry + ' includes' if '/' in entry else 'That is'} your own IP address: blocking it would lock you out.")
    if not block_ip(entry, form.reason): raise FormError(f"{entry} is blocked already.")
    db.add(AbuseEvent(kind="ip-banned", ip=entry, detail=form.reason[:300])); await db.commit()
    audit.log("admin", "admin", f"blocked IP {entry}: {form.reason}", "ok", client_ip(request))
    flash(request, f"{entry} is blocked: every request from it gets the blocked page.", "success")
    return RedirectResponse("/admin#ips", 303)
@router.post("/admin/ips/unban")
@public
async def admin_ip_unban(request: Request, form: AdminIPForm = Depends(AdminIPForm.as_form)):
    if r := await _gate(request): return r
    if not unblock_ip(form.ip): raise FormError(f"{form.ip.strip()} isn't on the list.")
    audit.log("admin", "admin", f"unblocked IP {form.ip.strip()}", "ok", client_ip(request))
    flash(request, f"{form.ip.strip()} is unblocked.", "success")
    return RedirectResponse("/admin#ips", 303)
@router.post("/admin/companies/{company_id}/delete")
@public
async def admin_delete_company(request: Request, company_id: str, form: AdminReasonForm = Depends(AdminReasonForm.as_form), db: AsyncSession = Depends(get_db)):
    if r := await _gate(request): return r
    import uuid
    try: cid = uuid.UUID(company_id)
    except ValueError: raise HTTPException(404, "No such company.") from None
    if not (c := await db.get(Company, cid, options=[selectinload(Company.members).joinedload(Membership.user)])): raise HTTPException(404, "No such company.")
    name = await erase_company(db, c, form.reason)
    audit.log("admin", "admin", f"deleted the company {name} ({cid}): {form.reason}", "ok", client_ip(request))
    flash(request, f"{name} was deleted; its members were told why.", "success")
    return RedirectResponse("/admin", 303)
@router.post("/admin/ai-review")
@public
async def admin_ai_review(request: Request, _: ActionForm = Depends(ActionForm.as_form)):
    if r := await _gate(request): return r
    from meet import ai
    ai.put(abuse.review)  # behind captions + the chat AI: it's not urgent
    flash(request, "The local AI is reading the last 7 days of events: its warnings show here in a minute or two.", "info")
    return RedirectResponse("/admin#warnings", 303)
