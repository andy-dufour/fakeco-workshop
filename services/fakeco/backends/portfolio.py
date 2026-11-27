"""portfolio-service: holdings, valuations and rebalance plans. The PortfolioSpecialist and RebalanceSpecialist tools call it, and so does concierge-api."""
import asyncio, hashlib, math, random

from fastapi import Request
from opentelemetry import trace
from pydantic import BaseModel

from .. import db, faults, queue, tickets
from ..agents.totals import ORDER, percents
from ..app import ApiError, create_app, or_disconnect, who

FUND = {"Stocks": "SPY-IX", "Bonds": "AGG-BD", "Real Estate": "VNQ-RE", "Commodities": "GLD-CM"}   # what a rebalance buys for a class
TARGETS = {"conservative": {"Stocks": 30, "Bonds": 50, "Cash": 10, "Real Estate": 5, "Commodities": 5},
           "moderate": {"Stocks": 55, "Bonds": 30, "Cash": 5, "Real Estate": 5, "Commodities": 5},
           "aggressive": {"Stocks": 75, "Bonds": 12, "Cash": 3, "Real Estate": 5, "Commodities": 5}}
HORIZON_SHIFT = {"under 3 years": -10, "3-5 years": -5, "5-10 years": 0, "10+ years": 5}   # points moved from bonds to stocks
MAX_LEGS = 8
POSITIONS = ("SELECT h.asset_id, h.symbol, i.name, i.asset_class, h.quantity, i.price FROM holdings h JOIN instruments i ON i.symbol = h.symbol "
             "WHERE h.account_id = %s ORDER BY h.quantity * i.price DESC NULLS LAST")
_outliving = set()   # queries that run on after their caller hung up
MAX_OUTLIVING = 3
STORE_LATENCY_S = (.02, .3)   # list_assets, get_asset_valuation and get_asset_positions read a data store a long way off: with the rest of the call (90-240 ms), 100-500 ms per tool call


class Positions(BaseModel):
    assets: list[str]


class Proposal(BaseModel):
    risk: str
    horizon: str
    leave_alone: str | None = None


async def _positions(account_id):
    """The positions of an account plus its cash, largest first. While `portfolio_breakdown_timeout` is on the query is slow, and for a share of the calls
    it takes longer than the caller waits. It has no statement timeout, so it runs on after the caller hung up."""
    f = await faults.get("portfolio_breakdown_timeout")
    if f is None:
        rows = await db.fetch(POSITIONS, account_id)
    elif random.random() < float(f.get("rate", .5)) and len(_outliving) < MAX_OUTLIVING:
        task = asyncio.create_task(db.fetch(POSITIONS, account_id, delay_s=random.uniform(15, 40), attrs={"app.outlived_cancelled_parent": True}))
        _outliving.add(task)
        task.add_done_callback(_outliving.discard)
        rows = await asyncio.shield(task)
    else:
        rows = await db.fetch(POSITIONS, account_id, delay_s=random.uniform(.03, .06) * float(f.get("factor", 12)))
    cash = await db.fetchval("SELECT cash_balance FROM accounts WHERE id = %s", account_id)
    out = [{**r, "market_value": round(r["quantity"] * r["price"], 2)} for r in rows]
    return out + [{"asset_id": None, "symbol": "CASH", "name": "Cash", "asset_class": "Cash", "quantity": cash, "price": 1.0, "market_value": round(cash, 2)}]


def _allocation(positions):
    by = {}
    for p in positions:
        by[p["asset_class"]] = by.get(p["asset_class"], 0.0) + p["market_value"]
    pct = percents(by)
    return [{"asset_class": c, "percent": pct[c], "value": round(by[c], 2)} for c in ORDER if c in by]


def _whole(shares, total=100):
    """Shares scaled to whole numbers that add up to `total`."""
    s = sum(shares.values()) or 1
    exact = {k: v * total / s for k, v in shares.items()}
    out = {k: math.floor(x) for k, x in exact.items()}
    for k in sorted(exact, key=lambda k: exact[k] - out[k], reverse=True)[:total - sum(out.values())]:
        out[k] += 1
    return out


