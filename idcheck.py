"""Identity numbers checked online with Cashfree's Verification Suite while ID_VERIFY=1 in .env (same keys as bank verification):
PAN (POST /verification/pan: valid + registered_name + name_match_result), Indian driving licences (POST /verification/driving-license:
dl_number + the holder's date of birth -> status VALID / INVALID + the name on record) and voter IDs (POST /verification/voter-id:
epic_number -> status + name). Aadhaar needs a one-time code from its holder, and other countries' IDs need a document-verification
provider: those are checked by format + check digit only (form.ID_FORMATS) and saved as such. Off: nothing is sent anywhere."""
import asyncio, json, re, secrets, urllib.error, urllib.request
from datetime import date
from bankcheck import NAME_OK
from system import CASHFREE_BASE_URL, CASHFREE_CLIENT_ID, CASHFREE_CLIENT_SECRET, ID_VERIFY
ONLINE = {"pan": "Cashfree PAN", "driving_licence": "Cashfree driving licence", "voter_id": "Cashfree voter ID"}  # what can be checked online
class IdCheckError(Exception): pass
def _post(path: str, body: dict) -> dict:
    if not (CASHFREE_CLIENT_ID and CASHFREE_CLIENT_SECRET): raise IdCheckError("ID verification isn't set up on this server (CASHFREE_CLIENT_ID / CASHFREE_CLIENT_SECRET): try again later.")
    req = urllib.request.Request(CASHFREE_BASE_URL + path, json.dumps(body).encode(), {"Content-Type": "application/json", "x-client-id": CASHFREE_CLIENT_ID, "x-client-secret": CASHFREE_CLIENT_SECRET})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 400: raise IdCheckError("The registry says that number isn't valid: check it and try again.") from None
        if e.code in (401, 403): raise IdCheckError("ID verification is misconfigured on this server: tell the site's admin.") from None
        if e.code == 429: raise IdCheckError("Too many ID checks at once: try again in a minute.") from None
        raise IdCheckError("ID verification is unavailable right now: try again shortly.") from None
    except (OSError, ValueError): raise IdCheckError("Couldn't reach the ID verification service: try again shortly.") from None
def _words(name: str) -> set[str]: return {w for w in re.sub(r"[^a-z ]", " ", name.lower()).split() if len(w) > 1}
def names_match(on_record: str, typed: str) -> bool:  # at least 2 words in common (every word when a name has just one): "Test Ben" isn't "Test Dan"
    a, b = _words(on_record), _words(typed)
    return bool(a and b) and len(a & b) >= min(2, len(a), len(b))
def _check(id_type: str, number: str, name: str, dob: date | None) -> dict:
    clean_name = re.sub(r"[^\w\s./&-]", "", name)[:50]
    if id_type == "pan":
        d = _post("/pan", {"pan": number, "name": clean_name})
        if d.get("valid") is not True or d.get("pan_status", "VALID") != "VALID": raise IdCheckError("The tax department says that PAN isn't valid.")
        if d.get("name_match_result") not in NAME_OK: raise IdCheckError(f"The name doesn't match the PAN records (they have \"{d.get('registered_name') or 'another name'}\").")
        return {"name": str(d.get("registered_name") or "")[:100], "by": ONLINE[id_type]}
    vid = "hx" + secrets.token_hex(12)  # a fresh verification id per check (Cashfree refuses one it has seen)
    if id_type == "driving_licence":
        if not dob: raise IdCheckError("Add your date of birth first (settings → profile): a driving licence is checked together with it.")
        d = _post("/driving-license", {"verification_id": vid, "dl_number": number, "dob": dob.isoformat()})
        on_record = (d.get("details_of_driving_licence") or {}).get("name") or ""
    else:  # voter_id
        d = _post("/voter-id", {"verification_id": vid, "epic_number": number, "name": clean_name})
        on_record = d.get("name") or ""
    if d.get("status") != "VALID": raise IdCheckError(f"The registry says that {'licence' if id_type == 'driving_licence' else 'voter ID'} isn't valid.")
    if on_record and not names_match(on_record, name): raise IdCheckError(f"The name on record is \"{on_record}\", not \"{name}\".")
    return {"name": str(on_record)[:100], "by": ONLINE[id_type]}
async def verify_id(id_type: str, number: str, name: str, dob: date | None) -> dict | None:
    """{name, by} once the registry confirms it; None when ID_VERIFY is off or this ID type can't be checked online; else IdCheckError."""
    return await asyncio.to_thread(_check, id_type, number, name, dob) if ID_VERIFY and id_type in ONLINE else None
