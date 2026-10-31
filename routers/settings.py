"""Settings: your profile picture, and the logo of a company you own or co-own. Pictures go through media.py (virus-scanned upload ->
re-encoded 256x256 WebP, metadata dropped) and are served from /media/. Until there's one, every page shows a reminder (base.html).
Every company must have a logo: it's required at creation, can be replaced but not removed, and editing a company needs one."""
import hmac, math, shutil, uuid
from datetime import timedelta
import changelog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from assist import forgive, rate_limited
import banks
from bankcheck import BankCheckError, unchecked_why, verify_bank
from idcheck import ONLINE, IdCheckError, verify_id
from form import ActionForm, BankForm, ContactCodeForm, DeleteAccountForm, EmailChangeForm, IdentityForm, PasswordChangeForm, PhoneChangeForm, PictureForm, RegionForm, UsernameForm, password_problem
from media import PictureError, discard_upload, remove_id_photo, remove_picture, store_id_photo, store_picture
from routers.chats import tell_names
from routers.companies import _for_deletion, can_edit, deletion_block, erase_company
from presence import hub
from system import (APP_NAME, BANK_VERIFY, CODE_ATTEMPTS, CODE_TTL_MIN, ID_VERIFY, MAX_SENDS, RESEND_COOLDOWN_S, FormError, ai_dir, code_hash, delivery_modes, end_session, flash,
                    identity_digest, mask_email, mask_phone, new_code, new_password_hash, password_matches, render, seal, send_email, send_sms, unflash_saved, unseal)
from tables import (ACCOUNT_TYPES, ID_COUNTRIES, ID_TYPES, MESSAGE_RETENTION_DAYS, REGIONS, ZONES, AccountRecovery, Approach, BankAccount, BlacklistedCredential, ChatMessage, Company, ContactChange, IdentityProof,
                    MeetingLine, Membership, MessageArchive, Profile, User, blacklisted, get_db, username_problem, utcnow)
router = APIRouter(tags=["settings"])
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
async def _picture(request: Request, saved: str, kind: str, key, what: str) -> None:
    unflash_saved(request, saved)
    try: await store_picture(saved, kind, key)
    except PictureError as e: raise FormError(str(e)) from None
    flash(request, f"{what} updated.", "success")
@router.get("/settings")
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):  # + the companies you own, which deleting your account deletes too
    me = await db.get(User, _uid(request))
    waiting = {c.channel: {"value": c.value, "left": max(0, int((c.expires_at - utcnow()).total_seconds())), "tries": max(0, CODE_ATTEMPTS - c.attempts)}
               for c in (await db.scalars(select(ContactChange).where(ContactChange.user_id == me.id))).all()}  # a new email / phone waiting for its code
    return render(request, "settings", {"me": me, "waiting": waiting, "ttl": CODE_TTL_MIN, "dev_delivery": "console" in delivery_modes().values(),
                                        "owned": (await db.scalars(select(Company.name).where(Company.owner_id == _uid(request)).order_by(Company.name))).all(),
                                        "retention_days": MESSAGE_RETENTION_DAYS, "bank": await db.get(BankAccount, _uid(request)), "account_types": ACCOUNT_TYPES,
                                        "identity": await db.get(IdentityProof, _uid(request)), "id_types": ID_TYPES, "id_countries": ID_COUNTRIES, "bank_verify": BANK_VERIFY,
                                        "id_verify": ID_VERIFY, "id_online": ONLINE, "region": request.state.user.get("region"), "timezone": request.state.user.get("tz"), "change_days": changelog.KEEP_DAYS})
# ---------- account: change username / delete account, each only with the current password ----------
async def _verified(db: AsyncSession, request: Request, password: str) -> User:
    uid = _uid(request)
    if rate_limited(f"password:{uid}", 10, 900): raise FormError("Too many wrong passwords; wait 15 minutes and try again.")  # a left-open session can't be used to guess it
    account = await db.get(User, uid)
    if not account or not await password_matches(password, account.password_hash): raise FormError("Wrong password: nothing was changed.")
    forgive(f"password:{uid}")  # only wrong ones count: a busy afternoon of right ones isn't guessing
    return account
