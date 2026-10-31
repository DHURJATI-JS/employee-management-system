"""/terms: the site's own terms & conditions (user asked): how to use it, that it's a student-grade project, that its owner isn't liable for
any damages, and that it isn't free to use: ask the owner first (CONTACT_EMAIL and OWNER_NUMBER in .env, shown on the page and never in the code).
Each company's own joining terms are separate (tables.STANDARD_TERMS + what the company writes, accepted before joining)."""
from fastapi import APIRouter, Request
from decoraters import cache, public
from system import OWNER_NUMBER, render
router = APIRouter(tags=["info"])
UPDATED = "31 October 2026"
@router.get("/terms")
@public
@cache.memoize(timeout=300)
async def terms(request: Request): return render(request, "terms", {"whatsapp": (OWNER_NUMBER, "".join(filter(str.isdigit, OWNER_NUMBER))), "updated": UPDATED})  # (as shown, the digits a wa.me link wants)
