"""Bank account verification ("penny drop": ₹1 sent to the account, the bank answers with the holder's name) with Cashfree's Bank Account
Verification API, v2 sync (POST /verification/bank-account/sync, headers x-client-id + x-client-secret; docs:
https://www.cashfree.com/docs/api-reference/vrs/v2/bav-v2/bank-account-verification-sync-v2). Only while BANK_VERIFY=1 in .env: when
it's off, accounts are saved unverified and nothing is sent anywhere. An invalid account comes back as HTTP 200 with account_status
INVALID; the holder's name must match the bank's at least moderately (name_match_result)."""
import asyncio, json, re, urllib.error, urllib.request
from system import BANK_VERIFY, CASHFREE_BASE_URL, CASHFREE_CLIENT_ID, CASHFREE_CLIENT_SECRET
NAME_OK = ("DIRECT_MATCH", "GOOD_PARTIAL_MATCH", "MODERATE_PARTIAL_MATCH")
WHY = {"INVALID_ACCOUNT_FAIL": "that account number doesn't exist at this bank", "ACCOUNT_BLOCKED": "that account is blocked",
       "INVALID_IFSC_FAIL": "the IFSC is wrong", "NRE_ACCOUNT_FAIL": "NRE accounts can't receive salaries"}
class BankCheckError(Exception): pass
def _cashfree(account: str, ifsc: str, name: str) -> dict:
    if not (CASHFREE_CLIENT_ID and CASHFREE_CLIENT_SECRET): raise BankCheckError("Bank verification isn't set up on this server (CASHFREE_CLIENT_ID / CASHFREE_CLIENT_SECRET): try again later.")
    body = json.dumps({"bank_account": account, "ifsc": ifsc, "name": re.sub(r"[^\w\s./&-]", "", name)[:100]}).encode()  # the name chars Cashfree accepts
    req = urllib.request.Request(CASHFREE_BASE_URL + "/bank-account/sync", body, {"Content-Type": "application/json", "x-client-id": CASHFREE_CLIENT_ID, "x-client-secret": CASHFREE_CLIENT_SECRET})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 422: raise BankCheckError("The bank couldn't check that account right now (its network may be down): try again in a few minutes.") from None
        if e.code == 429: raise BankCheckError("Too many bank checks at once: try again in a minute.") from None
        if e.code in (401, 403): raise BankCheckError("Bank verification is misconfigured on this server: tell the site's admin.") from None
        raise BankCheckError("Bank verification failed: check the account number and IFSC, then try again." if e.code == 400 else "Bank verification is unavailable right now: try again shortly.") from None
    except (OSError, ValueError): raise BankCheckError("Couldn't reach the bank verification service: try again shortly.") from None
    if data.get("account_status") != "VALID": raise BankCheckError(f"The bank says {WHY.get(data.get('account_status_code'), 'that account is not valid')}.")
    if data.get("name_match_result") not in NAME_OK: raise BankCheckError(f"The account holder's name doesn't match the bank's records (the bank has it as \"{data.get('name_at_bank') or 'another name'}\").")
    return {"name_at_bank": str(data.get("name_at_bank") or "")[:100], "reference": str(data.get("reference_id") or "")}
async def verify_bank(account: str, ifsc: str, name: str, country: str = "IN") -> dict | None:
    """{name_at_bank, reference} once the bank confirms the account and the name, None when it isn't checked (BANK_VERIFY off, or a bank
    outside India: the penny drop only reaches Indian banks), else BankCheckError."""
    return await asyncio.to_thread(_cashfree, account, ifsc, name) if BANK_VERIFY and country == "IN" else None
def unchecked_why(country: str) -> str: return "bank verification is off" if country == "IN" else "automatic checks reach Indian banks only"