@router.post("/settings/region")
async def save_region(request: Request, form: RegionForm = Depends(RegionForm.as_form), db: AsyncSession = Depends(get_db)):
    """Your region: currency (the salary you want defaults to it) + national holidays (your tasks calendar; no task can be due on them)."""
    account = await db.get(User, _uid(request))
    before = {"region": account.region, "time zone": account.timezone}
    account.region, account.timezone = ZONES[form.zone]
    region, tz, uid, name = account.region, account.timezone, account.id, account.username
    await db.commit()
    await changelog.person(db, uid, name, "region and time zone changed", changelog.diff(before, {"region": region, "time zone": tz}))
    flash(request, f"Region saved: {REGIONS[region][0]}, time zone {tz}. Times are now shown on your clock, amounts also in {REGIONS[region][1]}, and your calendar has its national holidays.", "success")
    return RedirectResponse("/settings#region", 303)
# ---------- email + phone (user asked): shown in settings; a new one counts only once the 6-digit code sent to IT comes back (10 minutes),
#            and the old one is told it changed. Your company's owner, co-owners, managers and HR see both (dashboard + member pages) ----------
LABEL = {"email": "email", "phone": "phone number"}
def _masked(channel: str, value: str) -> str: return mask_email(value) if channel == "email" else mask_phone(value)
async def _send_change(row: ContactChange) -> None:  # a fresh code to the NEW email / phone (only its HMAC is kept)
    code, now = new_code(), utcnow()
    row.code, row.expires_at, row.attempts, row.sent_at = code_hash(code), now + timedelta(minutes=CODE_TTL_MIN), 0, now
    text = f"Your {APP_NAME} code to confirm this {LABEL[row.channel]} is {code}. It expires in {CODE_TTL_MIN} minutes. Never share it."
    if row.channel == "email": await send_email(row.value, f"{APP_NAME}: confirm your new email: {code}", text + "\n\nIf you didn't ask for it, ignore this email: nothing changes without the code.")
    else: await send_sms(row.value, text)
async def _start_change(db: AsyncSession, account: User, channel: str, value: str) -> None:
    """Park the new email / phone and send its code; the account keeps the old one meanwhile."""
    if rate_limited(f"contact:{account.id}", 6, 3600): raise FormError("That's a lot of changes for one hour: try again later.")
    same = (lambda a, b: (a or "").lower() == (b or "").lower()) if channel == "email" else (lambda a, b: a == b)
    if same(value, account.email if channel == "email" else account.phone): raise FormError(f"That's already your {LABEL[channel]}.")
    if await db.scalar(select(User.id).where(func.lower(User.email) == value.lower() if channel == "email" else User.phone == value)): raise FormError(f"That {LABEL[channel]} belongs to another account.")
    if await blacklisted(db, **{channel: value}): raise FormError(f"That {LABEL[channel]} belonged to a deleted account and can't be used again.")
    row = await db.get(ContactChange, (account.id, channel)) or ContactChange(user_id=account.id, channel=channel, sends=0, created_at=utcnow())
    row.value, row.sends = value, (row.sends or 0) + 1
    await _send_change(row)  # a failed send raises before anything is saved
    db.add(row); await db.commit()
@router.post("/settings/email")
async def change_email(request: Request, form: EmailChangeForm = Depends(EmailChangeForm.as_form), db: AsyncSession = Depends(get_db)):
    account = await _verified(db, request, form.password)
    await _start_change(db, account, "email", str(form.email))
    flash(request, f"A code is on its way to {mask_email(str(form.email))}: type it below within {CODE_TTL_MIN} minutes. Until then your email stays {account.email}.", "info")
    return RedirectResponse("/settings#contact", 303)
@router.post("/settings/phone")
async def change_phone(request: Request, form: PhoneChangeForm = Depends(PhoneChangeForm.as_form), db: AsyncSession = Depends(get_db)):
    account = await _verified(db, request, form.password)
    await _start_change(db, account, "phone", form.phone)
    flash(request, f"A code was texted to {mask_phone(form.phone)}: type it below within {CODE_TTL_MIN} minutes. Until then your phone number stays {account.phone}.", "info")
    return RedirectResponse("/settings#contact", 303)
