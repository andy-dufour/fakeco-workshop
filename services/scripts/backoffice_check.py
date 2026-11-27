"""Proves the back-office processes against the running services (nothing is mocked): it sets up rows, publishes messages the way the tool backends do,
waits, and asserts on the database.

Needs Postgres seeded (python -m fakeco.seed --reset) and these services running with FAKECO_LOCAL=1:
ticket-status-service, order-router, ledger-service, compliance-service, venue-gateway, fraud-service, ach-gateway, sweep-service,
wallet-balance-service, settlement-service, notification-service, market-data-service, and something that runs the transfers.outbound worker
(transfers-service). scripts/run-local.sh starts all of them.

Run from services/:  FAKECO_LOCAL=1 .venv/bin/python scripts/backoffice_check.py [case-name ...]
It changes accounts ACC-0011 to ACC-0040 and turns faults on and off; run it on a throwaway database. Exit code 1 when a case fails."""
import asyncio, math, os, re, sys, time

os.environ["FAKECO_LOCAL"] = "1"
os.environ.pop("FAKECO_EXPORT", None)
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)   # never send anything from the check
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fakeco import db, http, queue, tickets  # noqa: E402

DONE = ("completed", "failed", "dead_lettered", "held")


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def near(a, b, tol=0.02):
    return abs(a - b) <= tol


async def until(fn, timeout, what, every=0.3):
    end = time.monotonic() + timeout
    while True:
        v = await fn()
        if v:
            return v
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")
        await asyncio.sleep(every)


async def one(sql, *a):
    return await db.fetchrow(sql, *a, quiet=True)


async def val(sql, *a):
    return await db.fetchval(sql, *a, quiet=True)


async def fault(name, on, **params):
    """Turn a fault flag on or off (the services cache flags for 3 s, so this waits for that)."""
    await db.execute("UPDATE faults SET active = %s, params = CASE WHEN %s THEN %s ELSE default_params END WHERE name = %s", on, on, db.J({**(await val("SELECT default_params FROM faults WHERE name = %s", name)), **params}), name, quiet=True)
    await asyncio.sleep(3.6)


async def setup(n, cash=50_000.0, wallet=300.0, floor=100.0, target=1000.0, hold=None):
    """Put account ACC-00nn into a known state. Returns (account_id, customer_id)."""
    acc, cust = f"ACC-{n:04d}", f"CUS-{n:04d}"
    await db.execute("UPDATE accounts SET cash_balance = %s, wallet_balance = %s WHERE id = %s", cash, wallet, acc, quiet=True)
    await db.execute("UPDATE customers SET wallet_floor = %s, wallet_target = %s WHERE id = %s", floor, target, cust, quiet=True)
    await db.execute("DELETE FROM holdings WHERE account_id = %s", acc, quiet=True)
    await db.execute("DELETE FROM transfers WHERE account_id = %s", acc, quiet=True)   # earlier runs count against the daily limit
    for sym, qty in (hold or {}).items():
        await db.execute("INSERT INTO holdings (asset_id, account_id, symbol, quantity) VALUES ('AST-' || lpad(nextval('asset_seq')::text, 6, '0'), %s, %s, %s)", acc, sym, qty, quiet=True)
    return acc, cust


async def cash(acc): return await val("SELECT cash_balance FROM accounts WHERE id = %s", acc)
async def wallet(acc): return await val("SELECT wallet_balance FROM accounts WHERE id = %s", acc)
async def held(acc, sym): return await val("SELECT quantity FROM holdings WHERE account_id = %s AND symbol = %s", acc, sym) or 0
async def price(sym): return await val("SELECT price FROM instruments WHERE symbol = %s", sym)
async def ticket_state(t): return await val("SELECT state FROM tickets WHERE id = %s", t)
async def notes(cust, kind): return await db.fetch("SELECT text FROM notifications WHERE customer_id = %s AND kind = %s ORDER BY id", cust, kind, quiet=True)
async def entries(ref): return await db.fetch("SELECT entry_type, amount FROM ledger_entries WHERE ref_id = %s ORDER BY id", ref, quiet=True)


