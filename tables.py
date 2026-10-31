import re, uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from ipaddress import IPv4Address, IPv6Address
from sqlalchemy import ARRAY, BigInteger, CheckConstraint, ForeignKey, Index, LargeBinary, Numeric, SmallInteger, String, UniqueConstraint, bindparam, cast, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import INET, JSONB, aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, aliased, mapped_column, relationship
from sqlalchemy.pool import NullPool
from system import ADMIN_DATABASE_URL, APP_NAME, CYCLE_DAYS, CYCLE_EVERY, DATABASE_URL, DEFAULT_PHONE_REGION, PLATFORM_ADMINS, POSTGRES_SSL, chat_file, identity_digest, remove_chat_files, templates
# ---------- async engine (runs as the least-privilege app role) ----------
_CONNECT = {"command_timeout": 15, "server_settings": {  # Postgres kills anything stuck, so one bad query can't hang the app or hold locks
    "application_name": re.sub(r"[^\w-]", "", APP_NAME)[:48] or "app", "statement_timeout": "15000", "lock_timeout": "5000",
    "idle_in_transaction_session_timeout": "60000"}} | ({"ssl": POSTGRES_SSL} if POSTGRES_SSL else {})
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=10, pool_recycle=1800, connect_args=_CONNECT)
Session = async_sessionmaker(engine, expire_on_commit=False)
async def get_db():
    """FastAPI dependency: `db: AsyncSession = Depends(get_db)`."""
    async with Session() as db: yield db
def utcnow() -> datetime: return datetime.now(UTC).replace(tzinfo=None)  # naive UTC, matching the TIMESTAMP columns
class Base(DeclarativeBase): pass
# ---------- company positions: the master "multi dict" (key -> details). Lower rank = more authority ----------
# top=True: the top level (owner + co-owners) is the only one that can change positions. "owner" goes only to the company's creator
# (exactly one per company); co-owners (any number) are appointed by the owner. Everyone joins as JOIN_AS and is promoted from there.
POSITIONS = {
    "owner":    {"label": "Owner",             "icon": "fa-crown",        "rank": 0, "top": True,  "can_invite": True},
    "co_owner": {"label": "Co-owner",          "icon": "fa-user-shield",  "rank": 1, "top": True,  "can_invite": True},
    "manager":  {"label": "Manager",           "icon": "fa-user-tie",     "rank": 2, "top": False, "can_invite": True},
    "hr":       {"label": "HR",                "icon": "fa-people-group", "rank": 3, "top": False, "can_invite": True},
    "finance":  {"label": "Finance",           "icon": "fa-coins",        "rank": 4, "top": False, "can_invite": False},
    "software": {"label": "Software engineer", "icon": "fa-code",         "rank": 4, "top": False, "can_invite": False},
    "employee": {"label": "Employee",          "icon": "fa-id-badge",     "rank": 5, "top": False, "can_invite": False},
    "rookie":   {"label": "Rookie",            "icon": "fa-seedling",     "rank": 6, "top": False, "can_invite": False},
    "freelancer": {"label": "Freelancer",      "icon": "fa-laptop-house", "rank": 6, "top": False, "can_invite": False},  # never monitored (no attendance)
}
STAFF = tuple(k for k, p in POSITIONS.items() if not p["top"])  # what a company can offer (chosen at creation)
TOP = tuple(k for k, p in POSITIONS.items() if p["top"])  # owner + co-owner
ASSIGNABLE = tuple(k for k in POSITIONS if k != "owner")  # what a position change can set
JOIN_AS = "rookie"  # forced by the backend on every join; every company must offer it
FREELANCE = "freelancer"  # the other way in: join as a freelancer (only if the company offers it); freelancers aren't monitored
JOIN_OPTIONS = (JOIN_AS, FREELANCE)
PAYERS = ("owner", "co_owner", "manager", "hr")  # record salary payouts + see the company's transaction statement
SELF_PAY = ("owner", "co_owner", "manager")  # may also record their own salary (HR can't pay themselves)
def monitored(position: str) -> bool: return position not in TOP and position != FREELANCE  # attendance is tracked for staff only
MAX_WORK_HOURS = 8  # working hours are capped at 8 a day; salaries are paid every CYCLE_DAYS days from the day someone joins (.env, system.py)
HOURS_EVERY_DAYS = 30  # working hours change at most once in this many days (user's rule); the ones set when a company is created don't count
MIN_EMPLOYEE_LIMIT, MAX_EMPLOYEE_LIMIT = 2, 2_147_483_647  # a company's employee limit (user's rule): at least 2, then as high as its owner or a co-owner wants (the top is only what the column holds)
MAX_NET_WORTH = 10**15
REGIONS = {"IN": ("India", "INR"), "US": ("United States", "USD"), "GB": ("United Kingdom", "GBP"), "AU": ("Australia", "AUD"), "CA": ("Canada", "CAD"),  # a person's region:
           "SG": ("Singapore", "SGD"), "AE": ("United Arab Emirates", "AED"), "DE": ("Germany", "EUR"), "FR": ("France", "EUR"), "NL": ("Netherlands", "EUR"),  # their currency +
           "IE": ("Ireland", "EUR"), "ES": ("Spain", "EUR"), "IT": ("Italy", "EUR")}  # national holidays (regions.py): shown on their calendar, no task can be due on them
REGION_ZONES = {  # each region's time zones (IANA, label): times are shown in the one a person picks with their region
    "IN": [("Asia/Kolkata", "India")], "GB": [("Europe/London", "UK")], "SG": [("Asia/Singapore", "Singapore")], "AE": [("Asia/Dubai", "UAE")],
    "US": [("America/New_York", "Eastern"), ("America/Chicago", "Central"), ("America/Denver", "Mountain"), ("America/Phoenix", "Arizona"), ("America/Los_Angeles", "Pacific"),
           ("America/Anchorage", "Alaska"), ("Pacific/Honolulu", "Hawaii")],
    "AU": [("Australia/Sydney", "Sydney · Canberra"), ("Australia/Melbourne", "Melbourne"), ("Australia/Brisbane", "Brisbane"), ("Australia/Adelaide", "Adelaide"),
           ("Australia/Darwin", "Darwin"), ("Australia/Perth", "Perth"), ("Australia/Hobart", "Hobart")],
    "CA": [("America/St_Johns", "Newfoundland"), ("America/Halifax", "Atlantic"), ("America/Toronto", "Eastern"), ("America/Winnipeg", "Central"), ("America/Regina", "Saskatchewan"),
           ("America/Edmonton", "Mountain"), ("America/Vancouver", "Pacific")],
    "DE": [("Europe/Berlin", "Germany")], "FR": [("Europe/Paris", "France")], "NL": [("Europe/Amsterdam", "Netherlands")], "IE": [("Europe/Dublin", "Ireland")],
    "ES": [("Europe/Madrid", "Spain"), ("Atlantic/Canary", "Canary Islands")], "IT": [("Europe/Rome", "Italy")]}
ZONES = {f"{r}:{tz}": (r, tz) for r, zones in REGION_ZONES.items() for tz, _ in zones}  # the picker's values: "US:America/New_York"
STANDARD_TERMS = [  # what joining any company here means: shown with the company's own terms, accepted together before joining (what the app enforces)
    ("fa-wallet", "Pay", f"Your salary is counted from the day you join and paid {CYCLE_EVERY}: at least your position's minimum, or what a job offer you accepted says. "
                         "Every payment is recorded with the bank's reference and a screenshot, and shows on your statement."),
    ("fa-user-clock", "Attendance", f"As staff, whether you're active in {APP_NAME} during the company's working hours is recorded: nothing you type or view. "
                                     "Freelancers, owners and co-owners aren't monitored."),
    ("fa-umbrella-beach", "Leave and leaving", "Leave is unpaid and needs approval from someone above you. Leaving the company needs approval too, "
                                                "and only happens once every salary due to you has been paid. The owner or a co-owner can also remove you, on the same condition, and you're told."),
    ("fa-eye", "What the company sees", "Its owner, co-owners, managers and HR see your profile, skills, email, phone number, identity proof and bank details "
                                         "(the full account number only where your salary is recorded). The company's change log, which they can download, records every "
                                         "change to your profile and documents while you're in it, with full ID and bank numbers."),
    ("fa-robot", "The payroll guard", "If the company can't cover what its people have earned, it's warned, and halted after 24 hours: you're alerted by notification and email."),
    ("fa-ranking-star", "The company AI", f"If the company uses it, an AI running on {APP_NAME}'s own server reads your reviews, task record, attendance, profile and CV to suggest or make promotions and to pick people for tasks. "
                                          "It can make mistakes; the company's owner and co-owners are responsible for what they let it do."),
    ("fa-business-time", "Focus", "During the company's working hours, staff can't browse or search other companies. If it keeps its staff to itself, you can't join "
                                   "another company while you work there."),
    ("fa-comments", "Chat", "You can chat with anyone in the company, one to one or in groups, the owner included. The company's owner, co-owners, managers and HR "
                            "can read every chat in it. Messages are encrypted at rest; if an account is deleted, its messages stay readable for 30 days, then are erased."),
    ("fa-video", "Meetings", "Its owner, co-owners, managers and HR can invite you to video meetings in working hours. Only the people invited can join. A meeting can be "
                             "recorded (everyone in it sees that), what's said is captioned by Whisper and translated to English, and the local AI takes notes: all on this "
                             "server, nothing goes to an outside service. Afterwards the people invited and the company's owner, co-owners, managers and HR can open the "
                             "recording, captions, chat and notes."),
    ("fa-file-signature", "Changes", "The company can change its terms: you're told, and asked to accept the new version on its dashboard.")]
ID_COUNTRIES = {  # a person's identity proof, for the country of every salary currency: country -> {type: label}
    "India": {"aadhaar": "Aadhaar", "pan": "PAN card", "passport": "Passport", "driving_licence": "Driving licence", "voter_id": "Voter ID (EPIC)"},
    "United States": {"us_ssn": "Social Security number", "us_passport": "US passport", "us_licence": "Driver's license / state ID"},
    "European Union": {"eu_national_id": "National identity card", "eu_passport": "EU passport"},
    "United Kingdom": {"uk_passport": "UK passport", "uk_licence": "UK driving licence", "uk_nino": "National Insurance number"},
    "Australia": {"au_passport": "Australian passport", "au_licence": "Australian driver licence", "au_tfn": "Tax file number"},
    "Canada": {"ca_passport": "Canadian passport", "ca_licence": "Canadian driver's licence", "ca_sin": "Social Insurance Number"},
    "Singapore": {"sg_nric": "NRIC / FIN", "sg_passport": "Singapore passport"},
    "United Arab Emirates": {"ae_emirates_id": "Emirates ID", "ae_passport": "UAE passport"}}
ID_TYPES = {k: f"{v} ({country})" for country, types in ID_COUNTRIES.items() for k, v in types.items()}
COMPANY_PROOFS = {"incorporation": "Certificate of incorporation", "gst": "GST registration certificate", "udyam": "Udyam (MSME) certificate",
                  "shop": "Shop & establishment licence", "other": "Other registration document"}  # a company's identity proof
ACCOUNT_TYPES = ("savings", "current", "salary")
CURRENCIES, SALARY_PERIODS = ("INR", "USD", "EUR", "GBP", "AUD", "CAD", "SGD", "AED"), ("month", "year")  # company salaries + approach offers
DEFAULT_CURRENCY = {"IN": "INR", "US": "USD", "GB": "GBP", "AU": "AUD", "CA": "CAD", "SG": "SGD", "AE": "AED"}.get((DEFAULT_PHONE_REGION or "").upper(), "USD")
MAX_SALARY = 10_000_000_000
REGISTRATION_TYPES = {"GSTIN": "GSTIN (India, GST)", "CIN": "CIN (India, company)", "EIN": "EIN (US)", "VAT": "VAT number (EU / UK)", "CRN": "Company number (UK)",
                      "OTHER": "Other registration number"}  # the company's official registration (form.py checks each format)
