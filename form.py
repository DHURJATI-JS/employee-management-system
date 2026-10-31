import re, unicodedata, uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any, Callable, ClassVar, Literal
from urllib.parse import urlsplit, urlunsplit
import phonenumbers
from pydantic import BaseModel, EmailStr, Field, ValidationError, create_model, field_validator, model_validator
import banks
from banks import COUNTRIES as BANK_COUNTRIES
from decoraters import SavedFile, as_form
from shop import COURIERS
from system import APP_NAME, DEFAULT_PHONE_REGION
from tables import (ACCOUNT_TYPES, ASSIGNABLE, COMPANY_PROOFS, ID_TYPES, CURRENCIES, DEFAULT_CURRENCY, JOIN_AS, JOIN_OPTIONS, MAX_EMPLOYEE_LIMIT, MAX_NET_WORTH, MAX_SALARY, MAX_WORK_HOURS, MIN_EMPLOYEE_LIMIT, POSITIONS,
                    REGISTRATION_TYPES, SALARY_PERIODS, SHOP_CATEGORIES, STAFF, ZONES)
COURIER_KEYS = tuple(COURIERS)
class FormModel(BaseModel):
    as_form: ClassVar[Callable[..., Any]]  # set by @as_form; declared so type checkers know it
    ONCE: ClassVar[int] = 0  # seconds: > 0 = what this form makes has no natural key to stop a repeat, so the same form sent again within them is refused (decoraters.as_form)
    @field_validator("*", mode="before")
    @classmethod
    def _no_nul(cls, v): return v.replace("\x00", "") if isinstance(v, str) else v  # every field: Postgres can't store a NUL byte (it would crash the request)
TWICE_S = 15
COMMON_WORDS = frozenset("""password passw0rd p@ssword p@ssw0rd pass letmein welcome admin administrator qwerty qwertyui qwertyuiop asdfgh asdfghjk asdfghjkl zxcvbn zxcvbnm
    iloveyou trustno monkey dragon master shadow sunshine princess football baseball superman batman michael jennifer charlie computer internet starwars whatever freedom
    hello secret changeme default login access abc abcd abcdefgh abcdefghi abcdefghij""".split())  # + what follows: digits and punctuation on the end don't make them any better
COMMON_PASSWORDS = frozenset("""12345678 123456789 1234567890 12345678910 87654321 987654321 0987654321 123123123 12341234 11223344 112233445566 1q2w3e4r 1q2w3e4r5t 1qaz2wsx qazwsxedc
    q1w2e3r4 q1w2e3r4t5 zaq12wsx a1b2c3d4 abc12345 abcd1234 aa123456 password1 passw0rd1 iloveyou1 11111111 00000000""".split())
def password_problem(password: str, username: str = "", email: str = "") -> str | None:
    """Why a new password won't do, or None (user's rule): 8+ characters, not a well-known one, not one character over and over, not your username or the name in your email.
    No demand for digits or symbols: a long passphrase is welcome."""
    low = password.lower()
    if len(password) < 8: return "Pick a password of at least 8 characters."
    if len(set(low)) == 1 or not password.strip(): return "That password is just one character repeated: pick something longer to guess."
    if low in COMMON_PASSWORDS or low.rstrip("0123456789!@#$%^&*.,_-+= ") in COMMON_WORDS: return "That's one of the most common passwords, the first ones anyone tries: pick another."
    if any(len(n) >= 3 and n in low for n in (username.lower(), email.lower().partition("@")[0])): return "Your password can't contain your username or the name in your email."
    return None
MIN_AGE, MAX_AGE = 18, 120  # signups under 18 are declined
def adult_since(today: date | None = None) -> date:  # the latest birthday that's MIN_AGE today (29 Feb -> 28 Feb in non-leap years)
    today = today or date.today()
    try: return today.replace(year=today.year - MIN_AGE)
    except ValueError: return today.replace(year=today.year - MIN_AGE, day=28)
def check_adult(dob: date) -> None:  # checked on the server: the date picker's max is only a convenience
    if dob > date.today() or dob.year < date.today().year - MAX_AGE: raise ValueError("Enter your real date of birth.")
    if dob > adult_since(): raise ValueError(f"You must be at least {MIN_AGE} to sign up for {APP_NAME}.")
