"""venue-gateway: sends an order to a trading venue (a fake FIX session) and returns the executions."""
import asyncio, random

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from .. import faults, telemetry
from ..app import ApiError, create_app
from ._common import around, regular_hours

VENUES = [("XNYS", .5), ("XNAS", .4), ("ARCX", .1)]
HOLD_S = 7.0   # past the order-router's 6 s limit


class Order(BaseModel):
    trade_id: str
    symbol: str
    side: str
    quantity: float
    price: float


def split(total, n):
    """total shares in n parts, each at least 1."""
    cuts = sorted(random.sample(range(1, total), n - 1)) if n > 1 else []
    return [b - a for a, b in zip([0] + cuts, cuts + [total])]


def build():
    app = create_app()

    @app.post("/v1/venue/orders")
    async def new_order(b: Order):
        sp = trace.get_current_span()
        venue = "ARCX" if not regular_hours() else random.choices([v for v, _ in VENUES], [w for _, w in VENUES])[0]
        qty = int(b.quantity)
        slow = await faults.factor("venue_slow")
        sp.set_attributes({"order.id": b.trade_id, "order.symbol": b.symbol, "order.side": b.side, "order.quantity": qty, "routing.venue": venue, "venue.slow_factor": slow})
        with telemetry.tracer().start_as_current_span("FIX NewOrderSingle", kind=SpanKind.INTERNAL, attributes={"fix.msg_type": "D", "fix.side": "1" if b.side == "buy" else "2", "routing.venue": venue}) as fix:
            if await faults.hit("venue_timeout"):
                # the venue accepts the order and never sends an execution report
                fix.set_attribute("fix.execution_report", "none")
                await asyncio.sleep(HOLD_S)
                raise ApiError(504, "venue_timeout", "no execution report")
            await asyncio.sleep(around(.04 * slow))
            nfill = min(qty, random.choices([1, 2, 3], [.6, .3, .1])[0])
            cancelled = nfill >= 2 and random.random() < .02   # the last part of the order is cancelled by the venue
            fills = []
            for i, q in enumerate(split(qty, nfill)):
                await asyncio.sleep(around(.06 * slow))
                cut = cancelled and i == nfill - 1
                price = round(b.price * random.uniform(.998, 1.002), 2)
                with telemetry.tracer().start_as_current_span("fix.execution_report", attributes={"fix.exec_type": "4" if cut else "F", "fix.last_qty": 0 if cut else q, "fix.last_px": price}):
                    pass
                if not cut:
                    fills.append({"quantity": q, "price": price})
        status = "partially_filled" if cancelled else "filled"
        sp.set_attributes({"order.status": status, "order.filled_quantity": sum(f["quantity"] for f in fills)})
        return {"venue": venue, "status": status, "fills": fills}

    return app
