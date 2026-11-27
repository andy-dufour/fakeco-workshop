"""ledger-service: cash balances, positions and the ledger entries behind them. Also posts the balance events (payroll, card purchases) and runs payroll."""
import asyncio, random

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from .. import customers, db, http, telemetry
from ..app import ApiError, create_app
from ._common import request_sweep, start_run


class Amount(BaseModel):
    account_id: str
    amount: float


class Position(BaseModel):
    symbol: str
    delta_quantity: float


class Holding(BaseModel):
    account_id: str
    symbol: str
    quantity: float


class Entry(BaseModel):
    entry_type: str
    amount: float


class Post(BaseModel):
    account_id: str
    ref_id: str | None = None
    entries: list[Entry]
    position: Position | None = None


class Events(BaseModel):
    count: int


class Run(BaseModel):
    run_id: int


async def _tx(c, sql, *args):
    """One statement inside the posting transaction, with the same CLIENT span db.py makes."""
    op, table = sql.split(None, 1)[0].upper(), next(t for t in ("ledger_entries", "accounts", "holdings") if t in sql)
    with telemetry.tracer().start_as_current_span(f"{op} {table}", kind=SpanKind.CLIENT, attributes={
            "db.system.name": "postgresql", "db.operation.name": op, "db.collection.name": table, "db.query.text": " ".join(sql.split())[:1000]}):
        cur = await c.execute(sql, args)
        return await cur.fetchall() if cur.description else []


async def _balance_event(account_id, customer_id, kind, amount, cause):
    """A change to the card account (the wallet) that the ledger records: the wallet moves, then the sweep worker is asked to look at it."""
    signed = amount if kind == "payroll_deposit" else -amount
    token = http.customer.set({"customer_id": customer_id, "account_id": account_id})
    try:
        with telemetry.tracer().start_as_current_span("balance_event", attributes={
                "account.id": account_id, **await customers.attrs(customer_id), "ledger.entry_type": kind, "ledger.amount_usd": signed}):
            ref = f"BE-{random.getrandbits(40):010x}"
            await db.execute("INSERT INTO ledger_entries (account_id, entry_type, amount, ref_id) VALUES (%s, %s, %s, %s)", account_id, kind, signed, ref)
            await http.call("wallet-balance-service", "POST", f"/v1/wallets/{account_id}/{'credit' if signed > 0 else 'debit'}", json={"amount": amount, "ref_id": ref},
                            route="/v1/wallets/{account_id}/" + ("credit" if signed > 0 else "debit"))
            await request_sweep(account_id, customer_id, cause, signed)
    finally:
        http.customer.reset(token)


async def _events(picks, payroll_share=.5):
    """One balance event on each of the accounts in `picks`, a few at a time. Returns the number done and the totals."""
    sem, out = asyncio.Semaphore(8), {"n": 0, "payroll": 0.0, "purchases": 0.0}

    async def one(a):
        async with sem:
            payroll = random.random() < payroll_share
            amount = round(random.lognormvariate(7.5, .45), 2) if payroll else round(random.lognormvariate(3.8, .8), 2)
            if not payroll:  # a card purchase cannot be larger than what is on the card
                w = await http.call("wallet-balance-service", "GET", f"/v1/wallets/{a['id']}", route="/v1/wallets/{account_id}")
                amount = min(amount, round(w["balance"], 2))
                payroll = amount < 1
                if payroll:
                    amount = round(random.lognormvariate(7.5, .45), 2)
            await _balance_event(a["id"], a["customer_id"], "payroll_deposit" if payroll else "card_purchase", amount, "payroll" if payroll else "card_purchase")
            out["n"] += 1
            out["payroll" if payroll else "purchases"] += amount
    await asyncio.gather(*(one(a) for a in picks))
    return out


