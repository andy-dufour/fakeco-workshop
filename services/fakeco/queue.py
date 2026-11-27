"""Message queues on a Postgres table (FOR UPDATE SKIP LOCKED).

publish() makes a PRODUCER span `publish <queue>` and stores its trace context in the row. A worker takes one message at a time and runs the handler
in a CONSUMER span `process <queue>` that is the root of a new trace, linked to the producer. A handler returns "completed" or "failed" (a business
rejection, final) or raises Retry (a technical failure): the message waits for a back-off and is tried again; the third failure dead-letters it.
"""
import asyncio, logging, random, time

from opentelemetry import context as otel_context, propagate, trace
from opentelemetry.trace import Link, SpanKind

from . import config, customers, db, faults, telemetry

log = logging.getLogger("fakeco.queue")
MSG = {"messaging.system": "postgresql"}
STATE_AFTER = {"completed": "completed", "failed": "failed", "retry": "retrying", "dead_lettered": "dead_lettered", "held": "held"}
# queue -> seconds an idle worker waits between polls, and the back-off before attempt 2 and 3 (shorter than a real broker's, so a demo does not wait minutes)
TUNING = {"orders": ((0.3, 1.5), (3, 10)), "transfers.outbound": ((0.5, 3.0), (6, 20)), "sweeps": ((0.2, 1.0), (4, 12)), "ach.returns": ((0.2, 1.0), (5, 15)),
          "settlement.instructions": ((1, 2), (5, 15)), "notifications": ((0.2, 1.0), (2, 6))}


class Retry(Exception):
    """A technical failure of one attempt. error_type is what the span and the dead letter show."""
    def __init__(self, message, error_type="Error"):
        super().__init__(message)
        self.error_type = error_type


async def publish(queue, kind, payload, *, ticket_id=None, customer_id=None, summary=None, delay_s=0.0, attrs=None):
    """Put a message on a queue. Returns its id."""
    with telemetry.tracer().start_as_current_span(f"publish {queue}", kind=SpanKind.PRODUCER, attributes={
            **MSG, "messaging.destination.name": queue, "messaging.operation.type": "publish", **(attrs or {})}) as sp:
        carrier = {}
        propagate.inject(carrier)
        row = await db.fetchrow(
            "INSERT INTO queue_messages (queue, kind, payload, traceparent, ticket_id, customer_id, summary, available_at, depth_at_enqueue) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s), (SELECT count(*) FROM queue_messages WHERE queue = %s AND state IN ('ready', 'retry'))) RETURNING id",
            queue, kind, db.J(payload), carrier.get("traceparent"), ticket_id, customer_id, summary, float(delay_s), queue, quiet=True)
        sp.set_attribute("messaging.message.id", f"msg-{row['id']}")
        if ticket_id:
            sp.set_attributes({"ticket.id": ticket_id, "idempotency.key": ticket_id})
        if customer_id:
            sp.set_attributes(await customers.attrs(customer_id))
        return row["id"]


async def _claim(queue):
    return await db.fetchrow(
        "UPDATE queue_messages SET state = 'inflight', started_at = now() WHERE id = (SELECT id FROM queue_messages WHERE queue = %s AND state IN ('ready', 'retry') "
        "AND available_at <= now() ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING *, extract(epoch FROM now() - available_at) * 1000 AS wait_ms, "
        "extract(epoch FROM available_at - enqueued_at) * 1000 AS backoff_ms", queue, quiet=True)


async def set_ticket(ticket_id, state, attempt=None, reason=None):
    """Tell ticket-status-service where a ticket is (the app's status requests read it)."""
    if not ticket_id:
        return
    from .http import call  # imported late: http needs telemetry set up by the service
    with telemetry.tracer().start_as_current_span("ticket.set_state", attributes={"ticket.id": ticket_id, "ticket.state": state}):
        await call("ticket-status-service", "PUT", f"/v1/tickets/{ticket_id}", json={"state": state, "attempt": attempt, "reason": reason}, route="/v1/tickets/{id}")