@router.post("/settings/contact/verify")
async def verify_contact(request: Request, form: ContactCodeForm = Depends(ContactCodeForm.as_form), db: AsyncSession = Depends(get_db)):
    uid, ch = _uid(request), form.channel
    if not (row := await db.get(ContactChange, (uid, ch))): raise FormError(f"There's no new {LABEL[ch]} waiting for a code: start again.")
    if row.expires_at < utcnow(): raise FormError(f"That code expired (a code works for {CODE_TTL_MIN} minutes): send a new one.")
    if row.attempts >= CODE_ATTEMPTS: raise FormError("Too many wrong codes: send a new one.")
    if not hmac.compare_digest(code_hash("".join(form.code.split())), row.code):
        row.attempts += 1; await db.commit()
        left = CODE_ATTEMPTS - row.attempts
        raise FormError(f"Wrong code ({left} {'try' if left == 1 else 'tries'} left)." if left else "Wrong code, and that was the last try: send a new one.")
    account, new = await db.get(User, uid), row.value
    old = account.email if ch == "email" else account.phone
    if ch == "email": account.email, account.email_verified_at = new, utcnow()
    else: account.phone, account.phone_verified_at = new, utcnow()
    await db.delete(row)
    try: await db.commit()
    except IntegrityError: await db.rollback(); raise FormError(f"That {LABEL[ch]} was taken by another account meanwhile: pick another one.") from None
    await changelog.person(db, uid, account.username, f"{LABEL[ch]} changed", f"{old} → {new} (confirmed with a code sent to the new one)")
    note = f"Your {APP_NAME} {LABEL[ch]} was just changed from this one to {_masked(ch, new)}. If this wasn't you, reset your password from the login page right away and tell support."
    try: await (send_email(old, f"{APP_NAME}: your email was changed", note) if ch == "email" else send_sms(old, note))  # the old one is told (the alarm if it wasn't them)
    except Exception as e: print(f"contact-change notice: {type(e).__name__}: {e}")
    flash(request, f"Your {LABEL[ch]} is now {new}: confirmed. The old one was told it changed.", "success")
    return RedirectResponse("/settings#contact", 303)
@router.post("/settings/contact/resend")
async def resend_contact(request: Request, form: ContactCodeForm = Depends(ContactCodeForm.as_form), db: AsyncSession = Depends(get_db)):
    if not (row := await db.get(ContactChange, (_uid(request), form.channel))): raise FormError(f"There's no new {LABEL[form.channel]} waiting for a code: start again.")
    if (wait := RESEND_COOLDOWN_S - (utcnow() - row.sent_at).total_seconds()) > 0: raise FormError(f"Wait {math.ceil(wait)}s before asking for another code.")
    if row.sends >= MAX_SENDS: raise FormError("That's the most codes for one change: cancel it and start again later.")
    row.sends += 1
    await _send_change(row); await db.commit()
    flash(request, f"A new code is on its way to {_masked(row.channel, row.value)}: it works for {CODE_TTL_MIN} minutes, and the old one doesn't any more.", "info")
    return RedirectResponse("/settings#contact", 303)
@router.post("/settings/contact/cancel")
async def cancel_contact(request: Request, form: ContactCodeForm = Depends(ContactCodeForm.as_form), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(ContactChange).where(ContactChange.user_id == _uid(request), ContactChange.channel == form.channel)); await db.commit()
    flash(request, f"Cancelled: your {LABEL[form.channel]} stays as it was.", "info")
    return RedirectResponse("/settings#contact", 303)
@router.post("/settings/username")
async def change_username(request: Request, form: UsernameForm = Depends(UsernameForm.as_form), db: AsyncSession = Depends(get_db)):
    account = await _verified(db, request, form.password)
    if form.username == account.username: raise FormError("That's already your username.")
    if why := await username_problem(db, form.username, me=account.id): raise FormError(why)  # (only its capitals changing is fine: it's still yours)
    old, account.username = account.username, form.username
    try: await db.commit()
    except IntegrityError:
        await db.rollback()
        raise FormError(f"The username {form.username} is taken.") from None
    await tell_names(db, account.id)  # colleagues' open chat pages + chat lists update it live
    await changelog.person(db, account.id, form.username, "username changed", f"{old} → {form.username}")
    flash(request, f"Username changed: {old} → {form.username}. Use the new one to log in.", "success")  # shown everywhere at once: pages read it from Postgres
    return RedirectResponse("/settings", 303)