@as_form
class RegisterForm(FormModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    email: EmailStr = Field(max_length=255)
    phone: str = Field(max_length=32)
    dob: date  # date of birth, YYYY-MM-DD from <input type="date">
    zone: Literal[tuple(ZONES)]  # "US:America/New_York": their region (currency + national holidays) and time zone (changeable in settings)
    password: str = Field(min_length=8, max_length=128)
    @model_validator(mode="after")
    def _adult(self): check_adult(self.dob); return self
    @model_validator(mode="after")
    def _password(self):
        if why := password_problem(self.password, self.username, str(self.email)): raise ValueError(why)
        return self
    @field_validator("phone")
    @classmethod
    def _e164(cls, v: str) -> str:  # real validation (Google's libphonenumber), stored as E.164: +14155550123
        try: number = phonenumbers.parse(v, DEFAULT_PHONE_REGION)
        except phonenumbers.NumberParseException: raise ValueError("enter the number with its country code, e.g. +1 415 555 0123") from None
        if not phonenumbers.is_valid_number(number): raise ValueError("that isn't a valid phone number")
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
@as_form
class DobForm(FormModel):  # one-time: accounts made before signup asked for it can add their date of birth (same 18+ rule); then it's locked
    dob: date
    @model_validator(mode="after")
    def _adult(self): check_adult(self.dob); return self
@as_form
class VerifyForm(FormModel):  # either code may be empty once that channel is verified
    email_code: str = Field(default="", max_length=12)
    phone_code: str = Field(default="", max_length=12)
@as_form
class ResendForm(FormModel):
    channel: Literal["email", "phone"]
@as_form
class LanguageForm(FormModel):  # the header's language switcher (routers/language.py)
    lang: Literal["en", "hi", "ar", "de", "fr", "nl", "es", "it", "zh"]  # = i18n.LANGUAGES
@as_form
class SavedAccountForm(FormModel):  # the login page's account chooser: take one account off this browser's list
    user_id: int
@as_form
class AdminLoginForm(FormModel):  # the admin console's sign-in (routers/admin.py): the user id + password in .env
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
@as_form
class AdminSwitchForm(FormModel):  # an admin console switch (single company mode): a checkbox, absent when it's turned off
    on: bool = False
@as_form
class AIModeForm(FormModel):  # the company AI's promotion review: off / it suggests / it promotes by itself
    mode: Literal["off", "suggest", "auto"]
@as_form
class AIBestForm(FormModel):  # "who's best for this task?": asking the local AI to read the top few
    task: str = Field(min_length=3, max_length=500)
    @field_validator("task")
    @classmethod
    def _task(cls, v: str) -> str: return " ".join(clean_text(v).split())
@as_form
class AdminReasonForm(FormModel):  # an admin action that needs a clear reason (ban a user / an IP, delete a company): it's shown to the people affected
    reason: str = Field(min_length=10, max_length=400)
    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str: return " ".join(v.split())
@as_form
class AdminIPForm(FormModel):  # block / unblock an IP address (or a range) by hand
    ip: str = Field(min_length=3, max_length=64)
    reason: str = Field(default="", max_length=400)
@as_form
class ForgotForm(FormModel):  # forgot the username or the password (routers/auth.py): who you are + where the code goes
    kind: Literal["username", "password"]
    who: str = Field(min_length=3, max_length=255)  # an email, a phone number (with its country code) or a username
    channel: Literal["email", "phone"] = "email"
@as_form
class RecoverForm(FormModel):  # the code (+ the new password, twice, when it's the password)
    code: str = Field(min_length=1, max_length=12)
    password: str = Field(default="", max_length=128)
    password_confirm: str = Field(default="", max_length=128)
def clean_text(v: str) -> str:
    """Multi-line text: control characters dropped (newlines kept), spaces collapsed, at most one blank line in a row."""
    lines = [" ".join("".join(ch for ch in line if ch.isprintable()).split()) for line in v.replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
def written_ok(v: str, extra: str) -> bool:
    """Letters, digits and extra, in any script. Not \\w: that misses the vowel signs of Hindi, Tamil, Thai and Arabic with its marks (a mark is its own character), so names in them were refused.
    A mark has to follow a letter, and a pile of them isn't writing (the most any script stacks is 3)."""
    run = 0
    for i, ch in enumerate(v):
        kind = unicodedata.category(ch)[0]
        run = run + 1 if kind == "M" else 0
        if run > 3 or (run and not i) or not (kind in "LMN" or ch in extra): return False
    return bool(v)
def person_ok(v: str) -> bool: return bool(v) and unicodedata.category(v[0])[0] == "L" and written_ok(v, " .'-") and not any(unicodedata.category(ch)[0] == "N" for ch in v)  # a name as a bank or an ID prints it
_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
def gstin_ok(v: str) -> bool:
    """A real-looking GSTIN: state code, PAN, entity number, 'Z', and the 15th character must match GST's check-digit algorithm."""
    if not re.fullmatch(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", v) or not (1 <= int(v[:2]) <= 38 or v[:2] in ("97", "99")): return False
    total = sum(p // 36 + p % 36 for p in (_GST_CHARS.index(c) * (2 if i % 2 else 1) for i, c in enumerate(v[:14])))
    return _GST_CHARS[(36 - total % 36) % 36] == v[14]
REGISTRATION_FORMATS = {  # type -> (check, what to tell people)
    "GSTIN": (gstin_ok, "a 15-character GSTIN like 27AAPFU0939F1ZV (its last character is a check digit)"),
    "CIN": (re.compile(r"[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}").fullmatch, "a 21-character CIN like U72900KA2015PTC123456"),
    "EIN": (re.compile(r"\d{2}-\d{7}").fullmatch, "an EIN like 12-3456789"),
    "VAT": (re.compile(r"[A-Z]{2}[A-Z0-9]{2,13}").fullmatch, "a VAT number starting with the country code, like DE123456789"),
    "CRN": (re.compile(r"(?:[A-Z]{2}\d{6}|\d{8})").fullmatch, "an 8-character company number like 01234567 or SC123456"),
    "OTHER": (re.compile(r"[A-Z0-9][A-Z0-9 ./-]{2,39}").fullmatch, "3-40 letters, digits, spaces or . / -")}
def clean_website(v: str) -> str:
    """'' or a normalised https://host/path: http(s) only, a real-looking host, no user:password@ (phishing-style links)."""
    if not (v := v.strip()): return ""
    url = urlsplit(v if re.match(r"(?i)https?://", v) else "https://" + v)
    host = url.hostname or ""
    if url.scheme.lower() not in ("http", "https") or url.username or url.password or not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,24}", host): raise ValueError("enter a website like example.com")
    return urlunsplit((url.scheme.lower(), host + (f":{url.port}" if url.port else ""), url.path, url.query, ""))[:200]
class _CompanyDetails(FormModel):  # shared by create + the edit modal (+ one salary_<position> field per staff position, added below)
    website: str = Field(default="", max_length=200)  # optional
    exact_address: str = Field(min_length=5, max_length=300)  # typed: unit / floor / building, on top of the map location
    @field_validator("exact_address")
    @classmethod
    def _address(cls, v: str) -> str:
        if len(v := " ".join(clean_text(v).split())) < 5: raise ValueError("type the company's exact address (unit, floor, building, street)")
        return v
    registration_type: Literal[tuple(REGISTRATION_TYPES)]  # required: the company's official registration
    registration_number: str = Field(min_length=2, max_length=40)
    location_place_id: str = Field(default="", max_length=300)  # from the map dialog; the route verifies it with Geoapify
    @field_validator("website")
    @classmethod
    def _website(cls, v: str) -> str: return clean_website(v)
    @model_validator(mode="after")
    def _registration(self):
        number = " ".join(self.registration_number.upper().split())
        number = number if self.registration_type == "OTHER" else number.replace(" ", "").replace("-", "") if self.registration_type != "EIN" else re.sub(r"^(\d{2})-?(\d{7})$", r"\1-\2", number.replace(" ", ""))
        check, example = REGISTRATION_FORMATS[self.registration_type]
        if not check(number): raise ValueError(f"That {self.registration_type if self.registration_type != 'OTHER' else 'registration number'} doesn't look right: enter {example}.")
        self.registration_number = number
        return self
    description: str = Field(default="", max_length=1000)  # what the company does
    hiring: str = Field(default="", max_length=1000)  # who they want to hire
    terms: str = Field(default="", max_length=5000)  # its own terms and conditions, accepted (with the standard ones) before anyone joins; a change asks members to accept again
    listed: bool = False  # checkbox: unticked sends nothing
    allow_other_companies: bool = False  # checkbox "employees may also join other companies" (ticked by default on the page)
    max_members: int | None = None  # the employee limit, required (_limit): everyone counts, owner and co-owners too; the edit route also keeps it at or above who's already in
    @model_validator(mode="after")
    def _limit(self):
        if not self.max_members: raise ValueError(f"Set the employee limit: at least {MIN_EMPLOYEE_LIMIT}, as high as you want.")
        if not MIN_EMPLOYEE_LIMIT <= self.max_members <= MAX_EMPLOYEE_LIMIT: raise ValueError(f"The employee limit must be from {MIN_EMPLOYEE_LIMIT} to {MAX_EMPLOYEE_LIMIT:,}.")
        return self
    positions: list[Literal[STAFF]] = Field(default=[], max_length=len(STAFF))  # checkboxes; only staff keys from the master list parse
    salary_currency: Literal[CURRENCIES]  # the unit of every minimum salary below
    salary_period: Literal[SALARY_PERIODS]
    work_start: time  # working hours, required: at most MAX_WORK_HOURS a day (also a database CHECK), on the company's clock
    work_end: time
    work_days: list[int] = Field(default=[], max_length=7)  # checkboxes 0 = Monday … 6 = Sunday
    @model_validator(mode="after")
    def _hours(self):
        span = (self.work_end.hour * 60 + self.work_end.minute) - (self.work_start.hour * 60 + self.work_start.minute)
        if span <= 0: raise ValueError("Working hours must end after they start, on the same day.")
        if span > MAX_WORK_HOURS * 60: raise ValueError(f"Working hours can't be more than {MAX_WORK_HOURS} hours a day.")
        if span < 60: raise ValueError("Working hours must be at least 1 hour a day.")
        if not self.work_days or any(d not in range(7) for d in self.work_days): raise ValueError("Pick the days people work (at least one).")
        self.work_start, self.work_end = self.work_start.replace(second=0, microsecond=0), self.work_end.replace(second=0, microsecond=0)
        return self
    def days_mask(self) -> int: return sum(1 << d for d in set(self.work_days))
    @field_validator("description", "hiring", "terms")
    @classmethod
    def _text(cls, v: str) -> str: return clean_text(v)
    @field_validator("positions")
    @classmethod
    def _positions(cls, v: list[str]) -> list[str]:  # JOIN_AS is always offered (everyone joins as it), whatever was submitted
        return sorted({*v, JOIN_AS}, key=STAFF.index)  # de-duplicated, in the master list's order
    @model_validator(mode="after")
    def _salaries(self):  # every offered position needs its minimum salary (the bare minimum the company pays for it)
        if missing := [POSITIONS[k]["label"] for k in self.positions if not getattr(self, f"salary_{k}")]: raise ValueError(f"Set the minimum salary for {', '.join(missing)}: every position you offer needs one.")
        return self
    def salaries(self) -> dict[str, int]: return {k: getattr(self, f"salary_{k}") for k in self.positions}  # offered positions only
CompanyDetails = create_model("CompanyDetails", __base__=_CompanyDetails, **{f"salary_{k}": (int | None, Field(default=None, ge=1, le=MAX_SALARY)) for k in STAFF})
@as_form
class CompanyForm(CompanyDetails):
    name: str = Field(max_length=64)
    logo: SavedFile  # required: every company has a logo. Scanned + saved only once every other field is valid; media.py re-encodes it or no company is created
    id_proof_type: Literal[tuple(COMPANY_PROOFS)]  # required: a photo of the company's identity proof (registration certificate etc.)
    id_proof: SavedFile
    net_worth: int | None = Field(default=None, ge=0, le=MAX_NET_WORTH)  # optional: the funds it starts with (later: the dashboard, password needed)
    net_worth_currency: Literal[("", *CURRENCIES)] = ""  # "" = the salary currency; any other is converted by the guard (fx.py)
    co_holder: str = Field(default="", max_length=100)  # optional, all or nothing: the company's own bank account (salaries paid from it, deposits into it); holder defaults to the name
    co_bank: str = Field(default="", max_length=100)
    co_branch: str = Field(default="", max_length=100)
    co_country: str = Field(default="IN", max_length=2)  # any country (banks.py)
    co_routing: str = Field(default="", max_length=20)
    co_ifsc: str = Field(default="", max_length=20)  # older name of co_routing
    co_swift: str = Field(default="", max_length=11)
    co_bank_address: str = Field(default="", max_length=200)
    co_account_type: Literal[ACCOUNT_TYPES] = "current"
    co_account: str = Field(default="", max_length=40)
    co_account_confirm: str = Field(default="", max_length=40)
    sells: bool = False  # "do you sell products?" (the create form asks)
    open_shop: bool = False  # ... and open a shop on the marketplace (routers/seller.py)
    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = unicodedata.normalize("NFC", " ".join(v.split()))  # collapse whitespace; no control characters can survive split()
        if not 2 <= len(v) <= 64 or not written_ok(v, "_ .,&'()+-"): raise ValueError("use 2-64 letters, digits, spaces or . , & ' ( ) + -")
        return v
    def company_account(self) -> "CompanyAccount | None":  # None = left empty (add it later on the dashboard)
        if not any(v.strip() for v in (self.co_bank, self.co_branch, self.co_routing, self.co_ifsc, self.co_swift, self.co_account, self.co_account_confirm)): return None
        return CompanyAccount(holder_name=self.co_holder.strip() or self.name, bank_name=self.co_bank, branch=self.co_branch, country=self.co_country, routing=self.co_routing or self.co_ifsc,
                              swift=self.co_swift, bank_address=self.co_bank_address, account_type=self.co_account_type, account_number=self.co_account, account_confirm=self.co_account_confirm)
    @model_validator(mode="after")
    def _co_account(self):
        try: self.company_account()
        except ValidationError as e:
            err, names = e.errors()[0], {"holder_name": "holder", "bank_name": "bank", "branch": "branch", "routing": "bank code", "account_number": "account no", "account_confirm": "account no again"}
            where = names.get(err["loc"][0], "") if err["loc"] else ""
            raise ValueError(f"Company bank account{' (' + where + ')' if where else ''}: {err['msg'].removeprefix('Value error, ')}") from None
        return self
@as_form
class CompanySettingsForm(CompanyDetails): pass  # edit modal: the route checks who may edit and that no held position is removed
class _Wants(FormModel):  # the salary range a person wants (profile form + the instruction bar's "I want 50k-80k a month")
    want_min: int | None = Field(default=None, ge=1, le=MAX_SALARY)  # empty = no salary preference
    want_max: int | None = Field(default=None, ge=1, le=MAX_SALARY)  # empty = "at least want_min"
    want_currency: Literal[CURRENCIES] = DEFAULT_CURRENCY
    want_period: Literal[SALARY_PERIODS] = "month"
    @model_validator(mode="after")
    def _range(self):
        if self.want_max and not self.want_min: raise ValueError("Give the lowest salary you'd accept too (the start of your range).")
        if self.want_max and self.want_max < self.want_min: raise ValueError("The top of your salary range is below its bottom.")
        return self
@as_form
class SalaryWantForm(_Wants): pass  # the instruction bar's confirmation: only the range
@as_form
class ProfileForm(_Wants):
    headline: str = Field(default="", max_length=120)
    bio: str = Field(default="", max_length=2000)
    skills: str = Field(default="", max_length=1200)  # comma separated, stored as a list
    years: int | None = Field(default=None, ge=0, le=60)
    discoverable: bool = False  # checkbox "let company owners find me"; unticked sends nothing
    @field_validator("headline")
    @classmethod
    def _headline(cls, v: str) -> str: return " ".join(clean_text(v).split())
    @field_validator("bio")
    @classmethod
    def _bio(cls, v: str) -> str: return clean_text(v)
    def skill_list(self) -> list[str]:
        return list(dict.fromkeys(s for s in (" ".join(clean_text(x).split()).lower()[:40] for x in re.split(r"[,\n;]", self.skills)) if s))[:40]
@as_form
class JoinCodeForm(FormModel):
    code: str = Field(max_length=64)
    @field_validator("code")
    @classmethod
    def _uuid4(cls, v: str) -> str:  # company codes are uuid4s: anything else is rejected before touching the database
        try: code = uuid.UUID(v.strip())
        except ValueError: raise ValueError("that isn't a valid company code") from None
        if code.version != 4: raise ValueError("that isn't a valid company code")
        return str(code)
@as_form
class JoinForm(FormModel):  # join as Rookie (always offered) or Freelancer (only if the company offers it: checked by the route)
    as_position: Literal[JOIN_OPTIONS] = JOIN_AS
    note: str = Field(default="", max_length=500)  # to whoever reviews the request
    accept_terms: bool = False  # checkbox: the company's terms and conditions (+ the standard ones); the route refuses without it
    terms_version: int = Field(default=0, ge=0)  # the version they read: if it changed meanwhile, they read it again
    @field_validator("note")
    @classmethod
    def _note(cls, v: str) -> str: return clean_text(v)
@as_form
class ChatMessageForm(FormModel):  # one chat message (encrypted at rest by the route)
    body: str = Field(min_length=1, max_length=2000)
    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        if not (v := clean_text(v)): raise ValueError("write a message first")
        return v
@as_form
class DirectChatForm(FormModel):  # start (or open) a one-to-one chat with someone in the same company
    company_id: uuid.UUID
    user_id: int
@as_form
class PersonalChatForm(FormModel):  # start (or open) a personal one-to-one chat: no company (routers/chats.py checks you're friends or share one)
    user_id: int
@as_form
class ChatFileForm(FormModel):  # a file sent in a chat (virus-scanned, any type: ClamAV is the safety net), + an optional line of text
    file: SavedFile
    body: str = Field(default="", max_length=2000)
@as_form
class ForwardForm(FormModel):  # forward a chat message (message_id) or one of the Deminic AI's answers (ai_id) into a chat
    message_id: int = 0
    ai_id: int = 0
@as_form
class PasswordForm(FormModel):  # just your password: downloading a change log (routers/changes.py)
    password: str = Field(min_length=1, max_length=128)
@as_form
class FriendRequestForm(FormModel):  # ask someone to be friends, by their username (routers/friends.py)
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
@as_form
class PersonalGroupForm(FormModel):  # a personal group chat of friends (no company): a name + who's in it
    title: str = Field(min_length=2, max_length=60)
    members: list[int] = Field(default=[], max_length=50)
    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        if len(v := " ".join(clean_text(v).split())) < 2: raise ValueError("give the group a name")
        return v
@as_form
class GroupChatForm(FormModel):  # a named group of people in the same company (the creator is always in it)
    ONCE: ClassVar[int] = TWICE_S
    company_id: uuid.UUID
    title: str = Field(min_length=2, max_length=60)
    members: list[int] = Field(default=[], max_length=50)
    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        if len(v := " ".join(clean_text(v).split())) < 2: raise ValueError("give the group a name")
        return v
@as_form
class ChatMembersForm(FormModel):  # add people to a group
    members: list[int] = Field(min_length=1, max_length=50)
TASK_BATCH = 5  # tasks given in one go (t0_… to t4_… on the form; empty blocks are skipped)
class _TaskBatch(FormModel):
    @model_validator(mode="after")
    def _tasks(self):
        if not (tasks := self.tasks()): raise ValueError("Give at least one task a title.")
        for i, t in tasks:
            if len(t["title"]) < 3: raise ValueError(f"Task {i}: give it a title (3+ characters).")
            if not t["deadline"]: raise ValueError(f"Task {i} ({t['title']}): set its deadline.")
            if not t["to"] and not t["ai"]: raise ValueError(f"Task {i} ({t['title']}): pick who it's for, or let the AI pick.")
        return self
    def tasks(self) -> list[tuple[int, dict]]:  # (its number on the form, {title, description, deadline (naive: company clock), to: user ids, ai: how many the AI should pick (0 = you did)}) for each filled-in block
        out = []
        for i in range(TASK_BATCH):
            title = " ".join(clean_text(getattr(self, f"t{i}_title")).split())
            if not title and not getattr(self, f"t{i}_to"): continue
            when = getattr(self, f"t{i}_deadline")
            out.append((i + 1, {"title": title, "description": clean_text(getattr(self, f"t{i}_description")), "deadline": when.replace(tzinfo=None) if when else None,
                                "to": sorted(set(getattr(self, f"t{i}_to"))), "ai": getattr(self, f"t{i}_ai")}))
        return out
TaskBatch = create_model("TaskBatch", __base__=_TaskBatch, **{k: v for i in range(TASK_BATCH) for k, v in ((f"t{i}_title", (str, Field(default="", max_length=120))),
                         (f"t{i}_description", (str, Field(default="", max_length=2000))), (f"t{i}_deadline", (datetime | None, None)), (f"t{i}_to", (list[int], Field(default=[], max_length=100))), (f"t{i}_ai", (int, Field(default=0, ge=0, le=5))))})
@as_form
class TaskForm(TaskBatch):  # the owner / a co-owner / a manager / HR giving tasks: routers/tasks.py checks who may get them
    ONCE: ClassVar[int] = TWICE_S
@as_form
class MeetingForm(FormModel):  # schedule a video meeting, or start one now (the "start now" button sends now=1): routers/meetings.py checks the slot + who's invited
    title: str = Field(min_length=3, max_length=120)
    agenda: str = Field(default="", max_length=2000)
    starts: datetime | None = None  # on the company's clock (datetime-local); not needed with now
    minutes: int = Field(default=30, ge=5, le=480)
    everyone: bool = False  # the whole company
    invite: list[int] = Field(default=[], max_length=300)
    now: bool = False
    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        if len(v := " ".join(clean_text(v).split())) < 3: raise ValueError("give the meeting a title (3+ characters)")
        return v
    @field_validator("agenda")
    @classmethod
    def _agenda(cls, v: str) -> str: return clean_text(v)
    @model_validator(mode="after")
    def _when(self):
        if not self.now and not self.starts: raise ValueError("Pick when it starts (or press start now).")
        if not self.everyone and not self.invite: raise ValueError("Invite at least one person (or tick everyone in the company).")
        return self
# ---------- the shop (routers/shop.py, routers/seller.py) ----------
Money = Annotated[Decimal, Field(ge=0, le=Decimal("1000000000"), max_digits=12, decimal_places=2)]  # an amount typed in major units (1299.50)
def _line(v: str, n: int) -> str: return " ".join(clean_text(v).split())[:n]
@as_form
class ShopOpenForm(FormModel):  # open a shop (owner / a co-owner): its currency + the platform's commission accepted
    currency: Literal[CURRENCIES]
    accept_commission: bool = False
@as_form
class ShopSettingsForm(FormModel):
    status: Literal["open", "paused"]
    tagline: str = Field(default="", max_length=160)
    shipping: Money = Decimal(0)  # flat, per order
    free_over: Money | None = None  # free shipping from this subtotal
    cod: bool = False
    returns_days: int = Field(default=7, ge=0, le=60)
    ai_tasks: bool = False
    staff: list[int] = Field(default=[], max_length=200)  # who packs + ships (gets the AI's shop tasks)
    @field_validator("tagline")
    @classmethod
    def _tagline(cls, v: str) -> str: return _line(v, 160)
@as_form
class ProductForm(FormModel):  # a product (shop managers): prices in any currency (the shop's when left out), tax included
    title: str = Field(min_length=3, max_length=140)
    description: str = Field(default="", max_length=5000)
    highlights: str = Field(default="", max_length=1500)
    category: Literal[tuple(SHOP_CATEGORIES)]
    brand: str = Field(default="", max_length=60)
    sku: str = Field(default="", max_length=40)
    price: Money
    currency: Literal[("", *CURRENCIES)] = ""  # what price + list price are in; "" = the shop's (orders charge in the shop's currency at the day's rate)
    mrp: Money | None = None
    tax_rate: Decimal = Field(default=Decimal(0), ge=0, le=50, max_digits=4, decimal_places=2)
    stock: int = Field(default=0, ge=0, le=1_000_000)
    stock_was: int | None = Field(default=None, ge=0, le=1_000_000)  # the edit page's: the stock it showed, so what sold while it stood open stays sold (routers/seller.py)
    max_per_order: int = Field(default=10, ge=1, le=100)
    status: Literal["draft", "active", "archived"] = "draft"
    @field_validator("title", "brand", "sku")
    @classmethod
    def _one(cls, v: str) -> str: return " ".join(clean_text(v).split())
    @field_validator("description", "highlights")
    @classmethod
    def _many(cls, v: str) -> str: return clean_text(v)
    @model_validator(mode="after")
    def _prices(self):
        if len(self.title) < 3: raise ValueError("Give the product a title (3+ characters).")
        if self.price <= 0: raise ValueError("Set the product's price.")
        if self.mrp is not None and self.mrp and self.mrp < self.price: raise ValueError("The list price (MRP) can't be lower than the price: leave it empty if there's no discount.")
        return self
@as_form
class NewProductForm(ProductForm):  # adding one (a save of an existing product can be repeated freely)
    ONCE: ClassVar[int] = TWICE_S
@as_form
class ProductMediaForm(FormModel):  # up to 4 photos + a video at a time (no type check here: media.py keeps only real images / videos)
    photo1: SavedFile = ""
    photo2: SavedFile = ""
    photo3: SavedFile = ""
    photo4: SavedFile = ""
    video: SavedFile = ""
    def photos(self) -> list[str]: return [p for p in (self.photo1, self.photo2, self.photo3, self.photo4) if p]
@as_form
class CartForm(FormModel):
    product_id: int
    qty: int = Field(default=1, ge=0, le=100)  # 0 = take it out
    token: str = Field(default="", max_length=32)  # one per rendered "add to cart" form: sent twice, it adds once
@as_form
class AddressForm(FormModel):  # a delivery address (stored encrypted)
    ONCE: ClassVar[int] = TWICE_S
    label: str = Field(default="", max_length=30)
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=6, max_length=20)
    line1: str = Field(min_length=3, max_length=200)
    line2: str = Field(default="", max_length=200)
    city: str = Field(min_length=2, max_length=80)
    state: str = Field(default="", max_length=80)
    postcode: str = Field(default="", max_length=12)
    country: Literal[tuple(k for k in BANK_COUNTRIES if k != "ZZ")]
    @field_validator("label", "name", "line1", "line2", "city", "state", "postcode")
    @classmethod
    def _clean(cls, v: str) -> str: return " ".join(clean_text(v).split())
    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        if not re.fullmatch(r"\+?[\d\s()-]{6,20}", v := v.strip()): raise ValueError("enter a phone number the courier can call")
        return re.sub(r"[\s()-]", "", v)
@as_form
class CheckoutForm(FormModel):
    address_id: int
    method: Literal["online", "cod"]
    token: str = Field(default="", max_length=64)  # the checkout page's one-time token: a double click can't order twice
@as_form
class ShipForm(FormModel):  # the order is on its way: courier + tracking number (or own delivery)
    courier: Literal[tuple(COURIER_KEYS)]
    awb: str = Field(default="", max_length=40)
    tracking_url: str = Field(default="", max_length=500)
    courier_name: str = Field(default="", max_length=60)
    courier_phone: str = Field(default="", max_length=20)
    @field_validator("awb")
    @classmethod
    def _awb(cls, v: str) -> str:
        if (v := v.strip().upper()) and not re.fullmatch(r"[A-Z0-9][A-Z0-9 -]{2,39}", v): raise ValueError("the tracking / AWB number: letters, digits, spaces or hyphens")
        return v
    @field_validator("tracking_url")
    @classmethod
    def _url(cls, v: str) -> str: return clean_link(v)
    @field_validator("courier_name", "courier_phone")
    @classmethod
    def _text(cls, v: str) -> str: return " ".join(clean_text(v).split())
    @model_validator(mode="after")
    def _who(self):
        if self.courier == "custom" and len(self.courier_name) < 2: raise ValueError("Say who delivers it (your own delivery person, or the courier's name).")
        if self.courier != "custom" and not self.awb: raise ValueError("Add the courier's tracking / AWB number.")
        return self
@as_form
class TrackForm(FormModel):  # a step of the journey, by the seller (what the courier's page says, or their own delivery)
    status: Literal["packed", "in_transit", "out_for_delivery", "attempted", "exception"]
    location: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=300)
    @field_validator("location", "note")
    @classmethod
    def _text(cls, v: str) -> str: return " ".join(clean_text(v).split())
@as_form
class DeliverForm(FormModel):  # marking it delivered: the buyer's 6-digit code (courier / seller), or a photo of the parcel at the door (seller)
    code: str = Field(default="", max_length=12)
    by_name: str = Field(default="", max_length=60)
    photo: SavedFile = ""
    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        if (v := re.sub(r"\s", "", v)) and not re.fullmatch(r"\d{6}", v): raise ValueError("the delivery code is 6 digits")
        return v
    @field_validator("by_name")
    @classmethod
    def _name(cls, v: str) -> str: return " ".join(clean_text(v).split())
@as_form
class ReasonForm(FormModel):  # cancel / return / decline: why
    reason: str = Field(default="", max_length=500)
    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str: return " ".join(clean_text(v).split())
@as_form
class RefundForm(FormModel):  # the seller accepts a return: the refund's reference (not when the payment service sends it back: the route checks) + whether the item goes back in stock
    refund_ref: str = Field(default="", max_length=64)
    restock: bool = False
    @field_validator("refund_ref")
    @classmethod
    def _ref(cls, v: str) -> str: return " ".join(clean_text(v).split())
@as_form
class ProductReviewForm(FormModel):  # a buyer's review: 1-5 stars, words, up to 4 photos + a video
    rating: int = Field(ge=1, le=5)
    title: str = Field(default="", max_length=120)
    body: str = Field(min_length=3, max_length=3000)
    photo1: SavedFile = ""
    photo2: SavedFile = ""
    photo3: SavedFile = ""
    photo4: SavedFile = ""
    video: SavedFile = ""
    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str: return " ".join(clean_text(v).split())
    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        if len(v := clean_text(v)) < 3: raise ValueError("say a few words about it")
        return v
    def photos(self) -> list[str]: return [p for p in (self.photo1, self.photo2, self.photo3, self.photo4) if p]
@as_form
class AskForm(FormModel):  # a question about a product: the AI answers (or the seller)
    question: str = Field(min_length=4, max_length=500)
    @field_validator("question")
    @classmethod
    def _q(cls, v: str) -> str:
        if len(v := " ".join(clean_text(v).split())) < 4: raise ValueError("ask a question (a few words)")
        return v
@as_form
class AnswerForm(FormModel):  # the seller's answer to a question / reply to a review
    text: str = Field(min_length=2, max_length=2000)
    @field_validator("text")
    @classmethod
    def _t(cls, v: str) -> str:
        if len(v := clean_text(v)) < 2: raise ValueError("write the answer")
        return v
@as_form
class CompetitorForm(FormModel):  # a competitor's listing of a similar product: its price is tracked daily
    url: str = Field(min_length=8, max_length=500)
    label: str = Field(default="", max_length=80)
    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if not (v := clean_link(v)): raise ValueError("paste the competitor's product page link")
        return v
    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str: return " ".join(clean_text(v).split())
@as_form
class SettlementForm(FormModel):  # platform admins: money moved between the platform and a seller
    company_id: uuid.UUID
    direction: Literal["to_seller", "to_platform"]
    amount: Money
    currency: Literal[CURRENCIES]
    reference: str = Field(min_length=4, max_length=64)
    note: str = Field(default="", max_length=300)
    @field_validator("reference")
    @classmethod
    def _reference(cls, v: str) -> str:
        if len(v := " ".join(clean_text(v).split()).upper()) < 4: raise ValueError("enter the bank's reference")
        return v
    @model_validator(mode="after")
    def _amount(self):
        if self.amount <= 0: raise ValueError("Enter the amount that moved.")
        return self
def clean_link(v: str) -> str:  # "" or a full http(s) link with its path (tracking pages, competitors' listings): no user:password@, a real host
    if not (v := v.strip()): return ""
    url = urlsplit(v if re.match(r"(?i)https?://", v) else "https://" + v)
    if url.scheme.lower() not in ("http", "https") or url.username or url.password or not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,24}", (url.hostname or "")): raise ValueError("paste a link like https://example.com/…")
    return urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path, url.query, ""))[:500]
