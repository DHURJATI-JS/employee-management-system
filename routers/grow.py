"""AI growth reviews (grow.py): your profile (on /profile) and a company you own or co-own (/companies/{id}/grow). Instant suggestions
always show; the AI review needs the local AI (its button is disabled with an alert while it isn't available) and runs in the background."""
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from assist import rate_limited
from form import ActionForm
from grow import get_review, instant_company, start_review
from routers.companies import can_edit
from system import AI_STATUS, FormError, flash, render
from tables import POSITIONS, Company, Membership, get_db
router = APIRouter(tags=["grow"])
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
def _can_start(request: Request) -> None:
    if not AI_STATUS["ready"]: raise FormError(f"The AI isn't available right now: {AI_STATUS['detail']}. The instant suggestions still work.")
    if rate_limited(f"ai-review:{_uid(request)}", 6, 3600): raise FormError("That's a lot of AI reviews in a short time; try again in a while.")
def _status(review) -> JSONResponse: return JSONResponse({"status": review.status if review else "none"}, headers={"Cache-Control": "no-store"})
@router.post("/profile/review")
async def review_profile(request: Request, _: ActionForm = Depends(ActionForm.as_form)):
    _can_start(request)
    flash(request, "The AI is reading your profile, CV and picture: this takes a minute or two." if await start_review("u", _uid(request)) else "A review is already running.", "info")
    return RedirectResponse("/profile#grow", 303)
@router.get("/profile/review/status")
async def profile_review_status(request: Request, db: AsyncSession = Depends(get_db)): return _status(await get_review(db, "u", _uid(request)))
async def _top(db: AsyncSession, request: Request, company_id: uuid.UUID) -> Membership:  # owner / co-owner, or the same 404
    m = await db.get(Membership, (_uid(request), company_id), options=[joinedload(Membership.company, innerjoin=True).options(selectinload(Company.salaries), selectinload(Company.members))])
    if not can_edit(m): raise HTTPException(404, "Only the owner or a co-owner can see a company's growth suggestions.")
    return m
@router.get("/companies/{company_id}/grow")
async def company_grow(request: Request, company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    m = await _top(db, request, company_id)
    return render(request, "grow_company", {"c": m.company, "instant": await instant_company(db, m.company, m.company.members, _uid(request)),
                                            "review": await get_review(db, "c", company_id), "positions": POSITIONS})
@router.post("/companies/{company_id}/review")
async def review_company(request: Request, company_id: uuid.UUID, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    await _top(db, request, company_id)
    _can_start(request)
    flash(request, "The AI is reviewing the company, its logo and the market: this takes a minute or two." if await start_review("c", company_id) else "A review is already running.", "info")
    return RedirectResponse(f"/companies/{company_id}/grow", 303)
@router.get("/companies/{company_id}/review/status")
async def company_review_status(request: Request, company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _top(db, request, company_id)
    return _status(await get_review(db, "c", company_id))
