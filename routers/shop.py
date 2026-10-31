"""The shop, for buyers (shop.py has the rules, shopai.py the AI, routers/seller.py the companies' side): the storefront and product pages
(public: anyone can look; buying, reviewing and asking need an account), photos + videos, ratings + reviews with photos / videos, the AI
shopping assistant, questions sellers answered, similar products, the cart, saved addresses (encrypted), checkout + payment, orders with
live tracking + the delivery code, cancelling, "I received it", returns. Two pages need no login: the delivery person's
(/shop/deliver/<token>: they type the buyer's code) and the webhooks (Cashfree's payments, couriers' live tracking: both signed).
As staff during your working hours somewhere, other companies' shops are off, like company search (payroll.at_work)."""
import json, secrets, uuid
from datetime import timedelta
from urllib.parse import parse_qs
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from assist import rate_limited
from decoraters import public
from form import ActionForm, AddressForm, AnswerForm, AskForm, CartForm, CheckoutForm, DeliverForm, ProductReviewForm, ReasonForm
from media import PictureError, discard_upload, store_shop_photo, store_video
from payroll import at_work
import shop, spam
from shop import (PaymentError, ShopError, address_data, cancel, cart_lines, delivered, delivery_code, gallery, hook_signed, items_of, line_problem, pay_mode, payment_link,
                  photo_file, price, proof_photo_file, refresh_rating, review_photo_file, review_video_file, settle_checkout, shipping_for, shop_price, similar, video_file, add_event)
import shopai
from system import AI_STATUS, FormError, flash, render, safe_referer, same_text, seal, unflash_saved, unseal, wants_html
from tables import (SHOP_CATEGORIES, Address, CartItem, Company, Membership, Order, OrderItem, Product, ProductQuestion, ProductReview, Shop, TrackingEvent, User, get_db, utcnow)
router = APIRouter(tags=["shop"])
def _uid(request: Request) -> int | None: return int(request.state.user["user_id"]) if getattr(request.state, "user", None) else None
def _need_login(request: Request) -> int:
    if not (uid := _uid(request)): raise FormError("Log in (or sign up) first: you need an account to buy, review or ask.")
    return uid
FOCUS = "It's working time at {}: browsing other companies' shops is off until it's over (the Focus rule you accepted)."
async def _busy(db: AsyncSession, request: Request, *company_ids) -> Company | None:  # staff in working hours: the company that keeps them from other companies' shops (the Focus rule), or None. company_ids: the shops in question: your own company's is never "other"
    if not (uid := _uid(request)) or not (c := await at_work(db, uid)): return None
    mine = set((await db.scalars(select(Membership.company_id).where(Membership.user_id == uid))).all())
    return c if not company_ids or set(company_ids) - mine else None
async def _focus(db: AsyncSession, request: Request, *company_ids):  # for pages: the error page, or None
    if c := await _busy(db, request, *company_ids): return render(request, "error", {"errmessage": FOCUS.format(c.name), "status": 403, "title": "focus time", "back_url": "/dashboard"}, status_code=403)
    return None
async def _no_focus(db: AsyncSession, request: Request, *company_ids) -> None:  # for forms: a shop's cart, its questions, its checkout are browsing it too
    if c := await _busy(db, request, *company_ids): raise FormError(FOCUS.format(c.name))
async def _product(db: AsyncSession, pid: int, uid: int | None) -> tuple[Product, Shop, Company, bool]:
    """(product, shop, company, the viewer runs its shop). Only sellable products are shown, except to their own shop's people."""
    p = await db.get(Product, pid)
    s, c = (await db.get(Shop, p.company_id), await db.get(Company, p.company_id)) if p else (None, None)
    if not p or not s or not c: raise HTTPException(404, "There's no such product.")
    seller = bool(uid and await shop.can_fulfil(db, uid, c.id))
    if not seller and (p.status != "active" or s.status != "open" or c.halted_at): raise HTTPException(404, "That product isn't for sale right now.")
    return p, s, c, seller
