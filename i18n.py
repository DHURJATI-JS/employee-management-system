"""The page language (user asked: a switcher for the languages of the regions and currencies here). Pages are translated where they're
made, not in the browser: each language gets its own compiled templates, from a Jinja overlay environment whose loader translates the
templates' own words (the text between tags + the placeholder / title / aria-label / alt / data-busy / data-tip attributes, with any {{ }}
inside kept as {0}, {1}… placeholders). Nothing a template prints is touched, so names, chats and company names stay as they were written.
The words come from LibreTranslate, a translator running on this server (Docker, 127.0.0.1:5000: nothing leaves the machine), and are
kept in locales/<lang>.json. A string not translated yet shows in English and is queued: run() translates the queue in the background and
those templates recompile. Picking a language queues every template's words for it; flash messages are translated on the way out
(prepare(), before the page is made). Not translated yet: the JS's own words, emails / SMS, and labels that come from Python."""
import asyncio, json, os, re, threading, urllib.request
from html import escape, unescape
from pathlib import Path
from jinja2 import BaseLoader
LANGUAGES = {"en": ("English", "English"), "hi": ("Hindi", "हिन्दी"), "ar": ("Arabic", "العربية"), "de": ("German", "Deutsch"), "fr": ("French", "Français"),
             "nl": ("Dutch", "Nederlands"), "es": ("Spanish", "Español"), "it": ("Italian", "Italiano"), "zh": ("Chinese", "中文")}
REGION_LANG = {"IN": "hi", "AE": "ar", "DE": "de", "FR": "fr", "NL": "nl", "ES": "es", "IT": "it", "SG": "zh"}  # suggested to people in that region (the rest: English)
LT_CODE = {"zh": "zh-Hans"}  # LibreTranslate's names
RTL = {"ar"}
COOKIE = "hx_lang"
TRANSLATOR = os.getenv("TRANSLATOR_URL", "http://127.0.0.1:5000")
LOCALES = Path(__file__).parent / "locales"
ATTRS = {"placeholder", "title", "aria-label", "alt", "data-busy", "data-tip"}  # attributes people read
SKIP = {"script", "style", "code", "kbd", "pre"}  # their text stays as written
_memory: dict[str, dict[str, str]] = {}  # lang -> {English: translation}
_version: dict[str, int] = {}
_queue: dict[str, dict[str, int]] = {}  # lang -> {English: 0 = on a page someone's looking at (first), 1 = the rest}
_lock = threading.Lock()
def lang_of(request) -> str:  # the account's choice, else this browser's cookie, else English
    code = (getattr(request.state, "user", None) or {}).get("lang") or request.cookies.get(COOKIE, "")
    return code if code in LANGUAGES else "en"
def table(lang: str) -> dict[str, str]:
    if lang not in _memory:
        try: _memory[lang] = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError): _memory[lang] = {}
    return _memory[lang]
def _save(lang: str) -> None:  # merged with what's on disk first (another process may have translated other words meanwhile), then replaced atomically
    LOCALES.mkdir(exist_ok=True)
    try: disk = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError): disk = {}
    merged = {**disk, **table(lang)}
    _memory[lang] = merged
    tmp = LOCALES / f".{lang}.{os.getpid()}.json.tmp"
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8"); os.replace(tmp, LOCALES / f"{lang}.json")
def known(text: str, lang: str) -> str | None:  # the translation, or None (queued for the worker)
    if (hit := table(lang).get(text)) is not None: return hit
    with _lock: _queue.setdefault(lang, {})[text] = 0  # a page someone's looking at: first
    return None
# ---------- the templates' own words ----------
_JINJA = re.compile(r"(\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})", re.S)
_EDGE = re.compile(r"^([\s/·•>»*:\-–—|()\[\]]*)(.*?)([\s/·•:\-–—|()\[\]]*)$", re.S)  # decoration around the words ("// …", "(…)"): kept as it is
def _translatable(core: str) -> bool:
    words = re.sub(r"\{\d+\}", "", core).strip()
    return len(re.findall(r"[^\W\d_]", words)) >= 2 and not re.fullmatch(r"[~./$#]\S*|\S*[/=_@]\S*|[\w-]+\.(sh|log|py|js|txt)", words)  # paths, commands, file names