_SQL_LIST = lambda keys: ", ".join(f"'{k}'" for k in keys)  # constants above, never user input
CONSTRAINTS = {  # (table, name) -> CHECK; re-applied at startup (see MIGRATIONS) so the database always matches this code
    ("companies", "companies_positions_valid"): f"cardinality(positions) > 0 AND positions <@ ARRAY[{_SQL_LIST(STAFF)}]::varchar[] AND '{JOIN_AS}' = ANY(positions)",
    ("companies", "companies_name_valid"): "char_length(btrim(name)) >= 2",
    ("company_members", "company_members_position_valid"): f"position IN ({_SQL_LIST(POSITIONS)})",
    ("companies", "companies_salary_unit_valid"): f"(salary_currency IS NULL OR salary_currency IN ({_SQL_LIST(CURRENCIES)})) AND (salary_period IS NULL OR salary_period IN ({_SQL_LIST(SALARY_PERIODS)}))",
    ("companies", "companies_registration_valid"): f"(registration_type IS NULL) = (registration_number IS NULL) AND (registration_type IS NULL OR registration_type IN ({_SQL_LIST(REGISTRATION_TYPES)}))",
    ("companies", "companies_location_valid"): "(location_place_id IS NULL) = (location_lat IS NULL) AND (location_lat IS NULL OR (location_lat BETWEEN -90 AND 90 AND location_lng BETWEEN -180 AND 180))",
    ("company_salaries", "company_salaries_valid"): f"position IN ({_SQL_LIST(STAFF)}) AND min_amount BETWEEN 1 AND {MAX_SALARY}",
    ("profiles", "profiles_wanted_salary_valid"): f"(want_min IS NULL OR (want_min BETWEEN 1 AND {MAX_SALARY} AND want_currency IN ({_SQL_LIST(CURRENCIES)}) AND want_period IN ({_SQL_LIST(SALARY_PERIODS)}))) "
                                                  "AND (want_max IS NULL OR (want_min IS NOT NULL AND want_max >= want_min))",
    ("approaches", "approaches_salary_valid"): f"salary_amount IS NULL OR salary_amount BETWEEN 1 AND {MAX_SALARY}",
    ("approaches", "approaches_currency_valid"): f"salary_currency IS NULL OR salary_currency IN ({_SQL_LIST(CURRENCIES)})",
    ("approaches", "approaches_period_valid"): f"salary_period IS NULL OR salary_period IN ({_SQL_LIST(SALARY_PERIODS)})",
    ("companies", "companies_work_hours_valid"): f"work_end > work_start AND work_end - work_start BETWEEN interval '1 hour' AND interval '{MAX_WORK_HOURS} hours' AND work_days BETWEEN 1 AND 127",
    ("companies", "companies_max_members_valid"): f"max_members IS NULL OR max_members >= {MIN_EMPLOYEE_LIMIT}",
    ("company_members", "company_members_salary_valid"): f"salary IS NULL OR salary BETWEEN 1 AND {MAX_SALARY}",
    ("bank_accounts", "bank_accounts_valid"): f"(country <> 'IN' OR ifsc ~ '^[A-Z]{{4}}0[A-Z0-9]{{6}}$') AND country ~ '^[A-Z]{{2}}$' AND (swift = '' OR swift ~ '^[A-Z]{{6}}[A-Z0-9]{{2}}([A-Z0-9]{{3}})?$') "
                                              f"AND account_type IN ({_SQL_LIST(ACCOUNT_TYPES)}) AND account_last4 ~ '^[0-9A-Z]{{4}}$' AND char_length(btrim(holder_name)) >= 2",  # banks.py: any country
    ("payouts", "payouts_amounts_valid"): f"gross BETWEEN 1 AND {MAX_SALARY} AND tds BETWEEN 0 AND gross AND net = gross - tds AND currency IN ({_SQL_LIST(CURRENCIES)}) "
                                          f"AND (salary_currency = '' OR salary_currency IN ({_SQL_LIST(CURRENCIES)})) AND counted >= 0 AND (fx_rate IS NULL OR fx_rate > 0)",
    ("payouts", "payouts_period_valid"): "cycle >= 1 AND period_end >= period_start AND period_end - period_start < 92 AND char_length(reference) >= 6",  # (CYCLE_DAYS can change in .env: old payouts keep their own period)
    ("attendance", "attendance_seconds_valid"): f"active_seconds BETWEEN 0 AND {MAX_WORK_HOURS * 3600}",
    ("identity_proofs", "identity_proofs_valid"): f"id_type IN ({_SQL_LIST(ID_TYPES)}) AND number_last4 ~ '^[0-9A-Z]{{4}}$' AND char_length(btrim(name_on_id)) >= 2",
    ("companies", "companies_id_proof_valid"): f"(id_proof_type IS NULL) = (id_proof_at IS NULL) AND (id_proof_type IS NULL OR id_proof_type IN ({_SQL_LIST(COMPANY_PROOFS)}))",
    ("companies", "companies_net_worth_valid"): f"net_worth IS NULL OR net_worth BETWEEN 0 AND {MAX_NET_WORTH}",  # never negative: payouts are capped at the clearance
    ("company_bank_accounts", "company_bank_accounts_valid"): f"(country <> 'IN' OR ifsc ~ '^[A-Z]{{4}}0[A-Z0-9]{{6}}$') AND country ~ '^[A-Z]{{2}}$' AND (swift = '' OR swift ~ '^[A-Z]{{6}}[A-Z0-9]{{2}}([A-Z0-9]{{3}})?$') "
                                                              f"AND account_type IN ({_SQL_LIST(ACCOUNT_TYPES)}) AND account_last4 ~ '^[0-9A-Z]{{4}}$' AND char_length(btrim(holder_name)) >= 2",
    ("deposits", "deposits_valid"): f"amount BETWEEN 1 AND {MAX_NET_WORTH} AND credited BETWEEN 0 AND {MAX_NET_WORTH} AND net_worth_after BETWEEN credited AND {MAX_NET_WORTH} "
                                    f"AND currency IN ({_SQL_LIST(CURRENCIES)}) AND (worth_currency = '' OR worth_currency IN ({_SQL_LIST(CURRENCIES)})) AND char_length(reference) >= 6",
    ("companies", "companies_net_worth_currency_valid"): f"net_worth_currency IS NULL OR net_worth_currency IN ({_SQL_LIST(CURRENCIES)})",
    ("fx_rates", "fx_rates_valid"): f"currency IN ({_SQL_LIST(CURRENCIES)}) AND per_usd > 0",
    ("companies", "companies_terms_valid"): "terms_version >= 1",
    ("companies", "companies_ai_promotions_valid"): "ai_promotions IN ('off', 'suggest', 'auto')",
    ("users", "users_region_valid"): f"region IS NULL OR region IN ({_SQL_LIST(REGIONS)})",
    ("users", "users_timezone_valid"): f"timezone IS NULL OR timezone IN ({_SQL_LIST({tz for zones in REGION_ZONES.values() for tz, _ in zones})})",
}
_checks = lambda table: tuple(CheckConstraint(sql, name=name) for (t, name), sql in CONSTRAINTS.items() if t == table)
# ---------- tables ----------
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    email: Mapped[str] = mapped_column(String(255))  # unique case-insensitively (index in MIGRATIONS)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True)  # E.164, e.g. +14155550123
    dob: Mapped[date | None]  # date of birth (signup requires 18+); NULL for accounts made before it was asked
    region: Mapped[str | None] = mapped_column(String(2))  # REGIONS: their currency + national holidays (signup / settings); NULL = not chosen yet
    timezone: Mapped[str | None] = mapped_column(String(64))  # one of REGION_ZONES[region]: every time is shown in it
    password_hash: Mapped[str] = mapped_column(String(200))
    email_verified_at: Mapped[datetime | None]
    phone_verified_at: Mapped[datetime | None]
    ip_created: Mapped[IPv4Address | IPv6Address | None] = mapped_column(INET)  # IP the account was created from
    ip_last_login: Mapped[IPv4Address | IPv6Address | None] = mapped_column(INET)
    last_login_at: Mapped[datetime | None]
    login_id: Mapped[uuid.UUID | None]  # one login at a time: a fresh uuid4 per login, mirrored in the Redis session; NULL = logged out
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    language: Mapped[str | None] = mapped_column(String(5))  # the UI language they picked (i18n.LANGUAGES); NULL = English
    banned_at: Mapped[datetime | None]  # banned by the admin (routers/admin.py): can't log in; out of every company; their chats stay (unlike deletion)
    ban_reason: Mapped[str] = mapped_column(String(500), default="", server_default="")
    banned_by: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # lazy="raise": async code must load relationships explicitly (selectinload), so no query ever runs by accident
    memberships: Mapped[list["Membership"]] = relationship(back_populates="user", cascade="all, delete-orphan", passive_deletes=True, lazy="raise")
    owned_companies: Mapped[list["Company"]] = relationship(back_populates="owner", cascade="all, delete-orphan", passive_deletes=True, lazy="raise")
    profile: Mapped["Profile | None"] = relationship(back_populates="user", cascade="all, delete-orphan", passive_deletes=True, lazy="raise")
class Company(Base):
    """id is a random uuid4 and doubles as the join code. positions = the STAFF positions this company offers (always incl. JOIN_AS)."""
    __tablename__ = "companies"
    __table_args__ = _checks("companies")
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)  # owner's account deleted -> company deleted
    positions: Mapped[list[str]] = mapped_column(ARRAY(String(16)))
    description: Mapped[str] = mapped_column(String(1000), default="", server_default="")  # what the company does
    hiring: Mapped[str] = mapped_column(String(1000), default="", server_default="")  # who they want to hire
    listed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))  # opt-in: shown in public search + recommendations, anyone can join
    allow_other_companies: Mapped[bool] = mapped_column(default=True, server_default=text("true"))  # False: its staff can't be in any other company (top level exempt)
    salary_currency: Mapped[str | None] = mapped_column(String(3))  # the unit of its minimum salaries (company_salaries); NULL = not set yet
    salary_period: Mapped[str | None] = mapped_column(String(5))  # "month" / "year"
    website: Mapped[str] = mapped_column(String(200), default="", server_default="")  # optional, normalised https://… (form.py)
    registration_type: Mapped[str | None] = mapped_column(String(8))  # one of REGISTRATION_TYPES; required for new companies
    registration_number: Mapped[str | None] = mapped_column(String(40))  # format-checked per type (a GSTIN's checksum too); unique per type
    location_place_id: Mapped[str | None] = mapped_column(String(300))  # the Geoapify place picked on the map, verified server-side (places.py)
    location_address: Mapped[str] = mapped_column(String(300), default="", server_default="")  # Geoapify's own address for it
    location_lat: Mapped[float | None]
    location_lng: Mapped[float | None]
    location_country: Mapped[str | None] = mapped_column(String(2))  # ISO country code, from Geoapify
    exact_address: Mapped[str] = mapped_column(String(300), default="", server_default="")  # typed by the owner: unit / floor / building, on top of the verified location
    timezone: Mapped[str | None] = mapped_column(String(64))  # IANA name from Geoapify (the location's); working hours + pay cycles use it (payroll.company_tz)
    work_start: Mapped[time] = mapped_column(default=time(9), server_default=text("'09:00'"))  # working hours, required: at most MAX_WORK_HOURS a day
    work_end: Mapped[time] = mapped_column(default=time(17), server_default=text("'17:00'"))
    work_days: Mapped[int] = mapped_column(SmallInteger, default=31, server_default=text("31"))  # bit 0 = Monday … bit 6 = Sunday (31 = Mon–Fri)
    hours_changed_at: Mapped[datetime | None]  # their last change: the next needs HOURS_EVERY_DAYS (payroll.hours_locked); NULL = never changed since creation
    max_members: Mapped[int | None]  # the employee limit: everyone in the company counts, owner and co-owners too (guard.no_room). Only they can see it, so it's never in the change log. NULL = none yet (older companies, until their next save)
    net_worth: Mapped[int | None] = mapped_column(BigInteger)  # funds available, in net_worth_currency: set by the owner / a co-owner, every payout takes its gross off it (converted)
    net_worth_currency: Mapped[str | None] = mapped_column(String(3))  # can differ from salary_currency (fx.py converts); NULL = salary_currency
    net_worth_updated_at: Mapped[datetime | None]
    net_worth_by: Mapped[str] = mapped_column(String(32), default="", server_default="")  # who last set it
    id_proof_type: Mapped[str | None] = mapped_column(String(16))  # its identity proof (COMPANY_PROOFS): the photo is on disk (system.id_file("c", id)), never public
    id_proof_at: Mapped[datetime | None]
    liability_since: Mapped[datetime | None]  # guard.py: when employee liability first went over net worth (the warning's timestamp); NULL = covered
    halted_at: Mapped[datetime | None]  # set 24 h after an unresolved warning: operations halted until the liability is covered again
    terms: Mapped[str] = mapped_column(String(5000), default="", server_default="")  # its own terms and conditions (owner / co-owners); "" = the standard ones only
    terms_version: Mapped[int] = mapped_column(default=1, server_default="1")  # +1 whenever its terms change: joining needs the current one accepted
    terms_updated_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # + embedding vector(ASSIST_DIM) (see MIGRATIONS): name + description + hiring + positions, kept fresh by assist.update_company_embedding
    ai_promotions: Mapped[str] = mapped_column(String(8), default="off", server_default="off")  # the company AI (companyai.py): off / suggest / auto
    ai_reviewed_at: Mapped[datetime | None]  # its last promotion review
    owner: Mapped["User"] = relationship(back_populates="owned_companies", lazy="raise")
    members: Mapped[list["Membership"]] = relationship(back_populates="company", cascade="all, delete-orphan", passive_deletes=True, lazy="raise")
    salaries: Mapped[list["CompanySalary"]] = relationship(cascade="all, delete-orphan", passive_deletes=True, lazy="raise", order_by="CompanySalary.position")
    def salary_of(self) -> dict[str, int]: return {s.position: s.min_amount for s in self.salaries}  # needs salaries loaded (selectinload)
class CompanySalary(Base):
    """The minimum salary a company offers for one of its positions, in the company's salary_currency per salary_period. Every offered
    position must have one (checked on create + edit); deleted with the company."""
    __tablename__ = "company_salaries"
    __table_args__ = _checks("company_salaries")
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[str] = mapped_column(String(16), primary_key=True)
    min_amount: Mapped[int] = mapped_column(BigInteger)
