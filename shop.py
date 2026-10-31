"""The marketplace: companies sell products to anyone with an account (routers/shop.py for buyers, routers/seller.py for a company's shop,
shopai.py for its AI). Here: prices in minor units (paise / cents, tax included), the platform's commission (COMMISSION_PERCENT of the
items, fixed when ordered, earned once an order is delivered and paid: "a successful transaction"), payments, order numbers, stock, the
order's life (placed -> packed -> shipped -> out for delivery -> delivered, or cancelled / returned) with the buyer told at every step
(bell + live on their open tabs + email), couriers + tracking links, the delivery code, product + review media on disk, and search +
similar products (pgvector).
Payments (PAYMENTS in .env): "test" = a pretend checkout page, no money moves (development); "cashfree" = Cashfree payment links (the
buyer pays on Cashfree's page, we check the link's status on return, by webhook and every few minutes: docs
https://www.cashfree.com/docs/api-reference/payments/latest/payment-links/create, API version 2026-01-01); "off" = cash on delivery only.
Online money is collected by the platform, which pays sellers their sales minus its commission; cash on delivery is collected by the
seller, who owes the platform its commission. ledger() keeps the balance; platform admins record settlements (routers/shopadmin.py)."""
import asyncio, base64, contextlib, hmac, json, os, re, secrets, urllib.error, urllib.request, uuid
import audit, changelog
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from types import SimpleNamespace
from jinja2 import pass_context
from markupsafe import Markup, escape
from sqlalchemy import Float, Integer, case, column, false, func, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from assist import STOPWORDS, content_vector, set_embedding
from fx import convert
from guard import notify, notify_many
from presence import hub
from system import APP_NAME, MEDIA_KEY, PLATFORM_ADMINS, UPLOAD_DIR, same_text, seal, send_email, templates, unseal
from tables import (CURRENCIES, REGIONS, SHOP_CATEGORIES, SHOP_MANAGERS, Address, CartItem, Company, Membership, Order, OrderItem, Product, ProductReview, Settlement, Shop, ShopStaff,
                    ShopTask, TrackingEvent, User, utcnow)
COMMISSION_PERCENT = Decimal(os.getenv("SHOP_COMMISSION_PERCENT", "5")).quantize(Decimal("0.01"))  # the platform's cut of every successful sale
PAYMENTS = os.getenv("PAYMENTS", "test").strip().lower()  # test / cashfree / off (cash on delivery only)
CASHFREE_PG_ID, CASHFREE_PG_SECRET = os.getenv("CASHFREE_PG_CLIENT_ID", ""), os.getenv("CASHFREE_PG_CLIENT_SECRET", "")
CASHFREE_PG_URL = (os.getenv("CASHFREE_PG_URL") or ("https://api.cashfree.com/pg" if os.getenv("CASHFREE_PG_ENV", "sandbox") == "production" else "https://sandbox.cashfree.com/pg")).rstrip("/")
CASHFREE_API_VERSION = "2026-01-01"
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/")  # where buyers come back to after paying (and webhooks, when it's https)
PAY_WINDOW =timedelta(minutes=int(os.getenv("SHOP_PAY_MINUTES", "30")))  # an online order unpaid this long is cancelled and its stock released
MAX_PHOTOS, MAX_VIDEOS, MAX_REVIEW_PHOTOS, LOW_STOCK = 8, 2, 4, 3
SHOP_DIR = UPLOAD_DIR / "shop"  # product + review photos / videos, delivery proofs: HMAC names, nothing in Postgres
SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AUD": "A$", "CAD": "C$", "SGD": "S$", "AED": "AED "}
COURIERS = {  # key -> (name, its public tracking page). No courier's site gives a link that takes the number (checked): the buyer gets the page + the number to paste, or the seller's exact link
    "bluedart": ("Blue Dart", "https://www.bluedart.com/web/guest/tracking"), "delhivery": ("Delhivery", "https://www.delhivery.com/"), "dtdc": ("DTDC", "https://www.dtdc.com/"),
    "indiapost": ("India Post (Speed Post)", "https://www.indiapost.gov.in/"), "ekart": ("Ekart", "https://ekartlogistics.com/"), "xpressbees": ("XpressBees", "https://www.xpressbees.com/"),
    "ecom": ("Ecom Express", "https://ecomexpress.in/"), "shadowfax": ("Shadowfax", "https://shadowfax.in/track"), "fedex": ("FedEx", "https://www.fedex.com/"),
    "dhl": ("DHL Express", "https://www.dhl.com/"), "ups": ("UPS", "https://www.ups.com/"), "usps": ("USPS", "https://www.usps.com/"), "royalmail": ("Royal Mail", "https://www.royalmail.com/"),
    "aramex": ("Aramex", "https://www.aramex.com/"), "auspost": ("Australia Post", "https://auspost.com.au/mypost/track/search"),
    "canadapost": ("Canada Post", "https://www.canadapost-postescanada.ca/track-reperage/en"), "custom": ("Own delivery / another courier", "")}
EVENTS = {  # a tracking step -> (what the buyer reads, icon, the order status it moves to or None)
    "placed": ("Order placed", "fa-receipt", None), "paid": ("Payment received", "fa-credit-card", None), "packed": ("Packed", "fa-box", "packed"),
    "shipped": ("Shipped", "fa-truck-fast", "shipped"), "in_transit": ("In transit", "fa-route", "shipped"), "out_for_delivery": ("Out for delivery", "fa-truck", "out_for_delivery"),
    "attempted": ("Delivery attempted", "fa-door-closed", None), "exception": ("Delayed or a problem", "fa-triangle-exclamation", None),
    "delivered": ("Delivered", "fa-house-circle-check", "delivered"), "cancelled": ("Cancelled", "fa-ban", "cancelled"),
    "return_requested": ("Return requested", "fa-rotate-left", None), "return_declined": ("Return declined", "fa-circle-xmark", None), "returned": ("Returned + refunded", "fa-money-bill-transfer", "returned")}
