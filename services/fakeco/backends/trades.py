"""trades-service: the customer's trades. A pending trade waits for the customer; submitting it hands it to the back office (the orders queue) with a ticket."""
import random

from fastapi import Request
from opentelemetry import trace
from pydantic import BaseModel, Field

from .. import db, faults, queue, tickets
from ..app import ApiError, create_app, or_disconnect, who

COLUMNS = "id, side, symbol, quantity, order_type, status, filled_quantity, avg_price, ticket_id, reject_reason, created_at, updated_at"
STATUSES = ("pending", "queued", "filled", "partially_filled", "cancelled", "rejected")


class NewTrade(BaseModel):
    side: str = Field(pattern="^(buy|sell)$")
    symbol: str
    quantity: int = Field(ge=1, le=100_000)
    mode: str = Field("now", pattern="^(now|pending)$")


class Change(BaseModel):
    quantity: int = Field(ge=1, le=100_000)


async def _slow():
    """Seconds the first query of a request takes longer while `trades_service_slow` is on; for a share of the requests, longer than a tool waits."""
    f = await faults.get("trades_service_slow")
    if f is None:
        return 0.0
    if random.random() < float(f.get("timeout_rate", .25)):
        return random.uniform(9.5, 14)
    return random.uniform(.01, .04) * float(f.get("factor", 40))


async def _send(trade, customer_id, account_id):
    """Hand a trade to the back office. Returns the ticket id."""
    tid = await tickets.create("trade", "orders", customer_id, account_id, trade["id"])
    await db.execute("UPDATE trades SET status = 'queued', ticket_id = %s, updated_at = now() WHERE id = %s", tid, trade["id"])
    await queue.publish("orders", "order", {"trade_id": trade["id"], "account_id": account_id, "customer_id": customer_id, "leg": False, "parent_ticket_id": None},
                        ticket_id=tid, customer_id=customer_id, summary=f"{trade['side']} {trade['quantity']} {trade['symbol']}")
    trace.get_current_span().set_attributes({"ticket.id": tid, "order.id": trade["id"]})
    return tid


async def _mine(trade_id, account_id, delay_s=0.0, pending=True):
    t = await db.fetchrow(f"SELECT {COLUMNS} FROM trades WHERE id = %s AND account_id = %s", trade_id, account_id, delay_s=delay_s)
    if not t:
        raise ApiError(404, "not_found", f"no trade {trade_id} in this account")
    if pending and t["status"] != "pending":
        raise ApiError(409, "not_pending", f"trade {trade_id} is {t['status']}, not pending")
    return t


def build():
    app = create_app()

    @app.get("/v1/trades")
    async def list_trades(request: Request, status: str = "all"):
        _, account_id = who()
        if status != "all" and status not in STATUSES:
            raise ApiError(400, "bad_status", f"unknown status {status}")

        async def work():
            where = "" if status == "all" else " AND status = %s" if status != "filled" else " AND status IN (%s, 'partially_filled')"
            rows = await db.fetch(f"SELECT {COLUMNS} FROM trades WHERE account_id = %s{where} ORDER BY created_at DESC, id DESC LIMIT 100", account_id, *([] if status == "all" else [status]),
                                  delay_s=await _slow())
            return {"trades": rows}
        return await or_disconnect(request, work())

    @app.post("/v1/trades")
    async def create(body: NewTrade):
        customer_id, account_id = who()
        symbol = body.symbol.upper()
        if not await db.fetchval("SELECT 1 FROM instruments WHERE symbol = %s AND asset_class <> 'Cash'", symbol):
            raise ApiError(404, "unknown_symbol", f"no instrument {symbol}")
        t = await db.fetchrow(f"INSERT INTO trades (id, account_id, customer_id, side, symbol, quantity) VALUES ('TRD-' || lpad(nextval('trade_seq')::text, 8, '0'), %s, %s, %s, %s, %s) "
                              f"RETURNING {COLUMNS}", account_id, customer_id, body.side, symbol, body.quantity)
        tid = await _send(t, customer_id, account_id) if body.mode == "now" else None
        return {"trade": {**t, "status": "queued" if tid else "pending", "ticket_id": tid}, "ticket_id": tid}

    @app.post("/v1/trades/{trade_id}/changes")
    async def propose_change(trade_id: str, body: Change, request: Request):
        _, account_id = who()

        async def work():
            t = await _mine(trade_id, account_id, await _slow())
            await db.execute("INSERT INTO trade_changes (trade_id, account_id, new_quantity) VALUES (%s, %s, %s)", trade_id, account_id, body.quantity)
            return {"trade_id": trade_id, "symbol": t["symbol"], "side": t["side"], "from_quantity": t["quantity"], "to_quantity": body.quantity}
        return await or_disconnect(request, work())

    @app.post("/v1/trades/{trade_id}/changes/apply")
    async def apply_change(trade_id: str, request: Request):
        customer_id, account_id = who()

        async def work():
            t = await _mine(trade_id, account_id, await _slow())
            change = await db.fetchrow("UPDATE trade_changes SET status = 'applied' WHERE id = (SELECT id FROM trade_changes WHERE trade_id = %s AND status = 'proposed' ORDER BY id DESC LIMIT 1) "
                                       "RETURNING new_quantity", trade_id)
            if not change:
                raise ApiError(409, "no_change", f"trade {trade_id} has no proposed change")
            await db.execute("UPDATE trades SET quantity = %s, updated_at = now() WHERE id = %s", change["new_quantity"], trade_id)
            tid = await _send({**t, "quantity": change["new_quantity"]}, customer_id, account_id)
            return {"trade_id": trade_id, "quantity": change["new_quantity"], "ticket_id": tid, "state": "queued"}
        return await or_disconnect(request, work())

    @app.post("/v1/trades/{trade_id}/submit")
    async def submit(trade_id: str):
        customer_id, account_id = who()
        t = await _mine(trade_id, account_id)
        tid = await _send(t, customer_id, account_id)
        return {"trade": {**t, "status": "queued", "ticket_id": tid}, "ticket_id": tid}

    @app.post("/v1/trades/{trade_id}/cancel")
    async def cancel(trade_id: str):
        _, account_id = who()
        await _mine(trade_id, account_id)
        return {"trade": await db.fetchrow(f"UPDATE trades SET status = 'cancelled', updated_at = now() WHERE id = %s RETURNING {COLUMNS}", trade_id)}

    return app