class Membership(Base):
    """Who works where, as what. ON DELETE CASCADE both ways: deleting a user removes them from every company, and deleting a
    company removes it from every member's workplaces, done by Postgres itself, whatever deletes the row."""
    __tablename__ = "company_members"
    __table_args__ = _checks("company_members")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)  # PK pair: no double joins
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True, index=True)
    position: Mapped[str] = mapped_column(String(16))
    joined_at: Mapped[datetime] = mapped_column(default=utcnow)  # salary cycle 1 starts this day (company time)
    salary: Mapped[int | None] = mapped_column(BigInteger)  # agreed in the job offer they accepted; NULL = their position's minimum
    terms_version: Mapped[int | None]  # the company's terms they accepted (STANDARD_TERMS + its own); NULL = joined before terms existed
    terms_accepted_at: Mapped[datetime | None]
    position_since: Mapped[datetime | None]  # when their position last changed (NULL = since joining): the company AI waits PROMOTE_AFTER_DAYS
    pay_history: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))  # what cycles already started when their pay changed stay at (payroll.keep_pay, user's rule): [{"until", "per", "low", "position"}], oldest first
    user: Mapped["User"] = relationship(back_populates="memberships", lazy="raise")
    company: Mapped["Company"] = relationship(back_populates="members", lazy="raise")
class AwayPeriod(Base):
    """Someone left a company (left_at) and came back (back_at; NULL = still gone). User's rule: back in its chats they see everything but what was said in between (seen_by)."""
    __tablename__ = "away_periods"
    __table_args__ = (Index("away_periods_who", "user_id", "company_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    left_at: Mapped[datetime] = mapped_column(default=utcnow)
    back_at: Mapped[datetime | None]
class OwnershipOffer(Base):
    """The owner offered the company to one of its co-owners (routers/membership.py, user's rule): nothing changes until that co-owner accepts. One open offer per company."""
    __tablename__ = "ownership_offers"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    to_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    by_name: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class BankAccount(Base):
    """Where someone's salary goes, in any country (banks.py): required before joining or creating a company. The account number / IBAN is
    stored only encrypted (AES-256-GCM, system.seal, bound to the user id); account_last4 is what statements and payslips show."""
    __tablename__ = "bank_accounts"
    __table_args__ = _checks("bank_accounts")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    holder_name: Mapped[str] = mapped_column(String(100))
    bank_name: Mapped[str] = mapped_column(String(100))
    branch: Mapped[str] = mapped_column(String(100), default="", server_default="")
    country: Mapped[str] = mapped_column(String(2), default="IN", server_default="IN")  # the bank's (banks.COUNTRIES; "ZZ" = another country)
    routing: Mapped[str] = mapped_column("ifsc", String(20), default="", server_default="")  # the local bank code: IFSC, ABA routing number, BSB… ("" where the IBAN is the account number). Column kept from India-only days
    swift: Mapped[str] = mapped_column(String(11), default="", server_default="")  # SWIFT/BIC: money from abroad needs it
    bank_address: Mapped[str] = mapped_column(String(200), default="", server_default="")  # optional, for transfers from abroad
    account_type: Mapped[str] = mapped_column(String(8))  # one of ACCOUNT_TYPES
    account_enc: Mapped[bytes] = mapped_column(LargeBinary)  # nonce + ciphertext; never in plain text
    account_last4: Mapped[str] = mapped_column(String(4))
    verified_at: Mapped[datetime | None]  # checked with the bank (bankcheck.py, outside DEV_MODE); NULL = saved unverified
    verified_name: Mapped[str] = mapped_column(String(100), default="", server_default="")  # the holder's name as the bank has it
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
class IdentityProof(Base):
    """A person's identity proof, required before joining or creating a company: the ID's type, the name on it and its number (stored
    only encrypted, system.seal bound to the user; the last 4 characters for display). The photo of the document is re-encoded and kept
    on disk under a keyed name (system.id_file("u", user id)), never public: only the person and the owner, co-owners and HR of a company
    they're in can open it."""
    __tablename__ = "identity_proofs"
    __table_args__ = _checks("identity_proofs")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    id_type: Mapped[str] = mapped_column(String(16))
    name_on_id: Mapped[str] = mapped_column(String(100))
    number_enc: Mapped[bytes] = mapped_column(LargeBinary)
    number_last4: Mapped[str] = mapped_column(String(4))
    verified_at: Mapped[datetime | None]  # checked online (idcheck.py, while ID_VERIFY=1); NULL = format + check digit only
    verified_by: Mapped[str] = mapped_column(String(32), default="", server_default="")  # e.g. "Cashfree PAN"
    verified_name: Mapped[str] = mapped_column(String(100), default="", server_default="")  # the name the registry has
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
class CompanyBankAccount(Base):
    """The company's own account, the one salaries are paid from: required before any payout is recorded (owner / co-owners set it,
    with their password). Same encryption as personal accounts (system.seal, bound to the company id)."""
    __tablename__ = "company_bank_accounts"
    __table_args__ = _checks("company_bank_accounts")
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    holder_name: Mapped[str] = mapped_column(String(100))
    bank_name: Mapped[str] = mapped_column(String(100))
    branch: Mapped[str] = mapped_column(String(100), default="", server_default="")
    country: Mapped[str] = mapped_column(String(2), default="IN", server_default="IN")  # any country, like personal accounts (banks.py)
    routing: Mapped[str] = mapped_column("ifsc", String(20), default="", server_default="")
    swift: Mapped[str] = mapped_column(String(11), default="", server_default="")
    bank_address: Mapped[str] = mapped_column(String(200), default="", server_default="")
    account_type: Mapped[str] = mapped_column(String(8))
    account_enc: Mapped[bytes] = mapped_column(LargeBinary)
    account_last4: Mapped[str] = mapped_column(String(4))
    verified_at: Mapped[datetime | None]  # checked with the bank (bankcheck.py, outside DEV_MODE)
    verified_name: Mapped[str] = mapped_column(String(100), default="", server_default="")
    updated_by: Mapped[str] = mapped_column(String(32), default="", server_default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
class Payout(Base):
    """One salary payment: a line on the employee's statement and on the company's. Recorded by an owner / co-owner / manager / HR
    after they've sent the money, with the amounts typed in, the account it went to (checked against the employee's bank details),
    the transfer's date + reference, and a screenshot (stored on disk under an HMAC name, media.proof_file; nothing about it here).
    One per member per cycle. Paid in any supported currency: the gross is also kept converted into the company's salary currency
    (what the minimum and the clearance were checked in) with the rate used. Names and bank details are snapshots, and both sides'
    links are SET NULL, so a statement survives the company or the account being deleted."""
    __tablename__ = "payouts"
    __table_args__ = (Index("payouts_one_per_period", "company_id", "user_id", "cycle", "period_start", unique=True),  # (with its dates: someone who left and came back counts their cycles from 1 again)
                      UniqueConstraint("company_id", "reference", name="payouts_reference_key"), *_checks("payouts"))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)  # who was paid
    paid_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    company_name: Mapped[str] = mapped_column(String(64))
    company_code: Mapped[str] = mapped_column(String(36), default="", server_default="")  # the company's id (its join code), kept after it's deleted
    employee_name: Mapped[str] = mapped_column(String(32))
    position: Mapped[str] = mapped_column(String(16))  # theirs when paid
    paid_by_name: Mapped[str] = mapped_column(String(32))
    paid_by_position: Mapped[str] = mapped_column(String(16))
    cycle: Mapped[int]  # 1 = the first 30 days after joining
    period_start: Mapped[date]
    period_end: Mapped[date]
    currency: Mapped[str] = mapped_column(String(3))  # what was sent: any supported currency, the payer picks it
    gross: Mapped[int] = mapped_column(BigInteger)  # salary before tax, typed in by the payer
    tds: Mapped[int] = mapped_column(BigInteger)  # tax deducted at source, typed in
    net: Mapped[int] = mapped_column(BigInteger)  # gross - tds: what was sent
    salary_currency: Mapped[str] = mapped_column(String(3), default="", server_default="")  # the company's salary currency then (its tax rules name the deduction)
    counted: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")  # gross in salary_currency at the day's rate (= gross when it's the same)
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))  # 1 currency = fx_rate salary_currency; NULL when they're the same
    holder_name: Mapped[str] = mapped_column(String(100))
    bank_name: Mapped[str] = mapped_column(String(100))
    bank_country: Mapped[str] = mapped_column(String(2), default="IN", server_default="IN")  # snapshot of the account it went to (banks.bank_line shows it)
    routing: Mapped[str] = mapped_column("ifsc", String(20))  # its local bank code ("" for an IBAN)
    swift: Mapped[str] = mapped_column(String(11), default="", server_default="")
    account_last4: Mapped[str] = mapped_column(String(4))
    transferred_at: Mapped[datetime]  # when the money was sent (typed in, company time -> naive UTC)
    timezone: Mapped[str] = mapped_column(String(64))  # the company's, for showing transferred_at as it was typed
    reference: Mapped[str] = mapped_column(String(40))  # the bank's transaction reference (UTR etc.), unique per company
    attendance: Mapped[str] = mapped_column(String(300), default="", server_default="")  # snapshot for the payslip: attendance + unpaid leave ("not monitored" for freelancers)
    from_bank: Mapped[str] = mapped_column(String(100), default="", server_default="")  # the company account it was paid from (snapshot)
    from_last4: Mapped[str] = mapped_column(String(4), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    seen_at: Mapped[datetime | None]  # NULL = unread on the employee's bell
class Deposit(Base):
    """Money paid into a company: added to its net worth (what every payout is checked against). Recorded by the owner / a co-owner
    once it arrived, with the amount, where it came from, the company account it was credited to (checked against the saved one),
    when, the bank's reference and a screenshot (on disk under an HMAC name, media.store_proof(…, "deposit"); nothing about it here).
    On the company's statement next to its payouts. Names are snapshots and the links SET NULL, so it outlives the company or account."""
    __tablename__ = "deposits"
    __table_args__ = (UniqueConstraint("company_id", "reference", name="deposits_reference_key"), *_checks("deposits"))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))  # who recorded it
    company_name: Mapped[str] = mapped_column(String(64))
    company_code: Mapped[str] = mapped_column(String(36))  # the company's id (its join code), kept after it's deleted
    by_name: Mapped[str] = mapped_column(String(32))
    by_position: Mapped[str] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))  # what arrived: any supported currency
    amount: Mapped[int] = mapped_column(BigInteger)  # typed in
    worth_currency: Mapped[str] = mapped_column(String(3), default="", server_default="")  # the net worth's currency then
    credited: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")  # amount converted into it (fx.py), what the net worth went up by
    source: Mapped[str] = mapped_column(String(100))  # where it came from, typed in: an investor, the owner's own account, a client…
    to_bank: Mapped[str] = mapped_column(String(100))  # the company account it was credited to (snapshot)
    to_last4: Mapped[str] = mapped_column(String(4))
    transferred_at: Mapped[datetime]  # when it arrived (typed in, company time -> naive UTC)
    timezone: Mapped[str] = mapped_column(String(64))
    reference: Mapped[str] = mapped_column(String(40))  # the bank's transaction reference, unique per company
    note: Mapped[str] = mapped_column(String(300), default="", server_default="")
    net_worth_after: Mapped[int] = mapped_column(BigInteger)  # in worth_currency
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class PromotionSuggestion(Base):
    """A promotion call of the company AI (companyai.py, user asked): who, from -> to, the signals it went by (reviews weighted by who wrote
    them, tasks, attendance) and the local AI's verdict + reason. status: suggested (waits for the owner / a co-owner), applied / dismissed
    by one of them, auto (the company's switch is on: applied by itself), or stale (their position changed some other way first)."""
    __tablename__ = "promotion_suggestions"
    __table_args__ = (CheckConstraint("status IN ('suggested', 'applied', 'dismissed', 'auto', 'stale')", name="promotion_suggestions_status_valid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    from_position: Mapped[str] = mapped_column(String(16))
    to_position: Mapped[str] = mapped_column(String(16))
    score: Mapped[float] = mapped_column(default=0.0)
    signals: Mapped[dict] = mapped_column(JSONB, default=dict)
    ai_says: Mapped[str] = mapped_column(String(600), default="")  # "" = the local AI wasn't available: never applied by itself then
    status: Mapped[str] = mapped_column(String(10), default="suggested")
    decided_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None]
class SiteSetting(Base):
    """A switch the admin flips for the whole site (routers/admin.py): "single_company" = on / off, and when it last changed."""
    __tablename__ = "site_settings"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)
SITE = {"single_company": False}  # the admin's switches as last read (app.py reads them at startup): what the templates hide or lock
async def single_company_mode(db: AsyncSession, lock: bool = True) -> bool:
    """Single company mode (user asked: an admin switch, only turned on while one company or none exists) is on: then no company at all can
    be created, the AI company finder + matching are off and the one company fills the companies page. lock: the switch's row stays locked
    until the caller commits, so the admin can't turn it on halfway through a creation."""
    s = await db.get(SiteSetting, "single_company", with_for_update=lock or None)
    SITE["single_company"] = bool(s and s.value == "on")
    return SITE["single_company"]