COURIER_EVENTS = ("in_transit", "out_for_delivery", "attempted", "exception", "delivered")  # what a courier / the live webhook may report
STATUS_LABEL = {"pending": "awaiting payment", "placed": "placed", "packed": "packed", "shipped": "shipped", "out_for_delivery": "out for delivery", "delivered": "delivered",
                "cancelled": "cancelled", "returned": "returned"}
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # order numbers: no 0/O or 1/I to misread
class ShopError(Exception): pass
EMPTY_CART = "Your cart is empty."
def pay_mode() -> str:  # how online payment works right now
    if PAYMENTS == "cashfree": return "cashfree" if CASHFREE_PG_ID and CASHFREE_PG_SECRET else "off"
    return "test" if PAYMENTS == "test" else "off"
# ---------- money: minor units, tax included ----------
def to_minor(v) -> int: return int((Decimal(str(v)) * 100).to_integral_value(ROUND_HALF_UP))
def major(minor: int) -> Decimal: return (Decimal(minor) / 100).quantize(Decimal("0.01"))
def price(cur: str, minor: int | None) -> str: return "" if minor is None else f"{'-' if minor < 0 else ''}{SYMBOLS.get(cur, cur + ' ')}{abs(Decimal(minor)) / 100:,.2f}"
@pass_context
def price_view(ctx, cur: str, minor: int | None):  # {{ price('INR', 129900) }}: "₹1,299.00" + "≈ $15.60" in the viewer's region currency
    shown, mine = price(cur, minor), REGIONS.get((ctx.get("user") or {}).get("region") or "", (None, None))[1]
    if not mine or mine == cur or minor is None or mine not in CURRENCIES: return shown
    return Markup(f'{escape(shown)} <small class="hx-fx" title="in your region\'s currency, at today\'s rate">≈ {escape(price(mine, convert(minor, cur, mine)))}</small>')
def tax_in(gross_minor: int, rate) -> int: return int((Decimal(gross_minor) * Decimal(rate) / (100 + Decimal(rate))).to_integral_value(ROUND_HALF_UP)) if rate else 0
def commission_of(subtotal_minor: int, percent: Decimal) -> int: return int((Decimal(subtotal_minor) * percent / 100).to_integral_value(ROUND_HALF_UP))
def discount(p: Product) -> int: return int(round(100 - p.price_minor * 100 / p.mrp_minor)) if p.mrp_minor and p.mrp_minor > p.price_minor else 0
def shipping_for(shop: Shop, subtotal: int) -> int: return 0 if shop.free_over_minor and subtotal >= shop.free_over_minor else shop.shipping_minor
def shop_price(p: Product, shop: Shop | None) -> int:  # what one costs in an order: its price in the shop's currency, at the day's rate when the seller priced it in another
    return p.price_minor if not shop or not p.currency or p.currency == shop.currency else convert(p.price_minor, p.currency, shop.currency)
templates.env.globals.update(price=price_view, price_plain=price, discount=discount, shop_price=shop_price, couriers=COURIERS, shop_events=EVENTS, order_status=STATUS_LABEL, shop_categories=SHOP_CATEGORIES)
# ---------- media on disk (HMAC names, like pictures and proofs: unguessable, nothing about them in Postgres) ----------
def _name(*key) -> str: return hmac.new(MEDIA_KEY, ":".join(map(str, key)).encode(), "sha256").hexdigest()[:32]
def photo_file(pid: int, slot: int, size: str = "full") -> Path: return SHOP_DIR / f"{_name('product-photo', pid, slot, size)}.webp"
def video_file(pid: int, slot: int, ext: str | None = None) -> Path | None:  # ext given: where to store it; else the one that exists (or None)
    if ext: return SHOP_DIR / f"{_name('product-video', pid, slot)}.{ext}"
    return next((p for e in ("mp4", "webm") if (p := SHOP_DIR / f"{_name('product-video', pid, slot)}.{e}").is_file()), None)
def review_photo_file(rid: int, slot: int, size: str = "full") -> Path: return SHOP_DIR / f"{_name('review-photo', rid, slot, size)}.webp"
def review_video_file(rid: int, ext: str | None = None) -> Path | None:
    if ext: return SHOP_DIR / f"{_name('review-video', rid)}.{ext}"
    return next((p for e in ("mp4", "webm") if (p := SHOP_DIR / f"{_name('review-video', rid)}.{e}").is_file()), None)
def proof_photo_file(order_id: int) -> Path: return SHOP_DIR / f"{_name('delivery-proof', order_id)}.webp"
def photos(pid: int) -> list[int]: return [s for s in range(1, MAX_PHOTOS + 1) if photo_file(pid, s).is_file()]
def videos(pid: int) -> list[int]: return [s for s in range(1, MAX_VIDEOS + 1) if video_file(pid, s)]
def review_photos(rid: int) -> list[int]: return [s for s in range(1, MAX_REVIEW_PHOTOS + 1) if review_photo_file(rid, s).is_file()]
def _v(p: Path | None) -> str:
    try: return f"?v={p.stat().st_mtime_ns // 1_000_000}" if p else ""
    except OSError: return ""
def cover(pid: int) -> str | None:  # the first photo's thumbnail URL (cards), or None
    return next((f"/shop/media/p/{pid}/{s}/thumb{_v(photo_file(pid, s, 'thumb'))}" for s in range(1, MAX_PHOTOS + 1) if photo_file(pid, s, "thumb").is_file()), None)