async def ticket_done(t, timeout=30, states=DONE):
    async def f():
        s = await ticket_state(t)
        return s if s in states else None
    return await until(f, timeout, f"ticket {t} to reach one of {states}")


async def processing_s(ticket):
    """Seconds the last attempt of the ticket's message took in the worker."""
    return float(await val("SELECT extract(epoch FROM finished_at - started_at) FROM queue_messages WHERE ticket_id = %s ORDER BY id DESC LIMIT 1", ticket))


async def send_trade(n, side, sym, qty):
    acc, cust = f"ACC-{n:04d}", f"CUS-{n:04d}"
    tid = await db.fetchval("INSERT INTO trades (id, account_id, customer_id, side, symbol, quantity, status) VALUES ('TRD-' || lpad(nextval('trade_seq')::text, 8, '0'), %s, %s, %s, %s, %s, 'queued') RETURNING id",
                            acc, cust, side, sym, qty, quiet=True)
    t = await tickets.create("trade", "orders", cust, acc, tid)
    await db.execute("UPDATE trades SET ticket_id = %s WHERE id = %s", t, tid, quiet=True)
    await queue.publish("orders", "order", {"trade_id": tid, "account_id": acc, "customer_id": cust, "leg": False, "parent_ticket_id": None},
                        ticket_id=t, customer_id=cust, summary=f"{side} {qty} {sym}")
    return tid, t


async def send_transfer(n, direction, amount, counterparty="bank", approved=False):
    acc, cust = f"ACC-{n:04d}", f"CUS-{n:04d}"
    xid = await db.fetchval("INSERT INTO transfers (id, account_id, customer_id, direction, amount, counterparty, status) VALUES ('TRF-' || lpad(nextval('transfer_seq')::text, 8, '0'), %s, %s, %s, %s, %s, 'queued') RETURNING id",
                            acc, cust, direction, amount, counterparty, quiet=True)
    t = await tickets.create("transfer", "transfers.outbound", cust, acc, xid)
    await db.execute("UPDATE transfers SET ticket_id = %s WHERE id = %s", t, xid, quiet=True)
    await queue.publish("transfers.outbound", "transfer", {"transfer_id": xid, "approved": approved}, ticket_id=t, customer_id=cust, summary=f"{direction} {amount}")
    return xid, t


async def send_sweep(n, cause="card_purchase", delta=-20.0):
    acc, cust = f"ACC-{n:04d}", f"CUS-{n:04d}"
    return await queue.publish("sweeps", "sweep", {"account_id": acc, "customer_id": cust, "cause": cause, "delta": delta}, customer_id=cust)


async def sweeps_idle(timeout=60):
    async def f():
        return await val("SELECT count(*) FROM queue_messages WHERE queue = 'sweeps' AND state IN ('ready', 'retry', 'inflight')") == 0
    await until(f, timeout, "the sweeps queue to drain")


async def run_row(kind):
    return await db.fetchval("INSERT INTO runs (kind, started_by) VALUES (%s, 'backoffice_check') RETURNING id", kind, quiet=True)


async def run_to_end(kind, service, timeout=90):
    rid = await run_row(kind)
    r = await http.call(service, "POST", "/v1/runs", json={"run_id": rid}, raise_for_status=False)
    check(isinstance(r, dict) and r.get("run_id") == rid, f"{service} did not answer the run request: {r}")

    async def f():
        row = await one("SELECT status, detail, ended_at FROM runs WHERE id = %s", rid)
        return row if row["status"] != "running" else None
    row = await until(f, timeout, f"the {kind} run to end")
    check(row["status"] == "completed" and row["detail"] and row["ended_at"], f"{kind} run ended as {row}")
    return row["detail"]


# ---------------------------------------------------------------- cases

BASELINE = {}


