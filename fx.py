"""Exchange rates, so the payroll guard compares a company's money in one currency. A company's net worth can be in any supported
currency (INR, say) while its salaries are in another (USD): the employee liability, the clearance and every payout are converted into
the net worth's currency before they're compared or taken off it, and a deposit in yet another currency is converted as it's added.
A salary can be paid in any of them too (user asked): its gross is converted into the salary currency for the minimum + the clearance,
and the payslip keeps the rate used.
Salary matching uses them too (pay_in): a salary range wanted in INR is compared with companies paying in USD, and the other way round.
Rates (units per US dollar) come from FX_URL (ExchangeRate-API's open endpoint by default: no key, updated daily; credited wherever
rates are shown), refreshed every FX_REFRESH_S by the guard loop and kept in Postgres (fx_rates), so a restart or an outage of the
rates service uses the last good ones. A fetch that moves any rate by more than half is refused (a broken or tampered answer). Until a
fetch has ever worked, built-in rates (30 Oct 2026) are used and shown as approximate."""
import asyncio, json, os, urllib.request
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from system import APP_NAME, templates
from tables import CURRENCIES, FxRate, Session, utcnow
FX_URL = os.getenv("FX_URL", "https://open.er-api.com/v6/latest/USD")
FX_REFRESH_S = int(os.getenv("FX_REFRESH_S", str(6 * 3600)))
FALLBACK = {"USD": "1", "INR": "95.292116", "EUR": "0.895934", "GBP": "0.749154", "AUD": "1.422859", "CAD": "1.398868", "SGD": "1.280873", "AED": "3.6725"}  # 30 Oct 2026
_fx: dict = {"per_usd": {k: Decimal(v) for k, v in FALLBACK.items()}, "at": None}  # at = when the rates in use were fetched (None = built-in)
def convert(amount: int | None, frm: str | None, to: str | None) -> int | None:  # whole units, rounded half up
    if amount is None or not frm or not to or frm == to: return amount
    r = _fx["per_usd"]
    return int((Decimal(amount) * r[to] / r[frm]).to_integral_value(ROUND_HALF_UP))
def pay_in(amount: int | None, cur: str, period: str, to_cur: str, to_period: str) -> int | None:  # a salary per month / year, as to_cur per to_period (x12 or ÷12, then the day's rate)
    if amount is None: return None
    k = {("month", "year"): Decimal(12), ("year", "month"): Decimal(1) / 12}.get((period, to_period), Decimal(1))
    return convert(int((Decimal(amount) * k).to_integral_value(ROUND_HALF_UP)), cur, to_cur)
def rate(frm: str, to: str) -> Decimal: return _fx["per_usd"][to] / _fx["per_usd"][frm]  # 1 frm = rate to, today (the pay dialog converts with it as you type)
def rate_text(frm: str, to: str, r: Decimal | None = None) -> str: return f"1 {frm} = {float(rate(frm, to) if r is None else r):.6g} {to}"  # "1 USD = 95.2921 INR"; r: a rate kept on a payslip
def rate_line(frm: str, to: str) -> str:  # "1 USD = 95.2921 INR · rates by ExchangeRate-API, updated 3 h ago"
    at = _fx["at"]
    age = "built-in, approximate: the rates service hasn't answered yet" if not at else f"updated {max(0, int((utcnow() - at).total_seconds() // 3600))} h ago"
    return f"{rate_text(frm, to)} · rates by ExchangeRate-API, {age}"
def _fetch() -> dict[str, Decimal]:
    req = urllib.request.Request(FX_URL, headers={"Accept": "application/json", "User-Agent": f"{APP_NAME} payroll guard"})
    with urllib.request.urlopen(req, timeout=20) as r: data = json.loads(r.read(500_000))
    if data.get("result") != "success" or data.get("base_code") != "USD": raise ValueError("unexpected answer")
    rates = {k: Decimal(str(data["rates"][k])) for k in CURRENCIES}
    if rates["USD"] != 1 or any(not v.is_finite() or v <= 0 for v in rates.values()): raise ValueError("invalid rates")
    if _fx["at"] and any(not (Decimal("0.5") < v / _fx["per_usd"][k] < Decimal("1.5")) for k, v in rates.items()): raise ValueError("a rate moved by more than half: kept the last ones")
    return rates
async def refresh(force: bool = False) -> bool:
    """Fetch new rates when the ones in use are older than FX_REFRESH_S (or force): saved to Postgres, then used. False = kept the old ones."""
    if not force and _fx["at"] and utcnow() - _fx["at"] < timedelta(seconds=FX_REFRESH_S): return False
    try: rates = await asyncio.to_thread(_fetch)
    except Exception as e: print(f"exchange rates: {type(e).__name__}: {e}"); return False
    now = utcnow()
    async with Session() as db:
        stmt = insert(FxRate).values([{"currency": k, "per_usd": v, "fetched_at": now} for k, v in rates.items()])
        await db.execute(stmt.on_conflict_do_update(index_elements=["currency"], set_={"per_usd": stmt.excluded.per_usd, "fetched_at": stmt.excluded.fetched_at}))
        await db.commit()
    _fx.update(per_usd=rates, at=now)
    return True
async def start() -> None:
    """At startup: the last saved rates (if every currency has one), then fresh ones if those are old."""
    try:
        async with Session() as db: rows = (await db.scalars(select(FxRate))).all()
        if {r.currency for r in rows} >= set(CURRENCIES): _fx.update(per_usd={r.currency: Decimal(r.per_usd) for r in rows}, at=min(r.fetched_at for r in rows))
    except Exception as e: print(f"exchange rates: saved ones unreadable ({type(e).__name__})")
    await refresh()
templates.env.globals.update(convert=convert, rate_line=rate_line, pay_in=pay_in, fx_rate=rate, rate_text=rate_text)  # {{ convert(amount, 'USD', 'INR') }}
