import asyncio, hashlib, hmac, math, re, secrets, time, uuid
from datetime import date, timedelta
import phonenumbers
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from decoraters import cache, guest_only, public
from form import MAX_AGE, ActionForm, ForgotForm, LoginForm, LogoutForm, RecoverForm, RegisterForm, ResendForm, SavedAccountForm, VerifyForm, adult_since, password_problem
from presence import hub
from system import (APP_NAME, CODE_ATTEMPTS, CODE_TTL_MIN, DEFAULT_PHONE_REGION, DUMMY_HASH, MAX_SENDS, RESEND_COOLDOWN_S, SECRET_KEY, SIGNUPS_PER_IP_HOUR, FormError, client_ip,
                    code_hash, delivery_modes, end_session, flash, mask_email, mask_phone, new_code, new_password_hash, password_matches, render, same_text, send_email, send_sms,
                    start_session)
from tables import REGION_ZONES, REGIONS, ZONES, AccountRecovery, IdentityProof, PendingSignup, User, blacklisted, get_db, username_problem, utcnow
import abuse, audit, changelog
router = APIRouter(tags=["auth"])  # accounts live in Postgres; Redis only gets a session after a successful login
# ---------- signup: form -> email + SMS codes -> account created only once both are verified ----------
SIGNUP_KEY, LABEL = "_signup", {"email": "email", "phone": "phone number"}
def _pid(token: str) -> str: return hashlib.sha256(token.encode()).hexdigest()
async def _pending(request: Request, db: AsyncSession) -> PendingSignup | None:
    token = request.session.get(SIGNUP_KEY)
    return await db.get(PendingSignup, _pid(token)) if token else None
async def _send_code(p: PendingSignup, channel: str) -> None:
    """New code for one channel: only its HMAC is stored; resets that channel's expiry and wrong-guess counter. An email or phone already in use (p.taken) gets a note instead and
    has no code at all, so that signup can never finish: to whoever typed it the page looks the same, so nobody learns who has an account, and the real owner hears about it."""
    code, now, closed = new_code(), utcnow(), channel in p.taken.split()
    for field, value in (("code", "" if closed else code_hash(code)), ("expires_at", now + timedelta(minutes=CODE_TTL_MIN)), ("attempts", 0), ("sent_at", now)): setattr(p, f"{channel}_{field}", value)
    setattr(p, f"{channel}_sends", getattr(p, f"{channel}_sends") + 1)
    text = (f"Someone tried to sign up for {APP_NAME} with this {LABEL[channel]}, but it can't be used for a new account: it already has one, or it belonged to one that was deleted. If that was you, "
            f"log in, or use “forgot username or password” on the login page. If it wasn't, ignore this: nothing has changed." if closed else
            f"Your {APP_NAME} verification code is {code}. It expires in {CODE_TTL_MIN} minutes. Never share it with anyone.")
    if channel == "email": await send_email(p.email, f"{APP_NAME}: someone tried to sign up with this email" if closed else f"{APP_NAME} verification code: {code}", text + ("" if closed else "\n\nIf you didn't try to sign up, you can ignore this email."))
    else: await send_sms(p.phone, text)
@router.get("/register")
@public
@guest_only
@cache.memoize(timeout=60)
async def register_page(request: Request):  # the zone picker preselects the server's default region (app.js swaps in the browser's time zone if it's listed)
    home = (DEFAULT_PHONE_REGION or "").upper() if (DEFAULT_PHONE_REGION or "").upper() in REGIONS else "IN"
    return render(request, "register", {"max_dob": adult_since().isoformat(), "min_dob": f"{date.today().year - MAX_AGE}-01-01", "default_zone": f"{home}:{REGION_ZONES[home][0][0]}"})
