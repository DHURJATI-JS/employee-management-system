"""/find: the AI company finder behind the sidebar's link (logged-in only; off in single company mode). The prompt is read (finder.read: rules + the local
AI), matched against every listed company's real data (finder.find) and shown with what each company meets or misses, plus the points
the prompt left out. Nothing is stored. Staff can't use it during their company's working hours (like search)."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
import finder
from assist import rate_limited
from form import FindForm
from routers.discover import on_duty, solo
from system import FormError, render
from tables import CURRENCIES, JOIN_AS, POSITIONS, Profile, get_db
router = APIRouter(tags=["find"])
EXAMPLES = ["a python company that pays me 10k-15k INR a month, pays out in USD and lets me work for other companies",
            "freelance design work in Pune, at most 6 hours a day, no weekends", "backend developer job paying at least $2,000 a month where I can become a manager"]
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
@router.get("/find")
async def find_page(request: Request, db: AsyncSession = Depends(get_db)):
    if busy := await on_duty(request, db, _uid(request)) or await solo(request, db): return busy  # (single company mode: off)
    return render(request, "find", {"prompt": "", "result": None, "examples": EXAMPLES})
@router.post("/find")
async def find(request: Request, form: FindForm = Depends(FindForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    if busy := await on_duty(request, db, uid) or await solo(request, db): return busy
    if rate_limited(f"find:{uid}", 12, 300): raise FormError("That's a lot of searches in a short time: wait a few minutes.")
    await db.commit()  # no transaction stays open while the model thinks: past a minute Postgres would close the connection and the page would crash (the fuzz test, with the AI busy)
    crit, how = await finder.read(form.prompt)
    result = await finder.find(db, uid, crit)
    profile = await db.get(Profile, uid)
    return render(request, "find", {"prompt": form.prompt, "result": result, "crit": crit, "how": how, "examples": EXAMPLES, "understood": finder.understood(crit),
                                    "missing": finder.missing(crit, result["you"], request.state.user, profile), "positions": POSITIONS, "join_as": JOIN_AS, "currencies": CURRENCIES})
