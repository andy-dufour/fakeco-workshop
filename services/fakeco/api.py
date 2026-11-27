"""concierge-api: the only public API (../SPEC.md section 3). Sign-in, the chat endpoint, and the customer, support, operations and compliance pages.

A customer's pages call the tool backends (the same services the agents use). Staff pages read the tables directly: they look across customers,
which no backend does.
"""
import asyncio, logging, secrets, time, uuid

import httpx
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
from pydantic import BaseModel, Field

from . import config, customers, db, faults, honeycomb, http, queue, telemetry, tickets
from .app import ApiError, create_app, or_disconnect
from .http import ServiceError, call
from .stories import RELEASE_FLAGS

log = logging.getLogger("fakeco.api")
MESSAGE = "id, role, text, cards, outcome, trace_id, feedback, author, created_at"
TRADE = "id, side, symbol, quantity, order_type, status, filled_quantity, avg_price, ticket_id, reject_reason, created_at, updated_at"
TRANSFER = "id, direction, amount, counterparty, status, ticket_id, reason, created_at, updated_at"
RUN_OWNER = {"settlement": "settlement-service", "sweep": "sweep-service", "pricing": "market-data-service", "payroll": "ledger-service"}
HISTORY_MESSAGES = 20
MAX_MESSAGE_CHARS = 2000
PEOPLE_TTL_S = 60
_people, _people_at = {}, 0.0
_health, _health_at = [], 0.0


class Login(BaseModel):
    user_id: str


class NewTrade(BaseModel):
    side: str
    symbol: str
    quantity: int
    mode: str = "now"


class NewTransfer(BaseModel):
    direction: str
    amount: float
    counterparty: str = "bank"


class ChatIn(BaseModel):
    conversation_id: str | None = None
    message: str
    chip: bool = False


class Feedback(BaseModel):
    rating: str = Field(pattern="^(up|down)$")


