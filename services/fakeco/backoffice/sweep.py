"""sweep-service: keeps each customer's card account (wallet) between its floor and target by moving cash, and runs the nightly sweep."""
import asyncio, time

from pydantic import BaseModel

from .. import customers, db, faults, http, queue, telemetry
from ..app import create_app
from ._common import around, money, notify, outside, start_run

CHUNK = 10
FAR_OVER = 1.5   # a wallet over this many times its target gives money back to the brokerage cash


class Run(BaseModel):
    run_id: int


async def sweep_account(account_id, customer_id):
    """Look at one wallet and move money if its rules say so. Returns (decision, amount): decision is to_wallet_topup, to_cash or none."""
    tracer = telemetry.tracer()
    token = http.customer.set({"customer_id": customer_id, "account_id": account_id})
    try:
        w = await http.call("wallet-balance-service", "GET", f"/v1/wallets/{account_id}", route="/v1/wallets/{account_id}")
        balance, floor, target = w["balance"], w["floor"], w["target"]
        want = round(target - balance, 2) if balance < floor else 0.0
        cash = await http.call("ledger-service", "POST", "/v1/ledger/check-buying-power", json={"account_id": account_id, "amount": want}, route="/v1/ledger/check-buying-power")
        # the card network checks the program limits before money moves; card_network_slow makes it slow
        await outside("program_limits.check", "card-network", around(.35, .2) * await faults.factor("card_network_slow"), **{"program.name": "broker_card"})
        if want > 0:
            move, direction = round(min(want, cash["available"]), 2), "to_wallet_topup"
        elif balance > target * FAR_OVER:
            move, direction = round(balance - target, 2), "to_cash"
        else:
            move, direction = 0.0, "none"
        if move < 1:
            move, direction = 0.0, "none"
        with tracer.start_as_current_span("evaluate_rules", attributes={"sweep.decision": direction, "sweep.amount_usd": move, "wallet.balance_usd": round(balance, 2),
                                                                        "sweep.floor_usd": floor, "sweep.target_usd": target, "balance.cash_usd": cash["available"]}):
            pass
        if move:
            ref = f"SWP-{account_id}-{int(time.time() * 1000)}"
            if direction == "to_wallet_topup":   # cash out of the brokerage account, into the wallet
                await http.call("ledger-service", "POST", "/v1/ledger/post", route="/v1/ledger/post", json={"account_id": account_id, "ref_id": ref, "entries": [{"entry_type": "sweep", "amount": -move}], "position": None})
                await http.call("wallet-balance-service", "POST", f"/v1/wallets/{account_id}/credit", json={"amount": move, "ref_id": ref}, route="/v1/wallets/{account_id}/credit")
                text = f"We moved {money(move)} from your brokerage cash to your card account to keep it above {money(floor)}."
            else:
                await http.call("wallet-balance-service", "POST", f"/v1/wallets/{account_id}/debit", json={"amount": move, "ref_id": ref}, route="/v1/wallets/{account_id}/debit")
                await http.call("ledger-service", "POST", "/v1/ledger/post", route="/v1/ledger/post", json={"account_id": account_id, "ref_id": ref, "entries": [{"entry_type": "sweep", "amount": move}], "position": None})
                text = f"We moved {money(move)} from your card account back to your brokerage cash."
            await notify(customer_id, "sweep.completed", text)
        return direction, move
    finally:
        http.customer.reset(token)


async def handle(m, span):
    p = m["payload"]
    span.set_attributes({"bo.process": "cash_sweep", "account.id": p["account_id"], **await customers.attrs(p["customer_id"]), "sweep.cause": p["cause"], "sweep.delta_usd": p["delta"]})
    direction, move = await sweep_account(p["account_id"], p["customer_id"])
    span.set_attributes({"sweep.decision": direction, "sweep.amount_usd": move})
    return "completed"


async def nightly(sp):
    """Every funded account, in chunks of CHUNK taken at the same time."""
    accounts = await db.fetch("SELECT a.id, a.customer_id FROM accounts a JOIN customers c ON c.id = a.customer_id WHERE c.funded ORDER BY a.id")
    moved, n = 0, 0
    for i in range(0, len(accounts), CHUNK):
        chunk = accounts[i:i + CHUNK]
        with telemetry.tracer().start_as_current_span("sweep.chunk", attributes={"chunk.index": i // CHUNK, "chunk.of": -(-len(accounts) // CHUNK), "sweep.accounts": len(chunk)}) as c:
            results = await asyncio.gather(*(sweep_account(a["id"], a["customer_id"]) for a in chunk))
            c.set_attribute("sweep.moves", sum(1 for _, amount in results if amount))
            moved += sum(1 for _, amount in results if amount)
            n += len(chunk)
    sp.set_attributes({"sweep.accounts": n, "sweep.moves": moved})
    return f"{n} accounts, {moved} moves"


def build():
    app = create_app(workers=[lambda: queue.worker("sweeps", handle, stuck_fault="sweep_worker_stuck")])

    @app.post("/v1/runs")
    async def runs(b: Run):
        return start_run(b.run_id, "sweep.nightly_run", nightly)

    return app