def plan(positions, risk, horizon, leave_alone):
    """(moves, legs): the percentage moves by asset class, and the orders that make them."""
    now = {a["asset_class"]: a for a in _allocation(positions)}
    current = {c: now[c]["percent"] if c in now else 0 for c in ORDER}
    target = dict(TARGETS.get(risk.lower(), TARGETS["moderate"]))
    shift = HORIZON_SHIFT.get(horizon.lower(), 0)
    shift = max(-target["Stocks"] + 5, min(shift, target["Bonds"] - 5))
    target["Stocks"] += shift
    target["Bonds"] -= shift
    left = next((c for c in ORDER if leave_alone and c.lower() == leave_alone.lower()), None)
    if left:   # that class keeps its share; the others share what is left in the proportions of the target
        rest = _whole({c: v for c, v in target.items() if c != left}, 100 - current[left])
        target = {**rest, left: current[left]}
    moves = [{"asset_class": c, "from": current[c], "to": target[c]} for c in ORDER if current[c] != target[c]]
    total, legs = sum(p["market_value"] for p in positions), []
    for m in moves:
        amount, cls = (m["to"] - m["from"]) / 100 * total, m["asset_class"]
        if cls == "Cash":   # cash is what the orders leave behind
            continue
        if amount > 0:
            price = next((p["price"] for p in positions if p["symbol"] == FUND[cls]), None)
            legs.append(("buy", FUND[cls], amount, price))
        else:
            need = -amount
            for p in [p for p in positions if p["asset_class"] == cls][:3]:
                take = min(p["market_value"], need)
                if take >= p["price"]:
                    legs.append(("sell", p["symbol"], take, p["price"]))
                    need -= take
    # "Cash" is the cash balance plus the money-market holdings. When the orders spend more than the cash balance, money-market holdings are sold
    # for the difference (with a margin for prices that move before the orders fill), so that the balance does not end below zero.
    short = sum(a if side == "buy" else -a for side, _, a, _ in legs) - next((p["market_value"] for p in positions if p["symbol"] == "CASH"), 0.0)
    if short > 0:
        short = short * 1.02 + 25
        for p in [p for p in positions if p["asset_class"] == "Cash" and p["symbol"] != "CASH"]:
            take = min(p["market_value"], short)
            if take >= p["price"]:
                legs.append(("sell", p["symbol"], take, p["price"]))
                short -= take
    legs.sort(key=lambda leg: leg[0] != "sell")   # sells first: when the list is cut to MAX_LEGS, it is buys that are dropped
    return moves, legs[:MAX_LEGS]