class Text(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Replay(BaseModel):
    message_ids: list[int] | None = None
    all: bool = False


class Minutes(BaseModel):
    minutes: float = Field(15, ge=1, le=720)


class FaultIn(BaseModel):
    active: bool
    params: dict | None = None
    minutes: float | None = Field(None, gt=0, le=1440)


class LoadgenIn(BaseModel):
    enabled: bool | None = None
    conversations_per_minute: float | None = Field(None, ge=0, le=120)
    transactions_per_minute: float | None = Field(None, ge=0, le=120)
    auto_stories: bool | None = None


class Stats(BaseModel):
    conversations: int = Field(0, ge=0)
    turns: int = Field(0, ge=0)
    transactions: int = Field(0, ge=0)
    errors: int = Field(0, ge=0)


class RunIn(BaseModel):
    kind: str


class Count(BaseModel):
    count: int = Field(ge=1, le=200)


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = None


class Restricted(BaseModel):
    symbol: str
    reason: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------- people and sign-in

async def people():
    """Everyone who can sign in, by id. Kept for a minute: the note of a customer (how many holdings) changes slowly."""
    global _people, _people_at
    if not _people or time.monotonic() - _people_at > PEOPLE_TTL_S:
        out = {}
        for r in await db.fetch("SELECT c.id, c.name, c.first_name, c.email, c.whale, a.id AS account_id, (SELECT count(*) FROM holdings h WHERE h.account_id = a.id) AS n "
                                "FROM customers c JOIN accounts a ON a.customer_id = c.id ORDER BY c.id", quiet=True):
            out[r["id"]] = {"id": r["id"], "role": "customer", "name": r["name"], "email": r["email"], "note": f"{'Whale: ' if r['whale'] else ''}{r['n']} holdings",
                            "account_id": r["account_id"], "first_name": r["first_name"]}
        for r in await db.fetch("SELECT id, name, role, email, hidden FROM staff ORDER BY id", quiet=True):
            out[r["id"]] = {"id": r["id"], "role": r["role"], "name": r["name"], "email": r["email"], "note": r["role"].capitalize(), "hidden": r["hidden"]}
        _people, _people_at = out, time.monotonic()
    return _people


def person(p):
    """The Person of the public API."""
    return {k: p[k] for k in ("id", "role", "name", "email", "note")}


async def customer(customer_id):
    p = (await people()).get(customer_id)
    if not p or p["role"] != "customer":
        raise ApiError(404, "not_found", f"no customer {customer_id}")
    return p


def need(*roles):
    """A route dependency: the signed-in person, who must have one of `roles`. It also says who the request is for, on the span and to the calls
    the handler makes (the x-customer-id and x-account-id headers)."""
    async def signed_in(request: Request):
        h = request.headers.get("authorization", "")
        token = h[7:].strip() if h[:7].lower() == "bearer " else ""
        s = await db.fetchrow("SELECT user_id FROM sessions WHERE token = %s", token, quiet=True) if token else None
        p = (await people()).get(s["user_id"]) if s else None
        if not p:
            raise ApiError(401, "unauthorized", "Sign in again.")
        sp = trace.get_current_span()
        sp.set_attribute("app.persona.role", p["role"])
        if p["role"] == "customer":
            sp.set_attributes({"app.customer.id": p["id"], "app.customer.name": p["name"], "account.id": p["account_id"]})
            http.customer.set({"customer_id": p["id"], "account_id": p["account_id"]})
        else:
            sp.set_attribute("app.staff.id", p["id"])
        if p["role"] not in roles:
            raise ApiError(403, "forbidden", f"A {p['role']} person may not do this.")
        return p
    return Depends(signed_in)


async def backend(service, method, path, **kw):
    """Call a backend for the signed-in person. Its 4xx answers (not found, not pending, a bad amount) are the caller's to see; anything else is a 502."""
    try:
        return await call(service, method, path, **kw)
    except ServiceError as e:
        if e.status < 500 and isinstance(e.body, dict):
            err = e.body.get("error") or {}
            raise ApiError(e.status, err.get("code", "bad_request"), err.get("message") or str(e.body.get("detail", "the request was not accepted"))[:300])
        raise


def as_customer(p):
    return {"x-customer-id": p["id"], "x-account-id": p["account_id"]}


# ---------------------------------------------------------------- views

async def ticket_views(rows):
    """Tickets of the public API. One query for all the notes; the place in the queue only for the first tickets that wait."""
    notes = {}
    if rows:
        for n in await db.fetch("SELECT ticket_id, author, text, created_at FROM ticket_notes WHERE ticket_id = ANY(%s) ORDER BY id", [t["id"] for t in rows], quiet=True):
            notes.setdefault(n["ticket_id"], []).append({k: n[k] for k in ("author", "text", "created_at")})
    out, placed = [], 0
    for t in rows:
        ahead = eta = None
        if t["state"] in ("queued", "retrying") and placed < 15:
            placed += 1
            ahead, eta = await tickets.position(t)
        out.append({"id": t["id"], "kind": t["kind"], "state": t["state"], "attempt": t["attempt"], "queue": t["queue"], "customer_id": t["customer_id"], "ref_id": t["ref_id"],
                    "reason": t["reason"], "queue_position": ahead, "eta_seconds": eta, "notes": notes.get(t["id"], []), "created_at": t["created_at"], "updated_at": t["updated_at"]})
    return out


async def tickets_where(state=None, customer_id=None, limit=100):
    return await ticket_views(await db.fetch("SELECT * FROM tickets WHERE (%s::text IS NULL OR state = %s) AND (%s::text IS NULL OR customer_id = %s) ORDER BY created_at DESC, id LIMIT %s",
                                             state or None, state or None, customer_id or None, customer_id or None, max(1, min(int(limit), 200))))


CONVERSATION_ROWS = (
    "SELECT c.id, c.customer_id, cu.name AS customer_name, c.title, c.updated_at, (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count, "
    "(SELECT m.outcome FROM messages m WHERE m.conversation_id = c.id AND m.role = 'assistant' ORDER BY m.id DESC LIMIT 1) AS last_outcome, "
    "EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.feedback = 'down') AS has_negative_feedback "
    "FROM conversations c JOIN customers cu ON cu.id = c.customer_id "
    "WHERE (%s::text IS NULL OR c.customer_id = %s) AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.outcome = %s)) "
    "AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = c.id AND m.feedback = %s)) ORDER BY c.updated_at DESC LIMIT %s")


async def conversation_rows(customer_id=None, outcome=None, feedback=None, limit=50):
    a = [customer_id or None, outcome or None, feedback or None]
    return await db.fetch(CONVERSATION_ROWS, a[0], a[0], a[1], a[1], a[2], a[2], max(1, min(int(limit), 200)))


STORY = ("SELECT name, title, description, kind, active AND (expires_at IS NULL OR expires_at > now()) AS active, "
         "CASE WHEN active AND expires_at > now() THEN expires_at END AS expires_at, parts FROM stories")
FAULT = ("SELECT name, description, active AND (expires_at IS NULL OR expires_at > now()) AS active, params, CASE WHEN active THEN story END AS story, "
         "CASE WHEN active AND expires_at > now() THEN expires_at END AS expires_at FROM faults")


def story_view(s):
    return {**{k: s[k] for k in ("name", "title", "description", "kind", "active", "expires_at")}, "faults": list(dict.fromkeys(p["fault"] for p in s["parts"]))}


async def stop_story(name):
    """Turn a story's faults off and put their params back. Returns True when the story was on."""
    await db.execute("UPDATE faults SET active = false, params = default_params, story = NULL, starts_at = NULL, expires_at = NULL WHERE story = %s", name)
    return bool(await db.execute("UPDATE stories SET active = false, expires_at = NULL WHERE name = %s AND active", name))


async def surge():
    """What the faults in force ask of the load generator (SPEC 3.5)."""
    out = {"volume": 1.0, "flows": {}, "balance_events_factor": 1.0, "valuation_per_minute": 0.0}
    for name in ("load_sell_off", "load_rebalance_rush"):
        p = await faults.get(name)
        if p is not None:
            out["volume"] *= float(p.get("volume", 1))
            for flow, weight in (p.get("flows") or {}).items():
                out["flows"][flow] = out["flows"].get(flow, 1.0) * float(weight)
    p = await faults.get("payday_burst")
    if p is not None:
        out["balance_events_factor"] = float(p.get("factor", 1))
    p = await faults.get("asset_valuation_rush")
    if p is not None:
        out["valuation_per_minute"] = float(p.get("per_minute", 2.5))
    return out


async def loadgen_view():
    rows = {r["key"]: r["value"] for r in await db.fetch("SELECT key, value FROM settings WHERE key IN ('loadgen', 'loadgen_stats')", quiet=True)}
    return {**rows["loadgen"], "stats": rows["loadgen_stats"], "surge": await surge()}


async def health():
    """Ask every service's /healthz (no spans: this is a poll). Kept for a few seconds, so several open pages cost one round."""
    global _health, _health_at
    if _health and time.monotonic() - _health_at < 4:
        return _health

    async def one(c, name, kind):
        t0 = time.monotonic()
        try:
            ok = (await c.get(config.url(name) + "/healthz")).status_code == 200
        except httpx.HTTPError:
            ok = False
        return {"name": name, "kind": kind, "healthy": ok, "latency_ms": round((time.monotonic() - t0) * 1000)}
    with suppress_instrumentation():
        async with httpx.AsyncClient(timeout=2.0) as c:
            _health = list(await asyncio.gather(*(one(c, name, kind) for name, (kind, _, _) in config.SERVICES.items() if name != config.service_name())))
    _health.insert(0, {"name": config.service_name(), "kind": "edge", "healthy": True, "latency_ms": 0})
    _health_at = time.monotonic()
    return _health


async def change_marker(flag, at):
    """The Honeycomb marker of a flag change, on or off: a release flag looks like a release ("deploy <build id>"), any other like a configuration change.
    A release flag also makes the build id the service.version of every service (they read it from the settings table, see app.watch_version)."""
    if flag in RELEASE_FLAGS:
        build = secrets.token_hex(4)[:7]
        await db.execute("INSERT INTO settings VALUES ('service_version', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", db.J(build), quiet=True)
        telemetry.set_version(build)
        await honeycomb.marker(f"deploy {build}", "deploy", at)
    else:
        await honeycomb.marker("configuration change", "config", at)


async def janitor():
    """Housekeeping for a demo that runs for days: end stories whose time is up, and delete old rows so the small disk does not fill."""
    n = 0
    while True:
        try:
            for s in await db.fetch("SELECT name FROM stories WHERE active AND expires_at <= now()", quiet=True):
                await stop_story(s["name"])
            for f in await db.fetch("UPDATE faults SET active = false, params = default_params, starts_at = NULL, expires_at = NULL WHERE active AND story IS NULL AND expires_at <= now() RETURNING name", quiet=True):
                if f["name"] in RELEASE_FLAGS:   # a release flag with a time limit that is up: the release is rolled back
                    asyncio.create_task(change_marker(f["name"], time.time()))
            if n % 20 == 0:   # every 10 minutes
                await db.execute("DELETE FROM queue_messages WHERE state IN ('done', 'failed') AND finished_at < now() - interval '6 hours'", quiet=True)
                await db.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE updated_at < now() - interval '3 days')", quiet=True)
                await db.execute("DELETE FROM conversations WHERE updated_at < now() - interval '3 days'", quiet=True)
                await db.execute("DELETE FROM notifications WHERE created_at < now() - interval '3 days'", quiet=True)
                await db.execute("DELETE FROM sessions WHERE created_at < now() - interval '7 days'", quiet=True)
                await db.execute("DELETE FROM processed_messages WHERE processed_at < now() - interval '1 day'", quiet=True)
                await db.execute("DELETE FROM account_values WHERE as_of < now() - interval '3 days'", quiet=True)
            n += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("janitor: unexpected error")
        await asyncio.sleep(30)


