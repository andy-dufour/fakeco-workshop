"""The handler of the transfers.outbound queue (transfers-service runs it as the worker): limits, screening, risk, then the money moves."""
from .. import config, customers, db, http, queue, telemetry
from ._common import money, notify, request_sweep

DAILY_LIMIT_USD = 25_000
FRAUD_HOLD_SCORE = 0.8
REASONS = {"invalid_transfer": "the transfer is not valid", "daily_limit_exceeded": "it is over the daily transfer limit", "aml_not_cleared": "it did not pass our compliance screening",
           "insufficient_funds": "there is not enough cash in your account", "insufficient_wallet_funds": "there is not enough on your card account"}


async def reject(x, reason, span):
    span.set_attributes({"transfer.status": "failed", "transfer.reject_reason": reason})
    await db.execute("UPDATE transfers SET status = 'failed', reason = %s, updated_at = now() WHERE id = %s", reason, x["id"])
    word = "deposit" if x["direction"] == "in" else "withdrawal"
    await notify(x["customer_id"], "transfer.failed", f"Your {word} of {money(x['amount'])} could not be completed: {REASONS.get(reason, reason)}.")
    return "failed", reason


async def handle(m, span):
    p = m["payload"]
    tracer = telemetry.tracer()
    x = await db.fetchrow("SELECT * FROM transfers WHERE id = %s", p["transfer_id"])
    if not x:
        return "failed", "transfer_not_found"
    acct, cust, amount, out = x["account_id"], x["customer_id"], x["amount"], x["direction"] == "out"
    span.set_attributes({"bo.process": "cash_transfer", "account.id": acct, **await customers.attrs(cust), "transfer.id": x["id"], "transfer.direction": x["direction"],
                         "transfer.amount_usd": amount, "transfer.counterparty": x["counterparty"], "transfer.approved": bool(p.get("approved"))})
    if x["status"] in ("completed", "returned", "failed"):   # a message that comes twice does not move money twice
        span.set_attribute("transfer.status", x["status"])
        return ("failed", x["reason"]) if x["status"] == "failed" else "completed"
    await queue.set_ticket(m["ticket_id"], "processing", m["attempt"])
    token = http.customer.set({"customer_id": cust, "account_id": acct})
    try:
        with tracer.start_as_current_span("transfer.validate"):
            ok = amount > 0 and x["direction"] in ("in", "out") and x["counterparty"] in ("bank", "card_account") and await db.fetchval("SELECT 1 FROM accounts WHERE id = %s", acct)
        if not ok:
            return await reject(x, "invalid_transfer", span)

        with tracer.start_as_current_span("limits.check", attributes={"limit.daily_usd": DAILY_LIMIT_USD}) as lc:
            today = await db.fetchval("SELECT coalesce(sum(amount), 0) FROM transfers WHERE account_id = %s AND status = 'completed' AND created_at > now() - interval '1 day' AND id <> %s", acct, x["id"])
            lc.set_attribute("limit.used_usd", round(today, 2))
        if today + amount > DAILY_LIMIT_USD:
            return await reject(x, "daily_limit_exceeded", span)

        aml = await http.call("compliance-service", "POST", "/v1/compliance/aml-screen", route="/v1/compliance/aml-screen",
                              json={"transfer_id": x["id"], "account_id": acct, "amount": amount, "direction": x["direction"]})
        if not aml["cleared"]:
            return await reject(x, "aml_not_cleared", span)
        score = (await http.call("fraud-service", "POST", "/v1/fraud/score-transfer", route="/v1/fraud/score-transfer",
                                 json={"transfer_id": x["id"], "account_id": acct, "amount": amount, "direction": x["direction"]}))["score"]
        span.set_attribute("risk.score", score)

        if not p.get("approved") and (amount >= config.HOLD_AMOUNT_USD or score >= FRAUD_HOLD_SCORE):
            reason = f"amount over ${config.HOLD_AMOUNT_USD:,}" if amount >= config.HOLD_AMOUNT_USD else f"risk score {score:.2f}"
            await db.execute("UPDATE transfers SET status = 'held', risk_score = %s, hold_reason = %s, updated_at = now() WHERE id = %s", score, reason, x["id"])
            await notify(cust, "transfer.held", f"Your {'withdrawal' if out else 'deposit'} of {money(amount)} is on hold for a review by our compliance team ({reason}).")
            span.set_attributes({"transfer.status": "held", "transfer.hold_reason": reason})
            return "held", reason

        if out:   # a withdrawal cannot be larger than the cash in the account
            bp = await http.call("ledger-service", "POST", "/v1/ledger/check-buying-power", route="/v1/ledger/check-buying-power", json={"account_id": acct, "amount": amount})
            if not bp["ok"]:
                return await reject(x, "insufficient_funds", span)

        if x["counterparty"] == "card_account":
            # the money moves at once between the card account (the wallet) and the cash balance
            if out:
                await ledger_post(x, "transfer_card_account", -amount)
                await http.call("wallet-balance-service", "POST", f"/v1/wallets/{acct}/credit", json={"amount": amount, "ref_id": x["id"]}, route="/v1/wallets/{account_id}/credit")
            else:
                try:
                    await http.call("wallet-balance-service", "POST", f"/v1/wallets/{acct}/debit", json={"amount": amount, "ref_id": x["id"]}, route="/v1/wallets/{account_id}/debit")
                except http.ServiceError as e:
                    if e.status == 409:
                        return await reject(x, "insufficient_wallet_funds", span)
                    raise
                try:
                    await ledger_post(x, "transfer_card_account", amount)
                except Exception:   # put the money back before the message is tried again
                    await http.call("wallet-balance-service", "POST", f"/v1/wallets/{acct}/credit", json={"amount": amount, "ref_id": x["id"]}, route="/v1/wallets/{account_id}/credit")
                    raise
        else:
            try:
                await http.call("ach-gateway", "POST", "/v1/ach/entries", json={"transfer_id": x["id"], "direction": x["direction"], "amount": amount}, route="/v1/ach/entries")
            except http.ServiceError as e:
                if e.status == 503:
                    raise queue.Retry("partner bank unavailable (HTTP 503)", "BankUnavailableError")
                raise
            await ledger_post(x, "ach_debit" if out else "ach_pending", -amount if out else amount)

        await db.execute("UPDATE transfers SET status = 'completed', risk_score = %s, hold_reason = NULL, reason = NULL, updated_at = now() WHERE id = %s", score, x["id"])
        span.set_attribute("transfer.status", "completed")
        how = "your card account" if x["counterparty"] == "card_account" else "your bank account"
        await notify(cust, "transfer.completed", f"Your {'withdrawal of ' + money(amount) + ' to' if out else 'deposit of ' + money(amount) + ' from'} {how} is complete.")
        await request_sweep(acct, cust, "transfer", -amount if out else amount)
        return "completed"
    finally:
        http.customer.reset(token)


async def ledger_post(x, entry_type, signed):
    await http.call("ledger-service", "POST", "/v1/ledger/post", route="/v1/ledger/post",
                    json={"account_id": x["account_id"], "ref_id": x["id"], "entries": [{"entry_type": entry_type, "amount": signed}], "position": None})