@as_form
class RegionForm(FormModel):  # settings: your region (currency + national holidays) + time zone: every time and amount is shown in them
    zone: Literal[tuple(ZONES)]
@as_form
class ReviewForm(FormModel):  # a like or dislike for a colleague, always with the reason
    kind: Literal["like", "dislike"]
    reason: str = Field(min_length=3, max_length=500)
    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        if len(v := clean_text(v)) < 3: raise ValueError("say why (a few words at least)")
        return v
@as_form
class TermsForm(FormModel):  # a member accepting their company's updated terms (the version they read)
    terms_version: int = Field(ge=1)
    accept_terms: bool = False  # the checkbox: required by the route
def _account(v: str) -> str:  # an account number / IBAN typed back to record a payment: spaces / hyphens dropped, compared with the saved one by the route
    if not re.fullmatch(r"[A-Z0-9]{4,34}", v := banks.squash(v)): raise ValueError("enter the account number (or IBAN) the money went to")
    return v
def _company_holder(v: str) -> str:  # a company account's name: letters, digits and the punctuation company names use
    if not 2 <= len(v := " ".join(v.split())) <= 100 or not written_ok(v, "_ .,&'()+/-"): raise ValueError("enter the account's name exactly as the bank has it, e.g. Acme Technologies Pvt Ltd")
    return v
