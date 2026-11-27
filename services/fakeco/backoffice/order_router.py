"""order-router: executes customer orders (and the legs of a rebalance) against the venue, and books the result."""
import httpx
from opentelemetry.trace import SpanKind

from .. import customers, db, http, queue, telemetry
from ..app import create_app
from ..http import ServiceError
from ._common import money, notify, regular_hours, request_sweep

VENUE_LIMIT_S = 6.0
REASONS = {"insufficient_buying_power": "there is not enough cash in your account", "insufficient_position": "you do not hold that many shares",
           "restricted_security": "this security is restricted", "unknown_symbol": "we could not find that security", "invalid_order": "the order is not valid"}


async def reject(trade, reason, span):
    """A business rejection: final, the customer is told."""
    span.set_attributes({"order.status": "rejected", "order.reject_reason": reason})
    await db.execute("UPDATE trades SET status = 'rejected', reject_reason = %s, updated_at = now() WHERE id = %s", reason, trade["id"])
    await notify(trade["customer_id"], "order.rejected", f"Your {trade['side']} of {trade['quantity']} {trade['symbol']} was rejected: {REASONS.get(reason, reason)}.")
    return "failed", reason


async def order(m, span):
    p = m["payload"]
    tracer = telemetry.tracer()
    span.set_attributes({"bo.process": "order_execution", "account.id": p["account_id"], **await customers.attrs(p["customer_id"]), "order.id": p["trade_id"], "order.leg": bool(p.get("leg"))})
    if not m["ticket_id"] and p.get("parent_ticket_id"):
        span.set_attribute("ticket.parent_id", p["parent_ticket_id"])
    await queue.set_ticket(m["ticket_id"], "processing", m["attempt"])
    t = await db.fetchrow("SELECT * FROM trades WHERE id = %s", p["trade_id"])
    if not t:
        return "failed", "trade_not_found"
    if t["status"] in ("filled", "partially_filled", "rejected", "cancelled"):   # a message that comes twice does not trade twice
        span.set_attribute("order.status", t["status"])
        return ("failed", t["reject_reason"]) if t["status"] in ("rejected", "cancelled") else "completed"
    buy, qty, sym = t["side"] == "buy", t["quantity"], t["symbol"]
    session = "regular" if regular_hours() else "extended"
    span.set_attributes({"order.side": t["side"], "order.symbol": sym, "order.quantity": qty, "order.session": session})

    with tracer.start_as_current_span("order.validate", attributes={"order.type": "market" if session == "regular" else "limit", "order.time_in_force": "day"}) as v:
        known = await db.fetchval("SELECT 1 FROM instruments WHERE symbol = %s", sym)
        if qty <= 0 or t["side"] not in ("buy", "sell"):
            return await reject(t, "invalid_order", span)
        if not known:
            return await reject(t, "unknown_symbol", span)
        price = (await http.call("market-data-service", "GET", f"/v1/quotes/{sym}", route="/v1/quotes/{symbol}"))["price"]
        v.set_attribute("order.price", price)
    notional = round(qty * price, 2)
    span.set_attribute("order.notional_usd", notional)

    if buy and not p.get("leg"):   # a leg of a rebalance is paid for by the sells of the same plan
        bp = await http.call("ledger-service", "POST", "/v1/ledger/check-buying-power", json={"account_id": t["account_id"], "amount": notional}, route="/v1/ledger/check-buying-power")
        if not bp["ok"]:
            return await reject(t, "insufficient_buying_power", span)
    elif not buy:
        pos = await http.call("ledger-service", "POST", "/v1/ledger/check-position", json={"account_id": t["account_id"], "symbol": sym, "quantity": qty}, route="/v1/ledger/check-position")
        if not pos["ok"]:
            return await reject(t, "insufficient_position", span)

    screen = await http.call("compliance-service", "POST", "/v1/compliance/screen-order", json={"account_id": t["account_id"], "symbol": sym, "side": t["side"], "quantity": qty},
                             route="/v1/compliance/screen-order")
    if not screen["allowed"]:
        return await reject(t, "restricted_security", span)

    with tracer.start_as_current_span("venue.send_order", kind=SpanKind.INTERNAL, attributes={"server.address": "venue-gateway"}) as vs:
        try:
            ex = await http.call("venue-gateway", "POST", "/v1/venue/orders", json={"trade_id": t["id"], "symbol": sym, "side": t["side"], "quantity": qty, "price": price},
                                 timeout=VENUE_LIMIT_S, route="/v1/venue/orders")
        except httpx.TimeoutException:
            raise queue.Retry(f"no execution report within {int(VENUE_LIMIT_S * 1000)} ms", "VenueTimeoutError")
        except ServiceError as e:
            raise queue.Retry(f"venue answered HTTP {e.status}", "VenueError")
        vs.set_attributes({"routing.venue": ex["venue"], "order.status": ex["status"]})

    for f in ex["fills"]:
        with tracer.start_as_current_span("record_execution", attributes={"execution.quantity": f["quantity"], "execution.price": f["price"]}):
            await db.execute("INSERT INTO executions (trade_id, quantity, price, venue) VALUES (%s, %s, %s, %s)", t["id"], f["quantity"], f["price"], ex["venue"])
    filled = sum(f["quantity"] for f in ex["fills"])
    value = round(sum(f["quantity"] * f["price"] for f in ex["fills"]), 2)
    avg = round(value / filled, 4) if filled else None
    sign = -1 if buy else 1
    with tracer.start_as_current_span("allocate", attributes={"order.filled_quantity": filled, "order.status": ex["status"]}):
        await http.call("ledger-service", "POST", "/v1/ledger/post", route="/v1/ledger/post", json={
            "account_id": t["account_id"], "ref_id": t["id"], "entries": [{"entry_type": "trade", "amount": round(sign * value, 2)}],
            "position": {"symbol": sym, "delta_quantity": -sign * filled}})
        await db.execute("UPDATE trades SET status = %s, filled_quantity = %s, avg_price = %s, updated_at = now() WHERE id = %s", ex["status"], filled, avg, t["id"])
    span.set_attributes({"order.status": ex["status"], "order.filled_quantity": filled, "order.venue": ex["venue"], "order.avg_price": avg or 0})
    await queue.publish("settlement.instructions", "instruction", {"trade_id": t["id"], "symbol": sym, "notional": value}, customer_id=t["customer_id"], summary=f"{t['side']} {filled} {sym}")
    await notify(t["customer_id"], "order.confirmed", f"Your {t['side']} of {filled} {sym} was {'filled' if ex['status'] == 'filled' else 'partly filled'} at {money(avg)}.")
    await request_sweep(t["account_id"], t["customer_id"], "trade", sign * value)
    return "completed"


