"""The company AI's controls (companyai.py has the logic). The owner and co-owners: its promotion review's mode (off / suggest / auto), running the review now, and applying
or dismissing what it suggested (the same rules as changing a position by hand). Whoever gives tasks: "who's best for this task?"."""
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
import changelog, companyai
from assist import rate_limited
from form import ActionForm, AIBestForm, AIModeForm
from guard import halted_reason
from meet import ai
from routers.companies import assignable, can_manage
from routers.tasks import can_assign
from system import AI_STATUS, FormError, flash, render
from tables import POSITIONS, TASKERS, TOP, Company, Membership, PromotionSuggestion, get_db, utcnow
router = APIRouter(tags=["companyai"])
MODES = {"off": "off", "suggest": "on: it suggests, you decide", "auto": "on: it promotes people by itself"}
async def _top(db: AsyncSession, request: Request, company_id: uuid.UUID) -> tuple[Company, Membership]:  # the company (locked, members + users loaded) + you: the owner or a co-owner
    uid = int(request.state.user["user_id"])
    c = await db.get(Company, company_id, with_for_update=True, options=[selectinload(Company.members).joinedload(Membership.user, innerjoin=True), selectinload(Company.salaries)])
    me = next((m for m in c.members if m.user_id == uid), None) if c else None
    if not me or me.position not in TOP: raise FormError("Only the owner or a co-owner can use the company AI's controls.")  # same answer whether it exists or not
    return c, me
back = lambda company_id: RedirectResponse(f"/companies/{company_id}/dashboard#ai", 303)
@router.post("/companies/{company_id}/ai/mode")
async def ai_mode(request: Request, company_id: uuid.UUID, form: AIModeForm = Depends(AIModeForm.as_form), db: AsyncSession = Depends(get_db)):
    c, me = await _top(db, request, company_id)
    was, c.ai_promotions, name = c.ai_promotions, form.mode, me.user.username
    await db.commit()
    if was != form.mode: changelog.company(company_id, "AI promotion review changed", f"{was} → {form.mode}", by=name)
    if was == "off" and form.mode != "off" and not rate_limited(f"ai-review:{company_id}", 1, 600): ai.put(lambda: companyai.review(company_id))  # its first look, right away (flipping the switch can't flood the AI queue)
    flash(request, f"AI promotion review {MODES[form.mode]}." + (" It looks at everyone once a day; its first look has started." if was == "off" and form.mode != "off" else ""), "success")
    return back(company_id)
@router.post("/companies/{company_id}/ai/review")
async def ai_review(request: Request, company_id: uuid.UUID, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    c, _me = await _top(db, request, company_id)
    if c.ai_promotions == "off": raise FormError("Turn the AI promotion review on first.")
    if rate_limited(f"ai-review:{company_id}", 1, 600): raise FormError("It ran a few minutes ago: try again in a while.")
    ai.put(lambda: companyai.review(company_id))
    flash(request, "The AI is reading the shortlist's reviews: about a minute per person on this server. What it decides appears here, and you're notified.", "info")
    return back(company_id)
# ---------- "who's best for this task?": whoever gives tasks here (the owner, co-owners, managers, HR), among the people ranked below them ----------
async def _best(db: AsyncSession, request: Request, company_id: uuid.UUID, task: str) -> dict:
    me = await db.get(Membership, (int(request.state.user["user_id"]), company_id), options=[joinedload(Membership.company, innerjoin=True).selectinload(Company.members).joinedload(Membership.user, innerjoin=True)])
    if not me or me.position not in TASKERS: raise HTTPException(404, "There's no such page.")  # same answer whether it exists or not
    people = [m for m in me.company.members if can_assign(me, m.position)]
    return {"c": me.company, "me": me, "task": task, "rows": await companyai.rank(db, me.company, people, task) if task and people else [], "nobody": not people, "positions": POSITIONS, "ready": AI_STATUS.get("ready"), "pace_text": companyai.pace_text, "verdict": None}
@router.get("/companies/{company_id}/ai/best")
async def best_page(request: Request, company_id: uuid.UUID, task: str = "", db: AsyncSession = Depends(get_db)):  # instant: the ranking; the local AI only when asked (POST)
    return render(request, "company_best", await _best(db, request, company_id, " ".join(task.split())[:500]))
@router.post("/companies/{company_id}/ai/best")
async def best_ask(request: Request, company_id: uuid.UUID, form: AIBestForm = Depends(AIBestForm.as_form), db: AsyncSession = Depends(get_db)):
    """The local AI reads the top few (reviews, work, profile, the words of their CVs): about a minute on this server, and limited, like every call to it."""
    page = await _best(db, request, company_id, form.task)
    if rate_limited(f"ai-best:{request.state.user['user_id']}", 10, 3600): raise FormError("That's a lot of questions to the AI in a short time: try again later.")
    await db.commit()  # (no transaction stays open while the model thinks)
    page["verdict"] = await companyai.verdict(form.task, page["rows"]) or {"best": "", "why": ""}
    return render(request, "company_best", page)
@router.post("/companies/{company_id}/ai/calls/{call_id}/{action}")
async def ai_call(request: Request, company_id: uuid.UUID, call_id: int, action: str, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """apply / dismiss one suggestion. Applying goes by the same rules as a position change by hand: only people below you, only positions you may give."""
    if action not in ("apply", "dismiss"): raise HTTPException(404, "There's no such action.")
    c, me = await _top(db, request, company_id)
    s = await db.get(PromotionSuggestion, call_id)
    if not s or s.company_id != company_id or s.status != "suggested": raise FormError("That suggestion has already been decided.")
    m = next((x for x in c.members if x.user_id == s.user_id), None)
    s.decided_by, s.decided_at = me.user.username, utcnow()
    if action == "dismiss":
        s.status, name = "dismissed", m.user.username if m else "them"
        await db.commit()
        flash(request, f"Dismissed: {name} stays as they are. The AI won't ask again for a week.", "info")
        return back(company_id)
    if not m or m.position != s.from_position:
        s.status = "stale"
        await db.commit()
        raise FormError("That person has left, or their position has changed since: the suggestion no longer applies.")
    if c.halted_at: raise FormError(halted_reason(c, "change anyone's position"))
    if not can_manage(me, m) or s.to_position not in assignable(me, c): raise FormError("You can't give that position to that person, or the company no longer offers it.")
    name, to = m.user.username, s.to_position
    await companyai.promote(db, c, m, to, me.user.username)
    s.status = "applied"
    await db.commit()
    flash(request, f"{name} promoted to {companyai.label(to)}: they've been told.", "success")
    return back(company_id)