def gallery(pid: int) -> list[dict]:  # every photo + video of a product, in order (photos first)
    return ([{"kind": "photo", "slot": s, "url": f"/shop/media/p/{pid}/{s}{_v(photo_file(pid, s))}", "thumb": f"/shop/media/p/{pid}/{s}/thumb{_v(photo_file(pid, s, 'thumb'))}"} for s in photos(pid)]
            + [{"kind": "video", "slot": s, "url": f"/shop/media/v/{pid}/{s}{_v(video_file(pid, s))}"} for s in videos(pid)])
templates.env.globals.update(product_cover=cover, product_gallery=gallery, review_photos=review_photos, review_video=review_video_file, proof_exists=lambda oid: proof_photo_file(oid).is_file())
def remove_product_media(pid: int) -> None:
    for s in range(1, MAX_PHOTOS + 1):
        for size in ("full", "thumb"):
            with contextlib.suppress(OSError): photo_file(pid, s, size).unlink(missing_ok=True)
    for s in range(1, MAX_VIDEOS + 1):
        while p := video_file(pid, s):
            with contextlib.suppress(OSError): p.unlink()
            if p.exists(): break
def remove_review_media(rid: int) -> None:
    for s in range(1, MAX_REVIEW_PHOTOS + 1):
        for size in ("full", "thumb"):
            with contextlib.suppress(OSError): review_photo_file(rid, s, size).unlink(missing_ok=True)
    if p := review_video_file(rid):
        with contextlib.suppress(OSError): p.unlink()
# ---------- products: search, similar ones (pgvector), ratings ----------
def product_text(p: Product) -> str: return " ".join([p.title, p.brand, SHOP_CATEGORIES.get(p.category, ""), p.highlights, p.description[:1500]])
async def update_embedding(db: AsyncSession, p: Product) -> None: await set_embedding(db, "products", "id", p.id, product_text(p))
def _sellable():  # an active product of an open shop whose company isn't halted by the payroll guard
    return (select(Product).join(Shop, Shop.company_id == Product.company_id).join(Company, Company.id == Product.company_id)
            .where(Product.status == "active", Shop.status == "open", Company.halted_at.is_(None)))
WORDS = literal_column("to_tsvector('english', products.title || ' ' || products.brand || ' ' || products.highlights || ' ' || left(products.description, 1500))")  # = the products_words GIN index (tables.MIGRATIONS)
def words_query(q: str) -> str:  # "vacuum flask" -> "vacuum:* | flask:*": any of the words, prefixes too; letters + digits only, so it can't break the tsquery syntax
    return " | ".join(f"{w}:*" for w in [w for w in dict.fromkeys(re.findall(r"[^\W_]+", q.lower())) if len(w) > 1 and w not in STOPWORDS][:8])
async def search(db: AsyncSession, q: str = "", category: str = "", sort: str = "relevance", page: int = 1, company_id=None, per: int = 24) -> tuple[list[Product], int]:
    """A page of sellable products: a query matches by its words in the title / brand / highlights / description (Postgres full text:
    any word, prefixes too) or by meaning (pgvector, the same local embeddings as company search); then the category / seller filter and
    the sort (relevance: the most words matched first, then the nearest in meaning). Returns (products, how many match)."""
    stmt = _sellable()
    if category in SHOP_CATEGORIES: stmt = stmt.where(Product.category == category)
    if company_id: stmt = stmt.where(Product.company_id == company_id)
    relevance = [Product.sold.desc(), Product.created_at.desc()]
    if q := " ".join(q.split())[:100]:
        qv, tq, like = content_vector(q), words_query(q), f"%{q.replace('%', '').replace('_', '')}%"
        tsq = lambda: func.to_tsquery(literal_column("'english'"), tq)
        stmt = stmt.where(or_(WORDS.op("@@")(tsq()) if tq else false(), Product.title.ilike(like), Product.brand.ilike(like), text("(products.embedding <=> CAST(:qv1 AS vector)) < 0.62").bindparams(qv1=qv)))
        relevance = ([func.ts_rank(WORDS, tsq()).desc()] if tq else []) + [text("products.embedding <=> CAST(:qv2 AS vector)").bindparams(qv2=qv)]
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    usd = case(*((Product.currency == cur, Product.price_minor * (convert(10**8, cur, "USD") / 10**8)) for cur in CURRENCIES), else_=Product.price_minor)  # prices in any currency, sorted fairly: US cents at today's rates
    order = {"price_low": [usd], "price_high": [usd.desc()], "rating": [Product.rating_avg.desc(), Product.rating_count.desc()],
             "newest": [Product.created_at.desc()], "popular": [Product.sold.desc()]}.get(sort) or relevance
    rows = (await db.scalars(stmt.order_by(*order, Product.id.desc()).offset((max(page, 1) - 1) * per).limit(per))).all()
    return list(rows), total or 0
async def similar(db: AsyncSession, p: Product, limit: int = 6, other_sellers: bool = True) -> list[tuple[Product, float]]:  # nearest sellable products by meaning
    ranked = text(f"""SELECT x.id, x.embedding <=> (SELECT embedding FROM products WHERE id = :pid) AS d FROM products x
        WHERE x.id <> :pid AND x.status = 'active' AND x.embedding IS NOT NULL {"AND x.company_id <> :cid" if other_sellers else ""}
        ORDER BY x.embedding <=> (SELECT embedding FROM products WHERE id = :pid) LIMIT :lim""").bindparams(pid=p.id, cid=p.company_id, lim=limit * 2).columns(column("id", Integer), column("d", Float)).subquery("ranked")
    rows = (await db.execute(_sellable().add_columns(ranked.c.d).join(ranked, ranked.c.id == Product.id).order_by(ranked.c.d).limit(limit))).all()
    return [(x, float(d)) for x, d in rows if d is not None and d < 0.8]
