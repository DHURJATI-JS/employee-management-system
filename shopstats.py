"""A shop's numbers (routers/seller.py). pandas works them out from its orders: sales by day, orders by status, sales by category, product
and payment method, the platform's commission and the tax inside the prices. Chart.js draws them on the shop's dashboard (interactive, light
+ dark); the monthly Excel statement (openpyxl) lists every order, every item with its taxable value + tax, tax by rate, the commission and
settlements, with the month's sales chart drawn by matplotlib. Colours: the validated categorical palette (dataviz skill's reference
palette: fixed slot order, light and dark steps). Pie charts only for part-to-whole, at most 6 slices (the rest is "Other")."""
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import pandas as pd
from sqlalchemy import select
from payroll import company_tz
from tables import SHOP_CATEGORIES, Order, OrderItem, Settlement
PALETTE = {"light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
           "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]}
INK = {"light": {"text": "#0b0b0b", "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "surface": "#fcfcfb"},
       "dark": {"text": "#f0efec", "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "surface": "#1a1a19"}}
COUNTED = ("placed", "packed", "shipped", "out_for_delivery", "delivered")  # sales = orders that weren't cancelled / returned / left unpaid
async def frames(db, c, start: datetime, end: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(orders, items) of company c created in [start, end) (naive UTC), money in major units, times on the company's clock."""
    orders = (await db.scalars(select(Order).where(Order.company_id == c.id, Order.created_at >= start, Order.created_at < end).order_by(Order.created_at))).all()
    items = (await db.scalars(select(OrderItem).where(OrderItem.order_id.in_([o.id for o in orders])))).all() if orders else []
    cols = ["id", "number", "created", "day", "buyer", "status", "method", "payment", "currency", "subtotal", "shipping", "tax", "total", "commission_percent", "commission", "success"]
    if not orders: return pd.DataFrame(columns=cols), pd.DataFrame(columns=["order_id", "number", "title", "sku", "category", "qty", "price", "gross", "tax_rate", "tax", "taxable"])
    from shop import successful
    m = lambda x: float(Decimal(x) / 100)
    of = pd.DataFrame([{"id": o.id, "number": o.number, "created": o.created_at, "buyer": o.buyer_name, "status": o.status, "method": o.payment_method, "payment": o.payment_status,
                        "currency": o.currency, "subtotal": m(o.subtotal_minor), "shipping": m(o.shipping_minor), "tax": m(o.tax_minor), "total": m(o.total_minor),
                        "commission_percent": float(o.commission_percent), "commission": m(o.commission_minor), "success": successful(o)} for o in orders])
    of["created"] = pd.to_datetime(of["created"]).dt.tz_localize(UTC).dt.tz_convert(str(company_tz(c))).dt.tz_localize(None)  # the company's clock
    of["day"] = of["created"].dt.date
    numbers = dict(zip(of["id"], of["number"]))
    itf = pd.DataFrame([{"order_id": i.order_id, "number": numbers.get(i.order_id, ""), "title": i.title, "sku": i.sku, "category": SHOP_CATEGORIES.get(i.category, i.category), "qty": i.qty,
                         "price": m(i.price_minor), "gross": m(i.price_minor * i.qty), "tax_rate": float(i.tax_rate), "tax": m(i.tax_minor)} for i in items])
    itf["taxable"] = itf["gross"] - itf["tax"]
    return of, itf
def _top(series: pd.Series, n: int = 5) -> tuple[list[str], list[float]]:  # the biggest n-1 slices + "Other": a pie never has more than n (<= 6)
    series = series[series > 0].sort_values(ascending=False)
    if len(series) > n: series = pd.concat([series.iloc[:n - 1], pd.Series({"Other": series.iloc[n - 1:].sum()})])
    return [str(k) for k in series.index], [round(float(v), 2) for v in series.values]
def dashboard(orders: pd.DataFrame, items: pd.DataFrame, start: date, end: date) -> dict:
    """What the dashboard shows, as plain lists for Chart.js (+ the same numbers for each chart's table view)."""
    days = pd.date_range(start, end - timedelta(days=1), freq="D").date
    sold = orders[orders["status"].isin(COUNTED)] if len(orders) else orders
    by_day = sold.groupby("day")["total"].sum().reindex(days, fill_value=0) if len(sold) else pd.Series(0.0, index=days)
    ok_items = items[items["order_id"].isin(sold["id"])] if len(items) and len(sold) else items.iloc[0:0] if len(items) else items
    cat = ok_items.groupby("category")["gross"].sum() if len(ok_items) else pd.Series(dtype=float)
    top = ok_items.groupby("title")["gross"].sum().sort_values(ascending=False).head(8) if len(ok_items) else pd.Series(dtype=float)
    status = orders.groupby("status")["id"].count() if len(orders) else pd.Series(dtype=float)
    method = sold.groupby("method")["total"].sum().rename({"online": "paid online", "cod": "cash on delivery"}) if len(sold) else pd.Series(dtype=float)
    from shop import STATUS_LABEL
    status = status.rename(STATUS_LABEL)
    won = orders[orders["success"]] if len(orders) else orders
    return {"kpi": {"sales": round(float(sold["total"].sum()), 2) if len(sold) else 0.0, "orders": int(len(sold)), "aov": round(float(sold["total"].mean()), 2) if len(sold) else 0.0,
                    "units": int(ok_items["qty"].sum()) if len(ok_items) else 0, "delivered": int(len(won)), "commission": round(float(won["commission"].sum()), 2) if len(won) else 0.0,
                    "tax": round(float(sold["tax"].sum()), 2) if len(sold) else 0.0, "cancelled": int((orders["status"] == "cancelled").sum()) if len(orders) else 0,
                    "returned": int((orders["status"] == "returned").sum()) if len(orders) else 0},
            "days": [d.strftime("%d %b") for d in days], "sales": [round(float(v), 2) for v in by_day.values],
            "category": _top(cat), "status": _top(status.astype(float), 6), "method": _top(method, 6), "top": ([str(k)[:40] for k in top.index], [round(float(v), 2) for v in top.values])}
def _chart_png(days: list[str], values: list[float], currency: str) -> bytes:  # the statement's sales chart (matplotlib, light: it's printed / opened in Excel)
    from matplotlib.figure import Figure
    pal, ink = PALETTE["light"], INK["light"]
    fig = Figure(figsize=(9, 3.2), dpi=110, facecolor=ink["surface"])
    ax = fig.add_subplot(facecolor=ink["surface"])
    x = range(len(values))
    ax.plot(x, values, color=pal[0], linewidth=2, solid_capstyle="round")
    ax.fill_between(x, values, color=pal[0], alpha=0.12, linewidth=0)
    ax.set_title(f"Sales by day ({currency})", loc="left", color=ink["text"], fontsize=11, pad=10)
    ax.grid(axis="y", color=ink["grid"], linewidth=0.8); ax.set_axisbelow(True)
    for side in ("top", "right", "left"): ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(ink["axis"])
    ax.tick_params(colors=ink["muted"], labelsize=8, length=0)
    step = max(1, len(days) // 8)
    ax.set_xticks(list(x)[::step], days[::step])
    ax.set_ylim(bottom=0)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:,.0f}")
    if values and max(values) > 0:
        i = max(range(len(values)), key=values.__getitem__)
        ax.annotate(f"{values[i]:,.2f}", (i, values[i]), textcoords="offset points", xytext=(0, 6), ha="center", fontsize=8, color=ink["text"])  # the one label: the best day
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", facecolor=ink["surface"])
    return buf.getvalue()
def statement(shop_name: str, company_code: str, currency: str, month: date, orders: pd.DataFrame, items: pd.DataFrame, settlements: list[Settlement], ledger: dict, days: list[str], sales: list[float]) -> bytes:
    """The month's statement as an Excel workbook: Summary (+ the sales chart), Orders, Items & tax, Tax by rate, Settlements."""
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.utils import get_column_letter
    sold = orders[orders["status"].isin(COUNTED)] if len(orders) else orders
    won = orders[orders["success"]] if len(orders) else orders
    ok_items = items[items["order_id"].isin(sold["id"])] if len(items) and len(sold) else items.iloc[0:0] if len(items) else items
    money = lambda s, col: round(float(s[col].sum()), 2) if len(s) else 0.0
    bal = ledger.get(currency, {})
    summary = pd.DataFrame([("Shop", shop_name), ("Company ID", company_code), ("Month", month.strftime("%B %Y")), ("Currency", currency), ("", ""),
                            ("Orders placed (not cancelled / returned)", int(len(sold))), ("Sales, tax included", money(sold, "total")), ("  of which shipping", money(sold, "shipping")),
                            ("  of which tax (GST / VAT) inside the prices", money(sold, "tax")), ("Taxable value of the items", round(float(ok_items["taxable"].sum()), 2) if len(ok_items) else 0.0),
                            ("Delivered + paid (successful)", int(len(won))), ("Platform commission on them", money(won, "commission")),
                            ("Cancelled orders", int((orders["status"] == "cancelled").sum()) if len(orders) else 0), ("Returned orders", int((orders["status"] == "returned").sum()) if len(orders) else 0), ("", ""),
                            ("Balance with the platform (all time, + = owed to you)", round(bal.get("balance", 0) / 100, 2)), ("Paid out to you (all time)", round(bal.get("paid_out", 0) / 100, 2)),
                            ("Commission you paid on cash orders (all time)", round(bal.get("paid_in", 0) / 100, 2))], columns=["", "Value"])
    o = sold.drop(columns=["id", "day", "success"]).copy() if len(sold) else pd.DataFrame(columns=["number"])
    if len(o): o["created"] = o["created"].dt.strftime("%Y-%m-%d %H:%M"); o["net to seller"] = (o["total"] - o["commission"]).round(2)
    it = ok_items.drop(columns=["order_id"]) if len(ok_items) else pd.DataFrame(columns=["number"])
    tax = ok_items.groupby("tax_rate").agg(items=("qty", "sum"), taxable=("taxable", "sum"), tax=("tax", "sum"), gross=("gross", "sum")).reset_index().round(2) if len(ok_items) else pd.DataFrame(columns=["tax_rate"])
    st = pd.DataFrame([{"date": s.at.strftime("%Y-%m-%d"), "direction": "paid to you" if s.direction == "to_seller" else "you paid the platform", "amount": round(s.amount_minor / 100, 2),
                        "currency": s.currency, "reference": s.reference, "note": s.note} for s in settlements]) if settlements else pd.DataFrame(columns=["date"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in (("Summary", summary), ("Orders", o), ("Items & tax", it), ("Tax by rate", tax), ("Settlements", st)):
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for i, col in enumerate(df.columns, 1): ws.column_dimensions[get_column_letter(i)].width = min(48, max(12, len(str(col)) + 2, *(len(str(v)) + 2 for v in df[col].head(200))))
        png = io.BytesIO(_chart_png(days, sales, currency))
        xw.sheets["Summary"].add_image(XLImage(png), "D2")
    return buf.getvalue()