class _Account(FormModel):  # a bank account's details, in any country (banks.py checks the local code / IBAN / SWIFT)
    country: str = Field(default="IN", max_length=2)  # the bank's country (banks.COUNTRIES)
    holder_name: str = Field(min_length=2, max_length=100)
    bank_name: str = Field(min_length=2, max_length=100)
    branch: str = Field(default="", max_length=100)
    routing: str = Field(default="", max_length=20)  # IFSC / ABA routing number / BSB / transit + institution / bank + branch code; none where the IBAN is the number
    ifsc: str = Field(default="", max_length=20)  # the same, under its India-only name (older pages and scripts)
    swift: str = Field(default="", max_length=11)
    bank_address: str = Field(default="", max_length=200)
    account_type: Literal[ACCOUNT_TYPES]
    account_number: str = Field(max_length=40)  # or the IBAN
    account_confirm: str = Field(max_length=40)
    @field_validator("bank_name", "branch")
    @classmethod
    def _bank(cls, v: str) -> str:
        if (v := " ".join(v.split())) and not (2 <= len(v) <= 100 and written_ok(v, "_ .,&'()/-")): raise ValueError("use letters, digits, spaces or . , & ' ( ) / -")
        return v
    @model_validator(mode="after")
    def _details(self):
        if (country := self.country.upper()) not in BANK_COUNTRIES: raise ValueError("Pick the bank's country.")
        if banks.squash(self.account_number) != banks.squash(self.account_confirm): raise ValueError("The two account numbers don't match: type it again.")
        try: self.routing, self.account_number, self.swift = banks.clean(country, self.routing or self.ifsc, self.account_number, self.swift)
        except ValueError as e: raise ValueError(str(e)[:1].upper() + str(e)[1:] + ".") from None
        self.country, self.ifsc, self.account_confirm, self.bank_address = country, self.routing, self.account_number, " ".join(clean_text(self.bank_address).split())
        return self