def build():
    app = create_app(workers=[janitor])

    @app.exception_handler(RequestValidationError)
    async def bad_request(request, e):
        first = e.errors()[0] if e.errors() else {}
        return JSONResponse({"error": {"code": "bad_request", "message": f"{'.'.join(str(x) for x in first.get('loc', [])[1:])}: {first.get('msg', 'not valid')}"}}, status_code=422)

    # ------------------------------------------------------------ 3.1 session

    @app.get("/api/config")
    async def get_config():
        return {"service_name": "concierge-web", "version": telemetry.version(), "otlp_path": "/otlp", "environment": config.environment(), "honeycomb_trace_url": await honeycomb.trace_url()}

    @app.get("/api/personas")
    async def personas():
        ps = (await people()).values()
        return {"customers": [person(p) for p in ps if p["role"] == "customer"], "staff": [person(p) for p in ps if p["role"] != "customer" and not p["hidden"]]}

    @app.post("/api/login")
    async def login(body: Login):
        p = (await people()).get(body.user_id)
        if not p:
            raise ApiError(404, "not_found", f"no person {body.user_id}")
        token = secrets.token_urlsafe(24)
        await db.execute("INSERT INTO sessions (token, user_id, role) VALUES (%s, %s, %s)", token, p["id"], p["role"])
        trace.get_current_span().set_attributes({"app.persona.role": p["role"], "app.customer.id" if p["role"] == "customer" else "app.staff.id": p["id"],
                                                                  **({"app.customer.name": p["name"]} if p["role"] == "customer" else {})})
        return {"token": token, "user": person(p)}

    @app.get("/api/me")
    async def me(p=need("customer", "support", "operations", "compliance")):
        return {**person(p), **({"account_id": p["account_id"]} if p["role"] == "customer" else {})}

    # ------------------------------------------------------------ 3.2 customer

    @app.get("/api/summary")
    async def summary(p=need("customer")):
        return {"customer": person(p), **await backend("portfolio-service", "GET", "/v1/portfolio/summary", route="/v1/portfolio/summary")}

    @app.get("/api/holdings")
    async def holdings(p=need("customer")):
        return await backend("portfolio-service", "GET", "/v1/holdings", route="/v1/holdings")

    @app.get("/api/instruments")
    async def instruments(p=need("customer")):
        return await backend("portfolio-service", "GET", "/v1/instruments", route="/v1/instruments")

    @app.get("/api/trades")
    async def trades(status: str = "all", p=need("customer")):
        return (await backend("trades-service", "GET", "/v1/trades", params={"status": status}, route="/v1/trades"))["trades"]

    @app.post("/api/trades")
    async def new_trade(body: NewTrade, p=need("customer")):
        return await backend("trades-service", "POST", "/v1/trades", json=body.model_dump(), route="/v1/trades")

    @app.post("/api/trades/{trade_id}/submit")
    async def submit_trade(trade_id: str, p=need("customer")):
        return await backend("trades-service", "POST", f"/v1/trades/{trade_id}/submit", route="/v1/trades/{id}/submit")

    @app.post("/api/trades/{trade_id}/cancel")
    async def cancel_trade(trade_id: str, p=need("customer")):
        return await backend("trades-service", "POST", f"/v1/trades/{trade_id}/cancel", route="/v1/trades/{id}/cancel")

    @app.get("/api/transfers")
    async def transfers(p=need("customer")):
        return await backend("transfers-service", "GET", "/v1/transfers", route="/v1/transfers")

    @app.post("/api/transfers")
    async def new_transfer(body: NewTransfer, p=need("customer")):
        r = await backend("transfers-service", "POST", "/v1/transfers", json=body.model_dump(), route="/v1/transfers")
        return {"transfer": r["transfer"], "ticket_id": r["ticket_id"]}

    @app.get("/api/tickets")
    async def my_tickets(p=need("customer")):
        return await tickets_where(customer_id=p["id"])

    @app.get("/api/tickets/{ticket_id}")
    async def my_ticket(ticket_id: str, p=need("customer")):
        t = await db.fetchrow("SELECT * FROM tickets WHERE id = %s AND customer_id = %s", ticket_id, p["id"])
        if not t:
            raise ApiError(404, "not_found", f"no ticket {ticket_id}")
        return (await ticket_views([t]))[0]

    @app.get("/api/notifications")
    async def notifications(p=need("customer")):
        return await db.fetch("SELECT id, kind, text, created_at FROM notifications WHERE customer_id = %s ORDER BY id DESC LIMIT 50", p["id"])

    # ------------------------------------------------------------ 3.3 chat

    @app.get("/api/conversations")
    async def conversations(p=need("customer")):
        return await db.fetch("SELECT c.id, c.title, c.updated_at, (SELECT count(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count FROM conversations c "
                              "WHERE c.customer_id = %s ORDER BY c.updated_at DESC LIMIT 30", p["id"])

    @app.get("/api/conversations/{conversation_id}")
    async def conversation(conversation_id: str, p=need("customer")):
        if not await db.fetchval("SELECT 1 FROM conversations WHERE id = %s AND customer_id = %s", conversation_id, p["id"], quiet=True):
            raise ApiError(404, "not_found", "no such conversation")
        return {"id": conversation_id, "customer_id": p["id"], "messages": await db.fetch(f"SELECT {MESSAGE} FROM messages WHERE conversation_id = %s ORDER BY id", conversation_id)}

    @app.post("/api/chat")
    async def chat(body: ChatIn, request: Request, p=need("customer")):
        text = body.message.strip()[:MAX_MESSAGE_CHARS]
        if not text:
            raise ApiError(422, "bad_request", "the message is empty")
        version = request.headers.get("x-app-version")
        cid = body.conversation_id
        if cid:
            if not await db.fetchval("SELECT 1 FROM conversations WHERE id = %s AND customer_id = %s", cid, p["id"]):
                raise ApiError(404, "not_found", "no such conversation")
        else:
            cid = uuid.uuid4().hex
            await db.execute("INSERT INTO conversations (id, customer_id, title) VALUES (%s, %s, %s)", cid, p["id"], text[:80])
        sp = trace.get_current_span()
        # app.conversation.id here; gen_ai.conversation.id belongs to the agent, cycle, model and tool spans only
        sp.set_attributes({"app.conversation.id": cid, "app.chat.chip": body.chip, "app.version": version or config.LATEST_APP_VERSION})
        rows = await db.fetch("SELECT role, text, cards FROM messages WHERE conversation_id = %s ORDER BY id DESC LIMIT %s", cid, HISTORY_MESSAGES + 1)
        rows.reverse()
        if rows and rows[-1]["role"] == "user" and rows[-1]["text"] == text:
            history = rows[:-1]   # the same message again after no answer came: do not store it twice
            sp.set_attribute("app.chat.retry", True)
        else:
            await db.execute("INSERT INTO messages (conversation_id, role, text) VALUES (%s, 'user', %s)", cid, text)
            history = rows[-HISTORY_MESSAGES:]
        ask = {"conversation_id": cid, "message": text, "chip": body.chip, "history": history, "app_version": version,
               "customer": {"id": p["id"], "account_id": p["account_id"], "first_name": p["first_name"], "name": p["name"]}}
        try:
            r = await or_disconnect(request, call("concierge-orchestrator", "POST", "/v1/chat", json=ask, timeout=config.HANG_CAP_S + 15, route="/v1/chat"), cap_s=config.HANG_CAP_S)
        except (ServiceError, httpx.HTTPError) as e:
            telemetry.fail(sp, type(e).__name__, str(e)[:300] or "no answer from the Concierge")
            raise ApiError(502, "concierge_unavailable", "The Concierge could not answer. Try again.")
        m = await db.fetchrow(f"INSERT INTO messages (conversation_id, role, text, cards, outcome, trace_id, tool_calls) VALUES (%s, 'assistant', %s, %s, %s, %s, %s) RETURNING {MESSAGE}",
                              cid, r["answer"], db.J(r["cards"]), r["outcome"], r["trace_id"], db.J(r["tool_calls"]))
        await db.execute("UPDATE conversations SET updated_at = now() WHERE id = %s", cid)
        sp.set_attributes({"app.chat.outcome": r["outcome"], "app.chat.cards": len(r["cards"])})
        return {"conversation_id": cid, "message": m}

    @app.post("/api/messages/{message_id}/feedback")
    async def feedback(message_id: int, body: Feedback, p=need("customer")):
        m = await db.fetchrow("UPDATE messages SET feedback = %s WHERE id = %s AND role = 'assistant' AND conversation_id IN (SELECT id FROM conversations WHERE customer_id = %s) "
                              "RETURNING conversation_id, outcome, trace_id", body.rating, message_id, p["id"])
        if not m:
            raise ApiError(404, "not_found", "no such message")
        trace.get_current_span().set_attributes({"app.feedback.rating": body.rating, "app.conversation.id": m["conversation_id"], "app.chat.outcome": m["outcome"] or "",
                                                 "app.feedback.trace_id": m["trace_id"] or ""})
        return {"ok": True}

    # ------------------------------------------------------------ 3.4 support

    @app.get("/api/support/customers")
    async def support_customers(q: str = "", p=need("support", "operations")):
        q = q.strip().lower()
        return [person(c) for c in (await people()).values() if c["role"] == "customer" and (not q or q in c["id"].lower() or q in c["name"].lower() or q in c["email"].lower())][:60]

    @app.get("/api/support/customers/{customer_id}")
    async def support_customer(customer_id: str, p=need("support", "operations")):
        c = await customer(customer_id)
        trace.get_current_span().set_attributes({"app.customer.id": c["id"], "app.customer.name": c["name"]})
        s = await backend("portfolio-service", "GET", "/v1/portfolio/summary", headers=as_customer(c), route="/v1/portfolio/summary")
        return {"customer": person(c), **s,
                "trades": await db.fetch(f"SELECT {TRADE} FROM trades WHERE account_id = %s ORDER BY created_at DESC, id DESC LIMIT 10", c["account_id"]),
                "transfers": await db.fetch(f"SELECT {TRANSFER} FROM transfers WHERE account_id = %s ORDER BY created_at DESC, id DESC LIMIT 10", c["account_id"]),
                "tickets": await tickets_where(customer_id=c["id"], limit=10), "conversations": await conversation_rows(customer_id=c["id"], limit=10)}

    @app.get("/api/support/conversations")
    async def support_conversations(customer_id: str = "", outcome: str = "", feedback: str = "", limit: int = 50, p=need("support", "operations")):
        return await conversation_rows(customer_id, outcome, feedback, limit)

    @app.get("/api/support/conversations/{conversation_id}")
    async def support_conversation(conversation_id: str, p=need("support", "operations")):
        c = await db.fetchrow("SELECT c.id, c.customer_id, cu.name AS customer_name FROM conversations c JOIN customers cu ON cu.id = c.customer_id WHERE c.id = %s", conversation_id)
        if not c:
            raise ApiError(404, "not_found", "no such conversation")
        trace.get_current_span().set_attributes({**await customers.attrs(c["customer_id"]), "app.conversation.id": conversation_id})
        return {**c, "messages": await db.fetch(f"SELECT {MESSAGE}, tool_calls FROM messages WHERE conversation_id = %s ORDER BY id", conversation_id)}

    @app.post("/api/support/conversations/{conversation_id}/reply")
    async def support_reply(conversation_id: str, body: Text, p=need("support")):
        c = await db.fetchrow("UPDATE conversations SET updated_at = now() WHERE id = %s RETURNING customer_id", conversation_id)
        if not c:
            raise ApiError(404, "not_found", "no such conversation")
        trace.get_current_span().set_attributes({**await customers.attrs(c["customer_id"]), "app.conversation.id": conversation_id})
        return {"message": await db.fetchrow(f"INSERT INTO messages (conversation_id, role, text, author) VALUES (%s, 'support', %s, %s) RETURNING {MESSAGE}", conversation_id, body.text, p["id"])}

    @app.get("/api/support/tickets")
    async def support_tickets(state: str = "", customer_id: str = "", limit: int = 100, p=need("support", "operations")):
        return await tickets_where(state, customer_id, limit)

    @app.post("/api/support/tickets/{ticket_id}/notes")
    async def ticket_note(ticket_id: str, body: Text, p=need("support", "operations")):
        t = await db.fetchrow("UPDATE tickets SET updated_at = now() WHERE id = %s RETURNING *", ticket_id)
        if not t:
            raise ApiError(404, "not_found", f"no ticket {ticket_id}")
        await db.execute("INSERT INTO ticket_notes (ticket_id, author, text) VALUES (%s, %s, %s)", ticket_id, p["id"], body.text)
        trace.get_current_span().set_attributes({"ticket.id": ticket_id, **await customers.attrs(t["customer_id"])})
        return (await ticket_views([t]))[0]

    # ------------------------------------------------------------ 3.5 operations

    @app.get("/api/ops/services")
    async def ops_services(p=need("operations")):
        return await health()

    @app.get("/api/ops/queues")
    async def ops_queues(p=need("operations")):
        rows = {r["queue"]: r for r in await db.fetch(
            "SELECT queue, count(*) FILTER (WHERE state = 'ready' AND available_at <= now()) AS ready, count(*) FILTER (WHERE state = 'ready' AND available_at > now()) AS scheduled, "
            "count(*) FILTER (WHERE state = 'inflight') AS inflight, count(*) FILTER (WHERE state = 'retry') AS retry, count(*) FILTER (WHERE state = 'dlq') AS dlq, "
            "extract(epoch FROM now() - min(available_at) FILTER (WHERE state = 'ready' AND available_at <= now())) AS oldest_ready_age_s, "
            "count(*) FILTER (WHERE state = 'done' AND finished_at > now() - interval '5 minutes') AS processed_5m, "
            "count(*) FILTER (WHERE state IN ('failed', 'dlq') AND finished_at > now() - interval '5 minutes') AS failed_5m FROM queue_messages GROUP BY queue")}
        zero = {"ready": 0, "scheduled": 0, "inflight": 0, "retry": 0, "dlq": 0, "oldest_ready_age_s": None, "processed_5m": 0, "failed_5m": 0}
        out = [{"queue": q, **{**zero, **{k: v for k, v in (rows.get(q) or {}).items() if k != "queue"}}, "consumer": consumer} for q, consumer in config.QUEUES.items()]
        for o in out:
            o["oldest_ready_age_s"] = round(float(o["oldest_ready_age_s"]), 1) if o["oldest_ready_age_s"] is not None else None
        return out

    def known_queue(name):
        if name not in config.QUEUES:
            raise ApiError(404, "not_found", f"no queue {name}")

    @app.get("/api/ops/queues/{name}/messages")
    async def ops_messages(name: str, state: str = "dlq", limit: int = 50, p=need("operations")):
        known_queue(name)
        if state not in ("ready", "inflight", "retry", "dlq"):
            raise ApiError(400, "bad_state", f"unknown state {state}")
        return await db.fetch("SELECT id, queue, state, attempt, ticket_id, customer_id, last_error, enqueued_at, summary FROM queue_messages WHERE queue = %s AND state = %s "
                              "ORDER BY id DESC LIMIT %s", name, state, max(1, min(limit, 200)))

    @app.post("/api/ops/queues/{name}/replay")
    async def ops_replay(name: str, body: Replay, p=need("operations")):
        known_queue(name)
        if not body.all and not body.message_ids:
            raise ApiError(422, "bad_request", "give message_ids or all")
        n = await queue.replay(name, None if body.all else body.message_ids)
        trace.get_current_span().set_attributes({"messaging.destination.name": name, "queue.replayed": n})
        return {"replayed": n}

    @app.get("/api/ops/stories")
    async def ops_stories(p=need("operations")):
        return [story_view(s) for s in await db.fetch(STORY + " ORDER BY position")]

    @app.post("/api/ops/stories/stop-all")
    async def stop_all(p=need("operations")):
        n = 0
        for s in await db.fetch("SELECT name FROM stories WHERE active"):
            n += await stop_story(s["name"])
        return {"stopped": n}

    @app.post("/api/ops/stories/{name}/start")
    async def start_story(name: str, body: Minutes | None = None, p=need("operations")):
        s = await db.fetchrow("SELECT name, title, parts FROM stories WHERE name = %s", name)
        if not s:
            raise ApiError(404, "not_found", f"no story {name}")
        await stop_story(name)
        t0, length = time.time(), (body.minutes if body else 15) * 60
        by_fault = {}
        for part in s["parts"]:
            by_fault.setdefault(part["fault"], []).append(part)
        for fault, parts in by_fault.items():
            # a fault in two parts (the partial fix): one row, with the later parts as a schedule in its params
            parts.sort(key=lambda x: x["from"])
            params = dict(parts[0]["params"])
            if len(parts) > 1:
                params["then"] = [{"at": t0 + x["from"] * length, "params": x["params"]} for x in parts[1:]]
            await db.execute("UPDATE faults SET active = true, params = %s, story = %s, starts_at = to_timestamp(%s), expires_at = to_timestamp(%s) WHERE name = %s",
                             db.J(params), name, t0 + parts[0]["from"] * length, t0 + max(x["to"] for x in parts) * length, fault)
        await db.execute("UPDATE stories SET active = true, started_at = to_timestamp(%s), expires_at = to_timestamp(%s), started_by = %s WHERE name = %s", t0, t0 + length, p["id"], name)
        trace.get_current_span().set_attributes({"app.story.name": name, "app.story.minutes": length / 60})
        if config.story_markers():
            asyncio.create_task(honeycomb.marker(f"story: {s['title']}", "story", t0, t0 + length))
        if name in config.story_build_markers():
            # looks like a deploy marker, so the audience sees "a release went out" and not which story
            asyncio.create_task(honeycomb.marker(f"deploy {secrets.token_hex(4)[:7]}", "deploy", t0))
        return story_view(await db.fetchrow(STORY + " WHERE name = %s", name))

    @app.post("/api/ops/stories/{name}/stop")
    async def stop_one(name: str, p=need("operations")):
        s = await db.fetchrow(STORY + " WHERE name = %s", name)
        if not s:
            raise ApiError(404, "not_found", f"no story {name}")
        await stop_story(name)
        trace.get_current_span().set_attribute("app.story.name", name)
        return story_view({**s, "active": False, "expires_at": None})

    @app.get("/api/ops/faults")
    async def ops_faults(p=need("operations")):
        return await db.fetch(FAULT + " ORDER BY name")

    @app.put("/api/ops/faults/{name}")
    async def set_fault(name: str, body: FaultIn, p=need("operations")):
        n = await db.execute("UPDATE faults SET active = %s, params = CASE WHEN %s THEN coalesce(%s::jsonb, params - 'then') ELSE default_params END, story = NULL, starts_at = NULL, "
                             "expires_at = CASE WHEN %s THEN now() + make_interval(secs => %s) END WHERE name = %s",
                             body.active, body.active, db.J(body.params) if body.params is not None else None, body.active and body.minutes is not None,
                             float(body.minutes or 0) * 60, name)
        if not n:
            raise ApiError(404, "not_found", f"no fault {name}")
        if config.fault_attributes():
            trace.get_current_span().set_attributes({"app.fault.name": name, "app.fault.active": body.active})
        # deliberately says nothing about which flag or which way: the audience should find the fault from the data, not from the marker
        asyncio.create_task(change_marker(name, time.time()))
        return await db.fetchrow(FAULT + " WHERE name = %s", name)

    @app.get("/api/ops/loadgen")
    async def loadgen(p=need("operations")):
        return await loadgen_view()

    @app.put("/api/ops/loadgen")
    async def set_loadgen(body: LoadgenIn, p=need("operations")):
        await db.execute("UPDATE settings SET value = value || %s WHERE key = 'loadgen'", db.J(body.model_dump(exclude_none=True)))
        return await loadgen_view()

    @app.post("/api/ops/loadgen/stats")
    async def loadgen_stats(body: Stats, p=need("operations")):
        await db.execute("UPDATE settings SET value = jsonb_build_object('conversations', (value->>'conversations')::bigint + %s, 'turns', (value->>'turns')::bigint + %s, "
                         "'transactions', (value->>'transactions')::bigint + %s, 'errors', (value->>'errors')::bigint + %s, "
                         "'last_seen_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')) WHERE key = 'loadgen_stats'",
                         body.conversations, body.turns, body.transactions, body.errors, quiet=True)
        return {"ok": True}

    @app.post("/api/ops/balance-events")
    async def balance_events(body: Count, p=need("operations")):
        return await backend("ledger-service", "POST", "/v1/balance-events", json={"count": body.count}, route="/v1/balance-events")

    @app.get("/api/ops/runs")
    async def runs(p=need("operations")):
        return await db.fetch("SELECT id, kind, status, detail, started_at, ended_at, started_by FROM runs ORDER BY id DESC LIMIT 20")

    @app.post("/api/ops/runs")
    async def start_run(body: RunIn, p=need("operations")):
        if body.kind not in RUN_OWNER:
            raise ApiError(422, "bad_request", f"unknown run kind {body.kind}")
        run = await db.fetchrow("INSERT INTO runs (kind, started_by) VALUES (%s, %s) RETURNING id, kind, status, detail, started_at, ended_at, started_by", body.kind, p["id"])
        trace.get_current_span().set_attributes({"app.run.id": run["id"], "app.run.kind": body.kind})
        try:
            await call(RUN_OWNER[body.kind], "POST", "/v1/runs", json={"run_id": run["id"]}, route="/v1/runs")
        except (ServiceError, httpx.HTTPError) as e:
            await db.execute("UPDATE runs SET status = 'failed', detail = %s, ended_at = now() WHERE id = %s", f"could not start: {type(e).__name__}", run["id"])
            raise ApiError(502, "upstream_error", f"{RUN_OWNER[body.kind]} did not take the run")
        return run

    @app.get("/api/ops/tickets")
    async def ops_tickets(state: str = "", limit: int = 100, p=need("operations")):
        return await tickets_where(state, None, limit)

    # ------------------------------------------------------------ 3.6 compliance

    @app.get("/api/compliance/holds")
    async def holds(p=need("compliance")):
        ps = await people()
        rows = await db.fetch(f"SELECT {TRANSFER}, customer_id, risk_score, hold_reason FROM transfers WHERE status = 'held' ORDER BY updated_at")
        return [{"transfer": {k: r[k] for k in TRANSFER.split(", ")}, "customer": person(ps[r["customer_id"]]), "ticket_id": r["ticket_id"], "risk_score": r["risk_score"],
                 "hold_reason": r["hold_reason"]} for r in rows]

    @app.post("/api/compliance/holds/{transfer_id}/decision")
    async def decide(transfer_id: str, body: DecisionIn, p=need("compliance")):
        r = await backend("transfers-service", "POST", f"/v1/transfers/{transfer_id}/decision", json={**body.model_dump(), "decided_by": p["id"]}, route="/v1/transfers/{id}/decision")
        return {"transfer": r["transfer"]}

    async def restricted_list():
        return await db.fetch("SELECT symbol, reason, added_by, created_at FROM restricted_list ORDER BY created_at DESC, symbol")

    @app.get("/api/compliance/restricted")
    async def restricted(p=need("compliance")):
        return await restricted_list()

    @app.post("/api/compliance/restricted")
    async def restrict(body: Restricted, p=need("compliance")):
        symbol = body.symbol.strip().upper()
        if not await db.fetchval("SELECT 1 FROM instruments WHERE symbol = %s", symbol):
            raise ApiError(404, "unknown_symbol", f"no instrument {symbol}")
        await db.execute("INSERT INTO restricted_list (symbol, reason, added_by) VALUES (%s, %s, %s) ON CONFLICT (symbol) DO UPDATE SET reason = EXCLUDED.reason, added_by = EXCLUDED.added_by",
                         symbol, body.reason, p["id"])
        return await restricted_list()

    @app.delete("/api/compliance/restricted/{symbol}")
    async def unrestrict(symbol: str, p=need("compliance")):
        await db.execute("DELETE FROM restricted_list WHERE symbol = %s", symbol.upper())
        return await restricted_list()

    return app
