"""What every service has: a FastAPI app with telemetry, the database pool, the customer context, /healthz, and its queue workers."""
import asyncio, contextlib, logging, os, time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import trace

from . import config, customers, db, http, telemetry

log = logging.getLogger("fakeco")


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ClientGone(Exception):
    """The caller closed the connection before the answer was ready."""


async def or_disconnect(request, coro, cap_s=None):
    """Run `coro`, but stop it when the caller hangs up (HTTP 499) or, with cap_s, when it takes longer than that (HTTP 504).
    Without this a handler runs on after its caller has gone, and its spans end long after their parent."""
    work, t0 = asyncio.ensure_future(coro), time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({work}, timeout=0.2)
            if done:
                return work.result()
            if await request.is_disconnected():
                raise ClientGone()
            if cap_s and time.monotonic() - t0 > cap_s:
                raise ApiError(504, "timeout", f"no answer within {cap_s:.0f} s")
    finally:
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)


class _CustomerContext:
    """Plain ASGI middleware: reads who the request is for, for the handler and for the calls it makes."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        h = {k: v for k, v in scope["headers"] if k in (b"x-customer-id", b"x-account-id")}
        cid, aid = h.get(b"x-customer-id", b"").decode(), h.get(b"x-account-id", b"").decode()
        if not cid:
            return await self.app(scope, receive, send)
        trace.get_current_span().set_attributes({**await customers.attrs(cid), "account.id": aid})
        token = http.customer.set({"customer_id": cid, "account_id": aid})
        try:
            await self.app(scope, receive, send)
        finally:
            http.customer.reset(token)


async def watch_version():
    """Every process takes the service.version that the last release flag change stored (api.change_marker); none stored yet: the one in config."""
    while True:
        try:
            telemetry.set_version(await db.fetchval("SELECT value #>> '{}' FROM settings WHERE key = 'service_version'", quiet=True) or config.VERSION)
        except Exception:
            log.exception("could not read the service version")
        await asyncio.sleep(3)


def create_app(workers=(), on_start=None, needs_db=True):
    """workers: callables that return a coroutine to run for the life of the process (queue workers, tickers)."""
    service = config.service_name()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        telemetry.init(service)
        if needs_db:
            await db.open_pool()
        if on_start:
            await on_start()
        tasks = [asyncio.create_task(w()) for w in [*workers, *([watch_version] if needs_db else [])]]
        log.info("%s started", service)
        yield
        for t in tasks:
            t.cancel()
        await db.close_pool()

    app = FastAPI(title=service, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    app.add_middleware(_CustomerContext)

    @app.exception_handler(ApiError)
    async def api_error(request, e):
        return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=e.status)

    @app.exception_handler(ClientGone)
    async def client_gone(request, e):
        telemetry.fail(trace.get_current_span(), "ClientClosedRequest", "client closed request")
        return Response(status_code=499)

    @app.exception_handler(http.ServiceError)
    async def service_error(request, e):
        return JSONResponse({"error": {"code": "upstream_error", "message": str(e)}}, status_code=502)

    @app.exception_handler(httpx.HTTPError)
    async def service_unreachable(request, e):
        """The other service did not answer (down, or slower than the timeout)."""
        timed_out = isinstance(e, httpx.TimeoutException)
        trace.get_current_span().set_attribute("error.type", type(e).__name__)
        return JSONResponse({"error": {"code": "upstream_timeout" if timed_out else "upstream_error", "message": f"{type(e).__name__}: {e}"[:300]}},
                            status_code=504 if timed_out else 502)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "service": service}

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz", exclude_spans=["send", "receive"])
    return app


def who():
    """The customer of the current request: (customer_id, account_id). Tool backends take the account from here, never from the model."""
    w = http.customer.get()
    if not w or not w.get("account_id"):
        raise ApiError(400, "no_customer", "this call needs the x-customer-id and x-account-id headers")
    return w["customer_id"], w["account_id"]
