"""Tickets: the receipt for work handed to the back office. Shared by the services that make them and the ones that show them."""
import secrets

from . import db

OPEN = ("queued", "processing", "retrying")
# seconds one message takes on average, for the estimate a customer hears
SERVICE_S = {"orders": 2.0, "transfers.outbound": 4.0, "sweeps": 1.5}


async def create(kind, queue, customer_id, account_id, ref_id):
    tid = "TKT-" + secrets.token_hex(4).upper()
    await db.execute("INSERT INTO tickets (id, kind, queue, customer_id, account_id, ref_id) VALUES (%s, %s, %s, %s, %s, %s)", tid, kind, queue, customer_id, account_id, ref_id)
    return tid


async def position(ticket):
    """(messages ahead of the ticket's message, seconds until it is done), or (None, None) when it is not waiting."""
    if ticket["state"] not in ("queued", "retrying"):
        return None, None
    row = await db.fetchrow(
        "SELECT (SELECT count(*) FROM queue_messages o WHERE o.queue = m.queue AND o.state IN ('ready', 'retry', 'inflight') AND (o.available_at, o.id) < (m.available_at, m.id)) AS ahead, "
        "greatest(0, extract(epoch FROM m.available_at - now())) AS wait_s FROM queue_messages m WHERE m.ticket_id = %s AND m.state IN ('ready', 'retry') ORDER BY m.id DESC LIMIT 1", ticket["id"])
    if not row:
        return None, None
    per = SERVICE_S.get(ticket["queue"], 2.0)
    return int(row["ahead"]), round(float(row["wait_s"]) + (int(row["ahead"]) + 1) * per)


async def view(ticket, notes=True):
    """The Ticket of the public API (SPEC 3.2)."""
    ahead, eta = await position(ticket)
    out = {"id": ticket["id"], "kind": ticket["kind"], "state": ticket["state"], "attempt": ticket["attempt"], "queue": ticket["queue"], "customer_id": ticket["customer_id"],
           "ref_id": ticket["ref_id"], "reason": ticket["reason"], "queue_position": ahead, "eta_seconds": eta, "notes": [],
           "created_at": ticket["created_at"], "updated_at": ticket["updated_at"]}
    if notes:
        out["notes"] = await db.fetch("SELECT author, text, created_at FROM ticket_notes WHERE ticket_id = %s ORDER BY id", ticket["id"], quiet=True)
    return out