class _Bank(_Account):  # a bank account: a person's (settings) or a company's (its dashboard); both only with the current password
    password: str = Field(min_length=1, max_length=128)
class CompanyAccount(_Account):  # the company's own account typed in while creating it (CompanyForm): no password, they're creating it
    @field_validator("holder_name")
    @classmethod
    def _holder(cls, v: str) -> str: return _company_holder(v)
@as_form
class BankForm(_Bank):  # settings: where your salary goes (needed to join or create a company)
    @field_validator("holder_name")
    @classmethod
    def _holder(cls, v: str) -> str:
        if not person_ok(v := " ".join(v.split())): raise ValueError("enter the name exactly as the bank has it (letters, spaces, . ' -)")
        return v
@as_form
class CompanyBankForm(_Bank):  # the company's own account, salaries are paid from it (owner / co-owners)
    @field_validator("holder_name")
    @classmethod
    def _holder(cls, v: str) -> str: return _company_holder(v)
@as_form
class NetWorthForm(FormModel):  # the company's funds available (owner / co-owners): what the clearance is worked out from
    net_worth: int = Field(ge=0, le=MAX_NET_WORTH)
    net_worth_currency: Literal[CURRENCIES]  # any supported currency: the guard converts the salaries' one into it (fx.py)
    password: str = Field(min_length=1, max_length=128)