async def refresh_rating(db: AsyncSession, pid: int) -> None:
    avg, n = (await db.execute(select(func.avg(ProductReview.rating), func.count(ProductReview.id)).where(ProductReview.product_id == pid))).one()
    if p := await db.get(Product, pid): p.rating_avg, p.rating_count = Decimal(avg or 0).quantize(Decimal("0.01")), n
# ---------- people ----------
async def shop_people_of(db: AsyncSession, company_ids) -> dict:  # who runs / fulfils each shop: its managers + shop staff, while they're in the company (one query, however many shops)
    out: dict = {cid: [] for cid in company_ids}
    staff = select(ShopStaff.user_id).where(ShopStaff.company_id == Membership.company_id)
    for cid, u in (await db.execute(select(Membership.company_id, User).join(User, User.id == Membership.user_id).where(Membership.company_id.in_(list(out)), or_(Membership.position.in_(SHOP_MANAGERS), User.id.in_(staff))))).all(): out[cid].append(u)
    return out
async def shop_people(db: AsyncSession, company_id) -> list[User]: return (await shop_people_of(db, [company_id]))[company_id]
async def can_fulfil(db: AsyncSession, uid: int, company_id) -> Membership | None:  # a shop manager, or one of its shop staff
    m = await db.get(Membership, (uid, company_id))
    if m and (m.position in SHOP_MANAGERS or await db.get(ShopStaff, (company_id, uid))): return m
    return None
async def want_tasks(db: AsyncSession, rows: list[dict]) -> None:
    """Ask the shop's AI to get things done (shopai.give_tasks hands them to the least busy shop staff in working hours); once per key. rows of company_id, key, title,
    description, due_hours: one query + one statement, however many."""
    if not rows: return
    from sqlalchemy.dialects.postgresql import insert
    on = set((await db.scalars(select(Shop.company_id).where(Shop.company_id.in_({r["company_id"] for r in rows}), Shop.ai_tasks))).all())
    if keep := [{"company_id": r["company_id"], "key": r["key"][:80], "title": r["title"][:120], "description": r["description"][:2000], "due_hours": r["due_hours"], "created_at": utcnow()} for r in rows if r["company_id"] in on]:
        await db.execute(insert(ShopTask).values(keep).on_conflict_do_nothing(constraint="shop_tasks_one_each"))
async def want_task(db: AsyncSession, company_id, key: str, title: str, description: str = "", due_hours: int = 8) -> None:
    await want_tasks(db, [{"company_id": company_id, "key": key, "title": title, "description": description, "due_hours": due_hours}])
# ---------- orders ----------
def new_number(prefix: str = "DM") -> str: return f"{prefix}-" + "".join(secrets.choice(ALPHABET) for _ in range(8))
def address_of(o: Order) -> dict:
    try: return json.loads(unseal(f"ship:{o.number}", o.ship_to_enc) or "{}")
    except ValueError: return {}
def address_data(a: Address) -> dict:
    try: return json.loads(unseal(f"address:{a.user_id}", a.data_enc) or "{}")
    except ValueError: return {}
def delivery_code(o: Order) -> str | None: return unseal(f"delivery:{o.number}", o.delivery_code_enc) if o.delivery_code_enc else None
def track_link(o: Order) -> str: return o.tracking_url or COURIERS.get(o.courier, ("", ""))[1]
def courier_label(o: Order) -> str: return (o.courier_name or "own delivery") if o.courier == "custom" else COURIERS.get(o.courier, (o.courier, ""))[0]
templates.env.globals.update(track_link=track_link, courier_label=courier_label, ship_to=address_of, delivery_code=delivery_code)
async def items_of(db: AsyncSession, order_ids) -> dict[int, list[OrderItem]]:
    out: dict[int, list] = {}
    for i in (await db.scalars(select(OrderItem).where(OrderItem.order_id.in_(list(order_ids))).order_by(OrderItem.id))).all() if order_ids else []: out.setdefault(i.order_id, []).append(i)
    return out
async def _tell(db: AsyncSession, o: Order, buyer_title: str, buyer_body: str, key: str, level: str = "info", seller_title: str = "", seller_body: str = "", email: bool = True, staff: list | None = None, notes: list | None = None) -> None:
    """Buyer: bell + every open tab (live tracking) + email; the shop's people: bell + tabs when there's something for them. For several orders at once (a checkout):
    staff = this shop's people, loaded once by the caller; notes = a list the notices go on, for the caller to write in one statement (guard.notify_many)."""
    c = SimpleNamespace(id=o.company_id)
    async def bell(users: list, title: str, body: str, link: str, k: str) -> None:
        if notes is None: await notify(db, users, c, level, title, body, link, k)
        else: notes.extend({"user_id": u.id, "company_id": o.company_id, "level": level, "title": title, "body": body, "link": link, "key": k} for u in users)
    buyer = await db.get(User, o.buyer_id) if o.buyer_id else None  # (the session keeps it: one query per buyer, not per order)
    if buyer:
        await bell([buyer], buyer_title, buyer_body, f"/shop/orders/{o.id}", f"order:{key}:{o.id}")
        await hub.tell([buyer.id], {"t": "order", "id": o.id, "status": o.status, "title": buyer_title})
        if email:
            try: await send_email(buyer.email, buyer_title, f"Hi {buyer.username},\n\n{buyer_body}\n\nOrder {o.number} · {APP_NAME} shop: {PUBLIC_URL}/shop/orders/{o.id}\n")
            except Exception as e: print(f"order email failed: {type(e).__name__}")
    if seller_title and o.company_id:
        people = staff if staff is not None else await shop_people(db, o.company_id)
        await bell(people, seller_title, seller_body, f"/companies/{o.company_id}/shop/orders/{o.id}", f"shop-order:{key}:{o.id}")
        await hub.tell([u.id for u in people], {"t": "shop", "id": o.id, "title": seller_title})