@router.post("/settings/password")
async def change_password(request: Request, form: PasswordChangeForm = Depends(PasswordChangeForm.as_form), db: AsyncSession = Depends(get_db)):
    """A new password while logged in: needs the current one. This stays your one login; any recovery code still out stops working, and your email + phone are told (the alarm if it wasn't you)."""
    account = await _verified(db, request, form.password)
    if form.new_password != form.new_password_confirm: raise FormError("The two new passwords don't match.")
    if form.new_password == form.password: raise FormError("That's your current password: pick a new one.")
    if why := password_problem(form.new_password, account.username, account.email): raise FormError(why)
    email, phone, name, uid = account.email, account.phone, account.username, account.id
    account.password_hash = await new_password_hash(form.new_password)
    await db.execute(delete(AccountRecovery).where(AccountRecovery.user_id == uid))
    await db.commit()
    await changelog.person(db, uid, name, "password changed", "while logged in, with the current password")
    note = f"Your {APP_NAME} password was just changed from your settings. If this wasn't you, reset it right away from the login page and tell support."
    for send in (lambda: send_email(email, f"{APP_NAME}: your password was changed", note), *((lambda: send_sms(phone, note),) if phone else ())):
        try: await send()
        except Exception as e: print(f"password-change notice: {type(e).__name__}: {e}")
    flash(request, "Password changed: use the new one next time you log in.", "success")
    return RedirectResponse("/settings#account", 303)
@router.post("/settings/bank")
async def save_bank(request: Request, form: BankForm = Depends(BankForm.as_form), db: AsyncSession = Depends(get_db)):
    """Where your salary goes. Needs your password (a left-open session can't redirect someone's pay); the account number is stored
    only encrypted (system.seal, bound to your user id), and only its last 4 digits are ever shown again."""
    account = await _verified(db, request, form.password)
    try: checked = await verify_bank(form.account_number, form.routing, form.holder_name, form.country)  # with the bank, when BANK_VERIFY is on (Indian banks)
    except BankCheckError as e: raise FormError(f"Bank details not saved: {e}") from None
    bank = await db.get(BankAccount, account.id) or BankAccount(user_id=account.id)
    new = bank.account_last4 is None
    facts = lambda b, number: {"holder": b.holder_name, "bank": b.bank_name, "branch": b.branch, "country": banks.NAMES.get(b.country, b.country), "account type": b.account_type,
                               "account number": number, "bank code": f"{banks.show_code(b.country, b.routing)} ({banks.code_label(b.country)})" if b.routing else "", "SWIFT / BIC": b.swift, "bank address": b.bank_address}
    before = {} if new else facts(bank, unseal(f"bank:{account.id}", bank.account_enc) or f"••••{bank.account_last4}")  # the change log keeps full numbers (user's choice), encrypted
    bank.holder_name, bank.bank_name, bank.branch, bank.account_type = form.holder_name, form.bank_name, form.branch, form.account_type
    bank.country, bank.routing, bank.swift, bank.bank_address = form.country, form.routing, form.swift, form.bank_address  # any country (banks.py)
    bank.account_enc, bank.account_last4 = seal(f"bank:{account.id}", form.account_number), form.account_number[-4:]
    bank.verified_at, bank.verified_name = (utcnow(), checked["name_at_bank"]) if checked else (None, "")
    after = facts(bank, form.account_number)
    db.add(bank)
    await db.commit()
    await changelog.person(db, account.id, account.username, "bank details " + ("added" if new else "changed"),
                           (changelog.facts(after) if new else changelog.diff(before, after) or "saved again, nothing changed") + (f" · verified with the bank: {checked['name_at_bank']}" if checked else " · not verified"))
    flash(request, f"Bank details {'saved' if new else 'updated'}: {form.bank_name} {'IBAN' if form.country in banks.IBAN else 'account'} ending {form.account_number[-4:]}"
          + (f", verified with the bank ({checked['name_at_bank']})." if checked else f" (not verified: {unchecked_why(form.country)}).")
          + (" You can join or create companies once your identity proof is in too." if new else ""), "success")
    return RedirectResponse("/settings#bank", 303)
