"""transfers-service: deposits and withdrawals. It takes the request, and its worker (backoffice/transfer_worker.py) does the transfer from the transfers.outbound queue."""
import logging

from opentelemetry import trace
from pydantic import BaseModel, Field

from .. import config, customers, db, queue, tickets
from ..app import ApiError, create_app, who

log = logging.getLogger("fakeco.transfers")
COLUMNS = "id, direction, amount, counterparty, status, ticket_id, reason, created_at, updated_at"
MAX_AMOUNT = 1_000_000


class Proposal(BaseModel):
    direction: str | None = Field(None, pattern="^(in|out)$")
    amount: float | None = None


class NewTransfer(BaseModel):
    direction: str = Field(pattern="^(in|out)$")
    amount: float
    counterparty: str = Field("bank", pattern="^(bank|card_account)$")


class Decision(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = None
    decided_by: str


def _check(amount):
    if not 0 < amount <= MAX_AMOUNT:
        raise ApiError(422, "bad_amount", f"the amount must be more than $0 and at most ${MAX_AMOUNT:,}")


def _worker():
    try:
        from ..backoffice.transfer_worker import handle
    except ImportError:   # the image was built without the back office: take requests, do no transfers
        log.warning("no transfer worker in this build; transfers stay queued")
        return []
    return [lambda: queue.worker("transfers.outbound", handle)]


def build():
    app = create_app(workers=_worker())

    @app.post("/v1/transfers/proposals")
    async def propose(body: Proposal):
        _, account_id = who()
        cash = await db.fetchval("SELECT cash_balance FROM accounts WHERE id = %s", account_id)
        if body.direction is None or body.amount is None:
            return {"form": True, "cash_balance": round(cash, 2)}
        _check(body.amount)
        return {"direction": body.direction, "amount": round(body.amount, 2), "needs_review": body.amount >= config.HOLD_AMOUNT_USD, "cash_balance": round(cash, 2)}

    @app.post("/v1/transfers")
    async def create(body: NewTransfer):
        customer_id, account_id = who()
        _check(body.amount)
        t = await db.fetchrow(f"INSERT INTO transfers (id, account_id, customer_id, direction, amount, counterparty, status) "
                              f"VALUES ('TRF-' || lpad(nextval('transfer_seq')::text, 8, '0'), %s, %s, %s, %s, %s, 'queued') RETURNING {COLUMNS}",
                              account_id, customer_id, body.direction, round(body.amount, 2), body.counterparty)
        tid = await tickets.create("transfer", "transfers.outbound", customer_id, account_id, t["id"])
        await db.execute("UPDATE transfers SET ticket_id = %s WHERE id = %s", tid, t["id"])
        await queue.publish("transfers.outbound", "transfer", {"transfer_id": t["id"], "approved": False}, ticket_id=tid, customer_id=customer_id,
                            summary=f"{'deposit' if body.direction == 'in' else 'withdrawal'} ${body.amount:,.2f} ({body.counterparty})")
        trace.get_current_span().set_attributes({"ticket.id": tid, "transfer.id": t["id"], "transfer.direction": body.direction, "transfer.amount": round(body.amount, 2)})
        return {"transfer": {**t, "ticket_id": tid}, "ticket_id": tid, "state": "queued"}

    @app.get("/v1/transfers")
    async def list_transfers():
        _, account_id = who()
        return await db.fetch(f"SELECT {COLUMNS} FROM transfers WHERE account_id = %s ORDER BY created_at DESC, id DESC LIMIT 100", account_id)

    @app.post("/v1/transfers/{transfer_id}/decision")
    async def decide(transfer_id: str, body: Decision):
        """Compliance approves or rejects a held transfer. An approved one goes back on the queue, marked approved."""
        t = await db.fetchrow("SELECT id, status, ticket_id, customer_id, direction, amount FROM transfers WHERE id = %s", transfer_id)
        if not t:
            raise ApiError(404, "not_found", f"no transfer {transfer_id}")
        if t["status"] != "held":
            raise ApiError(409, "not_held", f"transfer {transfer_id} is {t['status']}, not held")
        sp = trace.get_current_span()
        sp.set_attributes({"transfer.id": transfer_id, "ticket.id": t["ticket_id"], **await customers.attrs(t["customer_id"]), "compliance.decision": body.decision, "app.staff.id": body.decided_by})
        what = f"{'deposit' if t['direction'] == 'in' else 'withdrawal'} of ${t['amount']:,.2f}"
        if body.decision == "approve":
            await db.execute("UPDATE transfers SET status = 'queued', decided_by = %s, updated_at = now() WHERE id = %s", body.decided_by, transfer_id)
            await queue.set_ticket(t["ticket_id"], "queued", 1)
            await queue.publish("transfers.outbound", "transfer", {"transfer_id": transfer_id, "approved": True}, ticket_id=t["ticket_id"], customer_id=t["customer_id"],
                                summary=f"approved {what}")
        else:
            reason = body.reason or "rejected by compliance"
            await db.execute("UPDATE transfers SET status = 'failed', reason = %s, decided_by = %s, updated_at = now() WHERE id = %s", reason, body.decided_by, transfer_id)
            await queue.set_ticket(t["ticket_id"], "failed", None, reason)
            await queue.publish("notifications", "transfer.failed", {"customer_id": t["customer_id"], "text": f"Your {what} was not approved: {reason}."}, customer_id=t["customer_id"])
        return {"transfer": await db.fetchrow(f"SELECT {COLUMNS} FROM transfers WHERE id = %s", transfer_id)}

    return app