async def add_event(db: AsyncSession, o: Order, status: str, note: str = "", location: str = "", source: str = "seller", by: str = "", tell: bool = True) -> None:
    """One step of the order's journey: recorded, the order's status moved on (never back), the buyer told live."""
    db.add(TrackingEvent(order_id=o.id, status=status, note=note[:300], location=location[:120], source=source, by_name=by[:60], at=utcnow()))
    order = ("placed", "packed", "shipped", "out_for_delivery", "delivered")
    if (to := EVENTS[status][2]) and o.status in order and to in order and order.index(to) > order.index(o.status): o.status = to
    if tell and status not in ("placed", "paid"):
        label = EVENTS[status][0]
        await _tell(db, o, f"{label}: order {o.number}", f"{label}" + (f" · {location}" if location else "") + (f": {note}" if note else "") + f". {o.company_name}, order {o.number}.",
                    f"{status}:{secrets.token_hex(3)}", "warn" if status in ("exception", "attempted", "cancelled") else "info", email=status in ("shipped", "out_for_delivery", "delivered", "exception"))
async def confirm(db: AsyncSession, orders: list[Order], how: str, ref: str = "") -> None:
    """Orders become real: paid online (how="paid") or cash on delivery accepted (how="cod"). The seller is told + the AI asks for packing. A checkout's orders share one
    load of their items and shop people, and one statement for the notices and one for the tasks, however many sellers are in the cart."""
    if not (todo := [o for o in orders if o.status == "pending"]): return
    lines, staff, notes, tasks = await items_of(db, [o.id for o in todo]), await shop_people_of(db, {o.company_id for o in todo if o.company_id}), [], []
    for o in todo:
        o.status, o.payment_status = "placed", "paid" if how == "paid" else "cod"
        if how == "paid": o.paid_at, o.payment_ref = utcnow(), (ref or o.payment_ref)[:64]
        db.add(TrackingEvent(order_id=o.id, status="placed", source="system", at=utcnow()))
        if how == "paid": db.add(TrackingEvent(order_id=o.id, status="paid", note=f"{price(o.currency, o.total_minor)} via {o.payment_provider}", source="system", at=utcnow()))
        what = ", ".join(f"{i.qty} × {i.title}" for i in lines.get(o.id, []))[:400]
        await _tell(db, o, f"Order placed: {o.number}", f"Thanks! {o.company_name} has your order {o.number} ({what}), {price(o.currency, o.total_minor)}"
                    + (" paid." if how == "paid" else ", to pay in cash on delivery.") + " You'll get the tracking details as soon as it ships.", "placed",
                    seller_title=f"New order {o.number}", seller_body=f"{o.buyer_name} ordered {what}: {price(o.currency, o.total_minor)}" + (" (paid)." if how == "paid" else " (cash on delivery).") + " Pack and ship it.",
                    staff=staff.get(o.company_id, []), notes=notes)
        tasks.append({"company_id": o.company_id, "key": f"pack:{o.id}", "title": f"Pack and ship order {o.number}", "description": f"{what}\nShip to {o.ship_city or 'the buyer'}; add the courier + tracking number on the order page when it's sent.", "due_hours": 8})
    await notify_many(db, notes)
    await want_tasks(db, tasks)
async def products_of(db: AsyncSession, items: list, lock: bool = False) -> dict:  # the products of some order items: one query (locked in id order: two can't deadlock)
    ids = {i.product_id for i in items if i.product_id}
    q = select(Product).where(Product.id.in_(ids)).order_by(Product.id)
    return {p.id: p for p in (await db.scalars(q.with_for_update() if lock else q)).all()} if ids else {}
async def restock(db: AsyncSession, orders: list[Order]) -> None:
    lines = await items_of(db, [o.id for o in orders])
    items = [i for o in orders for i in lines.get(o.id, [])]
    products = await products_of(db, items, lock=True)
    for i in items:
        if p := products.get(i.product_id): p.stock += i.qty
async def unsold(db: AsyncSession, o: Order) -> None:  # a returned order isn't a sale any more: delivered() counted it
    items = (await items_of(db, [o.id])).get(o.id, [])
    products = await products_of(db, items, lock=True)
    for i in items:
        if p := products.get(i.product_id): p.sold = max(0, p.sold - i.qty)
def start_refund(o: Order) -> None:  # a paid order's money goes back: pretend payments at once; real ones through Cashfree (user's rule): "refund pending" until it confirms (shopai.poll_refunds)
    if o.payment_provider == "cashfree": o.payment_status = "refund_pending"
    else: o.payment_status, o.refunded_at = "refunded", utcnow()
def refund_note(o: Order) -> str:  # what the buyer is told
    return " Your payment is refunded." if o.payment_status == "refunded" else f" Your refund has started: {price(o.currency, o.total_minor)} goes back to how you paid, usually within a few working days." if o.payment_status == "refund_pending" else ""
async def cancel(db: AsyncSession, o: Order, why: str, by: str, source: str, restocked: bool = False, notes: list | None = None) -> None:
    """Cancelled before delivery: stock goes back, a paid order is refunded (start_refund), the other side is told. restocked + notes: a whole checkout at once (settle_checkout)."""
    if o.status in ("delivered", "cancelled", "returned"): raise ShopError(f"Order {o.number} is {STATUS_LABEL[o.status]}: it can't be cancelled.")
    if not restocked: await restock(db, [o])
    o.status, o.cancelled_at, o.cancel_reason = "cancelled", utcnow(), why[:300]
    if o.payment_status == "paid": start_refund(o)
    elif o.payment_status in ("pending", "cod"): o.payment_status = "failed" if o.payment_status == "pending" else o.payment_status
    db.add(TrackingEvent(order_id=o.id, status="cancelled", note=why[:300], source=source, by_name=by[:60], at=utcnow()))
    refund = refund_note(o)
    await _tell(db, o, f"Order {o.number} cancelled", f"Order {o.number} from {o.company_name} was cancelled" + (f" by {by}" if source != "buyer" else "") + (f": {why}" if why else "") + "." + refund, "cancelled", "warn",
                seller_title=f"Order {o.number} cancelled" if source == "buyer" else "", seller_body=f"{o.buyer_name} cancelled order {o.number}" + (f": {why}" if why else "") + ". Its stock is back.", notes=notes)