@router.post("/settings/identity")
async def save_identity(request: Request, form: IdentityForm = Depends(IdentityForm.as_form), db: AsyncSession = Depends(get_db)):
    """Your identity proof: the ID's type, number (stored only encrypted) and name, and a photo of it (re-encoded, never public)."""
    unflash_saved(request, form.photo)
    try:
        account = await _verified(db, request, form.password)
        try: checked = await verify_id(form.id_type, form.id_number, form.name_on_id, account.dob)  # online, when ID_VERIFY is on and the type allows it
        except IdCheckError as e: raise FormError(f"Identity proof not saved: {e}") from None
    except FormError: discard_upload(form.photo); raise
    try: await store_id_photo(form.photo, "u", account.id)
    except PictureError as e: raise FormError(f"ID photo: {e}") from None
    proof = await db.get(IdentityProof, account.id) or IdentityProof(user_id=account.id)
    new = proof.number_last4 is None
    facts = lambda kind, number, name: {"document": ID_TYPES.get(kind, kind), "number": number, "name on it": name}
    before = {} if new else facts(proof.id_type, unseal(f"identity:{account.id}", proof.number_enc) or f"••••{proof.number_last4}", proof.name_on_id)
    proof.id_type, proof.name_on_id, proof.number_enc, proof.number_last4 = form.id_type, form.name_on_id, seal(f"identity:{account.id}", form.id_number), form.id_number[-4:]
    proof.verified_at, proof.verified_by, proof.verified_name = (utcnow(), checked["by"], checked["name"]) if checked else (None, "", "")
    db.add(proof)
    await db.commit()
    after = facts(form.id_type, form.id_number, form.name_on_id)
    await changelog.person(db, account.id, account.username, "identity proof " + ("added" if new else "changed"),
                           (changelog.facts(after) if new else changelog.diff(before, after) or "saved again") + " · photo of it uploaded" + (f" · verified online: {checked['name']}" if checked else ""))
    await tell_names(db, account.id)  # the name on it is shown beside their username in chats: updated live
    flash(request, f"Identity proof {'saved' if new else 'updated'}: {ID_TYPES[form.id_type]} ending {form.id_number[-4:]}, with its photo"
          + (f", verified online ({checked['name']})." if checked else " (format and check digit checked)."), "success")
    return RedirectResponse("/settings#identity", 303)