async def case_buy_fills():
    acc, cust = await setup(11, cash=50_000, hold={"FXA-EQ": 3})
    t0, last = time.monotonic(), await val("SELECT coalesce(max(id), 0) FROM queue_messages")
    tid, tk = await send_trade(11, "buy", "ACM-EQ", 10)
    check(await ticket_done(tk) == "completed", f"ticket ended {await ticket_state(tk)}")
    tr = await one("SELECT * FROM trades WHERE id = %s", tid)
    check(tr["status"] in ("filled", "partially_filled") and tr["filled_quantity"] >= 1 and tr["avg_price"] > 0, f"trade {tr}")
    q, avg = tr["filled_quantity"], tr["avg_price"]
    px = await price("ACM-EQ")
    check(abs(avg / px - 1) < .01, f"avg price {avg} is far from the quote {px}")
    check(near(await cash(acc), 50_000 - q * avg, .05), f"cash {await cash(acc)} should be {50_000 - q * avg:.2f}")
    check(await held(acc, "ACM-EQ") == q and await held(acc, "FXA-EQ") == 3, "holding of ACM-EQ should equal the fill, FXA-EQ untouched")
    le = await entries(tid)
    check(len(le) == 1 and le[0]["entry_type"] == "trade" and near(le[0]["amount"], -q * avg, .05), f"ledger entries {le}")
    check(await val("SELECT count(*) FROM executions WHERE trade_id = %s", tid) >= 1, "no executions")

    async def si():
        return await one("SELECT * FROM settlement_instructions WHERE trade_id = %s", tid)
    s = await until(si, 15, "a settlement instruction")
    check(s["status"] == "pending" and near(s["notional"], q * avg, .05), f"instruction {s}")

    async def nt():
        return await notes(cust, "order.confirmed")
    n = await until(nt, 15, "an order.confirmed notification")
    check("ACM-EQ" in n[-1]["text"] and "filled" in n[-1]["text"], f"notification {n[-1]}")
    check(await val("SELECT count(*) FROM queue_messages WHERE queue = 'sweeps' AND id > %s AND payload->>'account_id' = %s AND payload->>'cause' = 'trade'", last, acc) == 1, "no sweeps message for the trade")
    BASELINE["order_s"] = p = await processing_s(tk)
    check(p < 3.0, f"the order took {p:.2f}s in the worker, over 3s")
    return f"{tid} {tr['status']} {q}@{avg}, cash {await cash(acc):.2f}, worker {p:.2f}s, end to end {time.monotonic() - t0:.1f}s"


async def case_sell_too_much():
    acc, cust = await setup(12, cash=1000, hold={"FXA-EQ": 5})
    tid, tk = await send_trade(12, "sell", "FXA-EQ", 50)
    check(await ticket_done(tk) == "failed", f"ticket ended {await ticket_state(tk)}")
    tr = await one("SELECT status, reject_reason FROM trades WHERE id = %s", tid)
    check(tr == {"status": "rejected", "reject_reason": "insufficient_position"}, f"trade {tr}")
    check(await held(acc, "FXA-EQ") == 5 and await cash(acc) == 1000 and not await entries(tid), "balances changed by a rejected order")
    check(await val("SELECT reason FROM tickets WHERE id = %s", tk) == "insufficient_position", "ticket reason")

    async def nt():
        return await notes(cust, "order.rejected")
    await until(nt, 15, "an order.rejected notification")
    return "rejected insufficient_position, ticket failed, nothing booked"


async def case_buy_without_cash():
    acc, cust = await setup(13, cash=10)
    tid, tk = await send_trade(13, "buy", "ACM-EQ", 10)
    check(await ticket_done(tk) == "failed", "ticket should fail")
    tr = await one("SELECT status, reject_reason FROM trades WHERE id = %s", tid)
    check(tr["status"] == "rejected" and tr["reject_reason"] == "insufficient_buying_power", f"trade {tr}")
    return "rejected insufficient_buying_power"


async def case_restricted_symbol():
    acc, cust = await setup(14)
    tid, tk = await send_trade(14, "buy", "ORB-EQ", 5)
    check(await ticket_done(tk) == "failed", "ticket should fail")
    tr = await one("SELECT status, reject_reason FROM trades WHERE id = %s", tid)
    check(tr == {"status": "rejected", "reject_reason": "restricted_security"}, f"trade {tr}")
    check(await cash(acc) == 50_000 and await held(acc, "ORB-EQ") == 0, "an order in a restricted symbol moved money")
    return "ORB-EQ rejected restricted_security"