async def delivered(db: AsyncSession, o: Order, how: str, by: str, note: str = "", source: str = "seller") -> None:
    """Delivered (the buyer's code, the buyer themselves, a proof photo, or the live tracking webhook): cash on delivery counts as collected,
    the sale counts (sold, commission earned), the buyer is asked to review."""
    if o.status in ("delivered", "cancelled", "returned", "pending"): raise ShopError(f"Order {o.number} is {STATUS_LABEL[o.status]}.")
    o.delivered_at, o.delivered_how = utcnow(), how
    if o.payment_status == "cod": o.payment_status = "collected"
    items = (await items_of(db, [o.id])).get(o.id, [])
    products = await products_of(db, items, lock=True)
    for i in items:
        if p := products.get(i.product_id): p.sold += i.qty
    await add_event(db, o, "delivered", note or {"code": "confirmed with the buyer's delivery code", "buyer": "the buyer confirmed it arrived", "photo": "left with a photo as proof",
                                                "webhook": "reported by the courier"}.get(how, ""), source=source, by=by)
    o.status = "delivered"
    await _tell(db, o, f"How was it? Review your order {o.number}", f"Your order {o.number} from {o.company_name} was delivered. Rate it and tell others what you think: photos and videos welcome.",
                "review", email=False, seller_title=f"Delivered: {o.number}", seller_body=f"Order {o.number} for {o.buyer_name} was delivered ({how}).")
def successful(o: Order) -> bool: return o.status == "delivered" and o.payment_status in ("paid", "collected")  # the platform's commission is earned on these
async def ledgers(db: AsyncSession, company_ids) -> dict:
    """Per seller, per currency, between the platform and the seller: successful sales (gross), the commission on them, what the platform owes
    (online sales it collected, minus commission), what the seller owes (commission on cash-on-delivery sales), settlements both ways,
    and the balance (> 0: the platform owes the seller; < 0: the seller owes the platform). Two queries, however many sellers."""
    out: dict = {cid: {} for cid in company_ids}
    blank = lambda: {"orders": 0, "gross": 0, "commission": 0, "online_due": 0, "cod_owed": 0, "paid_out": 0, "paid_in": 0}
    rows = (await db.execute(select(Order.company_id, Order.currency, Order.payment_method, func.count(), func.sum(Order.total_minor), func.sum(Order.commission_minor))
                             .where(Order.company_id.in_(list(out)), Order.status == "delivered", Order.payment_status.in_(("paid", "collected"))).group_by(Order.company_id, Order.currency, Order.payment_method))).all()
    for cid, cur, method, n, total, commission in rows:
        x = out[cid].setdefault(cur, blank()); x["orders"] += n; x["gross"] += total or 0; x["commission"] += commission or 0
        if method == "online": x["online_due"] += (total or 0) - (commission or 0)
        else: x["cod_owed"] += commission or 0
    for cid, cur, direction, total in (await db.execute(select(Settlement.company_id, Settlement.currency, Settlement.direction, func.sum(Settlement.amount_minor)).where(Settlement.company_id.in_(list(out)))
                                                        .group_by(Settlement.company_id, Settlement.currency, Settlement.direction))).all():
        x = out[cid].setdefault(cur, blank()); x["paid_out" if direction == "to_seller" else "paid_in"] += total or 0
    for by_currency in out.values():
        for x in by_currency.values(): x["balance"] = x["online_due"] - x["paid_out"] - x["cod_owed"] + x["paid_in"]
    return out
async def ledger(db: AsyncSession, company_id) -> dict[str, dict]: return (await ledgers(db, [company_id]))[company_id]
async def cart_lines(db: AsyncSession, uid: int) -> list[tuple[CartItem, Product, Shop | None, Company]]:
    rows = (await db.execute(select(CartItem, Product, Shop, Company).join(Product, Product.id == CartItem.product_id).join(Company, Company.id == Product.company_id)
                             .outerjoin(Shop, Shop.company_id == Product.company_id).where(CartItem.user_id == uid).order_by(Company.name, CartItem.added_at))).all()
    return [tuple(r) for r in rows]
def line_problem(p: Product, shop: Shop | None, c: Company, qty: int) -> str | None:  # why a cart line can't be bought right now
    if not shop or shop.status != "open" or c.halted_at: return f"{c.name} isn't selling right now"
    if p.status != "active": return "no longer for sale"
    if p.stock < qty: return "out of stock" if not p.stock else f"only {p.stock} left"
    if qty > p.max_per_order: return f"at most {p.max_per_order} per order"
    return None
