"""The platform's side of the shop (/shop/admin), for the usernames in PLATFORM_ADMINS (.env): what the marketplace earned (commission on
successful sales: delivered + paid), what's still on its way, every seller's balance with the platform (shop.ledger: online sales the
platform collected minus its commission, against the commission owed on cash-on-delivery sales), and settlements recorded both ways.
Anyone else gets a 404: the page doesn't exist for them."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
import audit
from form import SettlementForm
from guard import notify
import shop
from system import FormError, flash, render
from tables import CURRENCIES, Company, Order, Settlement, Shop, User, get_db, utcnow
router = APIRouter(tags=["shop admin"])
def _admin(request: Request) -> str:
    if (name := request.state.user["username"]).lower() not in shop.PLATFORM_ADMINS: raise HTTPException(404, "Not found.")
    return name
@router.get("/shop/admin")
async def admin_page(request: Request, db: AsyncSession = Depends(get_db)):
    _admin(request)
    won = (await db.execute(select(Order.currency, func.count(), func.sum(Order.total_minor), func.sum(Order.commission_minor)).where(Order.status == "delivered", Order.payment_status.in_(("paid", "collected")))
                            .group_by(Order.currency))).all()
    moving = (await db.execute(select(Order.currency, func.count(), func.sum(Order.total_minor), func.sum(Order.commission_minor)).where(Order.status.in_(("placed", "packed", "shipped", "out_for_delivery")))
                               .group_by(Order.currency))).all()
    shops = (await db.execute(select(Shop, Company.name).join(Company, Company.id == Shop.company_id).order_by(Company.name))).all()
    books = await shop.ledgers(db, [s.company_id for s, _ in shops]) if shops else {}
    balances = [(s, name, books[s.company_id]) for s, name in shops]
    recent = (await db.scalars(select(Settlement).order_by(Settlement.at.desc()).limit(30))).all()
    stuck = (await db.scalars(select(Order).where(Order.payment_status == "refund_pending", Order.refund_tries >= shop.MAX_REFUND_TRIES).order_by(Order.id).limit(100))).all()  # Cashfree kept turning these down: a person has to refund them
    return render(request, "shop_admin", {"won": won, "moving": moving, "balances": balances, "recent": recent, "stuck": stuck, "currencies": CURRENCIES, "pay": shop.pay_mode(), "commission": shop.COMMISSION_PERCENT})
@router.post("/shop/admin/settlements")
async def settle(request: Request, form: SettlementForm = Depends(SettlementForm.as_form), db: AsyncSession = Depends(get_db)):
    """Money moved: paid out to a seller (their online sales minus commission) or received from one (commission on cash orders)."""
    by = _admin(request)
    c = await db.get(Company, form.company_id)
    if not c or not await db.get(Shop, form.company_id): raise FormError("There's no such shop.")
    if await db.scalar(select(Settlement.id).where(Settlement.company_id == c.id, Settlement.reference == form.reference)): raise FormError(f"Reference {form.reference} is already on a settlement with {c.name}: each one moves money once.")
    db.add(Settlement(company_id=c.id, company_name=c.name, direction=form.direction, amount_minor=shop.to_minor(form.amount), currency=form.currency, reference=form.reference, note=form.note, by_name=by[:32], at=utcnow()))
    owner = await db.get(User, c.owner_id)
    what = f"{shop.price(form.currency, shop.to_minor(form.amount))} {'paid out to' if form.direction == 'to_seller' else 'received from'} {c.name} (reference {form.reference})"
    if owner: await notify(db, [owner], c, "info", f"Shop settlement: {shop.price(form.currency, shop.to_minor(form.amount))}", f"{shop.APP_NAME} recorded {what}.", f"/companies/{c.id}/shop#money", f"settlement:{form.reference}")
    await db.commit()
    audit.log(request.state.user, "shop", f"settlement recorded: {what}", "ok")
    flash(request, f"Recorded: {what}.", "success")
    return RedirectResponse("/shop/admin", 303)