async def case_venue_slow():
    await setup(15)
    await fault("venue_slow", True, factor=15)
    try:
        tid, tk = await send_trade(15, "buy", "ACM-EQ", 30)
        check(await ticket_done(tk, 40) == "completed", "ticket should complete")
        p = await processing_s(tk)
    finally:
        await fault("venue_slow", False)
    check(1.0 < p < 6, f"with venue_slow the order took {p:.2f}s (baseline {BASELINE.get('order_s', 0):.2f}s)")
    return f"order took {p:.2f}s in the worker (baseline {BASELINE.get('order_s', 0):.2f}s)"


async def case_venue_timeout_dlq_replay():
    acc, cust = await setup(16)
    await fault("venue_timeout", True, rate=1)
    try:
        tid, tk = await send_trade(16, "buy", "ACM-EQ", 10)
        seen = set()

        async def f():
            s = await ticket_state(tk)
            seen.add(s)
            return s == "dead_lettered"
        await until(f, 150, "the order to be dead-lettered", .5)
        m = await one("SELECT state, attempt, last_error FROM queue_messages WHERE ticket_id = %s", tk)
        check(m["state"] == "dlq" and m["attempt"] == 3 and "VenueTimeoutError" in m["last_error"], f"message {m}")
        check("retrying" in seen, f"the ticket never showed retrying (saw {seen})")
        check(await val("SELECT status FROM trades WHERE id = %s", tid) == "queued" and await cash(acc) == 50_000, "a timed-out order must not book anything")
    finally:
        await fault("venue_timeout", False)
    check(await queue.replay("orders") >= 1, "replay found no dead letter")
    check(await ticket_done(tk, 30) == "completed", f"ticket after replay: {await ticket_state(tk)}")
    tr = await one("SELECT status, filled_quantity FROM trades WHERE id = %s", tid)
    check(tr["status"] == "filled" and await held(acc, "ACM-EQ") == tr["filled_quantity"], f"trade after replay {tr}")
    return "retried (ticket showed retrying), dead-lettered on attempt 3, completed after replay"


async def case_rebalance_three_legs():
    acc, cust = await setup(17, cash=50_000, hold={"ACM-EQ": 20})
    legs = [{"side": "sell", "symbol": "ACM-EQ", "quantity": 5}, {"side": "buy", "symbol": "NVD-EQ", "quantity": 3}, {"side": "buy", "symbol": "SPY-IX", "quantity": 2}]
    pid = await db.fetchval("INSERT INTO rebalance_proposals (account_id, customer_id, moves, legs, status) VALUES (%s, %s, %s, %s, 'proposed') RETURNING id", acc, cust, db.J([]), db.J(legs), quiet=True)
    tk = await tickets.create("rebalance", "orders", cust, acc, str(pid))
    await queue.publish("orders", "rebalance", {"proposal_id": pid, "account_id": acc, "customer_id": cust}, ticket_id=tk, customer_id=cust, summary="rebalance")
    check(await ticket_done(tk) == "completed", "the rebalance ticket should complete")
    check(await val("SELECT status FROM rebalance_proposals WHERE id = %s", pid) == "applied", "proposal not applied")
    ts = await db.fetch("SELECT id, side, symbol, quantity FROM trades WHERE parent_ticket_id = %s ORDER BY id", tk, quiet=True)
    check([(t["side"], t["symbol"], t["quantity"]) for t in ts] == [(l["side"], l["symbol"], l["quantity"]) for l in legs], f"leg trades {ts}")

    async def filled():
        return await val("SELECT count(*) FROM trades WHERE parent_ticket_id = %s AND status IN ('filled', 'partially_filled')", tk) == 3
    await until(filled, 30, "three filled legs")
    fq = {t["symbol"]: t["filled_quantity"] for t in await db.fetch("SELECT symbol, filled_quantity FROM trades WHERE parent_ticket_id = %s", tk, quiet=True)}   # a venue can cancel the rest of an order
    check(await held(acc, "ACM-EQ") == 20 - fq["ACM-EQ"] and await held(acc, "NVD-EQ") == fq["NVD-EQ"] and await held(acc, "SPY-IX") == fq["SPY-IX"], f"holdings after the legs {fq}")
    check(await val("SELECT count(*) FROM queue_messages WHERE queue = 'orders' AND payload->>'parent_ticket_id' = %s AND state = 'done'", tk) == 3, "three order messages should be done")
    return f"3 legs queued and filled ({fq}); holdings match the fills"


