"""Downloading the change logs (changelog.py), as plain text: yours (settings), and a company's for its owner, co-owners, managers and HR
(PAYERS; its dashboard). Both need your password, like bank details: they hold full ID and bank numbers. Each download is itself logged."""
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
import audit, changelog
from form import PasswordForm
from payroll import company_tz
from routers.settings import _verified
from system import APP_NAME, FormError, attachment
from tables import PAYERS, Company, Membership, get_db
router = APIRouter(tags=["changes"])
def _file(text: str, name: str) -> Response:
    return Response(text, media_type="text/plain; charset=utf-8", headers={"Content-Disposition": attachment(name), "Cache-Control": "private, no-store"})
@router.post("/settings/changes")
async def my_changes(request: Request, form: PasswordForm = Depends(PasswordForm.as_form), db: AsyncSession = Depends(get_db)):
    """Everything that changed on your account, profile and documents (full numbers), and what you did yourself."""
    account = await _verified(db, request, form.password)
    try: zone = ZoneInfo(request.state.user.get("tz") or "UTC")
    except (ZoneInfoNotFoundError, ValueError): zone = ZoneInfo("UTC")
    changelog.flush()  # what's still queued is in the file first
    text = changelog.as_text("u", account.id, f"{APP_NAME} · the change log of {account.username}", zone, audit.who(request.state.user))
    changelog.add("u", account.id, "change log downloaded")
    return _file(text, f"{APP_NAME.lower()}-changes-{account.username}-{datetime.now(zone):%Y%m%d-%H%M}.txt")
@router.post("/companies/{company_id}/changes")
async def company_changes(request: Request, company_id: uuid.UUID, form: PasswordForm = Depends(PasswordForm.as_form), db: AsyncSession = Depends(get_db)):
    """Every change in the company and its members' profile + document changes: for its owner, co-owners, managers and HR."""
    m = await db.get(Membership, (int(request.state.user["user_id"]), company_id))
    if not m or m.position not in PAYERS: raise FormError("Only the company's owner, co-owners, managers and HR can download its change log.")
    await _verified(db, request, form.password)
    c = await db.get(Company, company_id)
    changelog.flush()
    zone = company_tz(c)
    text = changelog.as_text("c", company_id, f"{APP_NAME} · the change log of {c.name} (company {company_id})", zone, audit.who(request.state.user))
    changelog.company(company_id, "change log downloaded")
    return _file(text, f"{APP_NAME.lower()}-changes-{''.join(ch if ch.isalnum() else '-' for ch in c.name)[:40]}-{datetime.now(zone):%Y%m%d-%H%M}.txt")