async def place(db: AsyncSession, buyer: User, address: Address, method: str) -> tuple[str, list[Order]]:
    """Check out the buyer's cart: one order per company (its own shipping, the platform's commission fixed now), stock reserved (rows
    locked, in id order: two checkouts can't oversell), the cart emptied. Cash on delivery orders are placed at once; online ones wait for
    the payment (PAY_WINDOW). Returns (checkout number, orders). ShopError says what's wrong."""
    await db.execute(select(User.id).where(User.id == buyer.id).with_for_update(key_share=True))  # one checkout per person at a time, and only then read the cart: two at once (a double click, two tabs) read the same cart and placed the same order twice (the race test: 8 orders from 8 clicks)
    rows = (await db.execute(select(CartItem, Product).join(Product, Product.id == CartItem.product_id).where(CartItem.user_id == buyer.id).order_by(Product.id).with_for_update(of=Product))).all()
    if not rows: raise ShopError(EMPTY_CART)
    groups: dict = {}
    for ci, p in rows: groups.setdefault(p.company_id, []).append((ci, p))
    shops = {s.company_id: s for s in (await db.scalars(select(Shop).where(Shop.company_id.in_(list(groups))))).all()}  # one query each, however many sellers are in the cart
    companies = {c.id: c for c in (await db.scalars(select(Company).where(Company.id.in_(list(groups))))).all()}
    for cid, lines in groups.items():
        for ci, p in lines:
            if why := line_problem(p, shops.get(cid), companies[cid], ci.qty): raise ShopError(f"{p.title}: {why}. Update your cart, then check out again.")
    if method == "cod" and (no := [companies[cid].name for cid in groups if not shops[cid].cod]): raise ShopError(f"{', '.join(no)} doesn't offer cash on delivery: pay online, or buy from it separately.")
    if method == "online":
        if pay_mode() == "off": raise ShopError("Online payment is off right now: choose cash on delivery.")
        if len({shops[cid].currency for cid in groups}) > 1: raise ShopError("Your cart has shops pricing in different currencies: one online payment can't cover them. Check out each currency separately.")
    ship, checkout, orders, made, low = address_data(address), new_number("CK"), [], [], []
    for cid, lines in groups.items():
        shop, c = shops[cid], companies[cid]
        unit = {p.id: shop_price(p, shop) for _, p in lines}  # in the shop's currency (a product priced in another one: today's rate, fixed in the order)
        subtotal = sum(unit[p.id] * ci.qty for ci, p in lines)
        number = new_number()
        o = Order(number=number, checkout=checkout, company_id=cid, company_name=c.name, buyer_id=buyer.id, buyer_name=buyer.username, status="pending", payment_method=method,
                  payment_status="pending", payment_provider=pay_mode() if method == "online" else "", currency=shop.currency, subtotal_minor=subtotal, shipping_minor=shipping_for(shop, subtotal),
                  tax_minor=sum(tax_in(unit[p.id] * ci.qty, p.tax_rate) for ci, p in lines), total_minor=subtotal + shipping_for(shop, subtotal), commission_percent=COMMISSION_PERCENT,
                  commission_minor=commission_of(subtotal, COMMISSION_PERCENT), ship_to_enc=seal(f"ship:{number}", json.dumps(ship)), ship_city=address.city[:80], ship_country=address.country)
        orders.append(o); made.append((o, cid, lines, unit, shop, c))
    db.add_all(orders)
    await db.flush()  # one statement for all of them: their ids
    for o, cid, lines, unit, shop, c in made:
        number = o.number
        for ci, p in lines:
            p.stock -= ci.qty
            db.add(OrderItem(order_id=o.id, product_id=p.id, title=p.title, sku=p.sku, category=p.category, qty=ci.qty, price_minor=unit[p.id], tax_rate=p.tax_rate, tax_minor=tax_in(unit[p.id] * ci.qty, p.tax_rate)))
            if p.stock <= LOW_STOCK: low.append({"company_id": cid, "key": f"restock:{p.id}:{p.stock}", "title": f"Restock {p.title[:60]}", "description": f"Only {p.stock} left after order {number}: order more, or update the stock on the product's page.", "due_hours": 16})
        what = (" · ".join(f"{ci.qty} × {p.title} at {price(shop.currency, unit[p.id])}" for ci, p in lines) + f" · shipping {price(shop.currency, o.shipping_minor)} · total {price(shop.currency, o.total_minor)}"
                f" · {'cash on delivery' if method == 'cod' else 'paid online'} · to {address.city}, {address.country}")
        changelog.company(cid, f"order {number} placed by {buyer.username}", what); changelog.add("u", buyer.id, f"order {number} placed with {c.name}", what)
    await want_tasks(db, low)
    await db.execute(CartItem.__table__.delete().where(CartItem.user_id == buyer.id))
    await db.flush()
    if method == "cod": await confirm(db, orders, "cod")
    return checkout, orders
def hook_signed(secret: str, timestamp: str, raw: bytes, signature: str) -> bool:  # the live tracking webhook: hex HMAC-SHA256(secret, "<timestamp>.<body>"), 5 minutes either way
    try: fresh = abs(datetime.now(UTC).timestamp() - int(timestamp)) <= 300
    except ValueError: return False
    return fresh and same_text(hmac.new(secret.encode(), timestamp.encode() + b"." + raw, "sha256").hexdigest(), signature.lower())
# ---------- payments ----------
class PaymentError(Exception):
    code = 0  # the service's HTTP status (0 = it couldn't be reached)