async def case_deposit_and_card_withdrawal():
    acc, cust = await setup(18, cash=1000)
    t0 = time.monotonic()
    xid, tk = await send_transfer(18, "in", 250, "bank")
    check(await ticket_done(tk, 40) == "completed", f"deposit ticket {await ticket_state(tk)}")
    x = await one("SELECT status, risk_score FROM transfers WHERE id = %s", xid)
    check(x["status"] == "completed" and x["risk_score"] is not None, f"transfer {x}")
    check(near(await cash(acc), 1250), f"cash {await cash(acc)}")
    check([e["entry_type"] for e in await entries(xid)] == ["ach_pending"], "ledger entry")
    check(await val("SELECT count(*) FROM ach_entries WHERE transfer_id = %s", xid) == 1, "ach entry")

    async def nt():
        return await notes(cust, "transfer.completed")
    await until(nt, 15, "a transfer.completed notification")
    out = f"deposit {time.monotonic() - t0:.1f}s end to end"
    acc, cust = await setup(19, cash=5000, wallet=300)
    xid, tk = await send_transfer(19, "out", 300, "card_account")
    check(await ticket_done(tk, 40) == "completed", f"withdrawal ticket {await ticket_state(tk)}")
    check(near(await cash(acc), 4700) and near(await wallet(acc), 600), f"cash {await cash(acc)}, wallet {await wallet(acc)}")
    check([e["entry_type"] for e in await entries(xid)] == ["transfer_card_account"], "ledger entry")
    # the other way: from the card account into the brokerage account, and a withdrawal larger than the cash balance
    acc, cust = await setup(20, cash=5000, wallet=800)
    xid, tk = await send_transfer(20, "in", 200, "card_account")
    check(await ticket_done(tk, 40) == "completed" and near(await cash(acc), 5200) and near(await wallet(acc), 600), "card account deposit")
    acc, cust = await setup(29, cash=1500)   # (under $2,000, so the fraud score cannot put it on hold)
    xid, tk = await send_transfer(29, "out", 1900, "bank")
    check(await ticket_done(tk, 40) == "failed", "a withdrawal over the cash balance should fail")
    x = await one("SELECT status, reason FROM transfers WHERE id = %s", xid)
    check(x == {"status": "failed", "reason": "insufficient_funds"} and near(await cash(acc), 1500), f"transfer {x}")
    return f"{out}; card withdrawal (cash -300, wallet +300); card deposit; overdraw rejected insufficient_funds"


async def case_hold_then_approve():
    acc, cust = await setup(21, cash=1000)
    xid, tk = await send_transfer(21, "in", 12_000, "bank")
    check(await ticket_done(tk, 40) == "held", f"ticket {await ticket_state(tk)}")
    x = await one("SELECT status, risk_score, hold_reason FROM transfers WHERE id = %s", xid)
    check(x["status"] == "held" and x["risk_score"] is not None and "10,000" in x["hold_reason"], f"transfer {x}")
    check(await cash(acc) == 1000 and not await entries(xid) and await val("SELECT count(*) FROM ach_entries WHERE transfer_id = %s", xid) == 0, "a held transfer moved money")

    async def nt():
        return await notes(cust, "transfer.held")
    await until(nt, 15, "a transfer.held notification")
    await queue.publish("transfers.outbound", "transfer", {"transfer_id": xid, "approved": True}, ticket_id=tk, customer_id=cust, summary="approved")
    check(await ticket_done(tk, 40, ("completed", "failed")) == "completed", f"ticket after approval {await ticket_state(tk)}")
    check(await val("SELECT status FROM transfers WHERE id = %s", xid) == "completed" and near(await cash(acc), 13_000), "transfer after approval")
    return f"held (risk {x['risk_score']}, '{x['hold_reason']}'), completed after approved=true, cash 13000"