def build():
    app = create_app()

    @app.get("/v1/portfolio/breakdown")
    async def breakdown(request: Request):
        _, account_id = who()

        async def work():
            positions = await _positions(account_id)
            sp = trace.get_current_span()
            sp.set_attribute("portfolio.positions", len(positions))
            if await faults.hit("totals_bug") and len(positions) > 1:
                # a position whose instrument has no price yet (a new listing): it comes back without a value
                p = random.choice(positions[:-1])
                p["price"] = p["market_value"] = None
                sp.set_attributes({"portfolio.unpriced_positions": 1, "portfolio.unpriced_symbol": p["symbol"]})
            return {"account_id": account_id, "positions": [{k: p[k] for k in ("symbol", "asset_class", "quantity", "price", "market_value")} for p in positions]}
        return await or_disconnect(request, work())

    @app.get("/v1/portfolio/summary")
    async def summary():
        _, account_id = who()
        acct = await db.fetchrow("SELECT id, cash_balance, wallet_balance FROM accounts WHERE id = %s", account_id)
        if not acct:
            raise ApiError(404, "not_found", f"no account {account_id}")
        positions = await _positions(account_id)
        invested = round(sum(p["market_value"] for p in positions[:-1]), 2)
        return {"account": {"id": account_id, "cash_balance": round(acct["cash_balance"], 2), "wallet_balance": round(acct["wallet_balance"], 2), "invested_value": invested,
                            "total_value": round(invested + acct["cash_balance"], 2)}, "allocation": _allocation(positions)}

    @app.get("/v1/holdings")
    async def holdings():
        _, account_id = who()
        return [p for p in (await _positions(account_id))[:-1]]

    @app.get("/v1/instruments")
    async def instruments():
        return await db.fetch("SELECT symbol, name, asset_class, price FROM instruments WHERE asset_class <> 'Cash' ORDER BY symbol")

    @app.get("/v1/accounts/{account_id}/assets")
    async def list_assets(account_id: str):
        _, mine = who()
        if account_id != mine:
            raise ApiError(403, "forbidden", f"account {account_id} is not this customer's account")
        rows = await db.fetch("SELECT h.asset_id, h.symbol, i.name FROM holdings h JOIN instruments i ON i.symbol = h.symbol WHERE h.account_id = %s ORDER BY h.symbol", account_id,
                              latency_s=random.uniform(*STORE_LATENCY_S))
        trace.get_current_span().set_attributes({"account.id": account_id, "portfolio.assets": len(rows)})
        return {"account_id": account_id, "assets": rows}

    @app.get("/v1/accounts/{account_id}/assets/{asset_id}/valuation")
    async def valuation(account_id: str, asset_id: str):
        customer_id, mine = who()
        if account_id != mine:
            raise ApiError(403, "forbidden", f"account {account_id} is not this customer's account")
        h = await db.fetchrow("SELECT h.asset_id, h.symbol, i.name, h.quantity, i.price FROM holdings h JOIN instruments i ON i.symbol = h.symbol WHERE h.account_id = %s AND h.asset_id = %s",
                              account_id, asset_id)
        if not h:
            raise ApiError(404, "not_found", f"no asset {asset_id} in account {account_id}")
        # the valuation comes with the daily closes it is based on: 200 to 420 trading days, by customer (their statement period)
        days = 200 + int(hashlib.md5(customer_id.encode()).hexdigest()[:6], 16) % 220
        closes = await db.fetch("SELECT trade_date, close FROM price_history WHERE symbol = %s ORDER BY trade_date DESC LIMIT %s", h["symbol"], days,
                                latency_s=random.uniform(*STORE_LATENCY_S))
        trace.get_current_span().set_attributes({"account.id": account_id, "asset.id": asset_id, "asset.symbol": h["symbol"], "valuation.closes": len(closes)})
        return {**h, "market_value": round(h["quantity"] * h["price"], 2), "as_of": closes[0]["trade_date"] if closes else None,
                "closes": [{"date": c["trade_date"], "close": c["close"]} for c in closes]}

    @app.post("/v1/accounts/{account_id}/positions")
    async def positions(account_id: str, body: Positions):
        """The bulk tool: the positions of many assets in one call. In: asset names (symbols). Out: each name and its position (the value held), and nothing else."""
        _, mine = who()
        if account_id != mine:
            raise ApiError(403, "forbidden", f"account {account_id} is not this customer's account")
        rows = await db.fetch("SELECT h.symbol, h.quantity * i.price AS position FROM holdings h JOIN instruments i ON i.symbol = h.symbol WHERE h.account_id = %s AND h.symbol = ANY(%s)",
                              account_id, body.assets, latency_s=random.uniform(*STORE_LATENCY_S))
        by = {r["symbol"]: r["position"] for r in rows}
        missing = [a for a in body.assets if a not in by]
        if missing:
            raise ApiError(404, "not_found", f"no asset {missing[0]} in account {account_id}")
        trace.get_current_span().set_attributes({"account.id": account_id, "portfolio.assets": len(body.assets)})
        return [{"asset": a, "position": round(by[a], 2)} for a in body.assets]

    @app.post("/v1/rebalance/proposals")
    async def propose(body: Proposal):
        customer_id, account_id = who()
        positions = await _positions(account_id)
        moves, legs = plan(positions, body.risk, body.horizon, body.leave_alone)
        prices = {r["symbol"]: r["price"] for r in await db.fetch("SELECT symbol, price FROM instruments WHERE symbol = ANY(%s)", [s for _, s, _, _ in legs])} if legs else {}
        orders = [{"side": side, "symbol": symbol, "quantity": int(amount // (price or prices[symbol]))} for side, symbol, amount, price in legs]
        orders = [o for o in orders if o["quantity"] >= 1]
        pid = await db.fetchval("INSERT INTO rebalance_proposals (account_id, customer_id, risk, horizon, leave_alone, moves, legs) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                                account_id, customer_id, body.risk, body.horizon, body.leave_alone, db.J(moves), db.J(orders))
        trace.get_current_span().set_attributes({"rebalance.proposal_id": pid, "rebalance.risk": body.risk, "rebalance.horizon": body.horizon, "rebalance.trades": len(orders)})
        return {"proposal_id": pid, "risk": body.risk, "horizon": body.horizon, "moves": moves, "trades": len(orders)}

    @app.post("/v1/rebalance/proposals/{proposal_id}/apply")
    async def apply(proposal_id: int):
        customer_id, account_id = who()
        p = await db.fetchrow("SELECT id, status, legs FROM rebalance_proposals WHERE id = %s AND account_id = %s", proposal_id, account_id)
        if not p:
            raise ApiError(404, "not_found", f"no rebalance proposal {proposal_id} for this account")
        if p["status"] != "proposed":
            raise ApiError(409, "already_applied", f"rebalance proposal {proposal_id} was applied already")
        tid = await tickets.create("rebalance", "orders", customer_id, account_id, str(proposal_id))
        await db.execute("UPDATE rebalance_proposals SET status = 'queued', ticket_id = %s WHERE id = %s", tid, proposal_id)
        await queue.publish("orders", "rebalance", {"proposal_id": proposal_id, "account_id": account_id, "customer_id": customer_id}, ticket_id=tid, customer_id=customer_id,
                            summary=f"rebalance, {len(p['legs'])} trades")
        trace.get_current_span().set_attributes({"ticket.id": tid, "rebalance.proposal_id": proposal_id})
        return {"ticket_id": tid, "state": "queued", "trades": len(p["legs"])}

    return app