_D = ((0,1,2,3,4,5,6,7,8,9), (1,2,3,4,0,6,7,8,9,5), (2,3,4,0,1,7,8,9,5,6), (3,4,0,1,2,8,9,5,6,7), (4,0,1,2,3,9,5,6,7,8), (5,9,8,7,6,0,4,3,2,1), (6,5,9,8,7,1,0,4,3,2),
      (7,6,5,9,8,2,1,0,4,3), (8,7,6,5,9,3,2,1,0,4), (9,8,7,6,5,4,3,2,1,0))
_P = ((0,1,2,3,4,5,6,7,8,9), (1,5,7,6,2,8,3,0,9,4), (5,8,0,3,7,9,6,1,4,2), (8,9,1,6,0,4,3,5,2,7), (9,4,5,3,1,2,6,8,7,0), (4,2,8,6,5,7,3,9,0,1), (2,7,9,3,8,0,6,4,1,5), (7,0,4,6,9,1,3,2,5,8))
def verhoeff_ok(digits: str) -> bool:  # Aadhaar's check digit (the Verhoeff algorithm)
    c = 0
    for i, d in enumerate(reversed(digits)): c = _D[c][_P[i % 8][int(d)]]
    return c == 0
def luhn_ok(digits: str) -> bool:  # Canada's SIN, the UAE's Emirates ID
    total = 0
    for i, ch in enumerate(reversed(digits)): n = int(ch) * (2 if i % 2 else 1); total += n - 9 if n > 9 else n
    return total % 10 == 0