async def rebalance(m, span):
    """Plan the legs of an applied proposal and put one order per leg on the orders queue."""
    p = m["payload"]
    span.set_attributes({"bo.process": "rebalance_execution", "account.id": p["account_id"], **await customers.attrs(p["customer_id"]), "rebalance.proposal": p["proposal_id"]})
    await queue.set_ticket(m["ticket_id"], "processing", m["attempt"])
    prop = await db.fetchrow("SELECT * FROM rebalance_proposals WHERE id = %s", p["proposal_id"])
    if not prop:
        return "failed", "proposal_not_found"
    if prop["status"] == "applied":
        return "completed"
    with telemetry.tracer().start_as_current_span("plan_legs", attributes={"rebalance.legs": len(prop["legs"])}):
        legs = [(l["side"], l["symbol"], int(round(l["quantity"]))) for l in prop["legs"] if int(round(l["quantity"])) > 0]
        ids = []
        for side, sym, qty in legs:
            ids.append(await db.fetchval(
                "INSERT INTO trades (id, account_id, customer_id, side, symbol, quantity, status, parent_ticket_id) "
                "VALUES ('TRD-' || lpad(nextval('trade_seq')::text, 8, '0'), %s, %s, %s, %s, %s, 'queued', %s) RETURNING id", prop["account_id"], prop["customer_id"], side, sym, qty, m["ticket_id"]))
    span.set_attribute("rebalance.legs", len(legs))
    for tid, (side, sym, qty) in zip(ids, legs):
        await queue.publish("orders", "order", {"trade_id": tid, "account_id": prop["account_id"], "customer_id": prop["customer_id"], "leg": True, "parent_ticket_id": m["ticket_id"]},
                            customer_id=prop["customer_id"], summary=f"{side} {qty} {sym}", attrs={"ticket.parent_id": m["ticket_id"]} if m["ticket_id"] else None)
    await db.execute("UPDATE rebalance_proposals SET status = 'applied' WHERE id = %s", prop["id"])
    return "completed"


async def handle(m, span):
    token = http.customer.set({"customer_id": m["payload"]["customer_id"], "account_id": m["payload"]["account_id"]})
    try:
        return await (rebalance if m["kind"] == "rebalance" else order)(m, span)
    finally:
        http.customer.reset(token)


def build():
    return create_app(workers=[lambda: queue.worker("orders", handle)])
