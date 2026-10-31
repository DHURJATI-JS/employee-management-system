"""Bank accounts in any country: a person's (where their salary goes) and a company's own (salaries are paid from it, shop sales are paid
into it). Each country's banks are reached their own way: India by IFSC, the US by ABA routing number, Australia by BSB, Canada by transit
+ institution number, Singapore by bank + branch code; across the euro area, the UK, Switzerland, the Gulf and more, the IBAN is the
account number. Money from abroad also needs the bank's SWIFT/BIC. Checked here, not just their shape: an IBAN's mod-97 check digits and
its country's length, an ABA number's checksum, and that a BIC (and an IBAN) belong to the bank's country. The account number / IBAN is
stored only encrypted (system.seal); only its last 4 characters are ever shown again."""
import re
LOCAL = {  # country -> (the local code's name, its format (spaces / hyphens dropped), example, the account number's format, how it's described)
    "IN": ("IFSC", r"[A-Z]{4}0[A-Z0-9]{6}", "HDFC0001234", r"\d{9,18}", "9 to 18 digits"),
    "US": ("ABA routing number", r"\d{9}", "026009593", r"\d{4,17}", "4 to 17 digits"),
    "AU": ("BSB", r"\d{6}", "062-000", r"\d{5,10}", "5 to 10 digits"),
    "CA": ("transit + institution no.", r"\d{8}", "12345-003", r"\d{7,12}", "7 to 12 digits"),
    "SG": ("bank + branch code", r"\d{7}", "7171-081", r"\d{6,14}", "6 to 14 digits")}
IBAN = {"AE": 23, "AT": 20, "BE": 16, "BH": 22, "CH": 21, "DE": 22, "DK": 18, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "IE": 22, "IT": 27, "KW": 30,  # countries where the IBAN
        "LU": 20, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "QA": 29, "SA": 24, "SE": 24, "TR": 26}  # is the account number -> its length
OTHER = "ZZ"  # another country: an account number + the bank's SWIFT/BIC
NAMES = {"IN": "India", "US": "United States", "GB": "United Kingdom", "AU": "Australia", "CA": "Canada", "SG": "Singapore", "AE": "United Arab Emirates",
         "DE": "Germany", "FR": "France", "NL": "Netherlands", "IE": "Ireland", "ES": "Spain", "IT": "Italy", "AT": "Austria", "BE": "Belgium", "BH": "Bahrain",
         "CH": "Switzerland", "DK": "Denmark", "FI": "Finland", "KW": "Kuwait", "LU": "Luxembourg", "NO": "Norway", "PL": "Poland", "PT": "Portugal", "QA": "Qatar",
         "SA": "Saudi Arabia", "SE": "Sweden", "TR": "Türkiye", OTHER: "another country"}
COUNTRIES = {k: NAMES[k] for k in ("IN", *sorted((k for k in NAMES if k not in ("IN", OTHER)), key=NAMES.get), OTHER)}  # the picker: India first, "another country" last
IBAN_EXAMPLE = "DE89 3704 0044 0532 0130 00"
def scheme(country: str) -> str: return "iban" if country in IBAN else "local"
def picker() -> list[dict]:  # for the form: each country's code name, example and account format (app.js swaps the fields' labels)
    return [{"code": k, "name": v, "scheme": scheme(k), "label": LOCAL[k][0] if k in LOCAL else "bank code (optional)", "example": LOCAL[k][2] if k in LOCAL else "",
             "account": "IBAN, e.g. " + IBAN_EXAMPLE if k in IBAN else LOCAL[k][4] if k in LOCAL else "4 to 34 letters or digits"} for k, v in COUNTRIES.items()]
def iban_ok(iban: str) -> bool:  # ISO 13616: move the first 4 characters to the end, letters -> 10..35, the number mod 97 must be 1
    return bool(re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban)) and int("".join(str(int(ch, 36)) for ch in iban[4:] + iban[:4])) % 97 == 1