@router.post("/register")
@public
@guest_only
async def register(request: Request, form: RegisterForm = Depends(RegisterForm.as_form), db: AsyncSession = Depends(get_db)):
    if why := await username_problem(db, form.username, email=form.email): raise HTTPException(409, why)  # taken in any capitals, a deleted account's, or being signed up with now
    rows = (await db.execute(select(User.email, User.phone).where(or_(func.lower(User.email) == form.email.lower(), User.phone == form.phone)))).all()
    taken = {*(["email"] if any(r.email.lower() == form.email.lower() for r in rows) else []), *(["phone"] if any(r.phone == form.phone for r in rows) else []),  # in use, or a deleted account's (never again): saying so would tell anyone who has an account, so it's _send_code that deals with these
             *[ch for ch in ("email", "phone") if await blacklisted(db, **{ch: getattr(form, ch)})]}
    now, ip = utcnow(), client_ip(request)
    await db.execute(delete(PendingSignup).where(PendingSignup.created_at < now - timedelta(days=1)))  # housekeeping: old abandoned signups
    # restarting keeps the previous attempt's row (it just stops being this visitor's), so it still counts toward the hourly limit
    started = await db.scalar(select(func.count()).select_from(PendingSignup).where(PendingSignup.ip == ip, PendingSignup.created_at > now - timedelta(hours=1)))
    if started >= SIGNUPS_PER_IP_HOUR: raise HTTPException(429, "Too many signups from your network. Try again in an hour.")  # stops SMS/email flooding
    token = secrets.token_urlsafe(32)
    p = PendingSignup(id=_pid(token), username=form.username, email=form.email, phone=form.phone, dob=form.dob, region=ZONES[form.zone][0], timezone=ZONES[form.zone][1], password_hash=await new_password_hash(form.password), ip=ip, created_at=now,
                      email_sends=0, phone_sends=0, email_attempts=0, phone_attempts=0, email_verified=False, phone_verified=False, taken=" ".join(sorted(taken)))
    for channel in ("email", "phone"): await _send_code(p, channel)  # a failed send raises before anything is saved
    db.add(p)
    await db.commit()
    request.session[SIGNUP_KEY] = token
    flash(request, f"Codes sent to {mask_email(p.email)} and {mask_phone(p.phone)}.", "info")
    return RedirectResponse("/register/verify", 303)
@router.get("/register/username")
@public
async def username_free(request: Request, name: str = "", db: AsyncSession = Depends(get_db)):  # the signup form asks as you type; POST /register checks again, with the email (never taken here: it would tell strangers who's mid-signup)
    name = name.strip()[:40]
    if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", name): return {"ok": False, "why": "3 to 32 letters, digits or _"}
    why = await username_problem(db, name)
    return {"ok": not why, "why": why or f"{name} is free", "hold": bool(why and "signing up" in why)}  # hold: a warning only (it may be your own earlier try): the form's own POST decides
@router.get("/register/verify")
@public
@guest_only
async def verify_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not (p := await _pending(request, db)):
        flash(request, "Fill in the signup form first.", "info")
        return RedirectResponse("/register", 303)
    return render(request, "verify", {"pending": p, "masked_email": mask_email(p.email), "masked_phone": mask_phone(p.phone), "ttl": CODE_TTL_MIN,
                                      "dev_delivery": "console" in delivery_modes().values()})
@router.post("/register/verify")
@public
@guest_only
async def verify(request: Request, form: VerifyForm = Depends(VerifyForm.as_form), db: AsyncSession = Depends(get_db)):
    if not (p := await _pending(request, db)): raise FormError("Your signup expired. Please start again.")
    now, problems = utcnow(), []
    for channel in ("email", "phone"):
        if getattr(p, f"{channel}_verified"): continue
        code, attempts = "".join(getattr(form, f"{channel}_code").split()), getattr(p, f"{channel}_attempts")
        if not code: problems.append(f"enter the {LABEL[channel]} code")
        elif getattr(p, f"{channel}_expires_at") < now: problems.append(f"the {LABEL[channel]} code expired, send a new one")
        elif attempts >= CODE_ATTEMPTS: problems.append(f"too many wrong {LABEL[channel]} codes, send a new one")
        elif hmac.compare_digest(code_hash(code), getattr(p, f"{channel}_code")): setattr(p, f"{channel}_verified", True)
        else:
            setattr(p, f"{channel}_attempts", attempts + 1)
            problems.append(f"wrong {LABEL[channel]} code ({CODE_ATTEMPTS - attempts - 1} tries left)")
    if not (p.email_verified and p.phone_verified):
        await db.commit()  # keep verified channels + wrong-guess counts
        raise FormError("; ".join(problems).capitalize() + ".")
    if gone := await blacklisted(db, username=p.username, email=p.email, phone=p.phone):  # checked again here: deleted while this signup was pending
        await db.delete(p); await db.commit(); request.session.pop(SIGNUP_KEY, None)
        raise HTTPException(403, f"That {gone} belonged to a deleted account and can't be used to sign up again.")
    db.add(User(username=p.username, email=p.email, phone=p.phone, dob=p.dob, region=p.region, timezone=p.timezone, password_hash=p.password_hash, email_verified_at=now, phone_verified_at=now,
                ip_created=client_ip(request) or None))
    await db.delete(p)
    try: await db.commit()
    except IntegrityError: raise HTTPException(409, "That username, email or phone number was registered in the meantime. Please sign up again.") from None
    request.session.pop(SIGNUP_KEY, None)
    flash(request, "Email and phone verified. Account created, please log in.", "success")
    return RedirectResponse("/login", 303)