templates.env.globals["single_company"] = lambda: SITE["single_company"]  # {% if single_company() %}: the side links, the dashboard's buttons
class OverloadIP(Base):
    """Every IP address seen while the request limiter was overloaded (abuse.py; user asked: each and every incoming IP logged): how many
    requests it sent, how many were refused, when, the last account logged in from it and the first page it asked for. One row per
    overload (started_at) and address, kept abuse.KEEP_DAYS. No foreign keys: it's a log."""
    __tablename__ = "overload_ips"
    __table_args__ = (UniqueConstraint("started_at", "ip", name="overload_ips_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(index=True)  # the overload's start (naive UTC): its key
    ip: Mapped[str] = mapped_column(String(45))
    requests: Mapped[int] = mapped_column(BigInteger, default=0)
    refused: Mapped[int] = mapped_column(BigInteger, default=0)  # of them, answered 429 by the limiter
    first_seen: Mapped[datetime]
    last_seen: Mapped[datetime]
    user_id: Mapped[int | None]  # the last account logged in from it then
    path: Mapped[str] = mapped_column(String(200), default="")
class FxRate(Base):
    """The last good exchange rate per currency (units per US dollar), fetched by fx.py: kept so a restart or an outage of the rates
    service still has them."""
    __tablename__ = "fx_rates"
    __table_args__ = _checks("fx_rates")
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    per_usd: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    fetched_at: Mapped[datetime]
class Notice(Base):
    """A notification from the payroll guard (guard.py): liability warnings, halts, payday reminders, employee alerts. One row per
    recipient; key makes each one happen once (e.g. one reminder per member + cycle). Shown on the notifications page (the bell)."""
    __tablename__ = "notices"
    __table_args__ = (UniqueConstraint("user_id", "key", name="notices_once"), CheckConstraint("level IN ('info', 'warn', 'danger')", name="notices_level_valid"))
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"))  # kept when the company is deleted (that's a notice too)
    level: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(String(160))
    body: Mapped[str] = mapped_column(String(1000))
    link: Mapped[str] = mapped_column(String(200), default="")
    key: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    seen_at: Mapped[datetime | None]
class LeaveRequest(Base):
    """Leave someone asks for (start_day..end_day, company days). Approved by someone ranked above them among the owner, co-owners and
    managers (never themselves). Approved leave is unpaid: the working days it covers come off that cycle's salary (payroll.terms)."""
    __tablename__ = "leave_requests"
    __table_args__ = (CheckConstraint("status IN ('pending', 'approved', 'declined', 'cancelled')", name="leave_requests_status_valid"),
                      CheckConstraint("end_day >= start_day AND end_day - start_day < 60", name="leave_requests_days_valid"))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    start_day: Mapped[date]
    end_day: Mapped[date]
    reason: Mapped[str] = mapped_column(String(500), default="", server_default="")
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    decided_by: Mapped[str] = mapped_column(String(32), default="", server_default="")
    decided_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    user: Mapped["User"] = relationship(lazy="raise")
class MemberRequest(Base):
    """Joining or leaving a company, decided by its owner / a co-owner / a manager above the person (routers/membership.py), who sees the
    applicant's full details (profile + skills, identity proof, bank details). Joining needs one unless the company approached them (an
    open job offer); leaving always does, and is only approved once their salary is settled. One open request per company + person + kind."""
    __tablename__ = "member_requests"
    __table_args__ = (CheckConstraint("kind IN ('join', 'exit')", name="member_requests_kind_valid"),
                      CheckConstraint("status IN ('pending', 'approved', 'declined', 'cancelled')", name="member_requests_status_valid"))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(4))  # join / exit
    position: Mapped[str] = mapped_column(String(16), default="", server_default="")  # join: rookie or freelancer
    note: Mapped[str] = mapped_column(String(500), default="", server_default="")
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    decided_by: Mapped[str] = mapped_column(String(32), default="", server_default="")
    decided_at: Mapped[datetime | None]
    terms_version: Mapped[int | None]  # join: the company's terms they accepted when asking (approval needs the current version)
    terms_accepted_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    user: Mapped["User"] = relationship(lazy="raise")
class DeletionRequest(Base):
    """The owner asked to delete the company: it only happens once every co-owner has approved (approved_by); one decline drops the request.
    Salaries must be settled before the request and again before the deletion itself."""
    __tablename__ = "deletion_requests"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    requested_by: Mapped[str] = mapped_column(String(32))
    requested_at: Mapped[datetime] = mapped_column(default=utcnow)
    approved_by: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)
class Attendance(Base):
    """Seconds someone was active in the app during their company's working hours, per company-local day (presence.py adds them
    every tick while their tab is open and in use). Only for monitored positions: never owners, co-owners or freelancers."""
    __tablename__ = "attendance"
    __table_args__ = _checks("attendance")
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)
    active_seconds: Mapped[int] = mapped_column(default=0)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)
class Profile(Base):
    """A person's biodata for company recommendations: typed in, or extracted from an uploaded PDF (only the main points are kept,
    never contact details). + embedding vector(ASSIST_DIM) (see MIGRATIONS), kept fresh by assist.update_profile_embedding."""
    __tablename__ = "profiles"
    __table_args__ = (CheckConstraint("cardinality(skills) <= 40", name="profiles_skills_max"), *_checks("profiles"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    headline: Mapped[str] = mapped_column(String(120), default="")
    bio: Mapped[str] = mapped_column(String(2000), default="")
    skills: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    years: Mapped[int | None]  # years of experience
    resume_file: Mapped[str | None] = mapped_column(String(300))  # saved PDF name in uploads/ (never served)
    extracted_by: Mapped[str | None] = mapped_column(String(16))  # "claude" / "local" when filled from a PDF
    discoverable: Mapped[bool] = mapped_column(default=False, server_default=text("false"))  # opt-in: company owners can find them in people search
    want_min: Mapped[int | None] = mapped_column(BigInteger)  # the salary range they want (typed on /profile or told to the instruction bar)
    want_max: Mapped[int | None] = mapped_column(BigInteger)  # NULL = no upper end ("at least want_min")
    want_currency: Mapped[str | None] = mapped_column(String(3))
    want_period: Mapped[str | None] = mapped_column(String(5))
    def wants(self) -> str: return (f"{self.want_min:,}" + (f"–{self.want_max:,}" if self.want_max else "+") + f" {self.want_currency} per {self.want_period}") if self.want_min else ""
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    user: Mapped["User"] = relationship(back_populates="profile", lazy="raise")
class Approach(Base):
    """The company <-> person relation for job offers: a company's owner/co-owner asked a discoverable person to join, with a message
    and a salary offer. Shown in that person's notifications (+ emailed). One per company + person (PK): removed when they join;
    kept as declined (dismissed_at) when they dismiss it, so the same company can't ask again. Cascades away with either side."""
    __tablename__ = "approaches"
    __table_args__ = _checks("approaches")
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))  # who sent it
    message: Mapped[str] = mapped_column(String(1000), default="", server_default="")
    salary_amount: Mapped[int | None] = mapped_column(BigInteger)  # offered salary, whole currency units
    salary_currency: Mapped[str | None] = mapped_column(String(3))  # one of CURRENCIES
    salary_period: Mapped[str | None] = mapped_column(String(5))  # "month" / "year"
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    seen_at: Mapped[datetime | None]  # set when they open their notifications; NULL = counts as unread on the bell
    dismissed_at: Mapped[datetime | None]  # declined: hidden from them, shown as declined to the company
    company: Mapped["Company"] = relationship(lazy="raise")
    by: Mapped["User | None"] = relationship(foreign_keys=[by_user_id], lazy="raise")
class AIReview(Base):
    """The latest AI review (vLLM, grow.py) of one profile (user_id) or one company (company_id): exactly one is set, and
    the row is deleted with its owner. status: running / done / error; items = [{area, priority, title, detail}]."""
    __tablename__ = "ai_reviews"
    __table_args__ = (CheckConstraint("num_nonnulls(user_id, company_id) = 1", name="ai_reviews_one_owner"),
                      CheckConstraint("status IN ('running', 'done', 'error')", name="ai_reviews_status_valid"))
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(8))
    engine: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[str] = mapped_column(String(1200), default="")
    items: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str] = mapped_column(String(300), default="")
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
class BlacklistedCredential(Base):
    """The credential blacklist: the username, email and phone of every deleted account, as keyed HMACs (system.identity_digest, never
    the values themselves). Signup, account creation and username changes refuse them, so a deleted account can never be recreated."""
    __tablename__ = "credential_blacklist"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))  # username / email / phone
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
async def blacklisted(db: AsyncSession, **values: str | None) -> str | None:
    """The first of username= / email= / phone= that's on the credential blacklist, or None."""
    digests = {identity_digest(kind, v): kind for kind, v in values.items() if v}
    hit = await db.scalar(select(BlacklistedCredential.kind).where(BlacklistedCredential.digest.in_(digests))) if digests else None
    return {"username": "username", "email": "email address", "phone": "phone number"}.get(hit) if hit else None
HOLD_SIGNUP = timedelta(minutes=30)  # a username someone is signing up with stays theirs this long, while they enter the codes
async def username_problem(db: AsyncSession, name: str, me: int | None = None, email: str = "") -> str | None:
    """Why `name` can't be a new username, or None (user asked: unique, checked at signup). Taken by another account whatever its capital
    letters ("Alex" = "alex", also a unique index on lower(username)), a deleted account's, or being signed up with right now by someone
    else (an unverified signup holds it HOLD_SIGNUP). me: your own account (a username change); email: your own signup's (starting again)."""
    low = name.strip().lower()
    if have := await db.scalar(select(User.username).where(func.lower(User.username) == low, *((User.id != me,) if me else ()))):
        return f"The username {name} is taken" + (f" (as {have}: capital letters don't make a username different)" if have != name else "") + "."
    if low in PLATFORM_ADMINS and not (me and await db.scalar(select(User.id).where(User.id == me, func.lower(User.username) == low))):  # a platform admin's name opens the money page: if they rename, nobody else can pick it up
        return f"The username {name} is reserved."
    if await blacklisted(db, username=name): return f"The username {name} belonged to a deleted account and can't be used again."
    if await db.scalar(select(PendingSignup.id).where(func.lower(PendingSignup.username) == low, PendingSignup.created_at > utcnow() - HOLD_SIGNUP,
                                                      *((func.lower(PendingSignup.email) != email.strip().lower(),) if email else ())).limit(1)):
        return f"Someone is signing up as {name} right now. Pick another username."
    return None
class MessageArchive(Base):
    """Message history of deleted accounts: the job offers they sent or received, kept MESSAGE_RETENTION_DAYS after the deletion and then
    purged (app.py's maintenance loop). No foreign keys: the account is gone. Nothing in the app shows it."""
    __tablename__ = "message_archive"
    id: Mapped[int] = mapped_column(primary_key=True)
    deleted_user: Mapped[str] = mapped_column(String(32))  # their username at deletion
    direction: Mapped[str] = mapped_column(String(8))  # sent / received
    company: Mapped[str] = mapped_column(String(64))
    other_party: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(String(1000), default="")
    salary: Mapped[str] = mapped_column(String(80), default="")
    sent_at: Mapped[datetime]
    purge_after: Mapped[datetime] = mapped_column(index=True)
