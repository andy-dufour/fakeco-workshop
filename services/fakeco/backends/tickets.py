"""ticket-status-service: where a ticket stands. The check_ticket tool reads it; the queue workers write it."""
from fastapi import Request
from opentelemetry import trace
from pydantic import BaseModel

from .. import db, tickets
from ..app import ApiError, create_app, who
from ..http import call


class State(BaseModel):
    state: str
    attempt: int | None = None
    reason: str | None = None


def build():
    app = create_app()

    @app.get("/v1/tickets/{ticket_id}")
    async def check_ticket(ticket_id: str):
        customer_id, _ = who()
        t = await db.fetchrow("SELECT * FROM tickets WHERE id = %s", ticket_id)
        if not t or t["customer_id"] != customer_id:
            raise ApiError(404, "not_found", f"no ticket {ticket_id} for this customer")
        sp = trace.get_current_span()
        sp.set_attributes({"ticket.id": ticket_id, "ticket.state": t["state"], "ticket.kind": t["kind"], "queue.attempt": t["attempt"]})
        out = {"ticket_id": t["id"], "kind": t["kind"], "state": t["state"], "attempt": t["attempt"]}
        if t["state"] in tickets.OPEN:
            # a waiting or running ticket: how far back in the queue it is, and whether the worker is alive
            ahead, eta = await tickets.position(t)
            beat = await db.fetchrow("SELECT worker, extract(epoch FROM now() - beat_at) AS age_s FROM worker_heartbeats WHERE queue = %s ORDER BY beat_at DESC LIMIT 1", t["queue"])
            if ahead is not None:
                out.update(queue_position=ahead, eta_seconds=eta)
                sp.set_attributes({"queue.position": ahead, "queue.eta_s": eta})
            if beat:
                sp.set_attribute("queue.worker.heartbeat_age_s", round(float(beat["age_s"]), 1))
        elif t["state"] == "completed":
            rec = await call("ledger-service", "GET", "/v1/ledger/records", params={"ref_id": t["ref_id"]}, route="/v1/ledger/records", raise_for_status=False)
            if isinstance(rec, dict) and rec.get("entries"):
                out["ledger_entries"] = len(rec["entries"])
        if t["reason"]:
            out["reason"] = t["reason"]
        return out

    @app.put("/v1/tickets/{ticket_id}")
    async def update(ticket_id: str, body: State):
        n = await db.execute("UPDATE tickets SET state = %s, attempt = coalesce(%s, attempt), reason = %s, updated_at = now() WHERE id = %s", body.state, body.attempt, body.reason, ticket_id)
        if not n:
            raise ApiError(404, "not_found", f"no ticket {ticket_id}")
        trace.get_current_span().set_attributes({"ticket.id": ticket_id, "ticket.state": body.state})
        return {"ok": True}

    return app