@router.post("/register/resend")
@public
@guest_only
async def resend(request: Request, form: ResendForm = Depends(ResendForm.as_form), db: AsyncSession = Depends(get_db)):
    if not (p := await _pending(request, db)): raise FormError("Your signup expired. Please start again.")
    channel = form.channel
    if getattr(p, f"{channel}_verified"): raise FormError(f"Your {LABEL[channel]} is already verified.")
    if (wait := RESEND_COOLDOWN_S - (utcnow() - getattr(p, f"{channel}_sent_at")).total_seconds()) > 0: raise FormError(f"Wait {math.ceil(wait)}s before asking for another code.")
    if getattr(p, f"{channel}_sends") >= MAX_SENDS: raise FormError("Too many codes requested. Please start the signup again.")
    await _send_code(p, channel)
    await db.commit()
    flash(request, f"New code sent to {mask_email(p.email) if channel == 'email' else mask_phone(p.phone)}.", "info")
    return RedirectResponse("/register/verify", 303)
# ---------- forgot the username or the password (user asked): a 6-digit code to the account's own email or phone, good for CODE_TTL_MIN
#            (10) minutes and CODE_ATTEMPTS tries. The answer is the same whether or not an account matches (nobody learns who has one) ----------
RECOVERY_KEY, RECOVERIES_PER_IP_HOUR, CODES_PER_ACCOUNT_HOUR = "_recovery", 10, 5
async def _recovery(request: Request, db: AsyncSession) -> AccountRecovery | None:
    token = request.session.get(RECOVERY_KEY)
    return await db.get(AccountRecovery, _pid(token)) if token else None
def _phone_of(text: str) -> str | None:  # a phone number as typed -> E.164, else None
    try: n = phonenumbers.parse(text, DEFAULT_PHONE_REGION)
    except phonenumbers.NumberParseException: return None
    return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164) if phonenumbers.is_valid_number(n) else None
async def _capped(db: AsyncSession, user_id: int) -> bool:  # this account had its codes for the hour (5): no more are sent, the answer looks the same
    return await db.scalar(select(func.coalesce(func.sum(AccountRecovery.sends), 0)).where(AccountRecovery.user_id == user_id, AccountRecovery.created_at > utcnow() - timedelta(hours=1))) >= CODES_PER_ACCOUNT_HOUR
_sending: set = set()  # recovery codes on their way (a task nobody holds can be dropped half way)
async def _deliver(channel: str, to: str, subject: str, text: str) -> None:
    try: await (send_email(to, subject, text + "\n\nIf you didn't ask for it, ignore this email: nothing changes without the code.") if channel == "email" else send_sms(to, text))
    except Exception as e: print(f"recovery code: {type(e).__name__}")
def _send_recovery(r: AccountRecovery, user: User | None) -> None:
    """A fresh code for this recovery (only its HMAC is kept), to the account's own email or phone; to nobody when no account matched. Sent in the background: a real send takes
    time and can fail, and the answer has to come just as fast and look the same either way, or that alone tells who has an account."""
    code, now = new_code(), utcnow()
    r.code, r.expires_at, r.attempts, r.sent_at = code_hash(code), now + timedelta(minutes=CODE_TTL_MIN), 0, now
    if not user: return
    what = "username" if r.kind == "username" else "password reset"
    job = asyncio.create_task(_deliver(r.channel, user.email if r.channel == "email" else user.phone, f"{APP_NAME} {what} code: {code}", f"Your {APP_NAME} {what} code is {code}. It expires in {CODE_TTL_MIN} minutes. Never share it: {APP_NAME} never asks you for it."))
    _sending.add(job); job.add_done_callback(_sending.discard)