MESSAGE_RETENTION_DAYS = 30
class Chat(Base):
    """A conversation (routers/chats.py). Inside one company: "direct" = two members (one per pair, direct_key), "group" = a named group of
    members. Anyone in the company can chat with anyone in it, owner included. The company's owner, co-owners, managers and HR can read
    every chat in it (CHAT_READERS): every chat page and the standard terms say so. Deleted with the company. No company, only the people
    in it can read it: "personal" = two people (one per pair, direct_key; friends or colleagues), "pgroup" = a named personal group of friends.
    "enquiry" = someone contacting a company before joining it (direct_key = their user id; one per person and company), with its owner,
    co-owners and HR in it: a company chat they can be in without being in the company."""
    __tablename__ = "chats"
    __table_args__ = (CheckConstraint("kind IN ('direct', 'group', 'personal', 'pgroup', 'enquiry') AND (kind IN ('personal', 'pgroup')) = (company_id IS NULL)", name="chats_kind_valid"),
                      Index("chats_one_direct", "company_id", "direct_key", unique=True, postgresql_where=text("kind = 'direct'")),
                      Index("chats_one_personal", "direct_key", unique=True, postgresql_where=text("kind = 'personal'")),
                      Index("chats_one_enquiry", "company_id", "direct_key", unique=True, postgresql_where=text("kind = 'enquiry'")))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)  # NULL = a personal chat (two people, no company: not readable by any company)
    kind: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(String(60), default="", server_default="")  # groups only
    direct_key: Mapped[str | None] = mapped_column(String(40))  # "<smaller user id>:<larger>" for a direct / personal chat
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_message_at: Mapped[datetime] = mapped_column(default=utcnow)  # newest chats first
class Friendship(Base):
    """Two people who are friends, or a request from one to the other (routers/friends.py): one row per pair, low_id < high_id. Anyone can
    ask anyone (by username); the other accepts or declines. Friends chat privately: one to one or in personal groups (routers/chats.py)."""
    __tablename__ = "friendships"
    __table_args__ = (CheckConstraint("low_id < high_id AND status IN ('pending', 'friends') AND requested_by IN (low_id, high_id)", name="friendships_valid"),)
    low_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    high_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    requested_by: Mapped[int]  # one of the two (CHECK); the other one answers
    status: Mapped[str] = mapped_column(String(8), default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    since: Mapped[datetime | None]  # accepted at
class AIMessage(Base):
    """The private conversation with Deminic, the app's own local AI (deminic.py): only its owner ever sees it; no company can read it and
    the change logs never hold its words. Encrypted at rest (bound to its owner); the newest ones are what the AI remembers. A reply being
    written is a row with status "thinking". Deleted with the account, or when they clear it."""
    __tablename__ = "ai_messages"
    __table_args__ = (CheckConstraint("role IN ('me', 'ai') AND status IN ('done', 'thinking', 'failed')", name="ai_messages_valid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(2))
    body_enc: Mapped[bytes] = mapped_column(LargeBinary)  # {"text", "command"} as JSON, sealed with "ai:<user id>"
    status: Mapped[str] = mapped_column(String(8), default="done", server_default="done")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class FriendBlock(Base):
    """Someone you blocked: no friend requests and no personal messages from them (routers/friends.py). Company chats aren't affected."""
    __tablename__ = "friend_blocks"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    blocked_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class ChatMember(Base):
    __tablename__ = "chat_members"
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_read_at: Mapped[datetime | None]  # unread = others' messages after this
class ChatMessage(Base):
    """One message, encrypted at rest (system.seal, bound to its chat). When the sender's account is deleted, sender_id goes NULL and
    purge_after is set MESSAGE_RETENTION_DAYS ahead: the history stays readable that long, then purge_expired_messages removes it."""
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    sender_name: Mapped[str] = mapped_column(String(32))  # kept after the account is deleted
    body_enc: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    purge_after: Mapped[datetime | None] = mapped_column(index=True)  # set when the sender's account is deleted
CHAT_READERS = ("owner", "co_owner", "manager", "hr")  # can read every chat in their company (said on every chat page + in the standard terms)
def seen_by(uid: int):
    """A filter for ChatMessage queries: what uid may see. Not what a company's chats said while they were gone from it (user's rule; AwayPeriod), unless their own position
    can read every chat there, and never hiding their own enquiry to it, which they're in whether or not they're in the company. Personal chats have no company: nothing is hidden."""
    reader = select(Membership.user_id).where(Membership.user_id == uid, Membership.company_id == Chat.company_id, Membership.position.in_(CHAT_READERS))
    return ~select(AwayPeriod.id).join(Chat, Chat.company_id == AwayPeriod.company_id).where(Chat.id == ChatMessage.chat_id, AwayPeriod.user_id == uid, ChatMessage.created_at >= AwayPeriod.left_at, ChatMessage.created_at < AwayPeriod.back_at,
                                                                                            Chat.direct_key.is_distinct_from(str(uid)), ~reader.exists()).exists()
TASKERS =("owner", "co_owner", "manager", "hr")  # can assign tasks (to people ranked below them): routers/tasks.py
MAX_REVIEWS = 2  # likes / dislikes one person can ever give another in a company (withdrawn ones count): routers/tasks.py
class Task(Base):
    """A task given to one or more people in a company by its owner / a co-owner / a manager / HR (TASKERS), with a deadline on the
    company's clock. Assignees are told (bell + email) and mark it done; the assigner sees who has. Cancelling tells them too."""
    __tablename__ = "tasks"
    __table_args__ = (CheckConstraint("status IN ('open', 'cancelled')", name="tasks_status_valid"), CheckConstraint("char_length(btrim(title)) >= 3", name="tasks_title_valid"))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    deadline: Mapped[datetime]  # naive UTC (typed on the company's clock)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_name: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(10), default="open", server_default="open")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class TaskAssignee(Base):
    __tablename__ = "task_assignees"
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    done_at: Mapped[datetime | None]
class Review(Base):
    """A like or dislike one member gives another in the same company, always with a reason. One active per pair; withdrawing it
    ("unlike" / "undislike") is the only way to change your mind (no flipping), and at most MAX_REVIEWS ever per pair."""
    __tablename__ = "reviews"
    __table_args__ = (CheckConstraint("kind IN ('like', 'dislike')", name="reviews_kind_valid"), CheckConstraint("reviewer_id <> subject_id", name="reviews_not_self"),
                      CheckConstraint("char_length(btrim(reason)) >= 3", name="reviews_reason_valid"),
                      Index("reviews_one_active", "company_id", "reviewer_id", "subject_id", unique=True, postgresql_where=text("withdrawn_at IS NULL")))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(7))
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    withdrawn_at: Mapped[datetime | None]
MEET_HOSTS = TASKERS  # schedule meetings, start one now, record, end it for everyone: routers/meetings.py
MEET_READERS = CHAT_READERS  # can open every meeting's record in their company afterwards (said on every meeting page + in the standard terms)
class Meeting(Base):
    """A video meeting in one company (routers/meetings.py + meet.py), scheduled by its owner / a co-owner / a manager / HR (MEET_HOSTS)
    inside working hours on a working day that isn't a holiday, for the members they invite. Only those people (and whoever scheduled it)
    can join, and only while they're in the company. One live meeting per company at a time (meetings_one_live); scheduled ones can't
    overlap. Its in-call chat + captions (meeting_lines) and the AI's notes are kept encrypted, its recordings on disk (meet.recording_file):
    the people invited and the company's owner, co-owners, managers and HR (MEET_READERS) can open them afterwards. Deleted with the company."""
    __tablename__ = "meetings"
    __table_args__ = (CheckConstraint("status IN ('scheduled', 'live', 'ended', 'cancelled')", name="meetings_status_valid"), CheckConstraint("ends_at > starts_at", name="meetings_span_valid"),
                      CheckConstraint("char_length(btrim(title)) >= 3", name="meetings_title_valid"), Index("meetings_one_live", "company_id", unique=True, postgresql_where=text("status = 'live'")))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    agenda: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    starts_at: Mapped[datetime]  # naive UTC (typed on the company's clock)
    ends_at: Mapped[datetime]
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_by_name: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(10), default="scheduled", server_default="scheduled")
    started_at: Mapped[datetime | None]  # the first time someone joined
    ended_at: Mapped[datetime | None]
    reminded_at: Mapped[datetime | None]  # "starts in 10 minutes" went out
    beat_at: Mapped[datetime | None]  # live: the app process holding its room touches this every few seconds (meet.py); a silent one is reset
    notes_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # the AI's live notes (JSON), sealed to the meeting
    notes_at: Mapped[datetime | None]
    notes_upto: Mapped[int] = mapped_column(default=0, server_default="0")  # the last meeting_lines id the notes cover
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class MeetingInvite(Base):
    __tablename__ = "meeting_invites"
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    joined_at: Mapped[datetime | None]  # the first time they joined (attendance on the meeting's page)
class MeetingLine(Base):
    """What was said (kind "speech": Whisper's caption + its English) or written (kind "chat") in a meeting, encrypted (bound to the
    meeting). A deleted account's lines keep its name and stay MESSAGE_RETENTION_DAYS, then purge_expired_messages erases them."""
    __tablename__ = "meeting_lines"
    __table_args__ = (CheckConstraint("kind IN ('speech', 'chat')", name="meeting_lines_kind_valid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(6))
    speaker_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    speaker_name: Mapped[str] = mapped_column(String(32))  # kept after the account is deleted
    lang: Mapped[str] = mapped_column(String(8), default="", server_default="")  # speech: the language Whisper heard
    text_enc: Mapped[bytes] = mapped_column(LargeBinary)
    english_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # speech in another language: the local AI's English
    at: Mapped[datetime] = mapped_column(default=utcnow)
    purge_after: Mapped[datetime | None] = mapped_column(index=True)  # set when the speaker's account is deleted
# ---------- the shop: a marketplace for companies that sell products (shop.py, routers/shop.py for buyers, routers/seller.py for sellers) ----------
SHOP_MANAGERS = ("owner", "co_owner", "manager")  # run a company's shop: products, prices, orders, settings, competitors, the AI's suggestions
SHOP_MONEY = ("owner", "co_owner", "manager", "hr")  # see its money: the revenue charts, the Excel statement, the balance with the platform (user asked: not the shop staff)
SHOP_CATEGORIES = {"electronics": "Electronics", "fashion": "Fashion", "home": "Home & kitchen", "beauty": "Beauty & personal care", "books": "Books", "sports": "Sports & outdoors",
                   "toys": "Toys & games", "grocery": "Grocery & gourmet", "health": "Health & wellness", "office": "Office & stationery", "software": "Software & digital",
                   "handmade": "Handmade & crafts", "auto": "Automotive", "pets": "Pet supplies", "other": "Other"}
ORDER_STATUSES = ("pending", "placed", "packed", "shipped", "out_for_delivery", "delivered", "cancelled", "returned")  # pending = online payment not finished yet
PAYMENT_STATUSES = ("pending", "paid", "failed", "cod", "collected", "refund_pending", "refunded")  # cod = cash to collect on delivery; refund_pending = the payment service is sending it back (shopai.poll_refunds)
ORDERS_VALID = (f"status IN ({_SQL_LIST(ORDER_STATUSES)}) AND payment_status IN ({_SQL_LIST(PAYMENT_STATUSES)}) AND payment_method IN ('online', 'cod') "
                "AND total_minor = subtotal_minor + shipping_minor AND subtotal_minor > 0 AND commission_minor BETWEEN 0 AND subtotal_minor")
class Shop(Base):
    """A company selling on the marketplace. Opened while creating the company ("do you sell products?") or later from its dashboard by the
    owner / a co-owner; SHOP_MANAGERS run it, its shop staff pack + ship. The platform takes shop.COMMISSION_PERCENT of every successful
    sale (delivered and paid). Deleted with the company; its orders stay on buyers' accounts (orders.company_id SET NULL)."""
    __tablename__ = "shops"
    __table_args__ = (CheckConstraint("status IN ('open', 'paused') AND shipping_minor >= 0 AND (free_over_minor IS NULL OR free_over_minor > 0) AND returns_days BETWEEN 0 AND 60", name="shops_valid"),)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, unique=True)  # its public address (/shop/s/<this>): never the company's id, which is its private join code
    status: Mapped[str] = mapped_column(String(8), default="open", server_default="open")
    currency: Mapped[str] = mapped_column(String(3))  # every price in it, in minor units (paise / cents)
    tagline: Mapped[str] = mapped_column(String(160), default="", server_default="")
    shipping_minor: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")  # flat shipping per order
    free_over_minor: Mapped[int | None] = mapped_column(BigInteger)  # free shipping from this subtotal
    cod: Mapped[bool] = mapped_column(default=False, server_default=text("false"))  # cash on delivery offered
    returns_days: Mapped[int] = mapped_column(SmallInteger, default=7, server_default="7")  # returns accepted this many days after delivery
    ai_tasks: Mapped[bool] = mapped_column(default=True, server_default=text("true"))  # the AI gives shop tasks (pack, restock, reply, re-price) to its staff
    hook_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # live tracking webhook secret (sealed): couriers / aggregators push updates with it
    opened_at: Mapped[datetime] = mapped_column(default=utcnow)
    opened_by: Mapped[str] = mapped_column(String(32), default="", server_default="")
class ShopStaff(Base):  # who packs + ships its orders (and gets the AI's shop tasks), besides the shop managers
    __tablename__ = "shop_staff"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shops.company_id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
class Product(Base):
    """A product for sale. Prices are in its own currency (any of CURRENCIES, the seller picks it: the shop's by default), minor units, tax
    included (tax_rate % of it is tax: GST / VAT); an order charges them in the shop's currency at the day's rate (shop.shop_price). Its photos +
    videos are on disk under HMAC names (shop.photo_file / video_file), nothing about them here. rating_* and sold are kept up to date by
    shop.py; summary = the AI's "customers say" from its reviews. + embedding vector(ASSIST_DIM) (MIGRATIONS): search + similar products."""
    __tablename__ = "products"
    __table_args__ = (CheckConstraint(f"status IN ('draft', 'active', 'archived') AND category IN ({_SQL_LIST(SHOP_CATEGORIES)}) AND price_minor BETWEEN 1 AND 100000000000 "
                                      "AND (mrp_minor IS NULL OR mrp_minor >= price_minor) AND tax_rate BETWEEN 0 AND 50 AND stock >= 0 AND max_per_order BETWEEN 1 AND 100 "
                                      "AND char_length(btrim(title)) >= 3", name="products_valid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(140))
    description: Mapped[str] = mapped_column(String(5000), default="", server_default="")
    highlights: Mapped[str] = mapped_column(String(1500), default="", server_default="")  # one bullet per line
    category: Mapped[str] = mapped_column(String(16))
    brand: Mapped[str] = mapped_column(String(60), default="", server_default="")
    sku: Mapped[str] = mapped_column(String(40), default="", server_default="")
    price_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="", server_default="")  # what price + list price are in (MIGRATIONS: backfilled from the shop, CHECKed)
    mrp_minor: Mapped[int | None] = mapped_column(BigInteger)  # the list price: a discount is shown when it's higher
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0, server_default="0")
    stock: Mapped[int] = mapped_column(default=0, server_default="0")
    max_per_order: Mapped[int] = mapped_column(SmallInteger, default=10, server_default="10")
    status: Mapped[str] = mapped_column(String(8), default="draft", server_default="draft")
    rating_avg: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=0, server_default="0")
    rating_count: Mapped[int] = mapped_column(default=0, server_default="0")
    sold: Mapped[int] = mapped_column(default=0, server_default="0")
    summary: Mapped[str] = mapped_column(String(1200), default="", server_default="")  # the AI's review summary
    summary_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
class Address(Base):
    """A delivery address someone saved: the whole of it encrypted (system.seal, bound to them); city + country in clear for lists."""
    __tablename__ = "addresses"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(30), default="", server_default="")
    data_enc: Mapped[bytes] = mapped_column(LargeBinary)  # JSON: name, phone, line1, line2, city, state, postcode, country
    city: Mapped[str] = mapped_column(String(80))
    country: Mapped[str] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (CheckConstraint("qty BETWEEN 1 AND 100", name="cart_items_qty_valid"),)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    qty: Mapped[int] = mapped_column(SmallInteger)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    add_token: Mapped[str] = mapped_column(String(32), default="", server_default="")  # the last "add to cart" form this line took: the same form sent again counts once (routers/shop.py add_to_cart)