# ---------- storefront + product pages ----------
@router.get("/shop")
@public
async def storefront(request: Request, q: str = "", cat: str = "", sort: str = "relevance", page: int = 1, db: AsyncSession = Depends(get_db)):
    if blocked := await _focus(db, request): return blocked
    page = max(1, min(page, 200))
    items, total = await shop.search(db, q, cat, sort, page)
    sellers = dict((await db.execute(select(Company.id, Company.name).where(Company.id.in_({p.company_id for p in items})))).all()) if items else {}
    currencies = dict((await db.execute(select(Shop.company_id, Shop.currency).where(Shop.company_id.in_({p.company_id for p in items})))).all()) if items else {}
    sellable = shop._sellable().subquery()
    counts = dict((await db.execute(select(sellable.c.category, func.count()).group_by(sellable.c.category))).all()) if not q else {}
    return render(request, "shop_home", {"items": items, "total": total, "q": q, "cat": cat if cat in SHOP_CATEGORIES else "", "sort": sort, "page": page, "pages": max(1, -(-total // 24)),
                                         "sellers": sellers, "currencies": currencies, "counts": counts, "pay": pay_mode()})
@router.get("/shop/s/{public_id}")
@public
async def seller_store(request: Request, public_id: uuid.UUID, q: str = "", sort: str = "popular", db: AsyncSession = Depends(get_db)):
    """One company's shop front: its products, rating, policies. Its address is the shop's own public id, not the company's id: that one is its private join code.
    (An old link with a listed company's id, public anyway, is sent on to the new address.)"""
    if not (s := await db.scalar(select(Shop).where(Shop.public_id == public_id))):
        moved = await db.scalar(select(Shop.public_id).join(Company, Company.id == Shop.company_id).where(Company.id == public_id, Company.listed))
        if moved: return RedirectResponse(f"/shop/s/{moved}", 303)
        raise HTTPException(404, "That shop isn't open.")
    company_id, c = s.company_id, await db.get(Company, s.company_id)
    if blocked := await _focus(db, request, company_id): return blocked
    if not c or s.status != "open" or c.halted_at: raise HTTPException(404, "That shop isn't open.")
    items, total = await shop.search(db, q, "", sort, 1, company_id, 96)
    stars = (await db.execute(select(func.avg(ProductReview.rating), func.count(ProductReview.id)).join(Product, Product.id == ProductReview.product_id).where(Product.company_id == company_id))).one()
    return render(request, "shop_home", {"items": items, "total": total, "q": q, "cat": "", "sort": sort, "page": 1, "pages": 1, "sellers": {c.id: c.name}, "currencies": {c.id: s.currency},
                                         "counts": {}, "pay": pay_mode(), "store": {"c": c, "s": s, "rating": stars[0], "reviews": stars[1]}})
@router.get("/shop/p/{pid}")
@public
async def product_page(request: Request, pid: int, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    p, s, c, seller = await _product(db, pid, uid)
    if blocked := await _focus(db, request, c.id): return blocked
    reviews =(await db.scalars(select(ProductReview).where(ProductReview.product_id == pid).order_by(ProductReview.created_at.desc()).limit(60))).all()
    stars = dict((await db.execute(select(ProductReview.rating, func.count()).where(ProductReview.product_id == pid).group_by(ProductReview.rating))).all())
    mine = next((r for r in reviews if uid and r.user_id == uid), None) or (await db.scalar(select(ProductReview).where(ProductReview.product_id == pid, ProductReview.user_id == uid)) if uid else None)
    bought = await db.scalar(select(Order.id).join(OrderItem, OrderItem.order_id == Order.id).where(Order.buyer_id == uid, OrderItem.product_id == pid, Order.status.in_(("delivered", "returned"))).limit(1)) if uid else None
    public_qa = (await db.scalars(select(ProductQuestion).where(ProductQuestion.product_id == pid, ProductQuestion.seller_answer != "").order_by(ProductQuestion.answered_at.desc()).limit(20))).all()
    my_qa = (await db.scalars(select(ProductQuestion).where(ProductQuestion.product_id == pid, ProductQuestion.user_id == uid).order_by(ProductQuestion.created_at.desc()).limit(10))).all() if uid else []
    alike = await similar(db, p, 6)
    alike_cur = dict((await db.execute(select(Shop.company_id, Shop.currency).where(Shop.company_id.in_({x.company_id for x, _ in alike})))).all()) if alike else {}
    in_cart = await db.scalar(select(CartItem.qty).where(CartItem.user_id == uid, CartItem.product_id == pid)) if uid else None
    return render(request, "shop_product", {"p": p, "s": s, "c": c, "seller": seller, "staff": bool(uid and await db.get(Membership, (uid, c.id))), "media": gallery(pid), "reviews": reviews, "hist": stars, "mine": mine, "bought": bought,
                                            "public_qa": public_qa, "my_qa": my_qa, "alike": alike, "alike_cur": alike_cur, "in_cart": in_cart or 0, "summary": shopai.summary_of(p),
                                            "ai": AI_STATUS, "highlights": [h.strip("•-* ") for h in p.highlights.splitlines() if h.strip("•-* ")], "ship": shipping_for(s, shop_price(p, s)), "pay": pay_mode(), "add_token": secrets.token_urlsafe(18)})
# ---------- media (photos re-encoded, videos remuxed: media.py) ----------
def _file(path, kind: str):
    if not path or not path.is_file(): raise HTTPException(404, "Not found.")
    return FileResponse(path, media_type="image/webp" if kind == "photo" else "video/" + path.suffix.lstrip("."), headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})
@router.get("/shop/media/p/{pid}/{slot}")
@public
async def product_photo(request: Request, pid: int, slot: int, db: AsyncSession = Depends(get_db)):
    await _product(db, pid, _uid(request)); return _file(photo_file(pid, slot), "photo")
@router.get("/shop/media/p/{pid}/{slot}/thumb")
@public
async def product_thumb(request: Request, pid: int, slot: int, db: AsyncSession = Depends(get_db)):
    await _product(db, pid, _uid(request)); return _file(photo_file(pid, slot, "thumb"), "photo")
@router.get("/shop/media/v/{pid}/{slot}")
@public
async def product_video(request: Request, pid: int, slot: int, db: AsyncSession = Depends(get_db)):
    await _product(db, pid, _uid(request)); return _file(video_file(pid, slot), "video")
@router.get("/shop/media/r/{rid}/{slot}")
@public
async def review_photo(request: Request, rid: int, slot: int, thumb: bool = False, db: AsyncSession = Depends(get_db)):
    r = await db.get(ProductReview, rid)
    if not r: raise HTTPException(404, "Not found.")
    await _product(db, r.product_id, _uid(request)); return _file(review_photo_file(rid, slot, "thumb" if thumb else "full"), "photo")
@router.get("/shop/media/rv/{rid}")
@public
async def review_video(request: Request, rid: int, db: AsyncSession = Depends(get_db)):
    r = await db.get(ProductReview, rid)
    if not r: raise HTTPException(404, "Not found.")
    await _product(db, r.product_id, _uid(request)); return _file(review_video_file(rid), "video")
# ---------- cart ----------
@router.get("/shop/cart")
async def cart_page(request: Request, db: AsyncSession = Depends(get_db)):
    lines = await cart_lines(db, _uid(request))
    groups: dict = {}
    for ci, p, s, c in lines:
        g, unit = groups.setdefault(c.id, {"c": c, "s": s, "lines": [], "subtotal": 0}), shop_price(p, s)  # in the shop's currency (today's rate for another one)
        g["lines"].append({"ci": ci, "p": p, "unit": unit, "problem": line_problem(p, s, c, ci.qty)}); g["subtotal"] += unit * ci.qty
    for g in groups.values(): g["shipping"] = shipping_for(g["s"], g["subtotal"]) if g["s"] else 0
    return render(request, "shop_cart", {"groups": list(groups.values()), "ok": lines and not any(l["problem"] for g in groups.values() for l in g["lines"]), "pay": pay_mode()})
@router.post("/shop/cart")
async def add_to_cart(request: Request, form: CartForm = Depends(CartForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _need_login(request)
    p, s, c, _ = await _product(db, form.product_id, uid)
    await _no_focus(db, request, c.id)
    if why := line_problem(p, s, c, max(form.qty, 1)): raise FormError(f"{p.title}: {why}.")
    more, most, title = max(form.qty, 1), min(p.max_per_order, p.stock), p.title
    add = insert(CartItem).values(user_id=uid, product_id=p.id, qty=min(more, most), add_token=form.token)  # one statement: two clicks at once can't both find no line and both add one
    again = (CartItem.add_token == add.excluded.add_token) & (add.excluded.add_token != "")  # the same form sent twice (a retry, or clicked again while a slow page loads: the browser's own guard stops after 3 s): it already counted
    qty = await db.scalar(add.on_conflict_do_update(index_elements=[CartItem.user_id, CartItem.product_id], set_={"qty": case((again, CartItem.qty), else_=func.least(CartItem.qty + more, most)), "add_token": form.token}).returning(CartItem.qty))
    await db.commit()
    flash(request, f"{title} is in your cart ({qty}). ", "success")
    return RedirectResponse(safe_referer(request, f"/shop/p/{form.product_id}"), 303)
@router.post("/shop/cart/{pid}")
async def update_cart(request: Request, pid: int, form: CartForm = Depends(CartForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _need_login(request)
    if not (ci := await db.get(CartItem, (uid, pid))): return RedirectResponse("/shop/cart", 303)
    p = await db.get(Product, pid)
    if form.qty == 0 or not p: await db.delete(ci)
    else: ci.qty = min(form.qty, p.max_per_order, max(p.stock, 1))
    await db.commit()
    return RedirectResponse("/shop/cart", 303)
# ---------- addresses + checkout ----------
async def _addresses(db: AsyncSession, uid: int) -> list[tuple[Address, dict]]:
    return [(a, address_data(a)) for a in (await db.scalars(select(Address).where(Address.user_id == uid).order_by(Address.created_at.desc()))).all()]
@router.get("/shop/checkout")
async def checkout_page(request: Request, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    lines = await cart_lines(db, uid)
    if not lines: flash(request, "Your cart is empty.", "info"); return RedirectResponse("/shop", 303)
    groups: dict = {}
    for ci, p, s, c in lines:
        g = groups.setdefault(c.id, {"c": c, "s": s, "lines": [], "subtotal": 0}); g["lines"].append((ci, p)); g["subtotal"] += shop_price(p, s) * ci.qty
    for g in groups.values(): g["shipping"] = shipping_for(g["s"], g["subtotal"]) if g["s"] else 0
    token = secrets.token_urlsafe(24)
    request.session["checkout_tokens"] = [*request.session.get("checkout_tokens", [])[-4:], token]  # one per open page: a second tab mustn't void the first one's
    return render(request, "shop_checkout", {"groups": list(groups.values()), "addresses": await _addresses(db, uid), "pay": pay_mode(), "token": token,
                                             "cod": all(g["s"] and g["s"].cod for g in groups.values()), "currencies": sorted({g["s"].currency for g in groups.values() if g["s"]}),
                                             "countries": [(k, v) for k, v in shop_countries()], "region": request.state.user.get("region") or "IN"})
def shop_countries():
    from banks import COUNTRIES
    return [(k, v) for k, v in COUNTRIES.items() if k != "ZZ"]
@router.post("/shop/addresses")
async def add_address(request: Request, form: AddressForm = Depends(AddressForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _need_login(request)
    await db.execute(select(User.id).where(User.id == uid).with_for_update(key_share=True))  # (two at once can't both be the 10th)
    if await db.scalar(select(func.count()).where(Address.user_id == uid)) >= 10: raise FormError("You can keep 10 addresses: delete one first.")
    data = form.model_dump()
    db.add(Address(user_id=uid, label=form.label or form.city, data_enc=seal(f"address:{uid}", json.dumps(data)), city=form.city[:80], country=form.country))
    await db.commit()
    flash(request, f"Address saved: {form.label or form.city}.", "success")
    return RedirectResponse(safe_referer(request, "/shop/checkout"), 303)
@router.post("/shop/addresses/{aid}/delete")
async def delete_address(request: Request, aid: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    a = await db.get(Address, aid)
    if not a or a.user_id != _uid(request): raise HTTPException(404, "There's no such address.")
    await db.delete(a); await db.commit()
    flash(request, "Address deleted.", "info")
    return RedirectResponse(safe_referer(request, "/shop/checkout"), 303)
@router.post("/shop/checkout")
async def checkout(request: Request, form: CheckoutForm = Depends(CheckoutForm.as_form), db: AsyncSession = Depends(get_db)):
    uid = _need_login(request)
    tokens = request.session.get("checkout_tokens", [])
    if not any(same_text(form.token, t) for t in tokens):
        flash(request, "That checkout was already sent: here are your orders.", "info"); return RedirectResponse("/shop/orders", 303)
    await _no_focus(db, request, *{c.id for _, _, _, c in await cart_lines(db, uid)})
    request.session["checkout_tokens"] = [t for t in tokens if not same_text(form.token, t)]
    a, buyer = await db.get(Address, form.address_id), await db.get(User, uid)
    if not a or a.user_id != uid: raise FormError("Pick one of your saved addresses (or add one).")
    if rate_limited(f"checkout:{uid}", 20, 3600): raise FormError("That's a lot of orders in an hour: try again later.")
    try: ck, orders = await shop.place(db, buyer, a, form.method)
    except ShopError as e:
        await db.rollback()
        if str(e) == shop.EMPTY_CART and (just := await db.scalar(select(Order).where(Order.buyer_id == uid, Order.created_at > utcnow() - timedelta(minutes=1)).order_by(Order.id.desc()).limit(1))):  # sent twice at once: the first one won, so show what it made
            return RedirectResponse(f"/shop/pay/{just.checkout}" if just.status == "pending" else "/shop/orders", 303)
        raise FormError(str(e)) from None
    numbers = ", ".join(o.number for o in orders)
    await db.commit()
    if form.method == "cod":
        flash(request, f"Ordered: {numbers}. Pay in cash when it arrives; you'll get the tracking details as soon as it ships.", "success")
        return RedirectResponse("/shop/orders", 303)
    return RedirectResponse(f"/shop/pay/{ck}", 303)
# ---------- payment ----------
async def _checkout(db: AsyncSession, uid: int, ck: str) -> list[Order]:
    orders = list((await db.scalars(select(Order).where(Order.checkout == ck, Order.buyer_id == uid).order_by(Order.id))).all())
    if not orders: raise HTTPException(404, "There's no such checkout.")
    return orders
@router.get("/shop/pay/{ck}")
async def pay_page(request: Request, ck: str, db: AsyncSession = Depends(get_db)):
    """Test mode: a pretend payment page (no money moves). Cashfree: the payment link (created once per checkout), opened from here."""
    uid = _uid(request)
    orders = await _checkout(db, uid, ck)
    if not (pending := [o for o in orders if o.status == "pending"]): return RedirectResponse("/shop/orders", 303)
    total, cur, url, err = sum(o.total_minor for o in pending), pending[0].currency, pending[0].payment_url, ""
    if pending[0].payment_provider == "cashfree" and not url:
        try:
            url = await payment_link(ck, total, cur, f"{shop.APP_NAME} order " + ", ".join(o.number for o in pending), await db.get(User, uid))
            for o in pending: o.payment_url = url[:500]
            await db.commit()
        except PaymentError as e: err = str(e)
    return render(request, "shop_pay", {"orders": pending, "ck": ck, "total": total, "cur": cur, "url": url, "err": err, "mode": pending[0].payment_provider,
                                        "minutes": int(shop.PAY_WINDOW.total_seconds() // 60), "items": await items_of(db, [o.id for o in pending])})
@router.post("/shop/pay/{ck}/{outcome}")
async def pay_test(request: Request, ck: str, outcome: str, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """Test mode only: "pay" or "fail" the pretend payment."""
    if pay_mode() != "test" or outcome not in ("pay", "fail"): raise HTTPException(404, "Not found.")
    await _checkout(db, _uid(request), ck)
    n = await settle_checkout(db, ck, "PAID" if outcome == "pay" else "CANCELLED", f"TEST-{secrets.token_hex(4).upper()}")
    await db.commit()
    flash(request, (f"Test payment done: {n} order{'s' if n != 1 else ''} placed (no money moved)." if outcome == "pay" else "Test payment failed: the order is cancelled and its stock released."), "success" if outcome == "pay" else "info")
    return RedirectResponse("/shop/orders", 303)
@router.get("/shop/paid/{ck}")
async def paid(request: Request, ck: str, db: AsyncSession = Depends(get_db)):
    """Cashfree sends the buyer back here: the link's status decides (the webhook / poller may have already)."""
    orders = await _checkout(db, _uid(request), ck)
    if any(o.status == "pending" for o in orders) and pay_mode() == "cashfree":
        try:
            if (status := await shop.link_status(ck)) in ("PAID", "EXPIRED", "CANCELLED"): await settle_checkout(db, ck, status); await db.commit()
        except PaymentError as e: flash(request, f"Couldn't check the payment yet ({e}): it's checked again every few minutes.", "info")
    orders = await _checkout(db, _uid(request), ck)
    flash(request, "Payment received: your order is placed." if all(o.status != "pending" and o.payment_status == "paid" for o in orders) else
          "The payment isn't confirmed yet: we check again every few minutes, and the order is placed as soon as it is." if any(o.status == "pending" for o in orders) else "The payment didn't go through: the order is cancelled.", "info")
    return RedirectResponse("/shop/orders", 303)
@router.post("/shop/hooks/cashfree")
@public
async def cashfree_hook(request: Request, db: AsyncSession = Depends(get_db)):
    """Cashfree's payment-link webhook (PAYMENT_LINK_EVENT): signature checked on the raw body (shop.cashfree_signed), then data.link_id + data.link_status."""
    raw = await request.body()
    if not shop.cashfree_signed(raw, request.headers.get("x-webhook-timestamp", ""), request.headers.get("x-webhook-signature", "")): return JSONResponse({"detail": "bad signature"}, 401)
    try: event = json.loads(raw)
    except ValueError: event = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}  # the docs also mention form data
    data = event.get("data") if isinstance(event, dict) else None
    try: data = json.loads(data) if isinstance(data, str) else data
    except ValueError: data = None
    if not isinstance(data, dict): return {"ok": True}  # signed, but not one of ours (a refund's status: shopai.poll_refunds follows those itself)
    if (ck := str(data.get("link_id") or "")) and (status := str(data.get("link_status") or "")):
        order = data.get("order")
        await settle_checkout(db, ck, status, str(order.get("transaction_id") or "") if isinstance(order, dict) else ""); await db.commit()
    return {"ok": True}
# ---------- my orders ----------
async def _mine(db: AsyncSession, uid: int, oid: int, lock: bool = False) -> Order:  # lock: whatever changes it: two at once wait in line and the second sees the first's result (the race test: one order cancelled 8 times gave its stock back 8 times)
    o = await db.get(Order, oid, with_for_update=lock or None)
    if not o or o.buyer_id != uid: raise HTTPException(404, "There's no such order.")
    return o
def timeline(events: list[TrackingEvent], zone) -> list[dict]:  # for the page + its live refresh (JSON): text only
    return [{"status": e.status, "label": shop.EVENTS.get(e.status, (e.status,))[0], "icon": shop.EVENTS.get(e.status, ("", "fa-circle"))[1], "note": e.note, "location": e.location,
             "source": e.source, "by": e.by_name, "at": e.at.replace(tzinfo=shop.UTC).astimezone(zone).strftime("%d %b, %H:%M %Z")} for e in events]
@router.get("/shop/orders")
async def my_orders(request: Request, db: AsyncSession = Depends(get_db)):
    uid = _uid(request)
    orders = (await db.scalars(select(Order).where(Order.buyer_id == uid).order_by(Order.created_at.desc()).limit(60))).all()
    return render(request, "shop_orders", {"orders": orders, "items": await items_of(db, [o.id for o in orders])})
@router.get("/shop/orders/{oid}")
async def order_page(request: Request, oid: int, db: AsyncSession = Depends(get_db)):
    from routers.tasks import viewer
    o = await _mine(db, _uid(request), oid)
    events = (await db.scalars(select(TrackingEvent).where(TrackingEvent.order_id == oid).order_by(TrackingEvent.at, TrackingEvent.id))).all()
    s = await db.get(Shop, o.company_id) if o.company_id else None
    reviewed = set((await db.scalars(select(ProductReview.product_id).where(ProductReview.user_id == o.buyer_id))).all())
    window = s.returns_days if s else 0
    can_return = o.status == "delivered" and not o.return_requested_at and o.delivered_at and (utcnow() - o.delivered_at).days < window
    return render(request, "shop_order", {"o": o, "items": (await items_of(db, [oid])).get(oid, []), "events": timeline(events, viewer(request)), "shop": s, "reviewed": reviewed,
                                          "can_return": can_return, "window": window, "code": delivery_code(o) if o.status in ("shipped", "out_for_delivery") else None})
@router.get("/shop/orders/{oid}/proof")
async def order_proof(request: Request, oid: int, db: AsyncSession = Depends(get_db)):  # the photo the seller left as proof of delivery: the buyer's own order only
    await _mine(db, _uid(request), oid)
    if not (p := proof_photo_file(oid)).is_file(): raise HTTPException(404, "No photo.")
    return FileResponse(p, media_type="image/webp", headers={"Cache-Control": "private, no-store"})
@router.get("/shop/orders/{oid}/track")
async def order_track(request: Request, oid: int, db: AsyncSession = Depends(get_db)):  # the order page refreshes from this when told {"t": "order"}
    from routers.tasks import viewer
    o = await _mine(db, _uid(request), oid)
    events = (await db.scalars(select(TrackingEvent).where(TrackingEvent.order_id == oid).order_by(TrackingEvent.at, TrackingEvent.id))).all()
    return {"status": o.status, "label": shop.STATUS_LABEL[o.status], "events": timeline(events, viewer(request))}
@router.post("/shop/orders/{oid}/cancel")
async def cancel_mine(request: Request, oid: int, form: ReasonForm = Depends(ReasonForm.as_form), db: AsyncSession = Depends(get_db)):
    o = await _mine(db, _uid(request), oid, lock=True)
    if o.status not in ("pending", "placed", "packed"): raise FormError("It's already on its way: once it arrives you can return it instead.")
    try: await cancel(db, o, form.reason or "changed my mind", request.state.user["username"], "buyer")
    except ShopError as e: raise FormError(str(e)) from None
    number, state = o.number, o.payment_status
    await db.commit()
    flash(request, f"Order {number} cancelled" + (": your refund has started." if state == "refund_pending" else ": your payment is refunded." if state == "refunded" else "."), "info")
    return RedirectResponse(f"/shop/orders/{oid}", 303)
@router.post("/shop/orders/{oid}/received")
async def received(request: Request, oid: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    """The buyer says it arrived: delivered (one way of marking it, with the code and the photo)."""
    o = await _mine(db, _uid(request), oid, lock=True)
    if o.status not in ("shipped", "out_for_delivery"): raise FormError("That order isn't on its way.")
    try: await delivered(db, o, "buyer", request.state.user["username"], source="buyer")
    except ShopError as e: raise FormError(str(e)) from None
    await db.commit()
    flash(request, "Thanks: marked delivered. Rate what you bought below.", "success")
    return RedirectResponse(f"/shop/orders/{oid}#review", 303)
@router.post("/shop/orders/{oid}/return")
async def ask_return(request: Request, oid: int, form: ReasonForm = Depends(ReasonForm.as_form), db: AsyncSession = Depends(get_db)):
    o = await _mine(db, _uid(request), oid, lock=True)
    s = await db.get(Shop, o.company_id) if o.company_id else None
    if o.status != "delivered" or o.return_requested_at or not s or not o.delivered_at or (utcnow() - o.delivered_at).days >= s.returns_days: raise FormError("That order can't be returned (any more).")
    if len(form.reason) < 5: raise FormError("Say why you're returning it (a few words).")
    o.return_requested_at, o.return_reason = utcnow(), form.reason[:500]
    await add_event(db, o, "return_requested", form.reason, source="buyer", by=request.state.user["username"], tell=False)
    await shop._tell(db, o, f"Return requested: {o.number}", f"You asked to return order {o.number}. {o.company_name} will reply on the order's page.", "return", email=False,
                     seller_title=f"Return requested: {o.number}", seller_body=f"{o.buyer_name} wants to return order {o.number}: {form.reason[:300]}")
    await shop.want_task(db, o.company_id, f"return:{o.id}", f"Handle the return of order {o.number}", f"{o.buyer_name} wants to return it: {form.reason[:400]}\nAccept (with the refund's reference) or decline on the order's page.", 8)
    await db.commit()
    flash(request, "Return requested: the seller has been told.", "info")
    return RedirectResponse(f"/shop/orders/{oid}", 303)
# ---------- reviews ----------
@router.post("/shop/p/{pid}/review")
async def review(request: Request, pid: int, form: ProductReviewForm = Depends(ProductReviewForm.as_form), db: AsyncSession = Depends(get_db)):
    """Only after receiving it (a delivered order with it): one review per product per person, edit it to change it; photos + a video."""
    uploads = [*form.photos(), *([form.video] if form.video else [])]
    for u in uploads: unflash_saved(request, u)
    try:
        uid = _need_login(request)
        p, s, c, _ = await _product(db, pid, uid)
        await _no_focus(db, request, c.id)
        if await db.get(Membership, (uid, c.id)): raise FormError(f"You work at {c.name}: its own people can buy from its shop, but can't review it.")  # (user's rule) a verified 5 stars from the seller is no review
        order = await db.scalar(select(Order.id).join(OrderItem, OrderItem.order_id == Order.id).where(Order.buyer_id == uid, OrderItem.product_id == pid, Order.status.in_(("delivered", "returned"))).order_by(Order.id.desc()).limit(1))
        if not order: raise FormError("You can review it once you've received it from this shop.")
        if rate_limited(f"review-shop:{uid}", 20, 3600): raise FormError("That's a lot of reviews in an hour: try again later.")
    except Exception:
        for u in uploads: discard_upload(u)
        raise
    r = await db.scalar(select(ProductReview).where(ProductReview.product_id == pid, ProductReview.user_id == uid))
    new = r is None
    if new: r = ProductReview(product_id=pid, user_id=uid, order_id=order, author=request.state.user["username"], rating=form.rating, body=form.body); db.add(r)
    r.rating, r.title, r.body, r.updated_at = form.rating, form.title, form.body, utcnow()
    await db.flush()
    rid, saved, problems = r.id, 0, []
    for n, u in enumerate(form.photos(), 1):  # each photo re-encoded; a bad one is skipped, the review still saves
        slot = next((k for k in range(1, shop.MAX_REVIEW_PHOTOS + 1) if not review_photo_file(rid, k).is_file()), None)
        if not slot: discard_upload(u); problems.append("a review holds 4 photos at most"); continue
        try: await store_shop_photo(u, review_photo_file(rid, slot), review_photo_file(rid, slot, "thumb")); saved += 1
        except PictureError as e: problems.append(f"photo {n}: {e}")
    if form.video:
        try: await store_video(form.video, lambda ext: review_video_file(rid, ext)); saved += 1
        except PictureError as e: problems.append(f"video: {e}")
    await refresh_rating(db, pid)
    if form.rating <= 2:
        await shop.want_task(db, p.company_id, f"review:{rid}:{form.rating}", f"Reply to a {form.rating}-star review of {p.title[:50]}", f"“{form.body[:400]}”\nReply on the product's shop page; fix what went wrong if you can.", 8)
    await db.commit()
    flash(request, ("Review posted" if new else "Review updated") + (f" with {saved} photo{'s' if saved != 1 else ''}/video" if saved else "") + ". Thanks!" + (" Not kept: " + "; ".join(problems) if problems else ""), "success")
    return RedirectResponse(f"/shop/p/{pid}#reviews", 303)
@router.post("/shop/reviews/{rid}/delete")
async def delete_review(request: Request, rid: int, _: ActionForm = Depends(ActionForm.as_form), db: AsyncSession = Depends(get_db)):
    r = await db.get(ProductReview, rid)
    if not r or r.user_id != _uid(request): raise HTTPException(404, "There's no such review.")
    pid = r.product_id
    await db.delete(r); await db.flush(); await refresh_rating(db, pid); await db.commit()
    shop.remove_review_media(rid)
    flash(request, "Review deleted.", "info")
    return RedirectResponse(f"/shop/p/{pid}#reviews", 303)
# ---------- the AI shopping assistant ----------
@router.post("/shop/p/{pid}/ask")
async def ask(request: Request, pid: int, form: AskForm = Depends(AskForm.as_form), db: AsyncSession = Depends(get_db)):
    """A question about the product: the local AI answers it from the listing + reviews + the seller's answers (shopai.py), live on the page.
    Refused by spam.check_shop_ai, while your last question is being answered, or while shopai.BUSY questions wait."""
    uid = _need_login(request)
    p, s, c, _ = await _product(db, pid, uid)
    await _no_focus(db, request, c.id)
    spam.check_shop_ai(uid, c.id, pid, form.question)
    await db.execute(select(User.id).where(User.id == uid).with_for_update(key_share=True))  # (two at once can't both find nothing waiting)
    waiting, mine = (await db.execute(select(func.count(), func.count().filter(ProductQuestion.user_id == uid)).where(ProductQuestion.status == "pending", ProductQuestion.created_at > utcnow() - shopai.STALE))).one()
    if mine: raise FormError("The assistant is still answering your last question: wait for it first (one question at a time).")
    if waiting >= shopai.BUSY: raise FormError("The assistant is busy with other shoppers right now: try again in a minute.")
    q = ProductQuestion(product_id=pid, user_id=uid, asker=request.state.user["username"], question=form.question)
    if not AI_STATUS.get("ready"): q.status, q.answer, q.ask_seller, q.confidence = "done", "The shopping assistant is offline right now: I've sent your question to the seller instead.", True, "low"
    db.add(q); await db.flush()
    qid = q.id
    if q.ask_seller:
        await shop.want_task(db, p.company_id, f"question:{qid}", f"Answer a question about {p.title[:60]}", f"A shopper asked: “{form.question[:400]}”", 4)
    await db.commit()
    if AI_STATUS.get("ready"): shopai.ask(qid)
    if not wants_html(request) or request.headers.get("x-requested-with") == "fetch": return {"id": qid, "status": q.status}
    flash(request, "Asked: the answer appears below in a moment.", "info")
    return RedirectResponse(f"/shop/p/{pid}#ask", 303)
@router.get("/shop/questions/{qid}")
async def question(request: Request, qid: int, db: AsyncSession = Depends(get_db)):  # the asker's question, polled / pulled when told {"t": "answer"}
    q = await db.get(ProductQuestion, qid)
    if not q or q.user_id != _uid(request): raise HTTPException(404, "There's no such question.")
    return {"id": q.id, "question": q.question, "status": q.status, "answer": q.answer, "confidence": q.confidence, "sources": q.sources, "ask_seller": q.ask_seller, "seller_answer": q.seller_answer}
# ---------- the delivery person's page (no login: the link from the seller + the buyer's code) ----------
async def _courier(db: AsyncSession, token: str, lock: bool = False) -> Order:
    q = select(Order).where(Order.courier_token == token)
    o = await db.scalar(q.with_for_update() if lock else q) if len(token) >= 32 else None
    if not o: raise HTTPException(404, "That delivery link isn't valid (any more).")
    return o
@router.get("/shop/deliver/{token}")
@public
async def deliver_page(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    o = await _courier(db, token)
    return render(request, "shop_deliver", {"o": o, "to": shop.address_of(o) if o.status in ("shipped", "out_for_delivery") else {}, "items": (await items_of(db, [o.id])).get(o.id, [])})
@router.post("/shop/deliver/{token}")
@public
async def deliver(request: Request, token: str, form: DeliverForm = Depends(DeliverForm.as_form), db: AsyncSession = Depends(get_db)):
    if form.photo: discard_upload(form.photo)  # this page takes the code only (the seller's page takes photos)
    o = await _courier(db, token, lock=True)
    if rate_limited(f"deliver:{token}", 6, 900): raise FormError("Too many wrong codes: wait 15 minutes, or ask the seller to mark it delivered.")
    if not form.code or not (want := delivery_code(o)) or not same_text(form.code, want): raise FormError("That's not the buyer's delivery code: ask them for the 6 digits on their order page.")
    try: await delivered(db, o, "code", form.by_name or "the delivery person", source="courier")
    except ShopError as e: raise FormError(str(e)) from None
    o.courier_token = None  # used: the link stops working (and stops showing the address)
    number = o.number
    await db.commit()
    flash(request, f"Order {number} delivered: thank you!", "success")
    return RedirectResponse("/", 303)
# ---------- live tracking from couriers / aggregators (signed with the shop's secret) ----------
@router.post("/shop/track/{company_id}")
@public
async def tracking_hook(request: Request, company_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """{"awb": "...", "status": in_transit | out_for_delivery | attempted | exception | delivered, "location": "...", "note": "..."}, with headers
    X-Deminic-Timestamp (unix seconds) + X-Deminic-Signature (hex HMAC-SHA256 of "<timestamp>.<raw body>" with the shop's secret)."""
    raw, s = await request.body(), await db.get(Shop, company_id)
    secret = unseal(f"shop-hook:{company_id}", s.hook_secret_enc) if s and s.hook_secret_enc else None
    if not secret or not hook_signed(secret, request.headers.get("x-deminic-timestamp", ""), raw, request.headers.get("x-deminic-signature", "")): return JSONResponse({"detail": "bad signature"}, 401)
    try: event = json.loads(raw)
    except ValueError: return JSONResponse({"detail": "send JSON"}, 400)
    if not isinstance(event, dict): return JSONResponse({"detail": "send a JSON object"}, 400)
    awb, status =str(event.get("awb") or "").strip().upper()[:40], str(event.get("status") or "")
    if status not in shop.COURIER_EVENTS or not awb: return JSONResponse({"detail": f"status must be one of {', '.join(shop.COURIER_EVENTS)}, with the awb"}, 422)
    o = await db.scalar(select(Order).where(Order.company_id == company_id, Order.awb == awb, Order.status.in_(("shipped", "out_for_delivery"))).order_by(Order.id.desc()).limit(1).with_for_update())
    if not o: return JSONResponse({"detail": "no order on its way with that tracking number"}, 404)
    note, where = str(event.get("note") or "")[:300], str(event.get("location") or "")[:120]
    if status == "delivered": await delivered(db, o, "webhook", "courier", note, source="webhook")
    else: await add_event(db, o, status, note, where, source="webhook", by="courier")
    await db.commit()
    return {"ok": True, "order": o.number, "status": o.status}