async def case_bank_down():
    acc, cust = await setup(22, cash=1000)
    await fault("bank_down", True, rate=1)
    try:
        xid, tk = await send_transfer(22, "in", 100, "bank")
        await until(lambda: ticket_is(tk, "dead_lettered"), 200, "the transfer to be dead-lettered", .5)
        m = await one("SELECT state, attempt, last_error FROM queue_messages WHERE ticket_id = %s", tk)
        check(m["state"] == "dlq" and m["attempt"] == 3 and "BankUnavailableError" in m["last_error"], f"message {m}")
        check(await val("SELECT status FROM transfers WHERE id = %s", xid) == "queued" and await cash(acc) == 1000 and not await entries(xid), "a failed submission booked something")
    finally:
        await fault("bank_down", False)
    check(await queue.replay("transfers.outbound") >= 1, "no dead letter to replay")
    check(await ticket_done(tk, 40) == "completed" and near(await cash(acc), 1100), "transfer after replay")
    return "503 x3 -> dead-lettered (BankUnavailableError), completed after replay"


async def ticket_is(t, state):
    return await ticket_state(t) == state


async def case_ach_return():
    acc, cust = await setup(23, cash=1000)
    await fault("ach_returns", True, rate=1)
    try:
        xid, tk = await send_transfer(23, "in", 400, "bank")
        check(await ticket_done(tk, 40) == "completed" and near(await cash(acc), 1400), "the deposit should complete first")
        m = await until(lambda: one("SELECT id, extract(epoch FROM available_at - enqueued_at) AS delay, payload FROM queue_messages WHERE queue = 'ach.returns' AND payload->>'transfer_id' = %s", xid), 10, "an ach.returns message")
    finally:
        await fault("ach_returns", False)
    check(19 <= m["delay"] <= 91 and m["payload"]["code"] in ("R01", "R02", "R03"), f"return message {m}")
    await db.execute("UPDATE queue_messages SET available_at = now() WHERE id = %s", m["id"], quiet=True)   # do not wait the 20 to 90 s out

    async def returned():
        return await val("SELECT status FROM transfers WHERE id = %s", xid) == "returned"
    await until(returned, 30, "the transfer to be returned")
    x = await one("SELECT reason FROM transfers WHERE id = %s", xid)
    check(x["reason"] == m["payload"]["code"] and near(await cash(acc), 1000), f"reason {x}, cash {await cash(acc)}")
    check([e["entry_type"] for e in await entries(xid)] == ["ach_pending", "ach_return"], f"ledger {await entries(xid)}")
    check(await val("SELECT return_code FROM ach_entries WHERE transfer_id = %s", xid) == x["reason"], "ach entry return code")

    async def nt():
        return await notes(cust, "transfer.returned")
    await until(nt, 15, "a transfer.returned notification")
    return f"returned {x['reason']} (message delayed {m['delay']:.0f}s), cash back to 1000, entries reversed"


async def case_sweep_top_up():
    acc, cust = await setup(24, cash=5000, wallet=10, floor=100, target=1000)
    t0 = time.monotonic()
    await send_sweep(24)

    async def topped():
        return near(await wallet(acc), 1000)
    await until(topped, 20, "the wallet to be topped up")
    check(near(await cash(acc), 4010), f"cash {await cash(acc)}")
    check(any(e["entry_type"] == "sweep" and near(e["amount"], -990) for e in await entries(await val("SELECT ref_id FROM ledger_entries WHERE account_id = %s AND entry_type = 'sweep'", acc))), "sweep ledger entry")

    async def nt():
        return await notes(cust, "sweep.completed")
    await until(nt, 15, "a sweep.completed notification")
    out = f"wallet 10 -> 1000, cash 5000 -> 4010 in {time.monotonic() - t0:.1f}s"
    acc, cust = await setup(25, cash=5000, wallet=500)                  # inside its band: nothing moves
    await send_sweep(25)
    await sweeps_idle()
    check(await wallet(acc) == 500 and await cash(acc) == 5000, "a wallet inside its band must not move")
    acc, cust = await setup(26, cash=5000, wallet=2500, floor=100, target=1000)   # far over its target: money comes back
    await send_sweep(26, "payroll", 2000)
    await until(lambda: wallet_is(acc, 1000), 20, "the excess to leave the wallet")
    await until(lambda: cash_is(acc, 6500), 10, "the excess to arrive in the cash balance")   # the wallet is debited first, then the ledger posts
    return out + "; in-band wallet untouched; over-target wallet 2500 -> 1000, cash +1500"