def _render_run(parts: list[tuple[str, str]], lang: str, collect: set | None, attr: bool) -> str:
    """One run of text (or an attribute value): its words translated, {{ }} kept as {0}, {1}… placeholders; English when there's no translation yet."""
    raw = "".join(v for _, v in parts)
    if any(k == "text" and ("{" in v or "}" in v) for k, v in parts): return raw  # (braces of its own: left alone)
    exprs, src = [], ""
    for k, v in parts:
        if k == "expr": src += "{%d}" % len(exprs); exprs.append(v)
        else: src += unescape(v)  # "email &amp; phone" -> "email & phone" for the translator
    head, core, tail = _EDGE.match(src).groups()
    if core.startswith(("./", "~/")):  # terminal style: "./login" stays a command; "./start again" keeps its "./" and translates the words
        if " " not in core.strip(): return raw
        head, core = head + core[:2], core[2:]
    if not core or not _translatable(core): return raw
    if collect is not None: collect.add(core); return raw
    if lang == "en" or (done := known(core, lang)) is None: return raw
    if any(done.count("{%d}" % i) != 1 for i in range(len(exprs))) or re.search(r"[{}]", re.sub(r"\{\d+\}", "", done)): return raw  # placeholders lost or mangled
    return "".join(re.sub(r"\{(\d+)\}", lambda x: exprs[int(x.group(1))], escape(s, quote=attr)) for s in (head, done, tail))
def translate_source(source: str, lang: str, collect: set | None = None) -> str:
    """A template's source with its own words translated (with collect: its words gathered, the source unchanged). A small HTML state
    machine across the template's literal text: text between tags, attribute values, <script>/<style>/<code>… and comments skipped."""
    out, run = [], []
    st = {"mode": "text", "quote": "", "tag": "", "closing": False, "attr": "", "name": ""}
    def flush():
        if run: out.append(_render_run(run, lang, collect, st["mode"] == "attr")); run.clear()
    def add(c):
        if run and run[-1][0] == "text": run[-1] = ("text", run[-1][1] + c)
        else: run.append(("text", c))
    for piece in _JINJA.split(source):
        if not piece: continue
        if piece[:2] in ("{{", "{%", "{#"):
            if piece[:2] == "{{" and (st["mode"] == "text" or (st["mode"] == "attr" and st["attr"] in ATTRS)): run.append(("expr", piece))
            else: flush(); out.append(piece)
            continue
        i, n = 0, len(piece)
        while i < n:
            mode, c = st["mode"], piece[i]
            if mode == "skip":  # inside <script>, <style>, <code>…: as written, up to its closing tag
                j = piece.lower().find("</" + st["tag"], i)
                if j < 0: out.append(piece[i:]); break
                out.append(piece[i:j]); i = j; st["mode"] = "text"; continue
            if mode == "comment":
                j = piece.find("-->", i)
                if j < 0: out.append(piece[i:]); break
                out.append(piece[i:j + 3]); i = j + 3; st["mode"] = "text"; continue
            if mode == "text":
                if c == "<":
                    if piece.startswith("<!--", i): flush(); out.append("<!--"); i += 4; st["mode"] = "comment"; continue
                    if m := re.match(r"<(/?|!)([a-zA-Z][\w-]*)", piece[i:]):  # a tag, or <!doctype …>
                        flush(); st.update(mode="tag", tag=m.group(2).lower(), closing=bool(m.group(1)), name=""); out.append(c); i += 1; continue
                add(c); i += 1; continue
            if mode == "tag":
                if c in "\"'": st.update(mode="attr", quote=c, attr=st["name"].lower()); out.append(c); i += 1; continue
                if c == ">": out.append(c); i += 1; st["mode"] = "skip" if st["tag"] in SKIP and not st["closing"] else "text"; continue
                if c.isspace(): st["name"] = ""
                elif c != "=": st["name"] += c
                out.append(c); i += 1; continue
            if c == st["quote"]: flush(); out.append(c); i += 1; st.update(mode="tag", name=""); continue  # (mode "attr")
            if st["attr"] in ATTRS: add(c)
            else: out.append(c)
            i += 1
    flush()
    return "".join(out)
