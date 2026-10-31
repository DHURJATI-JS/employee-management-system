"""/load-test: the report of the last load test (reports/load_test.json, written when the test ran): every metric and the machine it ran on. Public like the credits: it holds numbers only, no names, paths or secrets.
The home page's alert links here (load_headline); with no report file there's neither an alert nor a page. The charts are plain SVG drawn here on the server, so the page needs no script and no chart library."""
import json, math, os
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException, Request
from decoraters import cache, public
from system import ROOT, render, templates
router = APIRouter(tags=["info"])
REPORT = ROOT / os.getenv("LOAD_REPORT", "reports/load_test.json")
WIDTH, HEIGHT, LEFT, RIGHT, TOP, BOTTOM = 640, 240, 54, 12, 22, 26  # a chart's drawing box (it scales to its card)
_read = {"stamp": None, "data": None}
def report() -> dict | None:  # re-read only when the file changes; no file or a broken one = no page and no alert
    try:
        if (stamp := REPORT.stat().st_mtime_ns) != _read["stamp"]: _read.update(stamp=stamp, data=json.loads(REPORT.read_text(encoding="utf-8")))
    except (OSError, ValueError): return None
    return _read["data"]
def headline() -> dict | None:  # {% set load = load_headline() %}: the home page's alert
    if not (r := report()): return None
    h = r["headline"]
    return {"requests": f"{h['requests']:,}", "length": h.get("length") or f"{h['hours']:g} hours", "people": h["peak_users"], "ok": f"{h['ok_pct']:g}", "answers": h["ai_answers"], "mvp": r.get("mvp"), "flood": (r.get("flood") or {}).get("short")}
templates.env.globals["load_headline"] = headline
def ms_text(v: float | None) -> str:  # 850 -> "850 ms", 2100 -> "2.1 s"
    if v is None: return "-"
    return f"{v / 1000:.3g} s" if v >= 1000 else f"{v:.3g} ms"
def nice(v: float) -> float:  # a round number for the top of an axis
    step = 10 ** math.floor(math.log10(v)) if v > 0 else 1
    return next(n * step for n in (1, 1.5, 2, 3, 4, 5, 7.5, 10) if n * step >= v) if v > 0 else 1
def plot(lines: list[tuple], count: int, tips: list[str], *, log: bool = False, tick=lambda v: f"{v:g}", top: float = 0, xlabels: list | None = None, stages: list[tuple] | None = None, dots: bool = False, label: int = -1) -> dict:
    """One chart's geometry, on one axis. lines: (name, values with None for a gap, css class); tips: the hover text of each slot; stages: (from slot, to slot, text) dividers for a chart over time; label: the line whose points carry their value."""
    values = [v for _, vs, _ in lines for v in vs if v is not None] or [1]
    if log:
        lo, hi = 10 ** math.floor(math.log10(max(min(values), 1))), 10 ** math.ceil(math.log10(max(max(values), 10)))
        marks, where = [10 ** p for p in range(round(math.log10(lo)), round(math.log10(hi)) + 1)], lambda v: (math.log10(max(v, lo)) - math.log10(lo)) / (math.log10(hi) - math.log10(lo))
    else:
        hi = top or nice(max(values))
        marks, where = [hi * i / 4 for i in range(5)], lambda v: min(v, hi) / hi
    tall, slot = HEIGHT - TOP - BOTTOM, (WIDTH - LEFT - RIGHT) / max(count, 1)
    x, y = lambda i: round(LEFT + (i + .5) * slot, 1), lambda v: round(TOP + tall * (1 - where(v)), 1)
    drawn, points = [], []
    for name, vs, cls in lines:
        d, pen = [], False
        for i, v in enumerate(vs):
            if v is None: pen = False; continue
            d.append(f"{'L' if pen else 'M'}{x(i)} {y(v)}"); pen = True
        drawn.append({"name": name, "d": " ".join(d), "cls": cls})
        if dots: points += [{"x": x(i), "y": y(v), "cls": cls, "tip": f"{name}: {tick(v)}", "text": tick(v) if lines.index((name, vs, cls)) == label else ""} for i, v in enumerate(vs) if v is not None]
    return {"w": WIDTH, "h": HEIGHT, "left": LEFT, "right": WIDTH - RIGHT, "top": TOP, "bottom": HEIGHT - BOTTOM, "lines": drawn, "points": points, "legend": len(lines) > 1, "grid": [{"y": y(m), "text": tick(m)} for m in marks],
            "hits": [{"x": round(LEFT + i * slot, 1), "w": round(slot, 1), "tip": t} for i, t in enumerate(tips)], "tall": tall, "xlabels": [{"x": x(i), "text": t} for i, t in enumerate(xlabels or [])],
            "stages": [{"x": round(LEFT + a * slot, 1), "mid": round(LEFT + (a + b) / 2 * slot, 1), "text": t} for a, b, t in stages or []]}