def _cashfree(method: str, path: str, body: dict | None = None, idempotency: str | None = None) -> dict | list:
    headers = {"Content-Type": "application/json", "Accept": "application/json", "x-client-id": CASHFREE_PG_ID, "x-client-secret": CASHFREE_PG_SECRET,
               "x-api-version": CASHFREE_API_VERSION, **({"x-idempotency-key": idempotency} if idempotency else {})}
    req = urllib.request.Request(CASHFREE_PG_URL + path, json.dumps(body).encode() if body is not None else None, headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try: why = json.loads(e.read() or b"{}").get("message") or ""
        except (ValueError, AttributeError): why = ""
        err = PaymentError({401: "online payments are misconfigured on this server", 429: "the payment service is busy: try again in a minute"}.get(e.code, f"the payment service said no ({e.code}{': ' + why[:120] if why else ''})"))
        err.code = e.code
        raise err from None
    except (OSError, ValueError): raise PaymentError("couldn't reach the payment service: try again shortly") from None
def _phone(p: str) -> str: return p[3:] if p.startswith("+91") and len(p) == 13 else p.lstrip("+")  # Cashfree's examples are 10-digit Indian numbers
async def payment_link(checkout: str, amount_minor: int, currency: str, purpose: str, buyer: User) -> str:
    """A Cashfree payment link for one checkout (link_id = the checkout number, so it can't be paid twice); returns its URL."""
    meta = {"return_url": f"{PUBLIC_URL}/shop/paid/{checkout}", **({"notify_url": f"{PUBLIC_URL}/shop/hooks/cashfree"} if PUBLIC_URL.startswith("https://") else {})}
    body = {"link_id": checkout, "link_amount": float(major(amount_minor)), "link_currency": currency, "link_purpose": purpose[:500],
            "customer_details": {"customer_phone": _phone(buyer.phone or ""), "customer_email": buyer.email, "customer_name": buyer.username},
            "link_notify": {"send_sms": False, "send_email": False}, "link_meta": meta, "link_expiry_time": (datetime.now(UTC) + PAY_WINDOW).isoformat(timespec="seconds")}
    data = await asyncio.to_thread(_cashfree, "POST", "/links", body, str(uuid.uuid5(uuid.NAMESPACE_URL, f"cashfree-link:{checkout}")))
    if not (url := str(data.get("link_url") or "")).startswith("https://"): raise PaymentError("the payment service didn't return a payment page")
    return url
async def link_status(checkout: str) -> str:  # ACTIVE / PAID / PARTIALLY_PAID / EXPIRED / CANCELLED (Cashfree)
    return str((await asyncio.to_thread(_cashfree, "GET", f"/links/{checkout}")).get("link_status") or "")
async def close_link(checkout: str) -> None: await asyncio.to_thread(_cashfree, "POST", f"/links/{checkout}/cancel")  # no further payments on it; only an ACTIVE link can be closed
MAX_REFUND_TRIES = 5  # refunds Cashfree turned down: then it needs a person (the platform's page shows it, the shop is told)
async def refund_status(o: Order) -> dict:
    """Asks Cashfree for this order's refund: its status if it was asked before, else it's created. Safe to repeat after a timeout: the same refund id + idempotency key, and each order
    of a checkout has its own amount against the checkout's one payment (docs: refunds/create, payment-links/get-orders-for-a-payment-link, API version 2026-01-01). PaymentError if it can't be asked."""
    rows = await asyncio.to_thread(_cashfree, "GET", f"/links/{o.checkout}/orders")  # the orders of a paid link: PAID ones by default
    if not (paid := next((str(x.get("order_id") or "") for x in rows if isinstance(x, dict) and x.get("order_status") == "PAID"), "")): raise PaymentError("the payment service shows no paid order for this checkout")
    rid = f"RF-{o.number}-{o.refund_tries}"
    try: return await asyncio.to_thread(_cashfree, "GET", f"/orders/{paid}/refunds/{rid}")
    except PaymentError as e:
        if e.code != 404: raise
    body = {"refund_amount": float(major(o.total_minor)), "refund_id": rid, "refund_note": f"Order {o.number} {o.status}"[:100], "refund_speed": "STANDARD"}
    return await asyncio.to_thread(_cashfree, "POST", f"/orders/{paid}/refunds", body, str(uuid.uuid5(uuid.NAMESPACE_URL, f"cashfree-refund:{rid}")))
def cashfree_signed(raw: bytes, timestamp: str, signature: str) -> bool:  # base64(HMAC-SHA256(client secret, timestamp + raw body)) == x-webhook-signature (Cashfree's docs)
    if not (CASHFREE_PG_SECRET and timestamp and signature): return False
    want = base64.b64encode(hmac.new(CASHFREE_PG_SECRET.encode(), timestamp.encode() + raw, "sha256").digest()).decode()
    return same_text(want, signature)
async def settle_checkout(db: AsyncSession, checkout: str, status: str, ref: str = "") -> int:
    """Apply a payment outcome to every order of a checkout ("PAID" -> confirmed; EXPIRED / CANCELLED -> cancelled, stock back). Returns
    how many orders changed. Idempotent: webhooks, the return page and the poller can all report the same payment."""
    late = []
    if status == "PAID":  # money for an order cancelled meanwhile (the buyer cancelled, then paid): it goes straight back
        late = list((await db.scalars(select(Order).where(Order.checkout == checkout, Order.status == "cancelled", Order.payment_status == "failed", Order.payment_provider == "cashfree").with_for_update())).all())
        for o in late:
            o.paid_at, o.payment_ref = utcnow(), (ref or o.payment_ref)[:64]
            start_refund(o)
            await _tell(db, o, f"Payment for cancelled order {o.number}", f"Your payment for order {o.number} arrived after the order was cancelled, so it can't be placed.{refund_note(o)}", "late-payment", "warn")
    orders = list((await db.scalars(select(Order).where(Order.checkout == checkout, Order.status == "pending").with_for_update())).all())
    if not orders and not late: return 0
    if status == "PAID": await confirm(db, orders, "paid", ref)
    elif status in ("EXPIRED", "CANCELLED", "FAILED"):
        notes: list = []
        await restock(db, orders)  # the whole checkout at once
        for o in orders: await cancel(db, o, "the payment wasn't completed in time" if status == "EXPIRED" else "the payment was cancelled", "payment", "system", restocked=True, notes=notes)
        await notify_many(db, notes)
    else: return 0
    audit.log("system", "shop", f"checkout {checkout}: payment {status.lower()} for {', '.join(o.number for o in [*orders, *late])}" + (" (some already cancelled: refunding those)" if late else ""), ref[:40])
    return len(orders) + len(late)