class TranslatingLoader(BaseLoader):
    """The usual templates, their own words in `lang`; up to date while the template file and lang's translations are unchanged."""
    def __init__(self, base, lang: str): self.base, self.lang = base, lang
    def get_source(self, environment, template):
        source, filename, uptodate = self.base.get_source(environment, template)
        ver = _version.get(self.lang, 0)
        return translate_source(source, self.lang), filename, lambda: (uptodate() if uptodate else True) and _version.get(self.lang, 0) == ver
_envs: dict = {}
def env_for(lang: str):
    from system import templates
    if lang == "en": return templates.env
    if lang not in _envs: _envs[lang] = templates.env.overlay(loader=TranslatingLoader(templates.env.loader, lang), cache_size=400)  # (its own cache: not the English one)
    return _envs[lang]
def queue_all(lang: str) -> int:
    """Queue every template's words that `lang` doesn't have yet (when someone picks it): the worker fills them in over the next minutes."""
    if lang == "en": return 0
    from system import ROOT
    words: set = set()
    for p in (ROOT / "templates").glob("*.html"): translate_source(p.read_text(encoding="utf-8"), lang, words)
    missing = {w for w in words if w not in table(lang)}
    with _lock:
        q = _queue.setdefault(lang, {})
        for w in missing: q.setdefault(w, 1)  # the rest of the site, after what's being looked at
    return len(missing)
# ---------- the translator ----------
def _libre(texts: list[str], lang: str, timeout: float = 120) -> list[str]:
    body = json.dumps({"q": texts, "source": "en", "target": LT_CODE.get(lang, lang), "format": "text"}).encode()
    req = urllib.request.Request(TRANSLATOR + "/translate", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r: out = json.loads(r.read()).get("translatedText")
    return out if isinstance(out, list) else [out]
def _tidy(en: str, tx: str) -> str:
    tx = " ".join(str(tx).split())
    tx = re.sub(r"([।。．])\.$", r"\1", tx)  # "…।." -> "…।"
    if not en.rstrip().endswith((".", "!", "?", "…", "।")) and tx.endswith((".", "।", "。")): tx = tx[:-1]  # no full stop the English didn't have
    return tx
async def run() -> None:  # started by app.py's lifespan: translates what's queued, 30 strings at a time per language
    while True:
        await asyncio.sleep(2)
        for lang in list(_queue):
            with _lock: batch = [w for w, _ in sorted(_queue.get(lang, {}).items(), key=lambda x: (x[1], x[0]))[:30]]
            if not batch: continue
            try: done = await asyncio.to_thread(_libre, batch, lang)
            except Exception as e:
                print(f"translator ({lang}): {type(e).__name__}: {e} (is the libretranslate container running?)"); await asyncio.sleep(30); continue
            m = table(lang)
            for en, tx in zip(batch, done): m[en] = _tidy(en, tx)
            with _lock:
                for w in batch: _queue[lang].pop(w, None)
            await asyncio.to_thread(_save, lang)
            _version[lang] = _version.get(lang, 0) + 1  # its templates recompile with the new words
async def prepare(request) -> None:
    """Before a page is made: its pending flash messages in the visitor's language (a short wait at most; English if the translator is slow)."""
    lang = lang_of(request)
    if lang == "en" or "session" not in request.scope: return
    todo = [m for _, m in request.session.get("_flashes", []) if table(lang).get(m) is None]
    if not todo: return
    try:
        done = await asyncio.wait_for(asyncio.to_thread(_libre, todo, lang, 4), 4)
        m = table(lang)
        for en, tx in zip(todo, done): m[en] = _tidy(en, tx)
    except Exception: pass
def flash_text(message: str, lang: str) -> str:  # a flash message as shown
    return message if lang == "en" else table(lang).get(message, message)