def charts(r: dict) -> dict:
    s, steps = r["series"], [x for x in r["stages"] if x["kind"] == "step"]
    n, people = len(s["rps"]), [x["users"] for x in steps]
    bands = [(x["from_min"], x["to_min"], f"{x['users']}") for x in r["stages"]]
    at = lambda i, text: f"minute {i + 1} · {s['users'][i]} people · {text}"
    return {"latency_people": plot([("half of requests faster than (p50)", [x["p50"] for x in steps], "hx-plot-b"), ("95% faster than (p95)", [x["p95"] for x in steps], "hx-plot-a")], len(steps), [f"{x['users']} people: p50 {ms_text(x['p50'])}, p95 {ms_text(x['p95'])}, p99 {ms_text(x['p99'])}" for x in steps], log=True, tick=ms_text, xlabels=people, dots=True, label=1),
            "rps_people": plot([("requests a second", [x["rps"] for x in steps], "hx-plot-a")], len(steps), [f"{x['users']} people: {x['rps']:g} requests a second" for x in steps], xlabels=people, dots=True, label=0, tick=lambda v: f"{v:.3g}"),
            "rps": plot([("requests a second", s["rps"], "hx-plot-a")], n, [at(i, f"{v:g} requests a second") for i, v in enumerate(s["rps"])], stages=bands),
            "latency": plot([("p50", s["p50"], "hx-plot-b"), ("p95", s["p95"], "hx-plot-a")], n, [at(i, f"p50 {ms_text(s['p50'][i])}, p95 {ms_text(s['p95'][i])}") for i in range(n)], log=True, tick=ms_text, stages=bands),
            "cpu": plot([("the whole machine", s["cpu"], "hx-plot-a")], n, [at(i, f"{s['cpu'][i]}% of the whole machine") for i in range(n)], top=100, tick=lambda v: f"{v:g}%", stages=bands),
            "app_cpu": plot([("the app server", s["app_cpu"], "hx-plot-a")], n, [at(i, f"app server {s['app_cpu'][i]}% of one core") for i in range(n)], tick=lambda v: f"{v:g}%", stages=bands),
            "memory": plot([("the app server's memory", s["app_mb"], "hx-plot-a")], n, [at(i, f"app server {s['app_mb'][i]} MB") for i in range(n)], tick=lambda v: f"{v:g} MB", stages=bands),
            "fails": plot([("failed requests", s["fails"], "hx-plot-c")], n, [at(i, f"{v} failed") for i, v in enumerate(s["fails"])], top=4 * math.ceil(max(max(s["fails"], default=1), 1) / 4), tick=lambda v: f"{v:g}", stages=bands)}  # (counts: a top that divides into whole numbers)
def when(stamp: str) -> str: return datetime.fromisoformat(stamp).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %B %Y, %H:%M IST")
@router.get("/load-test")
@public
@cache.memoize(timeout=60)
async def load_test(request: Request):
    if not (r := report()): raise HTTPException(404, "No load test has been published yet.")
    return render(request, "load_test", {"rep": r, "plots": charts(r), "ms": ms_text, "when": when(r["started"]), "ended": when(r["finished"])})
