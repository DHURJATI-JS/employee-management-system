"""How the server runs on Windows. asyncio has two event loops there, and each could be killed from outside: the selector loop, which uvicorn picks with reload, dies at about 512 open connections, and anyone can open
those; the proactor loop has no such limit, but a client resetting its connection just before it's accepted closed the listening socket for good. So: the proactor loop, with an accept that shrugs that off."""
import asyncio, sys
def quiet_resets(loop, context) -> None:  # a browser resetting its connection as it opens or closes makes asyncio print a stack trace. It means nothing; anything else is still printed
    exc, message = context.get("exception"), context.get("message", "")
    if not (isinstance(exc, ConnectionError) and "_call_connection_lost" in message or isinstance(exc, OSError) and "accept_coro" in repr(context.get("future"))): loop.default_exception_handler(context)
if sys.platform == "win32":
    class Proactor(asyncio.IocpProactor):
        def accept(self, listener):
            async def one():
                while True:
                    try: return await super(Proactor, self).accept(listener)
                    except OSError as e:  # that client went: on to the next one. The listening socket stays open
                        if listener.fileno() == -1: raise  # (it's closing down)
                        if not isinstance(e, ConnectionError) and e.winerror != 64: print(f"accept: {type(e).__name__}: {e}"); await asyncio.sleep(1)
            return asyncio.ensure_future(one(), loop=self._loop)
    def make() -> asyncio.AbstractEventLoop: return asyncio.ProactorEventLoop(Proactor())
    def safe(loop) -> bool: return isinstance(getattr(loop, "_proactor", None), Proactor)
else:
    make = asyncio.new_event_loop
    def safe(loop) -> bool: return True
