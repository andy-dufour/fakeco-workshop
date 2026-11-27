"""wallet-balance-service: the card account (the wallet) of each customer."""
from opentelemetry import trace
from pydantic import BaseModel

from .. import db
from ..app import ApiError, create_app


class Move(BaseModel):
    amount: float
    ref_id: str | None = None


def build():
    app = create_app()

    @app.get("/v1/wallets/{account_id}")
    async def balance(account_id: str):
        w = await db.fetchrow("SELECT a.wallet_balance AS balance, c.wallet_floor AS floor, c.wallet_target AS target FROM accounts a JOIN customers c ON c.id = a.customer_id WHERE a.id = %s", account_id)
        if not w:
            raise ApiError(404, "not_found", f"no account {account_id}")
        trace.get_current_span().set_attributes({"account.id": account_id, "wallet.balance_usd": round(w["balance"], 2)})
        return {"balance": round(w["balance"], 2), "floor": w["floor"], "target": w["target"]}

    @app.post("/v1/wallets/{account_id}/credit")
    async def credit(account_id: str, b: Move):
        if b.amount <= 0:
            raise ApiError(400, "bad_amount", "amount must be positive")
        row = await db.fetchrow("UPDATE accounts SET wallet_balance = wallet_balance + %s WHERE id = %s RETURNING wallet_balance", b.amount, account_id)
        if not row:
            raise ApiError(404, "not_found", f"no account {account_id}")
        trace.get_current_span().set_attributes({"account.id": account_id, "wallet.credit_usd": b.amount, "wallet.ref_id": b.ref_id or ""})
        return {"balance": round(row["wallet_balance"], 2)}

    @app.post("/v1/wallets/{account_id}/debit")
    async def debit(account_id: str, b: Move):
        if b.amount <= 0:
            raise ApiError(400, "bad_amount", "amount must be positive")
        row = await db.fetchrow("UPDATE accounts SET wallet_balance = wallet_balance - %s WHERE id = %s AND wallet_balance >= %s RETURNING wallet_balance", b.amount, account_id, b.amount)
        if not row:
            if not await db.fetchval("SELECT 1 FROM accounts WHERE id = %s", account_id):
                raise ApiError(404, "not_found", f"no account {account_id}")
            raise ApiError(409, "insufficient_wallet_balance", "the card account holds less than that")
        trace.get_current_span().set_attributes({"account.id": account_id, "wallet.debit_usd": b.amount, "wallet.ref_id": b.ref_id or ""})
        return {"balance": round(row["wallet_balance"], 2)}

    return app
