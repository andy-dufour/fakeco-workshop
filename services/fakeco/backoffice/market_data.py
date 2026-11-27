"""market-data-service: quotes, a ticker that moves the prices, and the nightly pricing run."""
import asyncio, logging

from opentelemetry import trace
from pydantic import BaseModel

from .. import db, faults, telemetry
from ..app import ApiError, create_app
from ._common import around, outside, start_run

log = logging.getLogger(__name__)
TICK_S = 15
CHUNK = 40


class Run(BaseModel):
    run_id: int


async def tick():
    """Every TICK_S seconds a small random walk on every price; market_sell_off adds its drift_pct to each step."""
    while True:
        await asyncio.sleep(TICK_S)
        try:
            sell_off = await faults.get("market_sell_off")
            drift = float(sell_off.get("drift_pct", -0.4)) / 100 if sell_off is not None else 0.0
            with telemetry.tracer().start_as_current_span("market.tick", attributes={"market.drift_pct": drift * 100}):
                n = await db.execute("UPDATE instruments SET price = greatest(0.01, round((price * (1 + (random() - 0.5) * 0.006 + %s))::numeric, 2)) WHERE asset_class <> 'Cash' AND price IS NOT NULL", drift)
                trace.get_current_span().set_attribute("market.instruments", n)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("price tick failed")   # skipped; the next tick tries again


async def pricing(sp):
    """Today's close for every instrument (in chunks), then the value of every account."""
    await outside("quotes.load_closing_prices", "quotes-vendor", around(.3))
    symbols = [r["symbol"] for r in await db.fetch("SELECT symbol FROM instruments ORDER BY symbol")]
    chunks = [symbols[i:i + CHUNK] for i in range(0, len(symbols), CHUNK)]
    for i, syms in enumerate(chunks):
        with telemetry.tracer().start_as_current_span("pricing.chunk", attributes={"chunk.index": i, "chunk.of": len(chunks), "pricing.positions": len(syms)}):
            await db.execute("INSERT INTO price_history (symbol, trade_date, close, volume) SELECT symbol, current_date, price, (random() * 5000000)::bigint + 20000 FROM instruments "
                             "WHERE symbol = ANY(%s) AND price IS NOT NULL ON CONFLICT (symbol, trade_date) DO UPDATE SET close = EXCLUDED.close", syms)
    accounts = await db.fetch("SELECT id FROM accounts ORDER BY id")
    with telemetry.tracer().start_as_current_span("pricing.accounts", attributes={"pricing.accounts": len(accounts)}):
        await db.execute("INSERT INTO account_values (account_id, value) SELECT a.id, a.cash_balance + a.wallet_balance + a.mmf_balance + coalesce(sum(h.quantity * i.price), 0) "
                         "FROM accounts a LEFT JOIN holdings h ON h.account_id = a.id LEFT JOIN instruments i ON i.symbol = h.symbol GROUP BY a.id")
    sp.set_attributes({"pricing.chunks": len(chunks), "pricing.accounts": len(accounts)})
    return f"{len(symbols)} instruments priced, {len(accounts)} accounts valued"


def build():
    app = create_app(workers=[tick])

    @app.get("/v1/quotes/{symbol}")
    async def quote(symbol: str):
        price = await db.fetchval("SELECT price FROM instruments WHERE symbol = %s", symbol)
        if price is None:
            raise ApiError(404, "not_found", f"no quote for {symbol}")
        trace.get_current_span().set_attributes({"quote.symbol": symbol, "quote.price": price})
        return {"symbol": symbol, "price": price}

    @app.post("/v1/runs")
    async def runs(b: Run):
        return start_run(b.run_id, "pricing.nightly", pricing)

    return app
