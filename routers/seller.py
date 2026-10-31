"""A company's shop (routers/shop.py is the buyers' side, shop.py the rules, shopai.py the AI, shopstats.py the numbers). Opened by the owner /
a co-owner (while creating the company, or here), run by its managers (SHOP_MANAGERS: products, prices, photos + videos, settings, competitor
tracking, the AI's briefs), orders packed + shipped by them and the shop staff they pick: courier + tracking number (or their own delivery
person, who gets a no-login link), journey steps, delivered (the buyer's code, or a photo), cancellations, returns + refunds; questions the
AI couldn't answer; replies to reviews. The dashboard: sales by day / category / status / payment (pandas -> Chart.js), the monthly Excel
statement (+ a matplotlib chart) and the balance with the platform: that money is for SHOP_MONEY only (owner, co-owners, managers, HR;
not the shop staff), and HR who aren't shop staff get just that part. The live-tracking webhook's secret is made here (shown once)."""
import asyncio, io, json, re, secrets, uuid
import changelog
from datetime import UTC, date, datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload
from assist import rate_limited
from form import ActionForm, AnswerForm, CompetitorForm, DeliverForm, NewProductForm, ProductForm, ProductMediaForm, ReasonForm, RefundForm, ShipForm, ShopOpenForm, ShopSettingsForm, TrackForm
from guard import notify
from media import PictureError, discard_upload, store_shop_photo, store_video
from payroll import company_tz
import shop
from shop import COMMISSION_PERCENT, COURIERS, ShopError, add_event, cancel, delivered, delivery_code, items_of, ledger, photo_file, price, proof_photo_file, to_minor, video_file
import shopai, shopstats
from presence import hub
from system import FormError, attachment, flash, render, safe_referer, same_text, seal, unflash_saved, unseal
from tables import (CURRENCIES, SHOP_CATEGORIES, SHOP_MANAGERS, SHOP_MONEY, TOP, Company, CompetitorLink, Membership, Order, PriceSnapshot, Product, ProductInsight, ProductQuestion, ProductReview,
                    Settlement, Shop, ShopStaff, ShopTask, TrackingEvent, User, get_db, utcnow)
router = APIRouter(tags=["seller"])
def _uid(request: Request) -> int: return int(request.state.user["user_id"])
async def _seat(db: AsyncSession, request: Request, company_id: uuid.UUID, manage: bool = False, money: bool = False) -> tuple[Membership, Company, Shop | None, bool]:
    """(your membership, the company, its shop or None, you manage it). Shop pages are for its managers + shop staff; manage=True: managers
    only; money=True (sales, the statement, the balance): the owner, co-owners, managers and HR only (SHOP_MONEY), never just the shop staff."""
    me = await db.get(Membership, (_uid(request), company_id))
    c = await db.get(Company, company_id) if me else None
    if not me or not c: raise HTTPException(404, "You're not in that company.")
    s, manager = await db.get(Shop, company_id), me.position in SHOP_MANAGERS
    if money:
        if me.position not in SHOP_MONEY: raise FormError(f"Only {c.name}'s owner, co-owners, managers and HR can see the shop's sales, statement and balance.")
        return me, c, s, manager
    staff = bool(s and await db.get(ShopStaff, (company_id, me.user_id)))
    if (manage and not manager) or not (manager or staff): raise FormError(f"Only {c.name}'s owner, co-owners and managers{'' if manage else ' (and the shop staff they pick)'} can do that.")
    return me, c, s, manager
def _need_shop(s: Shop | None, c: Company) -> Shop:
    if not s: raise FormError(f"{c.name} doesn't have a shop yet: open one on its shop page.")
    return s
# ---------- the shop's page: open it, the dashboard, settings ----------
@router.get("/companies/{company_id}/shop")
async def shop_page(request: Request, company_id: uuid.UUID, days: int = 30, db: AsyncSession = Depends(get_db)):
    me = await db.get(Membership, (_uid(request), company_id))
    c = await db.get(Company, company_id, options=[selectinload(Company.members).joinedload(Membership.user, innerjoin=True)]) if me else None
    if not me or not c: raise HTTPException(404, "You're not in that company.")
    s, manager, money = await db.get(Shop, company_id), me.position in SHOP_MANAGERS, me.position in SHOP_MONEY
    if not s: return render(request, "seller_shop", {"c": c, "me": me, "s": None, "can_open": me.position in TOP, "commission": COMMISSION_PERCENT, "currencies": CURRENCIES})
    staff = set((await db.scalars(select(ShopStaff.user_id).where(ShopStaff.company_id == company_id))).all())
    ops = manager or me.user_id in staff  # orders, products, questions, the AI's tasks: the managers + shop staff (HR who aren't staff see only the money)
    if not (ops or money): raise HTTPException(404, "Only the shop's managers, staff and HR can open its dashboard.")
    recent = (await db.scalars(select(Order).where(Order.company_id == company_id, Order.status != "pending").order_by(Order.created_at.desc()).limit(8))).all()
    todo = await db.scalar(select(func.count()).where(Order.company_id == company_id, Order.status.in_(("placed", "packed"))))
    low = (await db.scalars(select(Product).where(Product.company_id == company_id, Product.status == "active", Product.stock <= shop.LOW_STOCK).order_by(Product.stock).limit(10))).all()
    asked = await db.scalar(select(func.count()).select_from(ProductQuestion).join(Product, Product.id == ProductQuestion.product_id)
                            .where(Product.company_id == company_id, ProductQuestion.ask_seller, ProductQuestion.seller_answer == ""))
    tasks = (await db.scalars(select(ShopTask).where(ShopTask.company_id == company_id).order_by(ShopTask.created_at.desc()).limit(10))).all()
    returns = await db.scalar(select(func.count()).where(Order.company_id == company_id, Order.status == "delivered", Order.return_requested_at.is_not(None), Order.return_declined_at.is_(None), Order.returned_at.is_(None)))
    counts = dict((await db.execute(select(Product.status, func.count()).where(Product.company_id == company_id).group_by(Product.status))).all())
    hook = unseal(f"shop-hook:{company_id}", s.hook_secret_enc) if s.hook_secret_enc else None
    return render(request, "seller_shop", {"c": c, "me": me, "s": s, "manager": manager, "ops": ops, "money": money, "staff": staff, "recent": recent, "todo": todo, "low": low, "asked": asked, "tasks": tasks,
                                           "returns": returns, "counts": counts, "ledger": await ledger(db, company_id) if money else {}, "days": days if days in (7, 30, 90, 365) else 30,
                                           "commission": COMMISSION_PERCENT, "currencies": CURRENCIES, "hook_set": bool(hook), "months": _months(), "pay": shop.pay_mode()})