async def wallet_is(acc, v):
    return near(await wallet(acc), v)


async def cash_is(acc, v):
    return near(await cash(acc), v)


async def case_card_network_slow():
    await setup(27, cash=5000, wallet=500)
    await fault("card_network_slow", True, factor=10)
    try:
        mid = await send_sweep(27)

        async def done():
            return await one("SELECT extract(epoch FROM finished_at - started_at) AS s, state FROM queue_messages WHERE id = %s AND state = 'done'", mid)
        r = await until(done, 40, "the sweep")
    finally:
        await fault("card_network_slow", False)
    check(r["s"] > 1.5, f"sweep took {r['s']:.2f}s with card_network_slow")
    return f"sweep took {r['s']:.1f}s with the card network 10x slower"


async def case_sweep_worker_stuck():
    await setup(28, cash=5000, wallet=500)
    await sweeps_idle()
    last = await val("SELECT coalesce(max(id), 0) FROM queue_messages")
    await fault("sweep_worker_stuck", True)
    try:
        for _ in range(6):
            await send_sweep(28)
        await asyncio.sleep(7)
        n = await val("SELECT count(*) FROM queue_messages WHERE queue = 'sweeps' AND state = 'ready' AND id > %s", last)
        check(n == 6, f"{n} sweeps messages waiting, expected 6 while the worker is stuck")
    finally:
        await fault("sweep_worker_stuck", False)
    await sweeps_idle(40)
    check(await val("SELECT count(*) FROM queue_messages WHERE queue = 'sweeps' AND payload->>'account_id' = 'ACC-0028' AND state = 'done' AND id > %s", last) == 6, "the messages should all be done")
    return "6 messages waited 7s untouched while stuck, then drained"


async def case_market_data():
    px = await val("SELECT price FROM instruments WHERE symbol = 'NVD-EQ'")
    q = await http.call("market-data-service", "GET", "/v1/quotes/NVD-EQ")
    check(q == {"symbol": "NVD-EQ", "price": px}, f"quote {q}")
    check((await http.call("market-data-service", "GET", "/v1/quotes/NOPE", raise_for_status=False))["error"]["code"] == "not_found", "unknown symbol")
    before = await val("SELECT sum(price) FROM instruments WHERE asset_class <> 'Cash'")
    await fault("market_sell_off", True, drift_pct=-5)
    try:
        async def fell():
            return await val("SELECT sum(price) FROM instruments WHERE asset_class <> 'Cash'") < before * .97
        await until(fell, 40, "a tick of the sell-off to lower prices", 1)
    finally:
        await fault("market_sell_off", False)
    after = await val("SELECT sum(price) FROM instruments WHERE asset_class <> 'Cash'")
    return f"quote ok; sell-off tick moved the sum of prices {before:.0f} -> {after:.0f}"