def tfn_ok(v: str) -> bool:  # Australia's tax file number: weighted sum divisible by 11
    weights = (1, 4, 3, 7, 5, 8, 6, 9, 10) if len(v) == 9 else (10, 7, 8, 4, 6, 3, 5, 1)
    return v.isdigit() and len(v) in (8, 9) and sum(int(d) * w for d, w in zip(v, weights)) % 11 == 0
def nric_ok(v: str) -> bool:  # Singapore NRIC / FIN check letter (the newer M series uses another table: format only)
    if not re.fullmatch(r"[STFGM]\d{7}[A-Z]", v): return False
    if v[0] == "M": return True
    total = sum(int(d) * w for d, w in zip(v[1:8], (2, 7, 6, 5, 4, 3, 2))) + (4 if v[0] in "TG" else 0)
    return ("JZIHGFEDCBA" if v[0] in "ST" else "XWUTRQPNMLK")[total % 11] == v[8]
_re = lambda pattern: re.compile(pattern).fullmatch
ID_FORMATS = {  # type -> (check, what to tell people); numbers are compared upper-case, without spaces or hyphens. Check digits where the ID has one
    "aadhaar": (lambda v: bool(re.fullmatch(r"[2-9]\d{11}", v)) and verhoeff_ok(v), "the 12-digit Aadhaar number (its last digit is a check digit)"),
    "pan": (_re(r"[A-Z]{5}\d{4}[A-Z]"), "the 10-character PAN, like ABCDE1234F"),
    "passport": (_re(r"[A-Z]\d{7}"), "the passport number, like K1234567"),
    "driving_licence": (_re(r"[A-Z]{2}\d{2}[0-9A-Z]{9,12}"), "the licence number, like MH1220110012345"),
    "voter_id": (_re(r"[A-Z]{3}\d{7}"), "the 10-character EPIC number, like ABC1234567"),
    "us_ssn": (_re(r"(?!000|666|9\d\d)\d{3}(?!00)\d{2}(?!0000)\d{4}"), "the 9-digit SSN, like 123-45-6789"),
    "us_passport": (_re(r"[A-Z]?\d{8,9}"), "the passport number (9 characters)"),
    "us_licence": (_re(r"[A-Z0-9]{4,20}"), "the license / ID number as printed"),
    "eu_national_id": (_re(r"[A-Z0-9]{5,20}"), "the card number as printed"),
    "eu_passport": (_re(r"[A-Z0-9]{6,12}"), "the passport number as printed"),
    "uk_passport": (_re(r"\d{9}"), "the 9-digit passport number"),
    "uk_licence": (_re(r"[A-Z9]{5}\d{6}[A-Z9]{2}\d[A-Z]{2}"), "the 16-character licence number, like MORGA753116SM9IJ"),
    "uk_nino": (_re(r"(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\d{6}[A-D]"), "the National Insurance number, like AB123456C"),
    "au_passport": (_re(r"[A-Z]{1,2}\d{7}"), "the passport number, like PA1234567"),
    "au_licence": (_re(r"[A-Z0-9]{4,11}"), "the licence number as printed"),
    "au_tfn": (tfn_ok, "the 8 or 9-digit tax file number (it has a check digit)"),
    "ca_passport": (_re(r"[A-Z]{2}\d{6}"), "the passport number, like AB123456"),
    "ca_licence": (_re(r"[A-Z0-9]{5,20}"), "the licence number as printed"),
    "ca_sin": (lambda v: bool(re.fullmatch(r"[1-79]\d{8}", v)) and luhn_ok(v), "the 9-digit SIN (it has a check digit)"),
    "sg_nric": (nric_ok, "the NRIC / FIN, like S1234567D (its last letter is a check letter)"),
    "sg_passport": (_re(r"[A-Z]\d{7}[A-Z]"), "the passport number, like E1234567N"),
    "ae_emirates_id": (lambda v: bool(re.fullmatch(r"784\d{12}", v)) and luhn_ok(v), "the 15-digit Emirates ID, like 784-1990-1234567-1 (its last digit is a check digit)"),
    "ae_passport": (_re(r"[A-Z0-9]{7,9}"), "the passport number as printed")}
assert set(ID_FORMATS) == set(ID_TYPES)  # every ID type has a check
@as_form
class IdentityForm(FormModel):  # settings: your identity proof (needed to join or create a company), only with the current password
    id_type: Literal[tuple(ID_TYPES)]
    id_number: str = Field(min_length=4, max_length=30)
    name_on_id: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=1, max_length=128)
    photo: SavedFile  # a clear photo of the document: scanned + saved only once everything else is valid, then re-encoded (media.store_id_photo)
    @field_validator("name_on_id")
    @classmethod
    def _name(cls, v: str) -> str:
        if not person_ok(v := " ".join(v.split())): raise ValueError("enter the name exactly as it's printed on the ID")
        return v
    @model_validator(mode="after")
    def _number(self):
        self.id_number = re.sub(r"[\s-]", "", self.id_number).upper()
        check, example = ID_FORMATS[self.id_type]
        if not check(self.id_number): raise ValueError(f"That {ID_TYPES[self.id_type]} number doesn't look right: enter {example}.")
        return self
@as_form
class CompanyProofForm(FormModel):  # the company dashboard: add / replace its identity proof (owner / co-owners)
    id_proof_type: Literal[tuple(COMPANY_PROOFS)]
    file: SavedFile
def _txref(v: str) -> str:  # a bank transaction reference (payouts + deposits)
    if not re.fullmatch(r"[A-Za-z0-9/-]{6,40}", v := v.strip().upper()): raise ValueError("enter the bank's transaction reference (UTR etc.): 6-40 letters, digits, / or -")
    return v
def _money_date(when: datetime) -> datetime:  # a transfer's date, read as company time. No bank was sending these in the year 1: such a date is a typo (and year 0001 couldn't even be moved to UTC: a crash)
    if when.year < 2000: raise ValueError("Enter the real date of the transfer.")
    return when.replace(tzinfo=None)
