"""Live presence + attendance. Every open tab of a logged-in user keeps one WebSocket to /ws/presence (routers/presence.py) and says
whether it's in use ("active": visible and touched in the last few minutes) or not ("idle"). This hub lives in this process's memory
(not Redis: Redis holds login sessions only). It knows who is active / idle / offline, pushes every change to the company dashboards
showing that person, and every TICK_S credits TICK_S seconds of attendance to each active member's monitored workplaces that are
inside working hours right now, on that company's clock. Credits are batched: one upsert per tick for everyone, capped at
MAX_WORK_HOURS a day by the database. Owners, co-owners and freelancers are never credited, and freelancers' presence isn't shown.
Someone tracked (staff, in working hours) who goes idle gets an "idle" message on every tab, which warns them; "active" when they're back.
Every RECHECK_S the hub re-reads everyone's memberships (promotions, leaving) and closes sockets whose login session has ended."""
import asyncio, os, time, uuid
from collections import namedtuple
from datetime import UTC, datetime
from fastapi import WebSocket
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from payroll import company_tz, in_hours
from system import reddisdb
from tables import FREELANCE, MAX_WORK_HOURS, Attendance, Company, Membership, Session, monitored, utcnow
TICK_S = max(1, int(os.getenv("PRESENCE_TICK_S", "30")))  # tests shorten it
RECHECK_S = int(os.getenv("PRESENCE_RECHECK_S", "300"))
DAY_CAP = MAX_WORK_HOURS * 3600
Seat = namedtuple("Seat", "company_id position timezone location_country work_start work_end work_days halted_at name")  # one membership + its company's clock
class Hub:
    def __init__(self):
        self.sockets: dict[int, dict[WebSocket, bool]] = {}  # user id -> {socket: in use}
        self.session: dict[WebSocket, str] = {}  # socket -> its Redis session key
        self.seats: dict[int, list[Seat]] = {}  # connected user -> their memberships
        self.watchers: dict[uuid.UUID, set[WebSocket]] = {}  # company -> dashboards showing it
        self.watching: dict[WebSocket, uuid.UUID] = {}
        self.shown: dict[int, str] = {}  # last status pushed per user
        self.pending: dict[tuple, int] = {}  # (company, user, day) -> seconds not written yet
    def status(self, uid: int) -> str:
        socks = self.sockets.get(uid)
        return "offline" if not socks else "active" if any(socks.values()) else "idle"
    async def _load_seats(self, uids: list[int]) -> None:
        async with Session() as db:
            rows = (await db.execute(select(Membership.user_id, Membership.company_id, Membership.position, Company.timezone, Company.location_country,
                                            Company.work_start, Company.work_end, Company.work_days, Company.halted_at, Company.name)
                                     .join(Company, Company.id == Membership.company_id).where(Membership.user_id.in_(uids)))).all()
        seats: dict[int, list[Seat]] = {uid: [] for uid in uids}
        for uid, *rest in rows: seats[uid].append(Seat(*rest))
        self.seats.update({uid: s for uid, s in seats.items() if uid in self.sockets})  # someone who left meanwhile isn't re-added
    def tracking(self, uid: int, now: datetime | None = None) -> list[str]:  # the companies counting uid's attendance right now (staff, working hours, not halted)
        now = now or datetime.now(UTC)
        return [s.name for s in self.seats.get(uid, ()) if monitored(s.position) and not s.halted_at and in_hours(s, now)]
    async def _push(self, uid: int) -> None:  # tell the dashboards of uid's companies, when their status really changed
        if (was := self.shown.get(uid, "offline")) == (status := self.status(uid)): return
        self.shown[uid] = status
        if status == "idle" and (names := self.tracking(uid)): await self._send(self.sockets.get(uid, {}), {"t": "idle", "companies": names})  # warn them: idle time isn't counted
        elif status == "active" and was == "idle": await self._send(self.sockets.get(uid, {}), {"t": "active"})  # back: every tab drops the warning
        for seat in self.seats.get(uid, ()):
            if seat.position != FREELANCE: await self._send(self.watchers.get(seat.company_id, ()), {"t": "presence", "company": str(seat.company_id), "user": uid, "status": status})
        if status == "offline": self.shown.pop(uid, None); self.seats.pop(uid, None)
    async def tell(self, uids, message: dict) -> None:  # every open tab of these users (a new chat message: routers/chats.py, never its text; the Deminic AI's reply as it's written: only its owner's tabs)
        for uid in set(uids): await self._send(self.sockets.get(uid, {}), message)
    async def _send(self, sockets, message: dict) -> None:
        for ws in list(sockets):
            try: await ws.send_json(message)
            except Exception: pass  # a closing socket: its own loop cleans it up
    async def join(self, ws: WebSocket, uid: int, session_key: str) -> None:
        new = uid not in self.sockets
        self.sockets.setdefault(uid, {})[ws] = True  # a tab that just opened is in use
        self.session[ws] = session_key
        if new: await self._load_seats([uid])
        await self._push(uid)
    async def leave(self, ws: WebSocket, uid: int) -> None:
        self.session.pop(ws, None)
        if cid := self.watching.pop(ws, None): self.watchers.get(cid, set()).discard(ws)
        socks = self.sockets.get(uid, {})
        socks.pop(ws, None)
        if not socks: self.sockets.pop(uid, None)
        await self._push(uid)
    async def set_active(self, ws: WebSocket, uid: int, active: bool) -> None:
        if ws in self.sockets.get(uid, {}): self.sockets[uid][ws] = active; await self._push(uid)
    async def watch(self, ws: WebSocket, uid: int, company) -> None:
        """A company dashboard asks for live statuses: only for a member of that company. Answers with everyone's status now."""
        try: cid = uuid.UUID(str(company))
        except ValueError: return
        async with Session() as db: members = (await db.execute(select(Membership.user_id, Membership.position).where(Membership.company_id == cid))).all()
        if uid not in {u for u, _ in members}: return
        if (old := self.watching.get(ws)) and old != cid: self.watchers.get(old, set()).discard(ws)
        self.watching[ws] = cid
        self.watchers.setdefault(cid, set()).add(ws)
        await self._send([ws], {"t": "snapshot", "company": str(cid), "statuses": {str(u): "untracked" if p == FREELANCE else self.status(u) for u, p in members}})
    def credit(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        for uid, socks in self.sockets.items():
            if not any(socks.values()): continue
            for s in self.seats.get(uid, ()):
                if monitored(s.position) and not s.halted_at and in_hours(s, now):  # a halted company (guard.py) tracks no attendance
                    key = (s.company_id, uid, now.astimezone(company_tz(s)).date())
                    self.pending[key] = self.pending.get(key, 0) + TICK_S
    async def flush(self) -> None:
        if not self.pending: return
        batch, self.pending, stamp = self.pending, {}, utcnow()
        stmt = insert(Attendance).values([{"company_id": c, "user_id": u, "day": d, "active_seconds": min(s, DAY_CAP), "first_seen": stamp, "last_seen": stamp}
                                          for (c, u, d), s in batch.items()])
        stmt = stmt.on_conflict_do_update(index_elements=["company_id", "user_id", "day"],
                                          set_={"active_seconds": func.least(Attendance.active_seconds + stmt.excluded.active_seconds, DAY_CAP), "last_seen": stmt.excluded.last_seen})
        try:
            async with Session() as db: await db.execute(stmt); await db.commit()
        except IntegrityError: await self._load_seats(list(self.sockets))  # a company was deleted / someone left mid-tick: that tick is dropped, seats refreshed
        except Exception:
            for k, v in batch.items(): self.pending[k] = self.pending.get(k, 0) + v  # e.g. Postgres briefly down: kept for the next tick
            raise
    async def kick(self, uid: int) -> None:  # their login just ended (logout, a newer login, a password reset, a ban, the account deleted): every open tab's live connection closes now, not at the next recheck
        for ws in list(self.sockets.get(uid, {})):
            try: await ws.close(code=4401)
            except Exception: pass
    async def recheck(self) -> None:  # the safety net for any session that ended without kick() (e.g. it simply expired)
        if not self.session: return
        sockets = list(self.session.items())
        async with reddisdb.pipeline(transaction=False) as p:
            for _, key in sockets: p.hget(key, "user_id")
            alive = await p.execute()  # a logged-out / replaced session has no user_id any more
        for (ws, _), uid in zip(sockets, alive):
            if not uid:
                try: await ws.close(code=4401)
                except Exception: pass
        if self.sockets: await self._load_seats(list(self.sockets))
    async def run(self) -> None:
        checked = time.monotonic()
        while True:
            await asyncio.sleep(TICK_S)
            try:
                self.credit()
                await self.flush()
                if time.monotonic() - checked >= RECHECK_S: checked = time.monotonic(); await self.recheck()
            except Exception as e: print(f"presence: {type(e).__name__}: {e}")
hub = Hub()
