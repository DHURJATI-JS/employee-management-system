import importlib.metadata as md
from fastapi import APIRouter, Request
from decoraters import cache, public
from resume import MAX_PAGES, PARSE_TIMEOUT_S
from system import (AI_ASSIST, BANK_VERIFY, CODE_ATTEMPTS, CODE_TTL_MIN, MAX_SENDS, MAX_UPLOAD_MB, RESEND_COOLDOWN_S, SESSION_TTL, SIGNUPS_PER_IP_HOUR, _load_blocklist,
                    render)
router = APIRouter(tags=["info"])
PACKAGES = ["fastapi", "starlette", "uvicorn", "pydantic", "sqlalchemy", "asyncpg", "redis", "jinja2", "python-multipart", "itsdangerous",
            "python-dotenv", "email-validator", "phonenumbers", "rcssmin", "rjsmin", "pypdf", "anthropic", "cryptography", "websockets", "tzdata", "pillow", "psutil"]
def _license(meta) -> str:  # from the installed package's own metadata, so it's never out of date
    if lic := meta.get("License-Expression"): return lic
    for c in meta.get_all("Classifier") or []:
        if c.startswith("License :: OSI Approved :: "): return c.rsplit(" :: ", 1)[1]
    lic = (meta.get("License") or "").strip()
    return lic if lic and len(lic) <= 40 and "\n" not in lic else "see project"
def _packages() -> list[dict]:
    out = []
    for name in PACKAGES:
        try: meta = md.metadata(name)
        except md.PackageNotFoundError: continue
        out.append({"name": meta["Name"], "version": meta["Version"], "license": _license(meta), "url": f"https://pypi.org/project/{meta['Name']}/"})
    return out
@router.get("/credits")
@public
@cache.memoize(timeout=300)
async def credits(request: Request):
    limits = {"code_ttl": CODE_TTL_MIN, "attempts": CODE_ATTEMPTS, "cooldown": RESEND_COOLDOWN_S, "max_sends": MAX_SENDS, "per_ip": SIGNUPS_PER_IP_HOUR,
              "upload_mb": f"{MAX_UPLOAD_MB:g}", "session_h": f"{SESSION_TTL / 3600:g}", "blocked": _load_blocklist()["count"], "pdf_pages": MAX_PAGES, "pdf_timeout": PARSE_TIMEOUT_S}
    return render(request, "credits", {"packages": _packages(), "limits": limits, "ai_engine": AI_ASSIST, "bank_verify": BANK_VERIFY})