def aba_ok(n: str) -> bool: return sum(int(d) * w for d, w in zip(n, (3, 7, 1) * 3)) % 10 == 0  # an ABA routing number's checksum
def squash(v: str) -> str: return re.sub(r"[\s.-]", "", v or "").upper()
def clean(country: str, routing: str, account: str, swift: str) -> tuple[str, str, str]:
    """(routing, account, swift) normalised for a bank in `country`, or ValueError saying what's wrong. routing is "" where the IBAN is the
    account number; a SWIFT/BIC is optional (money from abroad needs it) except for "another country", where it's how the bank is found."""
    routing, account, swift = squash(routing), squash(account), squash(swift)
    if swift:
        if not re.fullmatch(r"[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?", swift): raise ValueError("enter the SWIFT / BIC: 8 or 11 letters and digits, like HDFCINBBXXX")
        if country != OTHER and swift[4:6] != country: raise ValueError(f"that SWIFT / BIC is a bank in {NAMES.get(swift[4:6], swift[4:6])}, not {NAMES[country]}: pick the bank's own country")
    if country in IBAN:
        if account[:2] != country: raise ValueError(f"an IBAN from {NAMES[country]} starts with {country}, like {IBAN_EXAMPLE if country == 'DE' else country + '…'}")
        if len(account) != IBAN[country]: raise ValueError(f"an IBAN from {NAMES[country]} has {IBAN[country]} characters; that one has {len(account)}")
        if not iban_ok(account): raise ValueError("that IBAN's check digits don't add up: there's a typo in it")
        return "", account, swift
    if country in LOCAL:
        label, code_re, example, account_re, account_desc = LOCAL[country]
        if not re.fullmatch(code_re, routing): raise ValueError(f"enter the {label}, like {example}" + (" (4 letters, a zero, then 6 letters or digits)" if country == "IN" else ""))
        if country == "US" and not aba_ok(routing): raise ValueError("that ABA routing number's check digit is wrong: check it against a cheque or the bank's website")
        if not re.fullmatch(account_re, account): raise ValueError(f"enter the account number: {account_desc}")
        return routing, account, swift
    if not swift: raise ValueError("a bank in another country is found by its SWIFT / BIC: add it (the bank's website or your statement has it)")
    if not re.fullmatch(r"[A-Z0-9]{4,34}", account): raise ValueError("enter the account number: 4 to 34 letters or digits")
    if routing and not re.fullmatch(r"[A-Z0-9]{2,20}", routing): raise ValueError("the bank code: up to 20 letters or digits")
    return routing, account, swift
def code_label(country: str) -> str: return LOCAL[country][0] if country in LOCAL else "bank code"
def show_code(country: str, code: str) -> str:  # 062000 -> 062-000, 12345003 -> 12345-003, 7171081 -> 7171-081
    return {"AU": lambda c: f"{c[:3]}-{c[3:]}", "CA": lambda c: f"{c[:5]}-{c[5:]}", "SG": lambda c: f"{c[:4]}-{c[4:]}"}.get(country, lambda c: c)(code) if code else ""
def register(env) -> None: env.globals.update(bank_line=bank_line, bank_countries=picker, bank_code_label=code_label)  # system.py calls it for the templates
def bank_line(b) -> str:
    """One line for a saved account (a person's, a company's, or a payout's snapshot): "HDFC Bank, Pune · savings account ••••1234 ·
    IFSC HDFC0001234", "Deutsche Bank · IBAN ••••3000 · SWIFT DEUTDEFF · Germany". In templates: {{ bank_line(b) }}."""
    country = getattr(b, "country", None) or getattr(b, "bank_country", None) or "IN"
    parts = [b.bank_name + (f", {b.branch}" if getattr(b, "branch", "") else "")]
    parts.append(f"IBAN ••••{b.account_last4}" if country in IBAN else f"{getattr(b, 'account_type', '') + ' ' if getattr(b, 'account_type', '') else ''}account ••••{b.account_last4}")
    if b.routing: parts.append(f"{code_label(country)} {show_code(country, b.routing)}")
    if getattr(b, "swift", ""): parts.append(f"SWIFT {b.swift}")
    if country != "IN": parts.append(NAMES.get(country, country))
    return " · ".join(parts)