async def case_runs():
    out = []
    # settlement: instructions pile up as pending, a run with settle_fail at 1 buys every chunk in, a normal run settles
    async def drained():
        return await val("SELECT count(*) FROM queue_messages WHERE queue = 'settlement.instructions' AND state IN ('ready', 'retry', 'inflight')") == 0
    await until(drained, 30, "the settlement queue to drain")
    for i in range(25):
        await queue.publish("settlement.instructions", "instruction", {"trade_id": f"CHK-{int(time.time())}-{i}", "symbol": "ACM-EQ", "notional": 100.0 + i}, summary="check")
    await until(drained, 60, "the settlement queue to drain")
    pending = await val("SELECT count(*) FROM settlement_instructions WHERE status = 'pending'")
    check(pending >= 25, f"{pending} pending instructions")
    bought = await val("SELECT count(*) FROM settlement_instructions WHERE status = 'bought_in'")
    await fault("settle_fail", True, rate=1)
    try:
        d = await run_to_end("settlement", "settlement-service")
    finally:
        await fault("settle_fail", False)
    m = re.fullmatch(r"(\d+) instructions, (\d+) fails", d)
    check(m and int(m[1]) == pending and int(m[2]) == math.ceil(pending / 10), f"settle_fail run detail '{d}' (pending was {pending})")
    check(await val("SELECT count(*) FROM settlement_instructions WHERE status = 'bought_in'") - bought == pending, "failed chunks should be bought in")
    out.append(f"settlement(settle_fail=1) '{d}'")
    for i in range(12):
        await queue.publish("settlement.instructions", "instruction", {"trade_id": f"CHK2-{int(time.time())}-{i}", "symbol": "ACM-EQ", "notional": 50.0}, summary="check")
    await until(drained, 60, "the settlement queue to drain")
    d = await run_to_end("settlement", "settlement-service")
    check(d == "12 instructions, 0 fails" and await val("SELECT count(*) FROM settlement_instructions WHERE status = 'pending'") == 0, f"settlement run detail '{d}'")
    out.append(f"settlement '{d}'")

    d = await run_to_end("pricing", "market-data-service")
    n = await val("SELECT count(*) FROM instruments")
    check(d == f"{n} instruments priced, 60 accounts valued", f"pricing detail '{d}'")
    check(await val("SELECT count(*) FROM price_history WHERE trade_date = current_date") == n and await val("SELECT count(DISTINCT account_id) FROM account_values WHERE as_of > now() - interval '1 minute'") == 60, "pricing rows")
    out.append(f"pricing '{d}'")

    funded = await val("SELECT count(*) FROM customers WHERE funded")
    d = await run_to_end("sweep", "sweep-service", 120)
    check(re.fullmatch(rf"{funded} accounts, \d+ moves", d), f"sweep run detail '{d}'")
    out.append(f"sweep '{d}'")

    before = await val("SELECT coalesce(max(id), 0) FROM queue_messages WHERE queue = 'sweeps'")
    d = await run_to_end("payroll", "ledger-service", 120)
    m = re.fullmatch(r"(\d+) payroll deposits, \$[\d,]+", d)
    check(m and 36 <= int(m[1]) <= 44, f"payroll detail '{d}'")
    check(await val("SELECT count(*) FROM queue_messages WHERE queue = 'sweeps' AND id > %s AND payload->>'cause' = 'payroll'", before) == int(m[1]), "one sweeps message per deposit")
    out.append(f"payroll '{d}'")
    ev = await http.call("ledger-service", "POST", "/v1/balance-events", json={"count": 5})
    check(ev == {"published": 5}, f"balance-events {ev}")
    out.append("balance-events 5")
    await sweeps_idle(120)
    return "; ".join(out)


CASES = [case_buy_fills, case_sell_too_much, case_buy_without_cash, case_restricted_symbol, case_venue_slow, case_venue_timeout_dlq_replay, case_rebalance_three_legs,
         case_deposit_and_card_withdrawal, case_hold_then_approve, case_bank_down, case_ach_return, case_sweep_top_up, case_card_network_slow, case_sweep_worker_stuck,
         case_market_data, case_runs]


async def main(only):
    await db.open_pool(wait_for_schema=False)
    await db.execute("UPDATE faults SET active = false, params = default_params", quiet=True)
    results = []
    try:
        for c in CASES:
            name = c.__name__.removeprefix("case_")
            if only and name not in only:
                continue
            t0 = time.monotonic()
            try:
                detail = await c()
                results.append((name, True, detail, time.monotonic() - t0))
            except Exception as e:
                results.append((name, False, f"{type(e).__name__}: {e}", time.monotonic() - t0))
            print(f"{'PASS' if results[-1][1] else 'FAIL'}  {name:34} {results[-1][3]:6.1f}s  {results[-1][2]}", flush=True)
    finally:
        await db.execute("UPDATE faults SET active = false, params = default_params", quiet=True)
        await db.close_pool()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)} of {len(results)} cases passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(set(sys.argv[1:]))))