@router.post("/settings/delete")
async def delete_account(request: Request, form: DeleteAccountForm = Depends(DeleteAccountForm.as_form), db: AsyncSession = Depends(get_db)):
    account = await _verified(db, request, form.password)
    uid, name = account.id, account.username
    owned = (await db.scalars(select(Company.id).where(Company.owner_id == uid))).all()
    companies = [await _for_deletion(db, cid) for cid in owned]  # going with the account: the same rules as deleting a company by hand, and its people are told the same way
    for c in companies:
        if any(m.position == "co_owner" for m in c.members): raise FormError(f"{c.name} has co-owners, and it can't go without them: hand it over to one, or ask them to approve deleting it, then delete your account.")
        if why := await deletion_block(db, c): raise FormError(f"You own {c.name}, and it goes with your account. {why}")
    cv = await db.scalar(select(Profile.resume_file).where(Profile.user_id == uid))
    # message history (job offers sent + received) is kept MESSAGE_RETENTION_DAYS, then purged; the credentials are blacklisted for good
    offers = (await db.scalars(select(Approach).where(or_(Approach.user_id == uid, Approach.by_user_id == uid))
                               .options(joinedload(Approach.company, innerjoin=True), joinedload(Approach.by)))).all()
    others = dict((await db.execute(select(User.id, User.username).where(User.id.in_({a.user_id for a in offers})))).all())
    purge = utcnow() + timedelta(days=MESSAGE_RETENTION_DAYS)
    db.add_all(MessageArchive(deleted_user=name, direction="received" if a.user_id == uid else "sent", company=a.company.name, sent_at=a.created_at, purge_after=purge,
                              other_party=(a.by.username if a.by else "") if a.user_id == uid else others.get(a.user_id, ""), message=a.message,
                              salary=f"{a.salary_amount:,} {a.salary_currency} per {a.salary_period}" if a.salary_amount else "") for a in offers)
    await db.execute(update(ChatMessage).where(ChatMessage.sender_id == uid, ChatMessage.purge_after.is_(None)).values(purge_after=purge))  # their chat messages: readable 30 more days
    await db.execute(update(MeetingLine).where(MeetingLine.speaker_id == uid, MeetingLine.purge_after.is_(None)).values(purge_after=purge))  # what they said / wrote in meetings: the same
    workplaces = (await db.scalars(select(Membership.company_id).where(Membership.user_id == uid))).all()
    for kind, value in (("username", name), ("email", account.email), ("phone", account.phone)):
        if value and not await db.get(BlacklistedCredential, digest := identity_digest(kind, value)): db.add(BlacklistedCredential(digest=digest, kind=kind))
    for c in companies: await erase_company(db, c)  # everyone in it told, its files gone, its change log kept a while (each commits)
    await db.execute(delete(User).where(User.id == uid))  # Postgres cascades: memberships, profile, offers to you
    await db.commit()
    await hub.kick(uid)  # their open tabs' live connections end with the account
    until = changelog.retire("u", uid)  # their change log stays 30 days, then it's erased (changelog.purge)
    await changelog.person(db, uid, name, "account deleted", f"this change log is kept until {until}, then erased", companies=[c for c in workplaces if c not in owned])
    remove_picture("u", uid); remove_id_photo("u", uid)  # and the files on disk: picture, ID photo, CV
    shutil.rmtree(ai_dir(uid), ignore_errors=True)  # the pictures they showed the Deminic AI (its messages went with the account)
    if cv: discard_upload(cv)
    resp = RedirectResponse("/", 303)
    await end_session(request, resp)
    flash(request, f"Account {name} deleted{f', with the {len(owned)} compan' + ('y' if len(owned) == 1 else 'ies') + ' it owned' if owned else ''}. Goodbye!", "info")
    return resp
@router.post("/settings/picture")
async def upload_picture(request: Request, form: PictureForm = Depends(PictureForm.as_form), db: AsyncSession = Depends(get_db)):
    await _picture(request, form.file, "u", _uid(request), "Profile picture")
    await changelog.person(db, _uid(request), request.state.user["username"], "profile picture changed")
    return RedirectResponse("/settings", 303)
@router.post("/settings/picture/remove")
async def delete_picture(request: Request, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    gone = remove_picture("u", _uid(request))
    if gone: await changelog.person(db, _uid(request), request.state.user["username"], "profile picture removed")
    flash(request, "Profile picture removed." if gone else "You don't have a profile picture.", "info")
    return RedirectResponse("/settings", 303)
async def _top(db: AsyncSession, request: Request, company_id: uuid.UUID) -> Membership:  # owner / co-owner of this company, or the same error
    m = await db.get(Membership, (_uid(request), company_id), options=[joinedload(Membership.company, innerjoin=True)])
    if not can_edit(m): raise FormError("Only the owner or a co-owner can change a company's logo.")
    return m
@router.post("/companies/{company_id}/logo")
async def upload_logo(request: Request, company_id: uuid.UUID, form: PictureForm = Depends(PictureForm.as_form), db: AsyncSession = Depends(get_db)):
    try: m = await _top(db, request, company_id)
    except FormError: unflash_saved(request, form.file); discard_upload(form.file); raise
    await _picture(request, form.file, "c", company_id, f"{m.company.name}'s logo")  # replace only: a company can't go back to no logo
    changelog.company(company_id, "logo changed")
    return RedirectResponse(f"/workplaces#c-{company_id}", 303)