async def _one(queue, handler, m, backoff):
    """Run one attempt of one message in a trace of its own."""
    service = config.service_name()
    links = []
    if m["traceparent"]:
        parent = trace.get_current_span(propagate.extract({"traceparent": m["traceparent"]})).get_span_context()
        if parent.is_valid:
            links.append(Link(parent))
    attrs = {**MSG, "messaging.destination.name": queue, "messaging.operation.type": "process", "messaging.consumer.group.name": f"{service}-workers",
             "messaging.message.id": f"msg-{m['id']}", "queue.attempt": m["attempt"], "queue.depth_at_enqueue": m["depth_at_enqueue"], "queue.wait_ms": round(float(m["wait_ms"]), 3),
             "queue.message.kind": m["kind"]}
    if m["attempt"] > 1 and not m["replay"]:
        attrs["queue.retry_backoff_ms"] = round(float(m["backoff_ms"]), 3)
    if m["replay"]:
        attrs["queue.replay"] = True
    if m["ticket_id"]:
        attrs["ticket.id"] = m["ticket_id"]
    if m["customer_id"]:
        attrs.update(await customers.attrs(m["customer_id"]))
    token = otel_context.attach(otel_context.Context())  # a new trace: the consumer is not a child of whatever started the worker
    try:
        with telemetry.tracer().start_as_current_span(f"process {queue}", kind=SpanKind.CONSUMER, attributes=attrs, links=links) as root:
            reason = None
            try:
                outcome = await handler(m, root)
                if isinstance(outcome, tuple):
                    outcome, reason = outcome
            except Retry as e:
                reason = f"{e.error_type}: {e}"
                outcome = "dead_lettered" if m["attempt"] >= config.MAX_ATTEMPTS else "retry"
                telemetry.fail(root, e.error_type, str(e))
            except Exception as e:  # a bug in a handler: treat it like a technical failure, with the stack on the span
                log.exception("handler of %s failed on message %s", queue, m["id"])
                reason = f"{type(e).__name__}: {e}"
                outcome = "dead_lettered" if m["attempt"] >= config.MAX_ATTEMPTS else "retry"
                root.record_exception(e)
                telemetry.fail(root, type(e).__name__, str(e))
            if outcome == "retry":
                wait = random.uniform(*backoff) * (1 if m["attempt"] == 1 else 2.5)
                with telemetry.tracer().start_as_current_span(f"publish {queue}.retry", kind=SpanKind.PRODUCER, attributes={
                        **MSG, "messaging.destination.name": f"{queue}.retry", "messaging.operation.type": "publish", "queue.attempt": m["attempt"], "queue.retry_backoff_ms": round(wait * 1000)}):
                    carrier = {}
                    propagate.inject(carrier)
                    await db.execute("UPDATE queue_messages SET state = 'retry', attempt = attempt + 1, last_error = %s, traceparent = %s, enqueued_at = now(), "
                                     "available_at = now() + make_interval(secs => %s) WHERE id = %s", reason, carrier.get("traceparent"), wait, m["id"], quiet=True)
            elif outcome == "dead_lettered":
                with telemetry.tracer().start_as_current_span(f"publish {queue}.dlq", kind=SpanKind.PRODUCER, attributes={
                        **MSG, "messaging.destination.name": f"{queue}.dlq", "messaging.operation.type": "publish", "queue.attempt": m["attempt"]}):
                    carrier = {}
                    propagate.inject(carrier)
                    await db.execute("UPDATE queue_messages SET state = 'dlq', last_error = %s, traceparent = %s, finished_at = now() WHERE id = %s", reason, carrier.get("traceparent"), m["id"], quiet=True)
                log.warning("message %s of %s dead-lettered after %s attempts: %s", m["id"], queue, m["attempt"], reason)
            else:
                await db.execute("UPDATE queue_messages SET state = %s, last_error = %s, finished_at = now() WHERE id = %s",
                                 "failed" if outcome == "failed" else "done", reason, m["id"], quiet=True)
            if outcome in STATE_AFTER and m["ticket_id"] and not m["payload"].get("no_ticket_update"):
                await set_ticket(m["ticket_id"], STATE_AFTER[outcome], m["attempt"] + (1 if outcome == "retry" else 0), reason)
            root.set_attribute("queue.outcome", outcome)
    finally:
        otel_context.detach(token)


async def worker(queue, handler, *, name="1", stuck_fault=None):
    """Take messages from `queue` for ever, one at a time. stuck_fault: while that fault is on, the worker takes nothing."""
    poll, backoff = TUNING[queue]
    me = f"{config.service_name()}-{name}"
    beat = 0.0
    while True:
        try:
            if time.monotonic() - beat > 5:
                beat = time.monotonic()
                await db.execute("INSERT INTO worker_heartbeats (worker, queue) VALUES (%s, %s) ON CONFLICT (worker) DO UPDATE SET beat_at = now()", me, queue, quiet=True)
            if stuck_fault and await faults.get(stuck_fault) is not None:
                await asyncio.sleep(2)
                continue
            m = await _claim(queue)
            if not m:
                await asyncio.sleep(random.uniform(*poll))
                continue
            await _one(queue, handler, m, backoff)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker %s of %s: unexpected error", me, queue)
            await asyncio.sleep(2)


async def replay(queue, ids=None):
    """Move dead letters back to the queue (an operator does this). Returns how many."""
    if ids:
        rows = await db.fetch("UPDATE queue_messages SET state = 'ready', replay = true, attempt = 1, available_at = now(), enqueued_at = now(), finished_at = NULL "
                              "WHERE queue = %s AND state = 'dlq' AND id = ANY(%s) RETURNING ticket_id", queue, list(ids))
    else:
        rows = await db.fetch("UPDATE queue_messages SET state = 'ready', replay = true, attempt = 1, available_at = now(), enqueued_at = now(), finished_at = NULL "
                              "WHERE queue = %s AND state = 'dlq' RETURNING ticket_id", queue)
    for r in rows:
        if r["ticket_id"]:
            await db.execute("UPDATE tickets SET state = 'queued', attempt = 1, reason = NULL, updated_at = now() WHERE id = %s", r["ticket_id"])
    return len(rows)