@router.get("/forgot")
@public
@guest_only
async def forgot_page(request: Request, kind: str = "password"):
    return render(request, "forgot", {"step": "ask", "kind": kind if kind in ("username", "password") else "password", "ttl": CODE_TTL_MIN})
@router.post("/forgot")
@public
@guest_only
async def forgot(request: Request, form: ForgotForm = Depends(ForgotForm.as_form), db: AsyncSession = Depends(get_db)):
    now, ip, who = utcnow(), client_ip(request), form.who.strip()
    await db.execute(delete(AccountRecovery).where(AccountRecovery.created_at < now - timedelta(days=1)))  # housekeeping: old ones
    if await db.scalar(select(func.count()).select_from(AccountRecovery).where(AccountRecovery.ip == ip, AccountRecovery.created_at > now - timedelta(hours=1))) >= RECOVERIES_PER_IP_HOUR:
        raise HTTPException(429, "Too many recovery attempts from your network: try again in an hour.")  # stops code flooding
    match = [func.lower(User.email) == who.lower()] if "@" in who else [func.lower(User.username) == who.lower(), *([User.phone == phone] if (phone := _phone_of(who)) else [])]
    user = (await db.scalars(select(User).where(or_(*match)).limit(1))).first()
    if user and (user.banned_at or await _capped(db, user.id)): user = None  # a banned account gets no codes (the answer looks the same)
    token = secrets.token_urlsafe(32)
    r = AccountRecovery(id=_pid(token), user_id=user.id if user else None, kind=form.kind, channel=form.channel, code="", expires_at=now, sends=1, ip=ip, created_at=now)
    _send_recovery(r, user)
    db.add(r); await db.commit()
    request.session[RECOVERY_KEY] = token
    audit.log(None, "auth", f"{form.kind} recovery asked by {form.channel}: {'code sent to an account' if user else 'no account matched (or its hourly codes were used)'}", "ok", ip)
    flash(request, f"If that matches an account, a 6-digit code is on its way to its {LABEL[form.channel]}. It works for {CODE_TTL_MIN} minutes.", "info")
    return RedirectResponse("/forgot/verify", 303)
@router.get("/forgot/verify")
@public
@guest_only
async def forgot_verify_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not (r := await _recovery(request, db)):
        flash(request, "Start here: say which account it is.", "info")
        return RedirectResponse("/forgot", 303)
    return render(request, "forgot", {"step": "code", "kind": r.kind, "channel": r.channel, "left": max(0, int((r.expires_at - utcnow()).total_seconds())), "ttl": CODE_TTL_MIN,
                                      "tries": max(0, CODE_ATTEMPTS - r.attempts), "dev_delivery": "console" in delivery_modes().values()})