def build():
    app = create_app()

    @app.post("/v1/ledger/check-buying-power")
    async def check_buying_power(b: Amount):
        cash = await db.fetchval("SELECT cash_balance FROM accounts WHERE id = %s", b.account_id)
        if cash is None:
            raise ApiError(404, "not_found", f"no account {b.account_id}")
        short = max(0.0, b.amount - cash)
        trace.get_current_span().set_attributes({"account.id": b.account_id, "balance.cash_usd": round(cash, 2), "balance.shortfall_usd": round(short, 2)})
        return {"ok": short <= 0, "available": round(cash, 2), "shortfall": round(short, 2)}

    @app.post("/v1/ledger/check-position")
    async def check_position(b: Holding):
        held = await db.fetchval("SELECT quantity FROM holdings WHERE account_id = %s AND symbol = %s", b.account_id, b.symbol) or 0
        trace.get_current_span().set_attributes({"account.id": b.account_id, "position.symbol": b.symbol, "position.held": held, "position.requested": b.quantity})
        return {"ok": held >= b.quantity, "held": held}

    @app.post("/v1/ledger/post")
    async def post(b: Post):
        sp = trace.get_current_span()
        total = round(sum(e.amount for e in b.entries), 2)
        sp.set_attributes({"account.id": b.account_id, "ledger.entries": len(b.entries), "ledger.amount_usd": total, "ledger.ref_id": b.ref_id or ""})
        async with db.pool().connection() as c:
            async with c.transaction():
                for e in b.entries:
                    await _tx(c, "INSERT INTO ledger_entries (account_id, entry_type, amount, ref_id) VALUES (%s, %s, %s, %s)", b.account_id, e.entry_type, e.amount, b.ref_id)
                rows = await _tx(c, "UPDATE accounts SET cash_balance = cash_balance + %s WHERE id = %s RETURNING cash_balance", total, b.account_id)
                if not rows:
                    raise ApiError(404, "not_found", f"no account {b.account_id}")  # leaving the block rolls the entries back
                if b.position and b.position.delta_quantity:
                    p = b.position
                    held = await _tx(c, "UPDATE holdings SET quantity = quantity + %s WHERE account_id = %s AND symbol = %s RETURNING quantity", p.delta_quantity, b.account_id, p.symbol)
                    if not held:
                        if p.delta_quantity < 0:
                            raise ApiError(409, "insufficient_position", f"{b.account_id} holds no {p.symbol}")
                        held = await _tx(c, "INSERT INTO holdings (asset_id, account_id, symbol, quantity) VALUES ('AST-' || lpad(nextval('asset_seq')::text, 6, '0'), %s, %s, %s) "
                                            "ON CONFLICT (account_id, symbol) DO UPDATE SET quantity = holdings.quantity + EXCLUDED.quantity RETURNING quantity",
                                         b.account_id, p.symbol, p.delta_quantity)
                    if held[0]["quantity"] < -1e-9:
                        raise ApiError(409, "insufficient_position", f"{b.account_id} holds less than {-p.delta_quantity} of {p.symbol}")
                    if held[0]["quantity"] <= 1e-9:
                        await _tx(c, "DELETE FROM holdings WHERE account_id = %s AND symbol = %s", b.account_id, p.symbol)
        return {"posted": len(b.entries), "cash_balance": round(rows[0]["cash_balance"], 2)}

    @app.get("/v1/ledger/records")
    async def records(ref_id: str):
        trace.get_current_span().set_attribute("ledger.ref_id", ref_id)
        return {"entries": await db.fetch("SELECT id, account_id, entry_type, amount, created_at FROM ledger_entries WHERE ref_id = %s ORDER BY id", ref_id)}

    @app.post("/v1/balance-events")
    async def balance_events(b: Events):
        accounts = await db.fetch("SELECT id, customer_id FROM accounts")
        out = await _events(random.choices(accounts, k=max(0, min(b.count, 1000))))
        trace.get_current_span().set_attributes({"balance_events.published": out["n"]})
        return {"published": out["n"]}

    @app.post("/v1/runs")
    async def runs(b: Run):
        async def payroll(sp):
            """About 40 customers get a payroll deposit; each one puts a message on the sweeps queue."""
            accounts = await db.fetch("SELECT id, customer_id FROM accounts ORDER BY random() LIMIT %s", random.randint(36, 44))
            out = await _events(accounts, payroll_share=1.0)
            sp.set_attributes({"payroll.deposits": out["n"], "payroll.total_usd": round(out["payroll"], 2)})
            return f"{out['n']} payroll deposits, ${out['payroll']:,.0f}"
        return start_run(b.run_id, "payroll.run", payroll)

    return app