def _months() -> list[str]:  # the last 12 months, for the statement picker
    d, out = date.today().replace(day=1), []
    for _ in range(12): out.append(d.strftime("%Y-%m")); d = (d - timedelta(days=1)).replace(day=1)
    return out
@router.post("/companies/{company_id}/shop/open")
async def open_shop(request: Request, company_id: uuid.UUID, form: ShopOpenForm = Depends(ShopOpenForm.as_form), db: AsyncSession = Depends(get_db)):
    me = await db.get(Membership, (_uid(request), company_id))
    c = await db.get(Company, company_id) if me else None
    if not me or not c or me.position not in TOP: raise FormError("Only the owner or a co-owner can open the company's shop.")
    if c.halted_at: raise FormError(f"{c.name} is halted by the payroll guard: cover what its people are owed first.")
    if not form.accept_commission: raise FormError(f"Tick that you accept the platform's {COMMISSION_PERCENT}% commission on successful sales.")
    if await db.get(Shop, company_id): return RedirectResponse(f"/companies/{company_id}/shop", 303)
    await open_for(db, c, form.currency, request.state.user["username"])
    await db.commit()
    changelog.company(company_id, "shop opened", f"prices in {form.currency} · the platform's {COMMISSION_PERCENT}% commission accepted")
    flash(request, f"{c.name}'s shop is open: add your first product.", "success")
    return RedirectResponse(f"/companies/{company_id}/shop/products#new", 303)
async def open_for(db: AsyncSession, c: Company, currency: str, by: str) -> Shop:  # also used while creating a company (routers/companies.py)
    s = Shop(company_id=c.id, currency=currency, status="open", opened_by=by[:32], opened_at=utcnow())
    db.add(s); await db.flush()
    return s
@router.post("/companies/{company_id}/shop/settings")
async def shop_settings(request: Request, company_id: uuid.UUID, form: ShopSettingsForm = Depends(ShopSettingsForm.as_form), db: AsyncSession = Depends(get_db)):
    me, c, s, _ = await _seat(db, request, company_id, manage=True)
    s = _need_shop(s, c)
    names = dict((await db.execute(select(User.id, User.username).join(Membership, Membership.user_id == User.id).where(Membership.company_id == company_id))).all())
    staff_now = set((await db.scalars(select(ShopStaff.user_id).where(ShopStaff.company_id == company_id))).all())
    facts = lambda s, staff: {"status": s.status, "tagline": s.tagline, "shipping": price(s.currency, s.shipping_minor), "free shipping from": price(s.currency, s.free_over_minor) if s.free_over_minor else "",
                              "cash on delivery": bool(s.cod), "returns (days)": s.returns_days, "AI gives tasks": bool(s.ai_tasks), "shop staff": sorted(names.get(u, str(u)) for u in staff)}
    before = facts(s, staff_now)
    s.status, s.tagline, s.shipping_minor, s.cod, s.returns_days, s.ai_tasks = form.status, form.tagline, to_minor(form.shipping), form.cod, form.returns_days, form.ai_tasks
    s.free_over_minor = to_minor(form.free_over) if form.free_over else None
    members = set(names)
    await db.execute(ShopStaff.__table__.delete().where(ShopStaff.company_id == company_id))
    db.add_all(ShopStaff(company_id=company_id, user_id=u) for u in set(form.staff) & members)
    after = facts(s, set(form.staff) & members)
    await db.commit()
    changelog.company(company_id, "shop settings changed", changelog.diff(before, after) or "saved again, nothing changed")
    flash(request, f"Shop settings saved ({'open' if form.status == 'open' else 'paused: nobody can buy until you open it again'}).", "success")
    return RedirectResponse(f"/companies/{company_id}/shop#settings", 303)