@router.post("/forgot/verify")
@public
@guest_only
async def forgot_verify(request: Request, form: RecoverForm = Depends(RecoverForm.as_form), db: AsyncSession = Depends(get_db)):
    if not (r := await _recovery(request, db)): raise FormError("That recovery expired or is finished: start again.")
    if r.kind == "password" and (why := password_problem(form.password)): raise FormError(why)  # checked first: a typo here doesn't cost a try
    if r.kind == "password" and form.password != form.password_confirm: raise FormError("The two new passwords don't match.")
    if r.expires_at < utcnow(): raise FormError(f"That code expired (a code works for {CODE_TTL_MIN} minutes): send a new one.")
    if r.attempts >= CODE_ATTEMPTS: raise FormError("Too many wrong codes: send a new one.")
    user = await db.get(User, r.user_id) if r.user_id else None
    if not user or not hmac.compare_digest(code_hash("".join(form.code.split())), r.code):
        r.attempts += 1; await db.commit()
        left = CODE_ATTEMPTS - r.attempts
        raise FormError(f"Wrong code ({left} {'try' if left == 1 else 'tries'} left)." if left else "Wrong code, and that was the last try: send a new one.")
    if r.kind == "password" and (why := password_problem(form.password, user.username, user.email)): raise FormError(why)  # only now, with the right code: before it, this would tell a stranger the account's username. It doesn't cost a try
    kind, channel, ip = r.kind, r.channel, client_ip(request)
    await db.execute(delete(AccountRecovery).where(or_(AccountRecovery.user_id == user.id, AccountRecovery.id == r.id)))  # used: this account's other codes stop working too
    if kind == "password": user.password_hash, user.login_id = await new_password_hash(form.password), None  # the new password; every device is logged out
    await db.commit()
    if kind == "password": await hub.kick(user.id)
    request.session.pop(RECOVERY_KEY, None)
    audit.log({"user_id": user.id, "username": user.username, "real": ""}, "auth", f"{kind} {'recovered' if kind == 'username' else 'reset'} with a code ({channel})", "ok", ip)
    if kind == "username":
        request.session["_recovered"] = user.username  # shown once (GET, so a refresh doesn't resend the form)
        return RedirectResponse("/forgot/done", 303)
    changelog.add("u", user.id, "password reset", f"with a code sent to the account's {LABEL[channel]}; every device was logged out", by=user.username)
    note = (f"Your {APP_NAME} password was just changed with a code sent to your {LABEL[channel]}, and every device was logged out. "
            f"If this wasn't you, reset it again right away from the login page and tell support.")
    for send in (lambda: send_email(user.email, f"{APP_NAME}: your password was changed", note), lambda: send_sms(user.phone, note)):  # both: the channel not used is the alarm
        try: await send()
        except Exception as e: print(f"password-change notice: {type(e).__name__}: {e}")
    flash(request, "Password changed: log in with the new one. Every device you were logged in on has been logged out.", "success")
    return RedirectResponse("/login", 303)
@router.get("/forgot/done")
@public
@guest_only
async def forgot_done(request: Request):
    if not (name := request.session.pop("_recovered", None)): return RedirectResponse("/forgot?kind=username", 303)
    return render(request, "forgot", {"step": "done", "username": name, "ttl": CODE_TTL_MIN})
@router.post("/forgot/resend")
@public
@guest_only
async def forgot_resend(request: Request, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    if not (r := await _recovery(request, db)): raise FormError("That recovery expired or is finished: start again.")
    if (wait := RESEND_COOLDOWN_S - (utcnow() - r.sent_at).total_seconds()) > 0: raise FormError(f"Wait {math.ceil(wait)}s before asking for another code.")
    if r.sends >= MAX_SENDS: raise FormError("That's the most codes for one try: start again in a while.")
    user = await db.get(User, r.user_id) if r.user_id else None
    if user and await _capped(db, user.id): user = None
    r.sends += 1
    _send_recovery(r, user)
    await db.commit()
    flash(request, f"A new code is on its way (if the account exists): it works for {CODE_TTL_MIN} minutes, and the old one doesn't any more.", "info")
    return RedirectResponse("/forgot/verify", 303)
# ---------- login / logout ----------
# The account chooser (user asked, like Google's): the accounts that logged in on this browser, kept in a cookie: their user ids only
# (never a password), signed with SECRET_KEY so nobody can put other ids in it to find out who has an account. Every time the login page
# shows them, each is checked against Postgres: an account that's gone or banned drops out. A × takes one off this browser's list.
ACCOUNTS_COOKIE, SAVED_MAX = "hx_accounts", 5
def _sig(payload: str) -> str: return hmac.new(SECRET_KEY.encode(), b"saved-accounts:" + payload.encode(), "sha256").hexdigest()[:32]
def saved_ids(request: Request) -> list[int]:
    payload, _, sig = request.cookies.get(ACCOUNTS_COOKIE, "").rpartition(".")
    if not payload or not same_text(_sig(payload), sig): return []
    return [int(x) for x in payload.split("-") if x.isdigit()][:SAVED_MAX]
def put_saved(request: Request, resp, ids: list[int]) -> None:
    ids = list(dict.fromkeys(ids))[:SAVED_MAX]
    if not ids: resp.delete_cookie(ACCOUNTS_COOKIE, path="/"); return
    payload = "-".join(map(str, ids))
    resp.set_cookie(ACCOUNTS_COOKIE, f"{payload}.{_sig(payload)}", max_age=365 * 86400, httponly=True, samesite="lax", secure=request.url.scheme == "https", path="/")
@router.get("/login")
@public
@guest_only
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):  # (not cached: the chooser is this browser's own)
    ids = saved_ids(request)
    ok = {r.id: r for r in (await db.execute(select(User.id, User.username, User.email).where(User.id.in_(ids), User.banned_at.is_(None)))).all()} if ids else {}
    accounts = [{"id": i, "username": ok[i].username, "email": mask_email(ok[i].email)} for i in ids if i in ok]
    resp = render(request, "login", {"accounts": accounts, "banned": request.session.pop("_banned", None)})
    if len(accounts) != len(ids): put_saved(request, resp, [a["id"] for a in accounts])  # gone / banned ones drop out of the cookie too
    return resp
