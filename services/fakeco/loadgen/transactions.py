"""Trades, transfers and balance events through the public API, sized so that most work and a few do not."""
import logging

from .api import OPS_USER
from .util import span

log = logging.getLogger("loadgen")

RESTRICTED_SYMBOL = "ORB-EQ"   # on the restricted list from the start (the seed); the load generator cannot read the list (compliance only)
INSTRUMENTS_TTL_S = 600
# (variant, weight) of a trade
TRADE_VARIANTS = [("buy", .58), ("sell", .26), ("sell_not_held", .05), ("buy_over_cash", .06), ("restricted", .03)]
MODES = [("now", .8), ("pending", .2)]
AFTER_PENDING = [("submit", .45), ("cancel", .30), ("leave", .25)]


def weighted(rng, table):
    return rng.choices([k for k, _ in table], [w for _, w in table])[0]


class Trader:
    def __init__(self, lg):
        self.lg = lg
        self.instruments_cache, self.instruments_at = [], None

    @property
    def api(self):
        return self.lg.api

    async def instruments(self, cid):
        now = self.lg.clock.now()
        if self.instruments_at is None or now - self.instruments_at > INSTRUMENTS_TTL_S or not self.instruments_cache:
            r = await self.api.call(cid, "GET", "/api/instruments")
            if r.ok and isinstance(r.data, list) and r.data:
                self.instruments_cache, self.instruments_at = [i for i in r.data if isinstance(i, dict) and i.get("price")], now
        return self.instruments_cache

    async def cash(self, cid):
        r = await self.api.call(cid, "GET", "/api/summary")
        try:
            return float(r.data["account"]["cash_balance"]) if r.ok else None
        except (KeyError, TypeError, ValueError):
            return None

    async def holdings(self, cid):
        r = await self.api.call(cid, "GET", "/api/holdings")
        return [h for h in r.data if isinstance(h, dict) and h.get("symbol") and h.get("quantity")] if r.ok and isinstance(r.data, list) else []

    # --- trades ---

    async def size_order(self, cid, rng, variant):
        """Returns (side, symbol, quantity, variant) for the variant, or a plainer one when the customer cannot do it (no cash, nothing held)."""
        instruments = await self.instruments(cid)
        if not instruments:
            return None
        cash = await self.cash(cid)
        held = await self.holdings(cid) if variant in ("sell", "sell_not_held") else []
        if variant == "restricted" and any(i["symbol"] == RESTRICTED_SYMBOL for i in instruments):
            return "buy", RESTRICTED_SYMBOL, rng.randint(1, 25), variant
        if variant == "sell_not_held":
            symbols = {h["symbol"] for h in held}
            others = [i for i in instruments if i["symbol"] not in symbols]
            if others:
                return "sell", rng.choice(others)["symbol"], rng.randint(1, 20), variant
        if variant == "sell" and held:
            h = rng.choice(held)
            return "sell", h["symbol"], max(1, int(h["quantity"] * rng.uniform(.05, .5))), variant
        if variant == "buy_over_cash" and cash is not None:
            i = rng.choice(instruments)
            return "buy", i["symbol"], int(cash // i["price"]) + rng.randint(5, 200), variant
        if cash is None:   # unknown balance: a small order
            return "buy", rng.choice(instruments)["symbol"], rng.randint(1, 5), "buy"
        affordable = [i for i in instruments if i["price"] <= cash * .5]
        if not affordable:   # cannot afford anything: the order fails, which is what happens to such a customer
            i = min(instruments, key=lambda x: x["price"])
            return "buy", i["symbol"], 1, "buy_over_cash"
        i = rng.choice(affordable)
        return "buy", i["symbol"], max(1, int(cash * rng.uniform(.02, .4) // i["price"])), "buy"

    async def trade(self, cid, rng, flow="txn_trade", step="place_order", mode=None, variant=None):
        """One order. Returns the trade when it was saved as pending (the caller may follow up), else None."""
        mode = mode or weighted(rng, MODES)
        with span("loadgen trade", cid, flow, step) as sp:
            order = await self.size_order(cid, rng, variant or weighted(rng, TRADE_VARIANTS))
            if order is None:
                return None
            side, symbol, quantity, variant = order
            sp.set_attributes({"app.loadgen.variant": variant, "app.loadgen.mode": mode})
            self.lg.counters.transactions += 1
            r = await self.api.call(cid, "POST", "/api/trades", json={"side": side, "symbol": symbol, "quantity": quantity, "mode": mode},
                                    expect_4xx=variant != "buy" and variant != "sell")
            trade = r.data.get("trade") if r.ok and isinstance(r.data, dict) else None
            return trade if mode == "pending" and isinstance(trade, dict) and trade.get("id") else None

    async def follow_up(self, cid, rng, trade):
        """Some seconds after a pending trade: send it to the back office, cancel it, or leave it for the customer's next chat."""
        action = weighted(rng, AFTER_PENDING)
        if action == "leave":
            return
        await self.lg.clock.sleep(rng.uniform(3, 30))
        with span("loadgen trade", cid, "txn_trade", f"{action}_order"):
            await self.api.call(cid, "POST", f"/api/trades/{trade['id']}/{action}")

    async def trade_and_follow_up(self, cid, rng):
        trade = await self.trade(cid, rng)
        if trade:
            await self.follow_up(cid, rng, trade)

    async def ensure_pending(self, cid, rng, flow):
        """Makes sure the customer has a pending trade for the trades flow to look at."""
        r = await self.api.call(cid, "GET", "/api/trades", params={"status": "pending"})
        if r.ok and isinstance(r.data, list) and not r.data:
            await self.trade(cid, rng, flow=flow, step="save_pending_order", mode="pending", variant="buy")

    # --- transfers ---

    async def transfer(self, cid, rng):
        with span("loadgen transfer", cid, "txn_transfer", "create_transfer") as sp:
            direction = "in" if rng.random() < .55 else "out"
            large = rng.random() < .03
            if large:   # compliance holds these
                direction = "in" if rng.random() < .7 else "out"
                amount = float(rng.randrange(100, 251) * 100)
            else:
                amount = float(rng.randrange(10, 101) * 10)
                if direction == "out":   # most withdrawals are covered by the cash balance
                    cash = await self.cash(cid)
                    if cash is not None and amount > cash:
                        amount = float(int(cash * rng.uniform(.2, .9) // 10 * 10))
                        if amount < 10:
                            direction, amount = "in", float(rng.randrange(10, 101) * 10)
            counterparty = "bank" if rng.random() < .65 else "card_account"
            sp.set_attributes({"app.loadgen.variant": "large" if large else "normal"})
            self.lg.counters.transactions += 1
            await self.api.call(cid, "POST", "/api/transfers", json={"direction": direction, "amount": amount, "counterparty": counterparty})

    # --- balance events ---

    async def balance_events(self, count):
        """Calls of at most 20 each; a failed call drops the rest."""
        while count > 0:
            n = min(20, count)
            with span("loadgen balance events", None, "balance_events", "publish"):
                r = await self.api.call(OPS_USER, "POST", "/api/ops/balance-events", json={"count": n})
            if not r.ok:
                return
            count -= n
