"""Web search for the Deminic AI (user asked): DuckDuckGo's keyless Lite page, Wikipedia's search API when that gives nothing. Fetched like
link previews (shopai.fetch_page: public addresses only, pinned, small, quick). Only the search words leave the server, never the
conversation; what comes back is data for the model, never instructions. Same query within an hour: cached. Who may search how often:
spam.web_allowed."""
import json, re, time, urllib.parse
from html import unescape
from shopai import FetchError, fetch_page
CACHE_S = 3600
_cache: dict[str, tuple[float, str, list[dict]]] = {}  # query -> (when, engine, results)
class SearchError(Exception): pass
def _clean(s: str) -> str: return " ".join(unescape(re.sub(r"<[^>]+>", " ", s)).split())
def _duckduckgo(q: str) -> list[dict]:
    _, html = fetch_page("https://lite.duckduckgo.com/lite/?" + urllib.parse.urlencode({"q": q, "kl": "wt-wt"}), 800_000)
    out = []
    for chunk in re.split(r"(?=<a[^>]+class=['\"]result-link['\"])", html)[1:]:  # one result each: its link, then its snippet
        if not (a := re.match(r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", chunk, re.S)): continue
        href = unescape(a.group(1))
        if "uddg=" in href: href = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg", [""])[0]  # DuckDuckGo's redirect -> the real address
        if not href.startswith(("http://", "https://")) or "duckduckgo.com/y.js" in href: continue  # ads
        s = re.search(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", chunk, re.S)
        out.append({"title": _clean(a.group(2))[:140], "url": href[:500], "snippet": _clean(s.group(1))[:400] if s else ""})
    return out
def _wikipedia(q: str) -> list[dict]:
    _, body = fetch_page("https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": 5}), 400_000)
    return [{"title": h["title"][:140], "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(h["title"].replace(" ", "_")), "snippet": _clean(h.get("snippet", ""))[:400]}
            for h in json.loads(body).get("query", {}).get("search", [])]
def search(q: str, n: int = 5) -> tuple[str, list[dict]]:
    """(engine, up to n results [{title, url, snippet}]) for a query; SearchError when neither engine answers."""
    q = " ".join(q.split())[:120]
    if (hit := _cache.get(q.lower())) and time.time() - hit[0] < CACHE_S: return hit[1], hit[2][:n]
    for engine, run in (("DuckDuckGo", _duckduckgo), ("Wikipedia", _wikipedia)):
        try: found = run(q)
        except (FetchError, ValueError, KeyError): continue
        if found:
            if len(_cache) > 500: _cache.clear()
            _cache[q.lower()] = (time.time(), engine, found)
            return engine, found[:n]
    raise SearchError("no search engine answered")
