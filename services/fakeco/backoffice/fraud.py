"""fraud-service: scores a transfer for risk (0 is safe, 1 is almost certainly fraud)."""
import random

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from .. import db, telemetry
from ..app import create_app
from ._common import around, outside


class Transfer(BaseModel):
    transfer_id: str
    account_id: str
    amount: float
    direction: str


def build():
    app = create_app()

    @app.post("/v1/fraud/score-transfer")
    async def score_transfer(b: Transfer):
        f = await db.fetchrow("SELECT count(*) AS n, coalesce(avg(amount), 0) AS typical FROM transfers WHERE account_id = %s AND status = 'completed'", b.account_id)
        with telemetry.tracer().start_as_current_span("infer_risk_model", kind=SpanKind.INTERNAL, attributes={"model.name": "transfer-risk-gbm", "model.version": "v12"}) as sp:
            await outside("model.infer", "model-serving", around(.045), **{"model.name": "transfer-risk-gbm"})
            # bigger than the customer's usual, money going out, and now and then a pattern the model does not like
            score = .04 + random.random() * .14 + min(.45, b.amount / 40_000) + (.08 if b.direction == "out" else 0) + (.1 if f["n"] and b.amount > 5 * f["typical"] else 0)
            if b.direction == "out" and b.amount >= 2000 and random.random() < .05:
                score += .5
            score = round(min(.99, score), 2)
            sp.set_attribute("risk.score", score)
        trace.get_current_span().set_attributes({"transfer.id": b.transfer_id, "account.id": b.account_id, "transfer.amount_usd": b.amount, "risk.score": score})
        return {"score": score}

    return app
