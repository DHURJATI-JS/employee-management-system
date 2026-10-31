"""Spam protection for chats, the Deminic AI and the shop's AI (user asked). In memory, like the other rate limits (assist.rate_limited): per
process, gone on a restart, nothing stored. Every way of writing goes through it: messages, files, forwards, enquiries, new chats (routers/chats.py),
questions to the AI (deminic.py, the instruction bar's handoff included) and to the shopping assistant (routers/shop.py, shopai.py).
People: a burst limit, a per-minute and a per-day limit, the same text again and again (forwards too), messages to many different chats at
once, too many links, junk (one key held down), files in bulk, and new chats a day. Tripping a rule is a strike: STRIKES within STRIKE_WINDOW
mute the person's sending for a while (reading still works). The AI (the local model, one job at a time, shared with meetings' captions):
stricter caps, the same question again, junk; its own strikes and mute. One question at a time + how many wait in all: deminic.guard.
The shopping assistant (same model, and what it can't answer becomes a bell + a task for a shop's real people): its own caps, per shop too, the same
question again or on product after product, junk, links, contact details; its own strikes and mute; and the model's own verdict (shopai.answer_question)."""
import hashlib, re, time
from collections import Counter, defaultdict, deque
import abuse
from system import FormError
_hits: dict[str, deque] = defaultdict(deque)  # "<rule>:<user>" -> times
_said: dict[str, deque] = defaultdict(deque)  # "<chat|ai|shopai>:<user>" -> (time, digest of the text, where)
_strikes: dict[str, deque] = defaultdict(deque)  # "<chat|ai|shopai>:<user>" -> times a rule was tripped
_muted: dict[str, float] = {}  # "<chat|ai|shopai>:<user>" -> until (time.time())
STRIKES, STRIKE_WINDOW = 3, 600  # 3 strikes in 10 minutes = muted
MUTE = {"chat": 600, "ai": 900, "shopai": 900}  # seconds: chats 10 minutes, the AIs 15
PAUSED = {"chat": "sending is", "ai": "Deminic AI is", "shopai": "the shopping assistant is"}
URL = re.compile(r"https?://|www\.", re.I)
LINK = re.compile(URL.pattern + r"|\b[a-z0-9-]{2,}\.(?:com|net|org|io|xyz|ru|shop)\b", re.I)  # + bare domains (only the endings a typo like "good.in blue" can't make)
CONTACT = re.compile(r"\+?\d[\d\s().-]{8,}\d|[\w.+-]+@[\w-]+\.\w+")
_calls = 0
def _recent(q: deque, window: float, now: float) -> deque:
    while q and now - (q[0][0] if isinstance(q[0], tuple) else q[0]) > window: q.popleft()
    return q
def _over(key: str, limit: int, window: float, now: float) -> bool: return len(_recent(_hits[key], window, now)) >= limit
def _norm(text: str) -> str: return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())  # case, punctuation and spacing don't make a message new
def _digest(text: str) -> str: return hashlib.sha256(_norm(text)[:600].encode()).hexdigest()[:20]
def junk(text: str) -> bool:
    """One key held down ("aaaa…", "!!!!…") or a few characters over and over ("hahaha…", "buybuybuy…"), in a longer message."""
    t = re.sub(r"\s+", "", text)
    if len(t) < 24: return False
    return Counter(t).most_common(1)[0][1] / len(t) > 0.6 or bool(re.search(r"(.{1,6}?)\1{14,}", t))
def _strike(key: str, why: str, now: float) -> FormError:
    s = _recent(_strikes[key], STRIKE_WINDOW, now); s.append(now)
    what, uid = key.split(":")
    abuse.record(f"{what}-strike", int(uid), "", why)  # the admin console sees who keeps hitting the limits (abuse.py)
    if len(s) >= STRIKES:
        _muted[key] = now + MUTE[what]; s.clear()
        abuse.record(f"{what}-mute", int(uid), "", f"paused {MUTE[what] // 60} minutes after {STRIKES} strikes")  # + a warning on their bell
        return FormError(f"{why} That's {STRIKES} times in a few minutes: {PAUSED[what]} paused for you for {MUTE[what] // 60} minutes (spam protection).")
    return FormError(why)