@router.post("/companies/{company_id}/shop/hook")
async def hook_secret(request: Request, company_id: uuid.UUID, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """A new secret for the live-tracking webhook (the old one stops working). Shown once, in the flash message."""
    me, c, s, _ = await _seat(db, request, company_id, manage=True)
    s = _need_shop(s, c)
    secret = secrets.token_urlsafe(32)
    s.hook_secret_enc = seal(f"shop-hook:{company_id}", secret)
    await db.commit()
    changelog.company(company_id, "tracking webhook secret replaced", "the old one stopped working (the secret itself is never logged)")
    flash(request, f"New tracking webhook secret (copy it now: it isn't shown again): {secret}", "success")
    return RedirectResponse(f"/companies/{company_id}/shop#tracking", 303)
@router.get("/companies/{company_id}/shop/data")
async def shop_data(request: Request, company_id: uuid.UUID, days: int = 30, db: AsyncSession = Depends(get_db)):
    """The dashboard's numbers (JSON for Chart.js): the last `days` days on the company's clock. SHOP_MONEY only."""
    me, c, s, _ = await _seat(db, request, company_id, money=True)
    s = _need_shop(s, c)
    days = days if days in (7, 30, 90, 365) else 30
    tz = company_tz(c)
    end_local = datetime.now(tz).date() + timedelta(days=1)
    start_local = end_local - timedelta(days=days)
    to_utc = lambda d: datetime.combine(d, datetime.min.time(), tz).astimezone(UTC).replace(tzinfo=None)
    orders, items = await shopstats.frames(db, c, to_utc(start_local), to_utc(end_local))
    return {"currency": s.currency, **await asyncio.to_thread(shopstats.dashboard, orders, items, start_local, end_local)}
@router.get("/companies/{company_id}/shop/statement")
async def statement(request: Request, company_id: uuid.UUID, month: str = "", db: AsyncSession = Depends(get_db)):
    """The month's statement (Excel): every order, every item with its tax, tax by rate, the commission and settlements, + the sales chart. SHOP_MONEY only."""
    me, c, s, manager = await _seat(db, request, company_id, money=True)
    s = _need_shop(s, c)
    if not (m := re.fullmatch(r"(\d{4})-(\d{2})", month)) or not (2000 <= int(m[1]) <= 2100 and 1 <= int(m[2]) <= 12): raise HTTPException(400, "month must be YYYY-MM (2000-2100)")
    first = date(int(m[1]), int(m[2]), 1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    tz = company_tz(c)
    to_utc = lambda d: datetime.combine(d, datetime.min.time(), tz).astimezone(UTC).replace(tzinfo=None)
    orders, items = await shopstats.frames(db, c, to_utc(first), to_utc(nxt))
    sett = (await db.scalars(select(Settlement).where(Settlement.company_id == company_id, Settlement.at >= to_utc(first), Settlement.at < to_utc(nxt)).order_by(Settlement.at))).all()
    data = await asyncio.to_thread(shopstats.dashboard, orders, items, first, nxt)
    book = await asyncio.to_thread(shopstats.statement, c.name, str(c.id), s.currency, first, orders, items, list(sett), await ledger(db, company_id), data["days"], data["sales"])
    name = re.sub(r"[^\w -]", "_", f"{c.name} shop statement {first:%Y-%m}")[:80]
    return Response(book, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": attachment(f"{name}.xlsx"), "Cache-Control": "private, no-store"})
# ---------- products ----------
@router.get("/companies/{company_id}/shop/products")
async def products_page(request: Request, company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    me, c, s, manager = await _seat(db, request, company_id)
    s = _need_shop(s, c)
    rows = (await db.scalars(select(Product).where(Product.company_id == company_id).order_by(Product.status, Product.updated_at.desc()).limit(500))).all()
    return render(request, "seller_products", {"c": c, "s": s, "me": me, "manager": manager, "products": rows, "categories": SHOP_CATEGORIES, "currencies": CURRENCIES})
async def _mine(db: AsyncSession, company_id, pid: int, lock: bool = False) -> Product:
    p = await db.get(Product, pid, with_for_update=lock or None)
    if not p or p.company_id != company_id: raise HTTPException(404, "There's no such product.")
    return p
def _facts(p: Product) -> dict:  # a product as the change log describes / compares it
    return {"title": p.title, "category": SHOP_CATEGORIES.get(p.category, p.category), "brand": p.brand, "SKU": p.sku, "price": price(p.currency, p.price_minor) if p.price_minor else "",
            "list price": price(p.currency, p.mrp_minor) if p.mrp_minor else "", "tax %": p.tax_rate, "stock": p.stock, "max per order": p.max_per_order, "status": p.status,
            "highlights": p.highlights, "description": p.description}
def _apply(p: Product, form: ProductForm, s: Shop) -> None:
    p.title, p.description, p.highlights, p.category, p.brand, p.sku = form.title, form.description, form.highlights, form.category, form.brand, form.sku
    p.price_minor, p.mrp_minor, p.tax_rate, p.stock, p.max_per_order, p.status = to_minor(form.price), to_minor(form.mrp) if form.mrp else None, form.tax_rate, form.stock, form.max_per_order, form.status
    p.currency = form.currency or p.currency or s.currency  # any currency the seller likes; orders charge it in the shop's (s.currency) at the day's rate
@router.post("/companies/{company_id}/shop/products")
async def create_product(request: Request, company_id: uuid.UUID, form: NewProductForm = Depends(NewProductForm.as_form), db: AsyncSession = Depends(get_db)):
    me, c, s, _ = await _seat(db, request, company_id, manage=True)
    _need_shop(s, c)
    if await db.scalar(select(func.count()).where(Product.company_id == company_id)) >= 2000: raise FormError("A shop can list 2,000 products at most.")
    p = Product(company_id=company_id, title=form.title, category=form.category, price_minor=1)
    _apply(p, form, s)
    db.add(p); await db.flush()
    await shop.update_embedding(db, p)
    pid, title, facts = p.id, p.title, _facts(p)
    await db.commit()
    changelog.company(company_id, f"product added (product {pid})", changelog.facts(facts))
    flash(request, f"“{title}” added{' and live' if form.status == 'active' else ' as a draft'}: add its photos and videos below.", "success")
    return RedirectResponse(f"/companies/{company_id}/shop/products/{pid}#media", 303)
@router.get("/companies/{company_id}/shop/products/{pid}")
async def product_admin(request: Request, company_id: uuid.UUID, pid: int, db: AsyncSession = Depends(get_db)):
    me, c, s, manager = await _seat(db, request, company_id)
    s = _need_shop(s, c)
    p = await _mine(db, company_id, pid)
    links = (await db.scalars(select(CompetitorLink).where(CompetitorLink.product_id == pid).order_by(CompetitorLink.created_at))).all()
    market = await shop.similar(db, p, 5)
    market_cur = dict((await db.execute(select(Shop.company_id, Shop.currency).where(Shop.company_id.in_({x.company_id for x, _ in market})))).all()) if market else {}
    sellers = dict((await db.execute(select(Company.id, Company.name).where(Company.id.in_({x.company_id for x, _ in market})))).all()) if market else {}
    insight = await db.get(ProductInsight, pid)
    questions = (await db.scalars(select(ProductQuestion).where(ProductQuestion.product_id == pid, ProductQuestion.status != "spam").order_by(ProductQuestion.ask_seller.desc(), ProductQuestion.created_at.desc()).limit(40))).all()
    reviews = (await db.scalars(select(ProductReview).where(ProductReview.product_id == pid).order_by(ProductReview.created_at.desc()).limit(40))).all()
    return render(request, "seller_product", {"c": c, "s": s, "me": me, "manager": manager, "p": p, "links": links, "market": market, "market_cur": market_cur, "sellers": sellers, "currencies": CURRENCIES,
                                              "insight": insight, "brief": shopai.insight_of(insight), "questions": questions, "reviews": reviews, "categories": SHOP_CATEGORIES,
                                              "media": shop.gallery(pid), "max_photos": shop.MAX_PHOTOS, "max_videos": shop.MAX_VIDEOS, "summary": shopai.summary_of(p), "convert": shop.convert})
@router.post("/companies/{company_id}/shop/products/{pid}")
async def update_product(request: Request, company_id: uuid.UUID, pid: int, form: ProductForm = Depends(ProductForm.as_form), db: AsyncSession = Depends(get_db)):
    me, c, s, _ = await _seat(db, request, company_id, manage=True)
    s = _need_shop(s, c)
    p = await _mine(db, company_id, pid, lock=True)  # locked: an order taking stock waits for this, and this sees what it took
    before, moved = _facts(p), form.stock_was is not None and form.stock_was != p.stock
    if moved: form.stock = max(0, p.stock + form.stock - form.stock_was)  # the page stood open while orders came in: what was typed is a change to what it showed, not a new total, or sold stock would come back
    _apply(p, form, s)
    await shop.update_embedding(db, p)
    title, after = p.title, _facts(p)
    await db.commit()
    changelog.company(company_id, f"product changed (product {pid})", changelog.diff(before, after) or "saved again, nothing changed")
    flash(request, f"“{title}” saved ({form.status})." + (f" Its stock changed while this page was open: it's {form.stock} now." if moved else ""), "success")
    return RedirectResponse(f"/companies/{company_id}/shop/products/{pid}", 303)
@router.post("/companies/{company_id}/shop/products/{pid}/media")
async def add_media(request: Request, company_id: uuid.UUID, pid: int, form: ProductMediaForm = Depends(ProductMediaForm.as_form), db: AsyncSession = Depends(get_db)):
    """Photos (re-encoded, metadata gone) + a video (checked + remuxed by the media service): up to MAX_PHOTOS / MAX_VIDEOS per product."""
    uploads = [*form.photos(), *([form.video] if form.video else [])]
    for u in uploads: unflash_saved(request, u)
    try: await _seat(db, request, company_id, manage=True); await _mine(db, company_id, pid)
    except Exception:
        for u in uploads: discard_upload(u)
        raise
    if not uploads: raise FormError("Pick a photo or a video to add.")
    added, problems = 0, []
    for n, u in enumerate(form.photos(), 1):
        slot = next((k for k in range(1, shop.MAX_PHOTOS + 1) if not photo_file(pid, k).is_file()), None)
        if not slot: discard_upload(u); problems.append(f"a product holds {shop.MAX_PHOTOS} photos"); continue
        try: await store_shop_photo(u, photo_file(pid, slot), photo_file(pid, slot, "thumb")); added += 1
        except PictureError as e: problems.append(f"photo {n}: {e}")
    if form.video:
        slot = next((k for k in range(1, shop.MAX_VIDEOS + 1) if not video_file(pid, k)), None)
        if not slot: discard_upload(form.video); problems.append(f"a product holds {shop.MAX_VIDEOS} videos")
        else:
            try: await store_video(form.video, lambda ext: video_file(pid, slot, ext)); added += 1
            except PictureError as e: problems.append(f"video: {e}")
    flash(request, (f"{added} added." if added else "Nothing added.") + (" " + "; ".join(problems) + "." if problems else ""), "success" if added and not problems else "info" if added else "error")
    return RedirectResponse(f"/companies/{company_id}/shop/products/{pid}#media", 303)
@router.post("/companies/{company_id}/shop/products/{pid}/media/{kind}/{slot}/delete")
async def delete_media(request: Request, company_id: uuid.UUID, pid: int, kind: str, slot: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    await _seat(db, request, company_id, manage=True); await _mine(db, company_id, pid)
    if kind == "photo" and 1 <= slot <= shop.MAX_PHOTOS:
        for size in ("full", "thumb"): photo_file(pid, slot, size).unlink(missing_ok=True)
        for k in range(slot + 1, shop.MAX_PHOTOS + 1):  # close the gap: the photos after it move up one
            for size in ("full", "thumb"):
                if photo_file(pid, k, size).is_file(): photo_file(pid, k, size).replace(photo_file(pid, k - 1, size))
    elif kind == "video" and 1 <= slot <= shop.MAX_VIDEOS and (v := video_file(pid, slot)): v.unlink(missing_ok=True)
    else: raise HTTPException(404, "Not found.")
    flash(request, f"{kind.capitalize()} removed.", "info")
    return RedirectResponse(f"/companies/{company_id}/shop/products/{pid}#media", 303)
@router.post("/companies/{company_id}/shop/products/{pid}/competitors")
async def add_competitor(request: Request, company_id: uuid.UUID, pid: int, form: CompetitorForm = Depends(CompetitorForm.as_form), db: AsyncSession = Depends(get_db)):
    await _seat(db, request, company_id, manage=True); await _mine(db, company_id, pid)
    if rate_limited(f"competitor:{company_id}", 20, 3600): raise FormError("That's a lot of competitor links in an hour: try again later.")  # each one is read at once, by the AI if it must be: adding and removing can't be a loop
    if await db.scalar(select(func.count()).where(CompetitorLink.product_id == pid)) >= 5: raise FormError("Track at most 5 competitor links per product: remove one first.")
    link = CompetitorLink(product_id=pid, url=form.url, label=form.label)
    db.add(link); await db.flush()
    lid = link.id
    await db.commit()
    asyncio.get_running_loop().create_task(shopai.check_competitor(lid))
    flash(request, "Competitor added: its price is read now and every day (refresh in a moment).", "success")
    return RedirectResponse(f"/companies/{company_id}/shop/products/{pid}#competitors", 303)
@router.post("/companies/{company_id}/shop/competitors/{lid}/delete")
async def delete_competitor(request: Request, company_id: uuid.UUID, lid: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    await _seat(db, request, company_id, manage=True)
    link = await db.get(CompetitorLink, lid)
    p = await _mine(db, company_id, link.product_id) if link else None
    if not link or not p: raise HTTPException(404, "Not found.")
    await db.delete(link); await db.commit()
    flash(request, "Competitor link removed.", "info")
    return RedirectResponse(f"/companies/{company_id}/shop/products/{p.id}#competitors", 303)
@router.post("/companies/{company_id}/shop/products/{pid}/brief")
async def ask_brief(request: Request, company_id: uuid.UUID, pid: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """The AI's pricing + quality brief for this product (background, local LLM: a minute or two). Tracking runs first, so it's fresh."""
    await _seat(db, request, company_id, manage=True); await _mine(db, company_id, pid)
    if not shopai.AI_STATUS.get("ready"): raise FormError(f"The local AI is offline right now ({shopai.AI_STATUS.get('detail', '')}).")
    if rate_limited(f"brief:{company_id}", 10, 3600): raise FormError("That's a lot of briefs in an hour: try again later.")
    row = await db.get(ProductInsight, pid) or ProductInsight(product_id=pid, status="running")
    row.status, row.created_at = "running", utcnow(); db.add(row); await db.commit()
    async def job(): await shopai.track_product(pid); await shopai.brief(pid)
    shopai.llm.put(job)
    flash(request, "The AI is reading the competition: the brief appears here in a minute or two (refresh).", "info")
    return RedirectResponse(f"/companies/{company_id}/shop/products/{pid}#brief", 303)
@router.post("/companies/{company_id}/shop/products/{pid}/suggestions/{n}")
async def suggestion_task(request: Request, company_id: uuid.UUID, pid: int, n: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """One of the brief's suggestions becomes a task (the AI gives it to the shop's staff in working hours)."""
    await _seat(db, request, company_id, manage=True)
    p = await _mine(db, company_id, pid)
    data = shopai.insight_of(await db.get(ProductInsight, pid)) or {}
    if not 0 <= n < len(data.get("suggestions", [])): raise HTTPException(404, "There's no such suggestion.")
    sug = data["suggestions"][n]
    await shop.want_task(db, company_id, f"brief:{pid}:{n}:{date.today()}", f"{sug['title']} ({p.title[:40]})", f"{sug['detail']}\nFrom the AI's brief on {p.title}.", 16)
    await db.commit()
    flash(request, "Made it a task: the AI gives it to the least busy shop staff during working hours.", "success")
    return RedirectResponse(f"/companies/{company_id}/shop/products/{pid}#brief", 303)
@router.get("/companies/{company_id}/shop/products/{pid}/prices")
async def price_history(request: Request, company_id: uuid.UUID, pid: int, db: AsyncSession = Depends(get_db)):
    """The product's daily price history next to its competitors' (JSON for Chart.js), in the product's own currency."""
    me, c, s, _ = await _seat(db, request, company_id)
    cur = (await _mine(db, company_id, pid)).currency or (s.currency if s else "INR")
    rows = (await db.scalars(select(PriceSnapshot).where(PriceSnapshot.product_id == pid, PriceSnapshot.day >= date.today() - timedelta(days=90)).order_by(PriceSnapshot.day))).all()
    days = sorted({r.day for r in rows})
    series: dict = {}
    for r in rows:
        key = (r.source, r.ref)
        x = series.setdefault(key, {"label": "yours" if r.source == "own" else r.label[:40] + (" (web)" if r.source == "link" else ""), "own": r.source == "own", "values": {}})
        x["values"][r.day] = round(shop.convert(r.price_minor, r.currency, cur) / 100, 2)
    ordered = sorted(series.values(), key=lambda x: (not x["own"], x["label"]))[:6]  # yours first, then at most 5 competitors: a readable chart
    return {"currency": cur, "days": [d.strftime("%d %b") for d in days], "series": [{"label": x["label"], "own": x["own"], "values": [x["values"].get(d) for d in days]} for x in ordered]}
# ---------- orders ----------
@router.get("/companies/{company_id}/shop/orders")
async def orders_page(request: Request, company_id: uuid.UUID, status: str = "", db: AsyncSession = Depends(get_db)):
    me, c, s, manager = await _seat(db, request, company_id)
    s = _need_shop(s, c)
    q = select(Order).where(Order.company_id == company_id, Order.status != "pending")
    if status == "todo": q = q.where(Order.status.in_(("placed", "packed")))
    elif status == "returns": q = q.where(Order.return_requested_at.is_not(None), Order.return_declined_at.is_(None), Order.returned_at.is_(None))
    elif status in shop.STATUS_LABEL: q = q.where(Order.status == status)
    orders = (await db.scalars(q.order_by(Order.created_at.desc()).limit(200))).all()
    return render(request, "seller_orders", {"c": c, "s": s, "orders": orders, "items": await items_of(db, [o.id for o in orders]), "status": status, "manager": manager})
async def _order(db: AsyncSession, company_id, oid: int) -> Order:
    o = await db.get(Order, oid, with_for_update=True)
    if not o or o.company_id != company_id: raise HTTPException(404, "There's no such order.")
    return o
@router.get("/companies/{company_id}/shop/orders/{oid}")
async def order_admin(request: Request, company_id: uuid.UUID, oid: int, db: AsyncSession = Depends(get_db)):
    from routers.shop import timeline
    from routers.tasks import viewer
    me, c, s, manager = await _seat(db, request, company_id)
    o = await db.get(Order, oid)
    if not o or o.company_id != company_id: raise HTTPException(404, "There's no such order.")
    events = (await db.scalars(select(TrackingEvent).where(TrackingEvent.order_id == oid).order_by(TrackingEvent.at, TrackingEvent.id))).all()
    return render(request, "seller_order", {"c": c, "s": s, "o": o, "items": (await items_of(db, [oid])).get(oid, []), "events": timeline(events, viewer(request)), "to": shop.address_of(o),
                                            "couriers": COURIERS, "manager": manager, "courier_link": f"/shop/deliver/{o.courier_token}" if o.courier_token else ""})
def _back(company_id, oid: int) -> RedirectResponse: return RedirectResponse(f"/companies/{company_id}/shop/orders/{oid}", 303)
@router.post("/companies/{company_id}/shop/orders/{oid}/pack")
async def pack(request: Request, company_id: uuid.UUID, oid: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    await _seat(db, request, company_id)
    o = await _order(db, company_id, oid)
    if o.status != "placed": raise FormError("Only a new order can be marked packed.")
    await add_event(db, o, "packed", by=request.state.user["username"])
    await db.commit()
    flash(request, "Marked packed: the buyer has been told.", "success")
    return _back(company_id, oid)
@router.post("/companies/{company_id}/shop/orders/{oid}/ship")
async def ship(request: Request, company_id: uuid.UUID, oid: int, form: ShipForm = Depends(ShipForm.as_form), db: AsyncSession = Depends(get_db)):
    """On its way: the courier + tracking number (or own delivery). The buyer gets the tracking link and their 6-digit delivery code;
    the delivery person a link of their own (no login) where they type that code."""
    await _seat(db, request, company_id)
    o = await _order(db, company_id, oid)
    if o.status not in ("placed", "packed"): raise FormError("Only an order that hasn't shipped yet can be shipped.")
    o.courier, o.awb, o.tracking_url, o.courier_name, o.courier_phone, o.shipped_at = form.courier, form.awb, form.tracking_url, form.courier_name, form.courier_phone, utcnow()
    code = f"{secrets.randbelow(10**6):06d}"
    o.delivery_code_enc, o.courier_token = seal(f"delivery:{o.number}", code), secrets.token_urlsafe(32)
    who = shop.courier_label(o)
    await add_event(db, o, "shipped", f"{who}" + (f", tracking number {form.awb}" if form.awb else "") + f". Your delivery code is {code}: give it to the delivery person when it arrives.", by=request.state.user["username"])
    await db.commit()
    flash(request, f"Shipped with {who}: the buyer has the tracking details and their delivery code. Send your delivery person the delivery link below.", "success")
    return _back(company_id, oid)
@router.post("/companies/{company_id}/shop/orders/{oid}/event")
async def journey(request: Request, company_id: uuid.UUID, oid: int, form: TrackForm = Depends(TrackForm.as_form), db: AsyncSession = Depends(get_db)):
    await _seat(db, request, company_id)
    o = await _order(db, company_id, oid)
    if o.status not in ("placed", "packed", "shipped", "out_for_delivery"): raise FormError("That order isn't on its way.")
    if form.status != "packed" and o.status in ("placed", "packed"): raise FormError("Ship it first (courier + tracking number).")
    await add_event(db, o, form.status, form.note, form.location, by=request.state.user["username"])
    await db.commit()
    flash(request, f"Added: {shop.EVENTS[form.status][0]}. The buyer sees it live.", "success")
    return _back(company_id, oid)
@router.post("/companies/{company_id}/shop/orders/{oid}/deliver")
async def mark_delivered(request: Request, company_id: uuid.UUID, oid: int, form: DeliverForm = Depends(DeliverForm.as_form), db: AsyncSession = Depends(get_db)):
    """Delivered: with the buyer's code (best), or with a photo of the parcel left for them (when they couldn't give the code)."""
    if form.photo: unflash_saved(request, form.photo)
    try:
        await _seat(db, request, company_id)
        o = await _order(db, company_id, oid)
        if o.status not in ("shipped", "out_for_delivery"): raise FormError("That order isn't on its way.")
        if not form.code and not form.photo: raise FormError("Type the buyer's 6-digit delivery code, or add a photo of the parcel where it was left.")
        if form.code and (not (want := delivery_code(o)) or not same_text(form.code, want)): raise FormError("That isn't the buyer's delivery code.")
    except Exception:
        if form.photo: discard_upload(form.photo)
        raise
    how = "code" if form.code else "photo"
    if how == "photo":
        try: await store_shop_photo(form.photo, proof_photo_file(oid), proof_photo_file(oid).with_suffix(".thumb.webp"))
        except PictureError as e: raise FormError(f"Photo: {e}") from None
    try: await delivered(db, o, how, request.state.user["username"])
    except ShopError as e: raise FormError(str(e)) from None
    o.courier_token = None
    await db.commit()
    flash(request, "Marked delivered" + (" with the buyer's code." if how == "code" else ": the buyer sees the photo and can report a problem."), "success")
    return _back(company_id, oid)
@router.get("/companies/{company_id}/shop/orders/{oid}/proof")
async def proof(request: Request, company_id: uuid.UUID, oid: int, db: AsyncSession = Depends(get_db)):  # the delivery photo: the shop's people (buyers see theirs via /shop/orders/<id>/proof)
    from fastapi.responses import FileResponse
    await _seat(db, request, company_id)
    o = await db.get(Order, oid)  # this shop's own orders only: the link holds any order number
    if not o or o.company_id != company_id or not (p := proof_photo_file(oid)).is_file(): raise HTTPException(404, "No photo.")
    return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "private, no-store"})
@router.post("/companies/{company_id}/shop/orders/{oid}/cancel")
async def cancel_order(request: Request, company_id: uuid.UUID, oid: int, form: ReasonForm = Depends(ReasonForm.as_form), db: AsyncSession = Depends(get_db)):
    await _seat(db, request, company_id, manage=True)
    o = await _order(db, company_id, oid)
    if len(form.reason) < 3: raise FormError("Say why it's cancelled: the buyer is told.")
    try: await cancel(db, o, form.reason, request.state.user["username"], "seller")
    except ShopError as e: raise FormError(str(e)) from None
    online = o.payment_status == "refund_pending"
    await db.commit()
    flash(request, "Cancelled: the buyer has been told and the stock is back." + (" Their refund is on its way through Cashfree." if online else ""), "info")
    return _back(company_id, oid)
async def _return(db: AsyncSession, request: Request, company_id, oid: int) -> Order:
    await _seat(db, request, company_id, manage=True)
    o = await _order(db, company_id, oid)
    if o.status != "delivered" or not o.return_requested_at or o.returned_at or o.return_declined_at: raise FormError("There's no return to answer on that order.")
    return o
@router.post("/companies/{company_id}/shop/orders/{oid}/return/accept")
async def accept_return(request: Request, company_id: uuid.UUID, oid: int, form: RefundForm = Depends(RefundForm.as_form), db: AsyncSession = Depends(get_db)):
    """Refunded (the refund's reference; a real online payment goes back through Cashfree instead, shop.start_refund); the platform's commission on it is gone and the
    product's sold count drops: it's no longer a successful sale."""
    o = await _return(db, request, company_id, oid)
    online = o.payment_provider == "cashfree" and o.payment_status == "paid"
    if not online and len(form.refund_ref) < 4: raise FormError("Type the refund's reference: the buyer is told it.")
    if form.restock: await shop.restock(db, [o])
    await shop.unsold(db, o)
    o.status, o.returned_at, o.refund_ref = "returned", utcnow(), form.refund_ref[:64]
    if o.payment_status == "paid": shop.start_refund(o)
    elif o.payment_status == "collected": o.payment_status, o.refunded_at = "refunded", utcnow()
    db.add(TrackingEvent(order_id=o.id, status="returned", note="refund through the payment service" if online else f"refund reference {form.refund_ref}", source="seller", by_name=request.state.user["username"], at=utcnow()))
    await shop._tell(db, o, f"Return accepted: {o.number}", f"{o.company_name} accepted your return of order {o.number}." + (shop.refund_note(o) if online else f" It's refunded (reference {form.refund_ref})."), "returned")
    await db.commit()
    flash(request, "Return accepted: the buyer has been told." + (" Their refund is on its way through Cashfree." if online else ""), "info")
    return _back(company_id, oid)
@router.post("/companies/{company_id}/shop/orders/{oid}/return/decline")
async def decline_return(request: Request, company_id: uuid.UUID, oid: int, form: ReasonForm = Depends(ReasonForm.as_form), db: AsyncSession = Depends(get_db)):
    o = await _return(db, request, company_id, oid)
    if len(form.reason) < 5: raise FormError("Say why the return is declined: the buyer is told.")
    o.return_declined_at = utcnow()  # answered: off the waiting list, and it can't be declined again
    db.add(TrackingEvent(order_id=o.id, status="return_declined", note=form.reason[:300], source="seller", by_name=request.state.user["username"], at=utcnow()))
    await shop._tell(db, o, f"Return declined: {o.number}", f"{o.company_name} declined the return of order {o.number}: {form.reason[:300]}", "return-declined", "warn")
    await db.commit()
    flash(request, "Return declined: the buyer has been told.", "info")
    return _back(company_id, oid)
# ---------- questions + reviews ----------
@router.get("/companies/{company_id}/shop/questions")
async def questions_page(request: Request, company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    me, c, s, manager = await _seat(db, request, company_id)
    rows = (await db.execute(select(ProductQuestion, Product).join(Product, Product.id == ProductQuestion.product_id).where(Product.company_id == company_id, ProductQuestion.status != "spam")
                             .order_by((ProductQuestion.seller_answer == "").desc(), ProductQuestion.ask_seller.desc(), ProductQuestion.created_at.desc()).limit(200))).all()
    return render(request, "seller_questions", {"c": c, "s": s, "rows": rows, "manager": manager})
@router.post("/companies/{company_id}/shop/questions/{qid}")
async def answer(request: Request, company_id: uuid.UUID, qid: int, form: AnswerForm = Depends(AnswerForm.as_form), db: AsyncSession = Depends(get_db)):
    """The seller's answer: shown on the product page to everyone, the asker told, and the AI uses it for later questions."""
    me, c, s, _ = await _seat(db, request, company_id)
    q = await db.get(ProductQuestion, qid)
    p = await db.get(Product, q.product_id) if q else None
    if not q or not p or p.company_id != company_id: raise HTTPException(404, "There's no such question.")
    q.seller_answer, q.answered_by, q.answered_at = form.text, request.state.user["username"], utcnow()
    if q.user_id and (asker := await db.get(User, q.user_id)):
        await notify(db, [asker], c, "info", f"{c.name} answered your question", f"About {p.title}: “{q.question[:200]}” {form.text[:400]}", f"/shop/p/{p.id}#qa", f"answer:{qid}")
        await hub.tell([asker.id], {"t": "answer", "id": qid})
    await db.commit()
    flash(request, "Answered: it's on the product's page now.", "success")
    return RedirectResponse(safe_referer(request, f"/companies/{company_id}/shop/questions") + f"#q-{qid}", 303)
@router.post("/companies/{company_id}/shop/reviews/{rid}/reply")
async def reply(request: Request, company_id: uuid.UUID, rid: int, form: AnswerForm = Depends(AnswerForm.as_form), db: AsyncSession = Depends(get_db)):
    me, c, s, _ = await _seat(db, request, company_id, manage=True)
    r = await db.get(ProductReview, rid)
    p = await db.get(Product, r.product_id) if r else None
    if not r or not p or p.company_id != company_id: raise HTTPException(404, "There's no such review.")
    r.reply, r.reply_by, r.replied_at = form.text, request.state.user["username"], utcnow()
    if r.user_id and (who := await db.get(User, r.user_id)):
        await notify(db, [who], c, "info", f"{c.name} replied to your review", f"About {p.title}: {form.text[:400]}", f"/shop/p/{p.id}#review-{rid}", f"review-reply:{rid}")
    await db.commit()
    flash(request, "Reply posted under the review.", "success")
    return RedirectResponse(safe_referer(request, f"/companies/{company_id}/shop/products/{p.id}") + f"#review-{rid}", 303)
