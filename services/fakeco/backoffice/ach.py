"""ach-gateway: submits ACH entries to the partner bank, and processes the entries the bank sends back."""
import asyncio, random

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from .. import customers, db, faults, http, queue, telemetry
from ..app import ApiError, create_app
from ._common import around, money, notify, outside

RETURNS = {"R01": "insufficient funds", "R02": "account closed", "R03": "no account / unable to locate account"}


class Entry(BaseModel):
    transfer_id: str
    direction: str
    amount: float


async def handle(m, span):
    """The bank sent an entry back: reverse the ledger posting, mark the transfer returned, tell the customer."""
    p = m["payload"]
    t = await db.fetchrow("SELECT * FROM transfers WHERE id = %s", p["transfer_id"])
    if not t:
        return "failed", "transfer_not_found"
    span.set_attributes({"bo.process": "ach_return", "transfer.id": t["id"], "account.id": t["account_id"], **await customers.attrs(t["customer_id"]), "ach.return_code": p["code"]})
    if t["ticket_id"]:
        span.set_attribute("ticket.parent_id", t["ticket_id"])
    token = http.customer.set({"customer_id": t["customer_id"], "account_id": t["account_id"]})
    try:
        await db.execute("UPDATE ach_entries SET status = 'returned', return_code = %s WHERE transfer_id = %s", p["code"], t["id"])
        entries = (await http.call("ledger-service", "GET", "/v1/ledger/records", params={"ref_id": t["id"]}, route="/v1/ledger/records"))["entries"]
        if not any(e["entry_type"] == "ach_return" for e in entries):   # a message delivered twice must not reverse twice
            posted = sum(e["amount"] for e in entries if e["entry_type"] in ("ach_pending", "ach_debit"))
            await http.call("ledger-service", "POST", "/v1/ledger/post", route="/v1/ledger/post", json={
                "account_id": t["account_id"], "ref_id": t["id"], "entries": [{"entry_type": "ach_return", "amount": -posted}], "position": None})
        await db.execute("UPDATE transfers SET status = 'returned', reason = %s, updated_at = now() WHERE id = %s", p["code"], t["id"])
        await notify(t["customer_id"], "transfer.returned", f"Your {'deposit' if t['direction'] == 'in' else 'withdrawal'} of {money(t['amount'])} was returned by the bank: {RETURNS.get(p['code'], p['code'])} ({p['code']}).")
    finally:
        http.customer.reset(token)
    return "completed"


def build():
    app = create_app(workers=[lambda: queue.worker("ach.returns", handle)])

    @app.post("/v1/ach/entries")
    async def submit(b: Entry):
        sp = trace.get_current_span()
        sp.set_attributes({"transfer.id": b.transfer_id, "transfer.direction": b.direction, "transfer.amount_usd": b.amount})
        done = await db.fetchrow("SELECT id, batch_id FROM ach_entries WHERE transfer_id = %s AND status <> 'returned' ORDER BY id LIMIT 1", b.transfer_id)
        if done:   # the transfer was submitted before: do not send it twice
            sp.set_attribute("idempotency.found", True)
            return {"entry_id": str(done["id"]), "batch_id": done["batch_id"]}
        await outside("external_account.verify", "account-verification-vendor", around(.12))
        entry_type = "debit" if b.direction == "in" else "credit"
        batch = f"B{random.getrandbits(24):06X}"
        with telemetry.tracer().start_as_current_span("nacha.build_entry", attributes={"ach.sec_code": "WEB", "ach.entry_type": entry_type, "ach.batch_id": batch}):
            pass
        if await faults.hit("bank_down"):
            with telemetry.tracer().start_as_current_span("bank.submit", kind=SpanKind.CLIENT, attributes={"server.address": "partner-bank", "http.response.status_code": 503}) as bank:
                await asyncio.sleep(around(.4))
                telemetry.fail(bank, "503", "partner bank unavailable (HTTP 503)")
            raise ApiError(503, "bank_unavailable", "partner bank unavailable")
        await outside("bank.submit", "partner-bank", around(.25), **{"http.response.status_code": 202})
        for i in range(random.choice([1, 2])):
            await outside("bank.status_poll", "partner-bank", around(.2), **{"ach.status": "pending" if i == 0 else "accepted"})
        entry_id = await db.fetchval("INSERT INTO ach_entries (transfer_id, entry_type, batch_id, status) VALUES (%s, %s, %s, 'accepted') RETURNING id", b.transfer_id, entry_type, batch)
        sp.set_attribute("ach.batch_id", batch)
        if await faults.hit("ach_returns"):
            code = random.choice(list(RETURNS))
            await queue.publish("ach.returns", "ach_return", {"transfer_id": b.transfer_id, "code": code}, delay_s=random.uniform(20, 90), summary=f"{code} {b.transfer_id}")
        return {"entry_id": str(entry_id), "batch_id": batch}

    return app
