"""AI work someone is waiting for stops when they've gone (user asked: they went offline, or closed their tab), and what it used stays used.
A Job ties the work to the person it's for. presence.Hub.strays halts their jobs once they've had no tab open for GRACE_S: moving between pages closes a tab's socket for a moment, which is what the grace is
for, and another tab still open means they're still here. vLLM's answer stops at its next piece (grow._vllm). Claude can go a minute without sending anything while it thinks, so a halt closes its stream from outside
(silent). Either way the connection drops, which is what stops them generating.
Nothing is given back: every limit is counted when the work is asked for, and Claude's tokens up to that moment go into the month's count (tables.note_stream).
Work with no Job always finishes, because nobody in particular is waiting for it: ID photos and company numbers (closing a tab can't skip a check), a meeting's captions and notes (they're everyone's),
the spam review, the daily tracking and promotion reviews, review summaries."""
import asyncio, contextlib, contextvars, os, threading
GRACE_S = max(1, int(os.getenv("AI_OFFLINE_GRACE_S", "15")))
class Halted(Exception): pass
_current: contextvars.ContextVar = contextvars.ContextVar("ai_job", default=None)  # asyncio.to_thread takes it along to the engine's thread
_running: dict[int, set] = {}  # user id -> their jobs, from the moment they're asked for (a question waiting its turn is one too)
class Job:
    def __init__(self, uid: int): self.uid, self.stop, self.close = uid, threading.Event(), None; _running.setdefault(uid, set()).add(self)
    def __enter__(self): self.outer = _current.set(self); return self
    def __exit__(self, *_):
        _current.reset(self.outer)
        if not (jobs := _running.get(self.uid, set())) - {self}: _running.pop(self.uid, None)
        else: jobs.discard(self)
def check() -> None:
    if (job := _current.get()) and job.stop.is_set(): raise Halted("Stopped: whoever asked for it went offline. It still counts as used.")
def people() -> set[int]: return set(_running)
def halt(uid: int) -> None:
    for job in _running.get(uid, ()):
        if not job.stop.is_set():
            job.stop.set()
            with contextlib.suppress(Exception):
                if job.close: job.close()
@contextlib.contextmanager
def silent(close):  # around a read that may hear nothing for a long time: close() ends it from outside, and whatever error that makes in the read means Halted
    job = _current.get()
    if job: job.close = close
    try:
        check()
        yield
    except (Exception, asyncio.CancelledError):
        check()
        raise
    finally:
        if job: job.close = None
async def run(job: Job, work) -> None:  # a job waiting in a queue, when its turn comes
    with job:
        try: await work
        except Halted: print(f"AI work for user {job.uid} stopped: they went offline")