@router.post("/login/accounts/remove")
@public
async def forget_account(request: Request, form: SavedAccountForm = Depends(SavedAccountForm.as_form)):
    resp = RedirectResponse("/login", 303)
    put_saved(request, resp, [i for i in saved_ids(request) if i != form.user_id])
    return resp
TRIES_PER_ACCOUNT, TRIES_PER_IP, TRIES_WINDOW_S = 8, 40, 900  # wrong passwords allowed in 15 minutes: per account from one IP (a stranger can't lock the real owner out), and per IP
_wrong: dict[str, list[float]] = {}  # in this process's memory (Redis holds login sessions only)
@router.post("/login")
@public
@guest_only
async def login(request: Request, form: LoginForm = Depends(LoginForm.as_form), db: AsyncSession = Depends(get_db)):
    ip, name = client_ip(request), form.username.strip().lower()[:64]
    tries, now = ((f"{ip}|{name}", TRIES_PER_ACCOUNT), (ip, TRIES_PER_IP)), time.monotonic()
    if any(sum(now - t < TRIES_WINDOW_S for t in _wrong.get(k, ())) >= most for k, most in tries):  # the same answer whether the account exists or not
        abuse.record("login-locked", None, ip, f"too many wrong passwords, last for “{name[:32]}”", every=60)
        raise HTTPException(429, "Too many wrong passwords. Wait 15 minutes, or use “forgot username or password” on this page.")
    user = await db.scalar(select(User).where(func.lower(User.username) == name))  # any capitals: usernames are unique that way
    if not await password_matches(form.password, user.password_hash if user else DUMMY_HASH) or not user:
        audit.log(None, "auth", f"login failed for username “{form.username[:32]}”", "401", ip)
        if len(_wrong) > 50_000: _wrong.clear()
        for k, _ in tries: _wrong[k] = [t for t in _wrong.get(k, ()) if now - t < TRIES_WINDOW_S] + [now]
        raise HTTPException(401, "Invalid username or password")
    _wrong.pop(tries[0][0], None)
    if user.banned_at:  # banned by the admin (routers/admin.py): said plainly, only to someone who knows the password
        audit.log(None, "auth", f"login refused: {user.username} is banned", "403", client_ip(request))
        request.session["_banned"] = {"username": user.username, "reason": user.ban_reason, "on": f"{user.banned_at:%d %b %Y}"}
        resp = RedirectResponse("/login", 303)
        put_saved(request, resp, [i for i in saved_ids(request) if i != user.id])
        return resp
    real = await db.scalar(select(IdentityProof.name_on_id).where(IdentityProof.user_id == user.id))
    audit.log({"user_id": user.id, "username": user.username, "real": real or ""}, "auth", "logged in", "ok", client_ip(request))
    user.ip_last_login, user.last_login_at = client_ip(request) or None, utcnow()
    user.login_id = uuid.uuid4()  # one login at a time: from this commit on, any older session's login_id no longer matches
    await db.commit()
    await hub.kick(user.id)  # the older device's live connections go too
    resp = RedirectResponse(request.session.pop("_next", "/dashboard"), 303)
    await start_session(request, resp, user.id, user.username, user.login_id)
    put_saved(request, resp, [user.id, *saved_ids(request)])  # the account chooser: this one first
    return resp
@router.post("/logout")
async def logout(request: Request, _: LogoutForm = Depends(LogoutForm.as_form), db: AsyncSession = Depends(get_db)):
    if (u := request.state.user) and (account := await db.get(User, int(u["user_id"]))):
        account.login_id = None  # nothing can match a logged-out account
        await db.commit()
        await hub.kick(account.id)  # its live connections end with it
    resp = RedirectResponse("/login", 303)
    await end_session(request, resp)
    flash(request, "Logged out.", "success")
    return resp