def _muted_now(key: str, now: float) -> None:
    if (left := round((_muted.get(key, 0) - now) / 60 + .49)) > 0:  # whole minutes left, rounded up
        mins = f"{left} more minute{'s' if left > 1 else ''}"
        raise FormError(f"You're sending too fast: chats are paused for you for {mins} (spam protection). You can still read." if key.startswith("chat") else
                        f"The shopping assistant is paused for you for {mins}: too many questions in a short time (spam protection)." if key.startswith("shopai") else
                        f"Deminic AI is paused for you for {mins}: too many messages in a short time (spam protection).")
    _muted.pop(key, None)
def _prune(now: float) -> None:  # now and then: forget people who've gone quiet
    global _calls
    _calls += 1
    if _calls % 2000: return
    for d in (_hits, _said, _strikes):
        for k in [k for k, q in d.items() if not _recent(q, 86400, now)]: del d[k]
    for k in [k for k, t in _muted.items() if t <= now]: del _muted[k]
def check_chat(uid: int, where, text: str = "", kind: str = "text") -> None:
    """Before a message (kind "text"), a file ("file") or a forward ("forward") is written into chat `where` (its id; a new enquiry: a key).
    Raises FormError (the drawer shows it, a page flashes it); on success it's counted."""
    now, key = time.time(), f"chat:{uid}"
    _prune(now); _muted_now(key, now)
    if _over(f"burst:{uid}", 8, 10, now): raise _strike(key, "Slow down: that's a lot of messages in a few seconds.", now)
    if _over(f"minute:{uid}", 40, 60, now): raise _strike(key, "That's a lot of messages in a minute: slow down a little.", now)
    if _over(f"day:{uid}", 1500, 86400, now): raise FormError("That's the most messages one person can send in a day: try again tomorrow.")
    if kind == "file" and _over(f"files:{uid}", 15, 600, now): raise _strike(key, "That's a lot of files in a few minutes: wait a little before sending more.", now)
    if text and junk(text): raise _strike(key, "That message looks like spam (the same characters over and over): it wasn't sent.", now)
    if len(URL.findall(text)) > 5: raise _strike(key, "Too many links in one message (5 at most): it wasn't sent.", now)
    said = _recent(_said[key], 600, now)
    if len(_norm(text)) >= 8 and sum(1 for _, d, _ in said if d == _digest(text)) >= 3:
        raise _strike(key, "You've sent that same message 3 times in the last few minutes: that looks like spam, so it wasn't sent again.", now)
    chats = {w for t, _, w in said if now - t <= 300}
    if where not in chats and len(chats) >= 12: raise _strike(key, "You're messaging a lot of different chats at once: wait a few minutes.", now)
    for rule in ("burst", "minute", "day", *(("files",) if kind == "file" else ())): _hits[f"{rule}:{uid}"].append(now)
    said.append((now, _digest(text) if text else "", where))
def check_new_chat(uid: int) -> None:  # starting a conversation (one to one, a group, an enquiry): 25 new ones a day
    now = time.time()
    _muted_now(f"chat:{uid}", now)
    if _over(f"new:{uid}", 25, 86400, now): raise FormError("That's a lot of new chats for one day: carry on in the ones you have, or try again tomorrow.")
    _hits[f"new:{uid}"].append(now)
def web_allowed(uid: int) -> bool:
    """May the Deminic AI search the web for this person now: 15 searches an hour and 60 a day each, 150 an hour for everyone together (the
    search engines mustn't take this server for a bot). Counted when it may."""
    now = time.time()
    if _over(f"web-hour:{uid}", 15, 3600, now) or _over(f"web-day:{uid}", 60, 86400, now) or _over("web-all", 150, 3600, now): return False
    for k in (f"web-hour:{uid}", f"web-day:{uid}", "web-all"): _hits[k].append(now)
    return True