class Order(Base):
    """One company's part of a checkout: what was bought (order_items), how it's paid (online: the platform collects; cash on delivery:
    the seller does), where it goes (sealed snapshot of the address), how it travels (courier, AWB, tracking_events) and the platform's
    commission (commission_percent of the items, fixed when ordered). A "successful transaction" = delivered and paid. Buyer + company
    are SET NULL with name snapshots, so an order outlives either."""
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint(ORDERS_VALID, name="orders_valid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(16), unique=True)  # "DM-7KQ2M9XA": what buyers and sellers see
    checkout: Mapped[str] = mapped_column(String(16), index=True)  # one checkout (a cart across several shops = one order each) is one payment: "CK-…" (the gateway's link id)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    company_name: Mapped[str] = mapped_column(String(64))
    buyer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    buyer_name: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    payment_method: Mapped[str] = mapped_column(String(6))
    payment_status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    payment_provider: Mapped[str] = mapped_column(String(10), default="", server_default="")  # test / cashfree ("" for cash on delivery)
    payment_ref: Mapped[str] = mapped_column(String(64), default="", server_default="")
    payment_url: Mapped[str] = mapped_column(String(500), default="", server_default="")
    currency: Mapped[str] = mapped_column(String(3))
    subtotal_minor: Mapped[int] = mapped_column(BigInteger)  # the items, tax included
    shipping_minor: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    tax_minor: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")  # the tax inside subtotal (statements)
    total_minor: Mapped[int] = mapped_column(BigInteger)
    commission_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    commission_minor: Mapped[int] = mapped_column(BigInteger)  # the platform's cut of the items: earned once delivered + paid, gone if returned
    ship_to_enc: Mapped[bytes] = mapped_column(LargeBinary)  # sealed JSON snapshot of the address
    ship_city: Mapped[str] = mapped_column(String(80), default="", server_default="")
    ship_country: Mapped[str] = mapped_column(String(2), default="", server_default="")
    courier: Mapped[str] = mapped_column(String(16), default="", server_default="")  # shipping.COURIERS key, or "custom"
    courier_name: Mapped[str] = mapped_column(String(60), default="", server_default="")  # custom couriers / own delivery: who
    courier_phone: Mapped[str] = mapped_column(String(20), default="", server_default="")
    awb: Mapped[str] = mapped_column(String(40), default="", server_default="")  # the tracking / AWB number
    tracking_url: Mapped[str] = mapped_column(String(500), default="", server_default="")  # the exact link, when the seller has one
    delivery_code_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # the buyer's 6-digit code: the courier / delivery person types it to mark it delivered
    courier_token: Mapped[str | None] = mapped_column(String(43), unique=True)  # the delivery person's link (no login): /shop/deliver/<token>
    delivered_how: Mapped[str] = mapped_column(String(8), default="", server_default="")  # code / buyer / photo / webhook
    cancel_reason: Mapped[str] = mapped_column(String(300), default="", server_default="")
    return_reason: Mapped[str] = mapped_column(String(500), default="", server_default="")
    refund_ref: Mapped[str] = mapped_column(String(64), default="", server_default="")
    refund_tries: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")  # refunds the payment service turned down (shopai.poll_refunds): each try has its own refund id
    refund_error: Mapped[str] = mapped_column(String(200), default="", server_default="")  # why the last one was turned down: "" = none
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    paid_at: Mapped[datetime | None]
    shipped_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    cancelled_at: Mapped[datetime | None]
    return_requested_at: Mapped[datetime | None]
    return_declined_at: Mapped[datetime | None]  # the seller said no: that's their one answer, and it's off the waiting list
    returned_at: Mapped[datetime | None]
    refunded_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
class OrderItem(Base):  # snapshots: the product can change or go away, the order can't
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(String(140))
    sku: Mapped[str] = mapped_column(String(40), default="", server_default="")
    category: Mapped[str] = mapped_column(String(16), default="other", server_default="other")
    qty: Mapped[int] = mapped_column(SmallInteger)
    price_minor: Mapped[int] = mapped_column(BigInteger)  # each, tax included
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    tax_minor: Mapped[int] = mapped_column(BigInteger)  # the tax inside price x qty
class TrackingEvent(Base):  # one step of a delivery, shown to the buyer as it happens
    __tablename__ = "tracking_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20))  # shipping.EVENTS key
    note: Mapped[str] = mapped_column(String(300), default="", server_default="")
    location: Mapped[str] = mapped_column(String(120), default="", server_default="")
    source: Mapped[str] = mapped_column(String(8))  # seller / courier / webhook / buyer / system
    by_name: Mapped[str] = mapped_column(String(60), default="", server_default="")
    at: Mapped[datetime] = mapped_column(default=utcnow)
class ProductReview(Base):
    """A buyer's rating (1-5) + review of a product they received; photos + a video on disk (shop.review_photo / review_video). One per
    product per person (edit it to change your mind); the seller may reply once (editable)."""
    __tablename__ = "reviews_shop"
    __table_args__ = (CheckConstraint("rating BETWEEN 1 AND 5 AND char_length(btrim(body)) >= 3", name="reviews_shop_valid"),
                      Index("reviews_shop_one_each", "product_id", "user_id", unique=True, postgresql_where=text("user_id IS NOT NULL")))
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))  # the delivered order it's about: "verified purchase"
    author: Mapped[str] = mapped_column(String(32))
    rating: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str] = mapped_column(String(120), default="", server_default="")
    body: Mapped[str] = mapped_column(String(3000))
    reply: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    reply_by: Mapped[str] = mapped_column(String(32), default="", server_default="")
    replied_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