@as_form
class PayoutForm(FormModel):  # recording a salary payment: every amount typed in by hand, the account it went to, when, the bank's reference + a screenshot
    cycle: int = Field(ge=1, le=100_000)
    currency: Literal[("", *CURRENCIES)] = ""  # what was sent: any supported currency; "" = the salary currency (the route converts for the minimum + clearance)
    gross: int = Field(ge=1, le=MAX_SALARY)  # salary before tax
    tds: int = Field(ge=0, le=MAX_SALARY)  # tax deducted at source (0 if none)
    account_number: str = Field(max_length=40)  # must match the employee's saved account (checked by the route)
    transferred_at: datetime  # <input type="datetime-local">: on the company's clock
    reference: str = Field(min_length=6, max_length=40)  # UTR / transaction id
    proof: SavedFile  # the transfer screenshot: scanned + saved by as_form only once everything else is valid, re-encoded by media.store_proof
    @field_validator("account_number")
    @classmethod
    def _number(cls, v: str) -> str: return _account(v)
    @field_validator("reference")
    @classmethod
    def _reference(cls, v: str) -> str: return _txref(v)
    @model_validator(mode="after")
    def _tax(self):
        if self.tds > self.gross: raise ValueError("TDS can't be more than the gross salary.")
        self.transferred_at = _money_date(self.transferred_at)
        return self
@as_form
class DepositForm(FormModel):  # money paid into the company (owner / co-owners, with their password): typed in by hand + a screenshot, added to the net worth
    amount: int = Field(ge=1, le=MAX_NET_WORTH)
    currency: Literal[CURRENCIES]  # what arrived; converted into the net worth's currency as it's added (fx.py)
    source: str = Field(min_length=2, max_length=100)  # where it came from: an investor, the owner's own account, a client…
    account_number: str = Field(max_length=40)  # the company account it was credited to: must match the saved one (checked by the route)
    transferred_at: datetime  # <input type="datetime-local">: on the company's clock
    reference: str = Field(min_length=6, max_length=40)
    note: str = Field(default="", max_length=300)
    password: str = Field(min_length=1, max_length=128)
    proof: SavedFile  # the bank's screenshot of the money arriving: scanned + saved only once everything else is valid, re-encoded by media.store_proof
    @field_validator("source", "note")
    @classmethod
    def _text(cls, v: str) -> str: return " ".join(clean_text(v).split())
    @field_validator("account_number")
    @classmethod
    def _number(cls, v: str) -> str: return _account(v)
    @field_validator("reference")
    @classmethod
    def _reference(cls, v: str) -> str: return _txref(v)
    @model_validator(mode="after")
    def _when(self):
        if len(self.source) < 2: raise ValueError("Say where the money came from (an investor, the owner's own account, a client…).")
        self.transferred_at = _money_date(self.transferred_at)
        return self
@as_form
class LeaveForm(FormModel):  # asking for leave (company days, inclusive); unpaid once approved
    ONCE: ClassVar[int] = TWICE_S
    start_day: date
    end_day: date
    reason: str = Field(default="", max_length=500)
    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str: return clean_text(v)
    @model_validator(mode="after")
    def _days(self):
        if self.end_day < self.start_day: raise ValueError("The leave must end on or after the day it starts.")
        if (self.end_day - self.start_day).days >= 60: raise ValueError("Ask for at most 60 days at a time.")
        return self
@as_form
class DecisionForm(FormModel):  # approve / decline: a leave request, or (co-owners) deleting the company
    decision: Literal["approve", "decline"]
@as_form
class PositionForm(FormModel):
    position: Literal[ASSIGNABLE]  # never "owner"; the route also checks who may give what (top level only, below themselves)
@as_form
class AssistForm(FormModel):  # the one-line instruction bar
    q: str = Field(min_length=1, max_length=200)
@as_form
class AIForm(FormModel):  # a message to the Deminic AI (its private chat: deminic.py); web = the chat's web toggle is on; image = a picture for it to look at
    text: str = Field(default="", max_length=2000)
    web: bool = False
    image: SavedFile = ""  # virus-scanned + saved by as_form; deminic.ask re-encodes it (media.py) or refuses it
@as_form
class FindForm(FormModel):  # the AI company finder (the sidebar's link): one prompt in plain words, never stored
    prompt: str = Field(min_length=3, max_length=500)
    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str) -> str:
        if len(v := " ".join(clean_text(v).split())) < 3: raise ValueError("say what you're looking for, e.g. \"a python company that pays 50k INR a month\"")
        return v
@as_form
class ConfirmForm(FormModel):
    confirm: str = Field(default="", max_length=64)
@as_form
class ActionForm(FormModel): pass  # no fields: a CSRF-checked button (leave company etc.)
@as_form
class LoginForm(FormModel):
    username: str
    password: str
@as_form
class LogoutForm(FormModel): pass  # no fields: just the CSRF check
@as_form
class ApproachForm(FormModel):  # people search: the job offer sent with an approach (in the company's salary currency + period)
    message: str = Field(min_length=10, max_length=1000)
    salary_amount: int = Field(ge=1, le=MAX_SALARY)  # whole currency units; the route checks it's at least the company's Rookie minimum
    @field_validator("message")
    @classmethod
    def _message(cls, v: str) -> str:
        if len(v := clean_text(v)) < 10: raise ValueError("write at least a short message (10+ characters)")
        return v
@as_form
class UsernameForm(FormModel):  # settings: a new username, only with the current password
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=1, max_length=128)
@as_form
class EmailChangeForm(FormModel):  # settings: a new email (with the current password); it counts once the code sent to it comes back
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=1, max_length=128)
@as_form
class PhoneChangeForm(FormModel):  # settings: a new phone number (with the current password); it counts once the code texted to it comes back
    phone: str = Field(max_length=32)
    password: str = Field(min_length=1, max_length=128)
    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        try: number = phonenumbers.parse(v, DEFAULT_PHONE_REGION)
        except phonenumbers.NumberParseException: raise ValueError("enter the number with its country code, e.g. +1 415 555 0123") from None
        if not phonenumbers.is_valid_number(number): raise ValueError("that isn't a valid phone number")
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
@as_form
class ContactCodeForm(FormModel):  # settings: the code sent to the new email / phone
    channel: Literal["email", "phone"]
    code: str = Field(default="", max_length=12)
@as_form
class DeleteAccountForm(FormModel):  # settings: deleting the account, only with the current password
    password: str = Field(min_length=1, max_length=128)
@as_form
class PasswordChangeForm(FormModel):  # settings: a new password, only with the current one (routers/settings.py checks the new one)
    password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(max_length=128)
    new_password_confirm: str = Field(max_length=128)
@as_form
class TransferForm(FormModel):  # the owner offering the company to a co-owner, with their password
    user_id: int
    password: str = Field(min_length=1, max_length=128)
@as_form
class TransferAnswerForm(FormModel):  # that co-owner's answer: accepting needs their password (the route checks), declining doesn't
    decision: Literal["accept", "decline"]
    password: str = Field(default="", max_length=128)
@as_form
class PictureForm(FormModel):
    file: SavedFile  # no type check here: scanned + saved by as_form, then media.py keeps it only if Pillow can decode it as a real image
@as_form
class ResumeForm(FormModel):
    file: SavedFile  # no type check here either: scanned + saved by as_form, then resume.py only parses it if its bytes are a PDF
