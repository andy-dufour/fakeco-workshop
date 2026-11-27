"""compliance-service: screens orders against the restricted list and transfers for money laundering."""

from opentelemetry import trace
from pydantic import BaseModel

from .. import db, telemetry
from ..app import create_app
from ._common import around, outside

VELOCITY_LIMIT = 50   # transfers in 24 hours


class Order(BaseModel):
    account_id: str
    symbol: str
    side: str
    quantity: float


class Aml(BaseModel):
    transfer_id: str
    account_id: str
    amount: float
    direction: str


def build():
    app = create_app()

    @app.post("/v1/compliance/screen-order")
    async def screen_order(b: Order):
        row = await db.fetchrow("SELECT reason FROM restricted_list WHERE symbol = %s", b.symbol)
        recent = await db.fetchval("SELECT count(*) FROM trades WHERE account_id = %s AND symbol = %s AND created_at > now() - interval '1 day'", b.account_id, b.symbol)
        with telemetry.tracer().start_as_current_span("position_limit.check", attributes={"order.symbol": b.symbol, "order.quantity": b.quantity, "trades.last_day": recent}):
            pass
        blocked = row is not None
        trace.get_current_span().set_attributes({"account.id": b.account_id, "order.symbol": b.symbol, "compliance.restricted": blocked})
        return {"allowed": not blocked, "reason": row["reason"] if row else None}

    @app.post("/v1/compliance/aml-screen")
    async def aml_screen(b: Aml):
        await outside("sanctions.screen", "screening-vendor", around(.07), **{"transfer.id": b.transfer_id})
        n = await db.fetchval("SELECT count(*) FROM transfers WHERE account_id = %s AND created_at > now() - interval '1 day'", b.account_id)
        cleared = n <= VELOCITY_LIMIT
        trace.get_current_span().set_attributes({"transfer.id": b.transfer_id, "account.id": b.account_id, "aml.transfers_24h": n, "aml.cleared": cleared})
        return {"cleared": cleared, "reason": None if cleared else f"{n} transfers in 24 hours"}

    return app