def check_ai(uid: int, text: str) -> None:
    """Before a question goes to the Deminic AI: 4 a minute, 40 an hour, 150 a day; not the same question a 3rd time in 10 minutes; no junk."""
    now, key = time.time(), f"ai:{uid}"
    _prune(now); _muted_now(key, now)
    if _over(f"ai-minute:{uid}", 4, 60, now): raise _strike(key, "That's a lot of questions in a minute: give Deminic a moment.", now)
    if _over(f"ai-hour:{uid}", 40, 3600, now): raise FormError("That's 40 questions to Deminic in an hour, the most there is: try again a little later.")
    if _over(f"ai-day:{uid}", 150, 86400, now): raise FormError("That's 150 questions to Deminic today, the most there is: try again tomorrow.")
    if junk(text): raise _strike(key, "That doesn't look like a question (the same characters over and over).", now)
    said = _recent(_said[key], 600, now)
    if sum(1 for _, d, _ in said if d == _digest(text)) >= 2: raise _strike(key, "You've asked that twice already: its answer is above.", now)
    for rule in ("ai-minute", "ai-hour", "ai-day"): _hits[f"{rule}:{uid}"].append(now)
    said.append((now, _digest(text), "ai"))
def check_shop_ai(uid: int, company, product: int, text: str) -> None:
    """Before a question goes to the shopping assistant: 4 a minute, 20 an hour, 60 a day, 15 a day to one shop; not the same question a 3rd time on a
    product, or on a 6th product in 10 minutes (the page's suggested questions are short and shared, so a few products are fine); no junk, links or contact details."""
    now, key = time.time(), f"shopai:{uid}"
    _prune(now); _muted_now(key, now)
    if _over(f"shop-minute:{uid}", 4, 60, now): raise _strike(key, "That's a lot of questions in a minute: give the assistant a moment.", now)
    if _over(f"shop-hour:{uid}", 20, 3600, now): raise FormError("That's 20 questions to the shopping assistant in an hour, the most there is: try again a little later.")
    if _over(f"shop-day:{uid}", 60, 86400, now): raise FormError("That's 60 questions to the shopping assistant today, the most there is: try again tomorrow.")
    if _over(f"shop-seller:{uid}:{company}", 15, 86400, now): raise FormError("That's 15 questions to this shop today, the most there is: try again tomorrow.")
    if junk(text) or sum(c.isalpha() for c in text) < 3: raise _strike(key, "That doesn't look like a question.", now)
    if CONTACT.search(text): raise FormError("Leave out phone numbers and email addresses: a question can end up on the public page.")  # (before LINK: an address's domain isn't an advert)
    if LINK.search(text): raise _strike(key, "Questions can't contain links: ask about the product itself.", now)
    said, digest =_recent(_said[key], 600, now), _digest(text)
    if sum(1 for _, d, w in said if d == digest and w == product) >= 2: raise _strike(key, "You've asked that twice already: its answer is on this page.", now)
    if len({w for _, d, w in said if d == digest and w != product}) >= 5: raise _strike(key, "That's the same question on a lot of products in a few minutes: that looks like spam.", now)
    for rule in ("shop-minute", "shop-hour", "shop-day", "shop-seller"): _hits[f"{rule}:{uid}:{company}" if rule == "shop-seller" else f"{rule}:{uid}"].append(now)
    said.append((now, digest, product))
def shop_ai_flagged(uid: int, question: str) -> None:  # the assistant itself judged a question to be spam (shopai.answer_question): a strike like any other, only nobody's waiting for the refusal
    _strike(f"shopai:{uid}", f"the shopping assistant called this spam: {question[:160]}", time.time())
