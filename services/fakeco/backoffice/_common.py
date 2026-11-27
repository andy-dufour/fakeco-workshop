"""Small helpers the back-office services share: waiting on an outside party, the two messages many processes publish, and background runs."""
import asyncio, datetime as dt, logging, math, random
from zoneinfo import ZoneInfo

from fastapi.responses import JSONResponse
from opentelemetry import context as otel_context, trace
from opentelemetry.trace import Link, SpanKind

from .. import db, queue, telemetry

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
_tasks = set()


def regular_hours(now=None):
    """True during US regular trading hours (Eastern, weekdays 9:30-16:00)."""
    local = (now or dt.datetime.now(ET)).astimezone(ET)
    return local.weekday() < 5 and dt.time(9, 30) <= local.time() < dt.time(16, 0)


def around(median_s, sigma=.3):
    """A duration in seconds with a log-normal spread around the median."""
    return median_s * math.exp(random.gauss(0, sigma))


async def outside(name, server, seconds, **attrs):
    """A call to a party that is not one of our services (a vendor, the bank, the card network): a CLIENT span that lasts as long as they take."""
    with telemetry.tracer().start_as_current_span(name, kind=SpanKind.CLIENT, attributes={"server.address": server, **attrs}) as sp:
        await asyncio.sleep(seconds)
        return sp


async def notify(customer_id, kind, text):
    await queue.publish("notifications", kind, {"customer_id": customer_id, "text": text}, customer_id=customer_id, summary=text[:80])


async def request_sweep(account_id, customer_id, cause, delta):
    await queue.publish("sweeps", "sweep", {"account_id": account_id, "customer_id": customer_id, "cause": cause, "delta": round(delta, 2)},
                        customer_id=customer_id, summary=f"{cause} {delta:+.2f}")


def money(x):
    return f"${x:,.2f}"


def start_run(run_id, name, work, kind=SpanKind.INTERNAL, attrs=None):
    """Answer 202 at once and do `work(span)` in a task under a span of its own (a new trace, linked to the request). `work` returns the run's
    detail; the runs row is then set completed, or failed when it raises."""
    caller = trace.get_current_span().get_span_context()

    async def go():
        token = otel_context.attach(otel_context.Context())
        try:
            with telemetry.tracer().start_as_current_span(name, kind=kind, attributes={"run.id": run_id, **(attrs or {})}, links=[Link(caller)] if caller.is_valid else []) as sp:
                try:
                    detail = await work(sp)
                    sp.set_attribute("run.detail", detail)
                    await db.execute("UPDATE runs SET status = 'completed', detail = %s, ended_at = now() WHERE id = %s", detail, run_id)
                except Exception as e:
                    log.exception("run %s failed", run_id)
                    sp.record_exception(e)
                    telemetry.fail(sp, type(e).__name__, str(e))
                    await db.execute("UPDATE runs SET status = 'failed', detail = %s, ended_at = now() WHERE id = %s", f"{type(e).__name__}: {e}"[:200], run_id)
        finally:
            otel_context.detach(token)
    t = asyncio.create_task(go())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return JSONResponse({"run_id": run_id, "status": "running"}, status_code=202)