class ProductQuestion(Base):
    """A shopper's question about a product: the AI answers it from the listing, the reviews and the seller's answers (shopai.py), and
    says when it can't; the seller can answer too (asked to when the AI couldn't). Seller-answered questions are shown to everyone."""
    __tablename__ = "product_questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    asker: Mapped[str] = mapped_column(String(32))
    question: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(8), default="pending", server_default="pending")  # pending / done / failed (the AI's answer) / spam (the AI says it isn't a question about the product: the shop never sees it)
    answer: Mapped[str] = mapped_column(String(2000), default="", server_default="")  # the AI's
    confidence: Mapped[str] = mapped_column(String(8), default="", server_default="")  # high / medium / low
    sources: Mapped[str] = mapped_column(String(120), default="", server_default="")  # what the answer comes from: listing, reviews, seller answers…
    ask_seller: Mapped[bool] = mapped_column(default=False, server_default=text("false"))  # the AI couldn't tell: the seller was asked
    seller_answer: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    answered_by: Mapped[str] = mapped_column(String(32), default="", server_default="")
    answered_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class CompetitorLink(Base):  # a competitor's listing of a similar product (any website): its price + rating fetched daily (shopai.py)
    __tablename__ = "competitor_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(500))
    label: Mapped[str] = mapped_column(String(80), default="", server_default="")
    price_minor: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="", server_default="")
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    reviews: Mapped[int | None]
    how: Mapped[str] = mapped_column(String(8), default="", server_default="")  # how the price was read: jsonld / meta / ai
    error: Mapped[str] = mapped_column(String(200), default="", server_default="")
    checked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class PriceSnapshot(Base):  # one price a day per product: its own, each similar product on the marketplace, each competitor link (the price-history chart)
    __tablename__ = "price_snapshots"
    __table_args__ = (UniqueConstraint("product_id", "source", "ref", "day", name="price_snapshots_one_a_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(8))  # own / market / link
    ref: Mapped[int] = mapped_column(default=0, server_default="0")  # the similar product's id / the link's id (0 = own)
    label: Mapped[str] = mapped_column(String(140), default="", server_default="")
    price_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    reviews: Mapped[int | None]
    day: Mapped[date]
class ProductInsight(Base):  # the AI's latest pricing + quality brief for one product (shopai.py), with its suggestions
    __tablename__ = "product_insights"
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(8))  # running / done / error
    data: Mapped[str] = mapped_column(String(8000), default="", server_default="")  # JSON
    error: Mapped[str] = mapped_column(String(300), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class Settlement(Base):
    """Money moved between the platform and a seller: the platform pays out online sales minus its commission ("to_seller"), a seller pays
    the commission on cash-on-delivery sales ("to_platform"). Recorded by a platform admin (shop.PLATFORM_ADMINS) with the bank reference."""
    __tablename__ = "settlements"
    __table_args__ = (CheckConstraint("direction IN ('to_seller', 'to_platform') AND amount_minor > 0 AND char_length(reference) >= 4", name="settlements_valid"),
                      Index("settlements_one_reference", "company_id", "reference", unique=True))
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id", ondelete="SET NULL"), index=True)
    company_name: Mapped[str] = mapped_column(String(64))
    direction: Mapped[str] = mapped_column(String(12))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    reference: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(String(300), default="", server_default="")
    by_name: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(default=utcnow)
class ShopTask(Base):
    """A task the shop's AI wants done (pack an order, restock, reply to a review, answer a question, re-price): given to the least busy of
    the shop's staff as a normal task (tables.Task, "Deminic AI") once it's working time there; key = one per thing."""
    __tablename__ = "shop_tasks"
    __table_args__ = (UniqueConstraint("company_id", "key", name="shop_tasks_one_each"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(2000), default="", server_default="")
    due_hours: Mapped[int] = mapped_column(SmallInteger, default=8, server_default="8")  # working hours to do it in
    status: Mapped[str] = mapped_column(String(8), default="pending", server_default="pending")  # pending / given / skipped
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    note: Mapped[str] = mapped_column(String(200), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    given_at: Mapped[datetime | None]
async def left_company(db: AsyncSession, company_id: uuid.UUID, user_id: int) -> None:
    """Someone is out of a company (they left, were removed or banned; the caller removes the membership): their pending leave is withdrawn, they're off the shop's staff,
    an ownership offer to them and the AI's open call on them lapse, and the time away starts for its chats."""
    who = lambda t: (t.company_id == company_id, t.user_id == user_id)
    await db.execute(update(LeaveRequest).where(*who(LeaveRequest), LeaveRequest.status == "pending").values(status="cancelled"))
    await db.execute(update(PromotionSuggestion).where(*who(PromotionSuggestion), PromotionSuggestion.status == "suggested").values(status="stale"))
    await db.execute(delete(ShopStaff).where(*who(ShopStaff)))
    await db.execute(delete(OwnershipOffer).where(OwnershipOffer.company_id == company_id, OwnershipOffer.to_user_id == user_id))
    db.add(AwayPeriod(company_id=company_id, user_id=user_id))
async def back_in_company(db: AsyncSession, company_id: uuid.UUID, user_id: int) -> None:  # they joined again: the time away ends, and what was said in it isn't unread (they can't open it)
    now = utcnow()
    await db.execute(update(AwayPeriod).where(AwayPeriod.company_id == company_id, AwayPeriod.user_id == user_id, AwayPeriod.back_at.is_(None)).values(back_at=now))
    await db.execute(update(ChatMember).where(ChatMember.user_id == user_id, ChatMember.chat_id.in_(select(Chat.id).where(Chat.company_id == company_id))).values(last_read_at=now))
async def purge_expired_messages() -> int:
    """Deleted accounts' history past MESSAGE_RETENTION_DAYS: archived job offers, chat and meeting messages, then chats nobody is left in."""
    async with Session() as db:
        now = utcnow()
        files = (await db.execute(select(ChatMessage.chat_id, ChatMessage.id).where(ChatMessage.purge_after < now))).all()  # the files sent with them go too (after the commit)
        n = (await db.execute(delete(MessageArchive).where(MessageArchive.purge_after < now))).rowcount
        n += (await db.execute(delete(ChatMessage).where(ChatMessage.purge_after < now))).rowcount
        n += (await db.execute(delete(MeetingLine).where(MeetingLine.purge_after < now))).rowcount
        emptied = (await db.execute(text("""DELETE FROM chats c WHERE NOT EXISTS (SELECT 1 FROM chat_messages m WHERE m.chat_id = c.id)
                                 AND (SELECT count(*) FROM chat_members cm WHERE cm.chat_id = c.id) < (CASE WHEN c.kind IN ('direct', 'personal') THEN 2 ELSE 1 END) RETURNING c.id"""))).scalars().all()
        await db.commit()
    for c, i in files: chat_file(c, i).unlink(missing_ok=True)
    remove_chat_files(emptied)
    return n
class PendingSignup(Base):
    """A signup waiting for its email + phone codes; it only becomes a User once both are verified. Codes are stored as HMACs,
    never in plain text. id = sha256 of the random token kept in the visitor's session (a DB leak can't resume a signup)."""
    __tablename__ = "pending_signups"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(32))
    email: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(20))
    dob: Mapped[date | None]
    region: Mapped[str | None] = mapped_column(String(2))
    timezone: Mapped[str | None] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(200))
    ip: Mapped[str] = mapped_column(String(45), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    taken: Mapped[str] = mapped_column(String(16), default="", server_default="")  # "email" / "phone" / "email phone": already in use, so no code exists for it and this signup can't finish; signup looks the same either way (routers/auth.py)
    email_code: Mapped[str] = mapped_column(String(64), default="")
    email_expires_at: Mapped[datetime] = mapped_column(default=utcnow)
    email_attempts: Mapped[int] = mapped_column(default=0)
    email_sends: Mapped[int] = mapped_column(default=0)
    email_sent_at: Mapped[datetime] = mapped_column(default=utcnow)
    email_verified: Mapped[bool] = mapped_column(default=False)
    phone_code: Mapped[str] = mapped_column(String(64), default="")
    phone_expires_at: Mapped[datetime] = mapped_column(default=utcnow)
    phone_attempts: Mapped[int] = mapped_column(default=0)
    phone_sends: Mapped[int] = mapped_column(default=0)
    phone_sent_at: Mapped[datetime] = mapped_column(default=utcnow)
    phone_verified: Mapped[bool] = mapped_column(default=False)
class AbuseEvent(Base):
    """Spam + limit + security events (user asked: the admin sees who exceeds the limits often): spam.py strikes and mutes, the request rate
    limiter's refusals, wrong admin logins, bans. Written by abuse.py in the background; read by the admin console and its AI review. 30 days."""
    __tablename__ = "abuse_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    ip: Mapped[str] = mapped_column(String(45), default="", server_default="", index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)  # chat-strike, chat-mute, ai-strike, ai-mute, shopai-strike, shopai-mute, rate-limit, admin-login, banned, ip-banned
    detail: Mapped[str] = mapped_column(String(300), default="", server_default="")
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
class AIWarning(Base):
    """What the local AI said about the abuse events (routers/admin.py, user asked: "the AI must warn"): one row per warning, newest first on
    the admin console. who = a username or an IP; level = low / medium / high."""
    __tablename__ = "ai_warnings"
    __table_args__ = (CheckConstraint("level IN ('low', 'medium', 'high')", name="ai_warnings_level_valid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    who: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(6))
    text: Mapped[str] = mapped_column(String(600))
    action: Mapped[str] = mapped_column(String(200), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
class ContactChange(Base):
    """A new email or phone number waiting for its code (routers/settings.py, user asked): the account keeps the old one until the 6-digit
    code sent to the NEW one comes back (CODE_TTL_MIN = 10 minutes, CODE_ATTEMPTS tries; only its HMAC is stored). One per account + channel."""
    __tablename__ = "contact_changes"
    __table_args__ = (CheckConstraint("channel IN ('email', 'phone')", name="contact_changes_valid"),)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    channel: Mapped[str] = mapped_column(String(5), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))  # the new email / phone (E.164)
    code: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)
    sends: Mapped[int] = mapped_column(default=1)
    sent_at: Mapped[datetime] = mapped_column(default=utcnow)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
class AccountRecovery(Base):
    """Someone who forgot their username or password (routers/auth.py, /forgot): a 6-digit code sent to the account's own email or phone,
    good for CODE_TTL_MIN (10) minutes and CODE_ATTEMPTS wrong tries; only its HMAC is stored. id = sha256 of the random token kept in the
    visitor's session (a DB leak can't resume one). No account matches: a row all the same (user_id NULL, nothing sent, no code works), so
    the answer never tells anyone who has an account."""
    __tablename__ = "account_recoveries"
    __table_args__ = (CheckConstraint("kind IN ('username', 'password') AND channel IN ('email', 'phone')", name="account_recoveries_valid"),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    channel: Mapped[str] = mapped_column(String(5))
    code: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)
    sends: Mapped[int] = mapped_column(default=1)
    sent_at: Mapped[datetime] = mapped_column(default=utcnow)
    ip: Mapped[str] = mapped_column(String(45), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
ASSIST_DIM = 256  # size of the instruction-bar embeddings (assist.py) stored with pgvector
MIGRATIONS = [  # idempotent: create_all() only creates missing tables, so columns added later to existing tables go here
    "CREATE EXTENSION IF NOT EXISTS vector",  # pgvector (needs the pgvector/pgvector Postgres image)
    f"CREATE TABLE IF NOT EXISTS assist_examples (id serial PRIMARY KEY, intent varchar(32) NOT NULL, phrase varchar(200) NOT NULL UNIQUE, embedding vector({ASSIST_DIM}) NOT NULL)",
    "CREATE INDEX IF NOT EXISTS assist_examples_embedding_hnsw ON assist_examples USING hnsw (embedding vector_cosine_ops)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(20)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_verified_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS ip_created INET",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS ip_last_login INET",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS login_id UUID",
    "CREATE UNIQUE INDEX IF NOT EXISTS users_phone_key ON users (phone)",
    "CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_key ON users (lower(email))",
    "CREATE UNIQUE INDEX IF NOT EXISTS company_members_one_owner ON company_members (company_id) WHERE position = 'owner'",  # exactly one owner
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS description VARCHAR(1000) NOT NULL DEFAULT ''",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS hiring VARCHAR(1000) NOT NULL DEFAULT ''",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS listed BOOLEAN NOT NULL DEFAULT false",  # existing companies stay private until an owner lists them
    "ALTER TABLE companies ALTER COLUMN listed SET DEFAULT false",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS allow_other_companies BOOLEAN NOT NULL DEFAULT true",  # existing companies keep today's behaviour
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS discoverable BOOLEAN NOT NULL DEFAULT false",  # nobody becomes searchable without ticking it
    f"ALTER TABLE profiles ADD COLUMN IF NOT EXISTS embedding vector({ASSIST_DIM})",  # (before its index: on a brand-new database the column isn't there yet)
    "CREATE INDEX IF NOT EXISTS profiles_embedding_hnsw ON profiles USING hnsw (embedding vector_cosine_ops)",
    "ALTER TABLE approaches ADD COLUMN IF NOT EXISTS message VARCHAR(1000) NOT NULL DEFAULT ''",  # approaches got a message + salary offer
    "ALTER TABLE approaches ADD COLUMN IF NOT EXISTS salary_amount BIGINT",
    "ALTER TABLE approaches ADD COLUMN IF NOT EXISTS salary_currency VARCHAR(3)",
    "ALTER TABLE approaches ADD COLUMN IF NOT EXISTS salary_period VARCHAR(5)",
    "ALTER TABLE approaches ADD COLUMN IF NOT EXISTS seen_at TIMESTAMP",
    "ALTER TABLE approaches ADD COLUMN IF NOT EXISTS dismissed_at TIMESTAMP",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS salary_currency VARCHAR(3)",  # existing companies: NULL until an owner sets salaries
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS salary_period VARCHAR(5)",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS website VARCHAR(200) NOT NULL DEFAULT ''",  # website, registration number, verified location
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS registration_type VARCHAR(8)",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS registration_number VARCHAR(40)",
    "CREATE UNIQUE INDEX IF NOT EXISTS companies_registration_key ON companies (registration_type, registration_number) WHERE registration_number IS NOT NULL",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS location_place_id VARCHAR(300)",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS location_address VARCHAR(300) NOT NULL DEFAULT ''",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS location_lat DOUBLE PRECISION",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS location_lng DOUBLE PRECISION",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS location_country VARCHAR(2)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS dob DATE",  # date of birth (signup: 18+ only)
    "ALTER TABLE pending_signups ADD COLUMN IF NOT EXISTS dob DATE",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS want_min BIGINT",  # the salary range a person wants
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS want_max BIGINT",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS want_currency VARCHAR(3)",
    "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS want_period VARCHAR(5)",
    f"ALTER TABLE companies ADD COLUMN IF NOT EXISTS embedding vector({ASSIST_DIM})",
    "CREATE INDEX IF NOT EXISTS companies_embedding_hnsw ON companies USING hnsw (embedding vector_cosine_ops)",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS timezone VARCHAR(64)",  # working hours (existing companies: 09:00-17:00, Mon-Fri)
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS work_start TIME NOT NULL DEFAULT '09:00'",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS work_end TIME NOT NULL DEFAULT '17:00'",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS work_days SMALLINT NOT NULL DEFAULT 31",
    "ALTER TABLE company_members ADD COLUMN IF NOT EXISTS salary BIGINT",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS attendance VARCHAR(300) NOT NULL DEFAULT ''",
    "ALTER TABLE payouts ALTER COLUMN attendance TYPE VARCHAR(300)",  # was 120: too short once unpaid leave is noted
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS company_code VARCHAR(36) NOT NULL DEFAULT ''",  # the company id on statements, kept after deletion
    "UPDATE payouts SET company_code = company_id::text WHERE company_code = '' AND company_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS member_requests_one_open ON member_requests (company_id, user_id, kind) WHERE status = 'pending'",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS from_bank VARCHAR(100) NOT NULL DEFAULT ''",  # company bank account + net worth
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS from_last4 VARCHAR(4) NOT NULL DEFAULT ''",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS net_worth BIGINT",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS net_worth_updated_at TIMESTAMP",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS net_worth_by VARCHAR(32) NOT NULL DEFAULT ''",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS liability_since TIMESTAMP",  # payroll guard
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS halted_at TIMESTAMP",
    "ALTER TABLE notices DROP CONSTRAINT IF EXISTS notices_company_id_fkey",  # notices outlive their company (its deletion is announced with one)
    "ALTER TABLE notices ADD CONSTRAINT notices_company_id_fkey FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE SET NULL",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS exact_address VARCHAR(300) NOT NULL DEFAULT 'xyz'",  # required now: existing companies get the placeholder "xyz" (user asked)
    "ALTER TABLE companies ALTER COLUMN exact_address SET DEFAULT ''",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS id_proof_type VARCHAR(16)",  # identity proofs + bank verification
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS id_proof_at TIMESTAMP",
    *(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col}" for t in ("bank_accounts", "company_bank_accounts") for col in ("verified_at TIMESTAMP", "verified_name VARCHAR(100) NOT NULL DEFAULT ''")),
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS net_worth_currency VARCHAR(3)",  # the net worth in its own currency (fx.py converts)
    "UPDATE companies SET net_worth_currency = salary_currency WHERE net_worth_currency IS NULL AND net_worth IS NOT NULL AND salary_currency IS NOT NULL",  # pinned: a later salary change doesn't re-denominate it
    "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS worth_currency VARCHAR(3) NOT NULL DEFAULT ''",
    "ALTER TABLE deposits ADD COLUMN IF NOT EXISTS credited BIGINT NOT NULL DEFAULT 0",
    "UPDATE deposits SET worth_currency = currency, credited = amount WHERE worth_currency = ''",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS terms VARCHAR(5000) NOT NULL DEFAULT ''",  # terms and conditions, accepted before joining
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS terms_version INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS terms_updated_at TIMESTAMP",
    *(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col}" for t in ("company_members", "member_requests") for col in ("terms_version INTEGER", "terms_accepted_at TIMESTAMP")),
    *(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col}" for t in ("users", "pending_signups") for col in ("region VARCHAR(2)", "timezone VARCHAR(64)")),  # currency, holidays, time zone
    "UPDATE users SET timezone = 'Asia/Kolkata' WHERE region = 'IN' AND timezone IS NULL",  # set before time zones were asked: the only one there
    *(f"ALTER TABLE identity_proofs ADD COLUMN IF NOT EXISTS {col}" for col in ("verified_at TIMESTAMP", "verified_by VARCHAR(32) NOT NULL DEFAULT ''", "verified_name VARCHAR(100) NOT NULL DEFAULT ''")),
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS notes_upto INTEGER NOT NULL DEFAULT 0",  # meetings: the last line the AI's notes cover
    "ALTER TABLE meetings ADD COLUMN IF NOT EXISTS beat_at TIMESTAMP",  # ... and the live room's heartbeat
    *(f"ALTER TABLE {t} {s}" for t in ("bank_accounts", "company_bank_accounts") for s in (  # bank accounts in any country (banks.py): ifsc holds any local bank code now
      "ADD COLUMN IF NOT EXISTS country VARCHAR(2) NOT NULL DEFAULT 'IN'", "ADD COLUMN IF NOT EXISTS swift VARCHAR(11) NOT NULL DEFAULT ''",
      "ADD COLUMN IF NOT EXISTS bank_address VARCHAR(200) NOT NULL DEFAULT ''", "ALTER COLUMN ifsc TYPE VARCHAR(20)", "ALTER COLUMN ifsc SET DEFAULT ''")),
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS bank_country VARCHAR(2) NOT NULL DEFAULT 'IN'", "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS swift VARCHAR(11) NOT NULL DEFAULT ''",
    "ALTER TABLE payouts ALTER COLUMN ifsc TYPE VARCHAR(20)",
    f"ALTER TABLE products ADD COLUMN IF NOT EXISTS embedding vector({ASSIST_DIM})",  # the shop: search + similar products (shop.py)
    "CREATE INDEX IF NOT EXISTS products_embedding_hnsw ON products USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS products_listing ON products (status, category, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS products_words ON products USING gin ((to_tsvector('english', title || ' ' || brand || ' ' || highlights || ' ' || left(description, 1500))))",  # = shop.WORDS
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS currency VARCHAR(3) NOT NULL DEFAULT ''",  # each product's own currency (user asked: not only the shop's)
    "UPDATE products p SET currency = s.currency FROM shops s WHERE s.company_id = p.company_id AND p.currency = ''",
    "UPDATE products p SET currency = COALESCE(c.salary_currency, 'INR') FROM companies c WHERE c.id = p.company_id AND p.currency = ''",  # (no shop row: never empty, or the CHECK below fails)
    "ALTER TABLE products DROP CONSTRAINT IF EXISTS products_currency_valid", f"ALTER TABLE products ADD CONSTRAINT products_currency_valid CHECK (currency IN ({_SQL_LIST(CURRENCIES)}))",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS checkout VARCHAR(16) NOT NULL DEFAULT ''", "CREATE INDEX IF NOT EXISTS ix_orders_checkout ON orders (checkout)",
    "ALTER TABLE cart_items ADD COLUMN IF NOT EXISTS add_token VARCHAR(32) NOT NULL DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS product_questions_waiting ON product_questions (created_at) WHERE status = 'pending'",  # the shopping assistant's queue, on every question asked: only what's still waiting is looked at
    "CREATE INDEX IF NOT EXISTS notices_unread ON notices (user_id) WHERE seen_at IS NULL",  # the bell's count, on every page: however long someone's history is, only what's unread is looked at
    "ALTER TABLE chats ALTER COLUMN company_id DROP NOT NULL", "ALTER TABLE chats ALTER COLUMN kind TYPE VARCHAR(8)", "ALTER TABLE chats DROP CONSTRAINT IF EXISTS chats_kind_valid",  # personal chats (no company)
    "ALTER TABLE chats ADD CONSTRAINT chats_kind_valid CHECK (kind IN ('direct', 'group', 'personal', 'pgroup', 'enquiry') AND (kind IN ('personal', 'pgroup')) = (company_id IS NULL))",  # + personal groups, enquiries
    "CREATE UNIQUE INDEX IF NOT EXISTS chats_one_personal ON chats (direct_key) WHERE kind = 'personal'",
    "CREATE UNIQUE INDEX IF NOT EXISTS chats_one_enquiry ON chats (company_id, direct_key) WHERE kind = 'enquiry'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS language VARCHAR(5)", "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_at TIMESTAMP",  # the UI language; admin bans
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_reason VARCHAR(500) NOT NULL DEFAULT ''", "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_by VARCHAR(64) NOT NULL DEFAULT ''",
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS salary_currency VARCHAR(3) NOT NULL DEFAULT ''", "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS counted BIGINT NOT NULL DEFAULT 0",  # salaries paid in any currency
    "ALTER TABLE payouts ADD COLUMN IF NOT EXISTS fx_rate NUMERIC(20, 10)", "UPDATE payouts SET salary_currency = currency, counted = gross WHERE salary_currency = ''",  # (older ones were all in it)
    "CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower ON users (lower(username))",  # usernames unique whatever their capital letters (user asked)
    "INSERT INTO site_settings (key, value, updated_at) VALUES ('single_company', 'off', now() AT TIME ZONE 'utc') ON CONFLICT (key) DO NOTHING",  # the admin's switch (off at first)
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS ai_promotions VARCHAR(8) NOT NULL DEFAULT 'off'", "ALTER TABLE companies ADD COLUMN IF NOT EXISTS ai_reviewed_at TIMESTAMP",  # the company AI
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS hours_changed_at TIMESTAMP",  # working hours change once every HOURS_EVERY_DAYS (existing companies: free until their first change)
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS max_members INTEGER",  # the employee limit (existing companies: none until their next save, which asks for one)
    "ALTER TABLE company_members ADD COLUMN IF NOT EXISTS position_since TIMESTAMP",
    "ALTER TABLE payouts DROP CONSTRAINT IF EXISTS payouts_one_per_cycle", "CREATE UNIQUE INDEX IF NOT EXISTS payouts_one_per_period ON payouts (company_id, user_id, cycle, period_start)",  # someone who left and came back counts their cycles from 1 again
    "ALTER TABLE company_members ADD COLUMN IF NOT EXISTS pay_history JSONB NOT NULL DEFAULT '[]'",  # a new pay starts with the next cycle (payroll.keep_pay)
    "ALTER TABLE shops ADD COLUMN IF NOT EXISTS public_id UUID NOT NULL DEFAULT gen_random_uuid()", "CREATE UNIQUE INDEX IF NOT EXISTS shops_public_id_key ON shops (public_id)",  # a shop's address isn't its company's join code
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS return_declined_at TIMESTAMP", "ALTER TABLE orders ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMP",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS refund_tries SMALLINT NOT NULL DEFAULT 0", "ALTER TABLE orders ADD COLUMN IF NOT EXISTS refund_error VARCHAR(200) NOT NULL DEFAULT ''",  # refunds through the payment service
    "ALTER TABLE orders ALTER COLUMN payment_status TYPE VARCHAR(16)", "ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_valid", f"ALTER TABLE orders ADD CONSTRAINT orders_valid CHECK ({ORDERS_VALID})",
    "ALTER TABLE pending_signups ADD COLUMN IF NOT EXISTS taken VARCHAR(16) NOT NULL DEFAULT ''",  # signup doesn't say who has an account
    "CREATE UNIQUE INDEX IF NOT EXISTS settlements_one_reference ON settlements (company_id, reference)",  # a deleted company's rows have no company_id, so they never clash
    "UPDATE leave_requests l SET status = 'cancelled' WHERE status = 'pending' AND NOT EXISTS (SELECT 1 FROM company_members m WHERE m.company_id = l.company_id AND m.user_id = l.user_id)",  # left before leaving withdrew them
    *(stmt for (table, name), sql in CONSTRAINTS.items()
      for stmt in (f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}", f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({sql})")),
]
def _app_role_sql() -> list[str]:
    """Create/refresh the least-privilege login the app runs as: rows only (SELECT/INSERT/UPDATE/DELETE), nothing else."""
    role, pw, db = DATABASE_URL.username or "", DATABASE_URL.password or "", DATABASE_URL.database or ""
    if role == ADMIN_DATABASE_URL.username: raise RuntimeError("POSTGRES_APP_USER must differ from POSTGRES_USER: the app must not run as the superuser")
    if not (re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role) and re.fullmatch(r"[A-Za-z0-9]{24,128}", pw) and re.fullmatch(r"\w{1,63}", db)):
        raise RuntimeError("POSTGRES_APP_USER: lowercase letters/digits/_; POSTGRES_APP_PASSWORD: 24+ letters/digits")  # validated: DDL can't take bind params
    return [f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN CREATE ROLE {role}; END IF; END $$",
            f"ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 50 PASSWORD '{pw}'",
            f'REVOKE ALL ON DATABASE "{db}" FROM PUBLIC', f'GRANT CONNECT ON DATABASE "{db}" TO {role}',
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC", f"REVOKE CREATE ON SCHEMA public FROM {role}", f"GRANT USAGE ON SCHEMA public TO {role}",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}", f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}"]
async def setup_database() -> None:
    """At startup, as the admin: create tables, run MIGRATIONS, (re)grant the app role. The admin connection is closed afterwards."""
    admin = create_async_engine(ADMIN_DATABASE_URL, poolclass=NullPool, connect_args=_CONNECT)
    try:
        async with admin.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            for sql in MIGRATIONS + _app_role_sql(): await conn.execute(text(sql))
    finally: await admin.dispose()
def _summary_query():
    top = Membership.position.in_(TOP)
    agg = lambda col: func.array_agg(aggregate_order_by(col, Company.name, Company.id)).filter(top)  # same order in every array
    unread = (select(func.count()).where(Approach.user_id == User.id, Approach.seen_at.is_(None), Approach.dismissed_at.is_(None)).correlate(User).scalar_subquery()
              + select(func.count()).where(Payout.user_id == User.id, Payout.seen_at.is_(None)).correlate(User).scalar_subquery()
              + select(func.count()).where(Notice.user_id == User.id, Notice.seen_at.is_(None)).correlate(User).scalar_subquery())
    seat = aliased(Membership)  # still in that company: someone who left keeps their rows, but those tasks and chats aren't theirs to open any more
    tasks = (select(func.count()).select_from(TaskAssignee).join(Task, Task.id == TaskAssignee.task_id)  # tasks to do (the sidebar's tasks link)
             .join(seat, (seat.company_id == Task.company_id) & (seat.user_id == TaskAssignee.user_id))
             .where(TaskAssignee.user_id == User.id, TaskAssignee.done_at.is_(None), Task.status == "open").correlate(User).scalar_subquery())
    live = (select(func.count()).select_from(MeetingInvite).join(Meeting, Meeting.id == MeetingInvite.meeting_id)  # meetings you're invited to that are on now
            .where(MeetingInvite.user_id == User.id, Meeting.status == "live").correlate(User).scalar_subquery())
    opens = or_(Chat.company_id.is_(None), Chat.direct_key == cast(User.id, String), select(seat.user_id).where(seat.user_id == User.id, seat.company_id == Chat.company_id).correlate(User, Chat).exists())  # personal, their own enquiry, or a company they're in
    chats = (select(func.count(ChatMessage.id)).join(ChatMember, (ChatMember.chat_id == ChatMessage.chat_id) & (ChatMember.user_id == User.id)).join(Chat, Chat.id == ChatMessage.chat_id)  # unread chat messages
             .where(ChatMessage.sender_id.is_distinct_from(User.id), ChatMessage.created_at > func.coalesce(ChatMember.last_read_at, ChatMember.added_at), opens).correlate(User).scalar_subquery())
    tracked = func.count(Membership.company_id).filter(Membership.position.not_in((*TOP, FREELANCE)))
    bank = select(BankAccount.user_id).where(BankAccount.user_id == User.id).correlate(User).exists()
    identity = select(IdentityProof.user_id).where(IdentityProof.user_id == User.id).correlate(User).exists()
    real = select(IdentityProof.name_on_id).where(IdentityProof.user_id == User.id).correlate(User).scalar_subquery()  # for the activity log (audit.py)
    asked = (select(func.count()).select_from(Friendship).where(Friendship.status == "pending", Friendship.requested_by != User.id,  # friend requests waiting for them
                                                               (Friendship.low_id == User.id) | (Friendship.high_id == User.id)).correlate(User).scalar_subquery())
    return (select(User.username, User.login_id, func.count(Membership.company_id), agg(Company.id), agg(Company.name), agg(Company.salary_currency.is_not(None)),
                   agg(Company.id_proof_at.is_not(None)), unread, tracked, bank, identity, chats, tasks, User.region, User.timezone, live, real, asked,
                   User.banned_at.is_not(None), User.language)
            .outerjoin(Membership, Membership.user_id == User.id).outerjoin(Company, Company.id == Membership.company_id)
            .where(User.id == bindparam("uid")).group_by(User.id))
SUMMARY = _summary_query()  # built once: it runs on every request, and building it each time was a quarter of a page's CPU (the load test)
async def account_summary(user_id: int) -> dict | None:
    """{"username", "login_id", "companies", "top", "unread", "monitored", "bank"} for a logged-in user straight from Postgres in one
    query (runs on every request), or None if the account no longer exists. top = ((company id, name, has salaries), ...) of the
    companies they own or co-own; unread = job offers + salary payouts they haven't seen yet (the notifications bell); monitored =
    workplaces where their attendance is tracked; bank = they've added bank details."""
    async with Session() as db: row = (await db.execute(SUMMARY, {"uid": user_id})).first()
    return {"username": row[0], "login_id": row[1], "companies": row[2], "top": tuple(zip(row[3] or (), row[4] or (), row[5] or (), row[6] or ())), "unread": row[7],
            "monitored": row[8], "bank": row[9], "identity": row[10], "chats": row[11], "tasks": row[12], "region": row[13], "timezone": row[14], "live": row[15], "real": row[16],
            "friend_requests": row[17], "banned": row[18], "language": row[19]} if row else None
