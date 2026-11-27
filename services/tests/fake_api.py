"""A small fake of the public API (SPEC section 3), just what the load generator uses. It behaves as the SPEC says and keeps a record of what it was asked,
so a test can check what the load generator did. Ids and options are random on purpose: a client that guesses an id or a chip text gets caught.

Use `Fake()` for the state and `make_app(fake)` for the FastAPI app; `FakeTransport(fake)` runs a client against it in this process (no network).
"""
import asyncio, random, re, time, uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

VERSIONS = ("0.1.0-demo", "0.1.1-demo", "0.2.0-demo")
SYMBOLS = ["FXA-EQ", "NVD-EQ", "ACM-EQ", "ORB-EQ", "BNDX", "TLT-BD", "VNQ-RE", "GLD-CM", "SPY-IX", "AGG-BD"] + [f"X{n:03d}-EQ" for n in range(140)]
STORIES = ["rate-limit-afternoon", "trades-service-slow", "market-sell-off", "busy-rebalance-morning", "portfolio-service-timeouts", "app-hangs-on-send",
           "prompt-change-lowers-quality", "portfolio-specialist-totals-bug", "payday-sweep-burst", "venue-slow-afternoon", "sweep-worker-stuck",
           "bank-outage-ach-returns", "settlement-fails-spike"]
RISKS = ["Risk level: conservative", "Risk level: moderate", "Risk level: aggressive"]
HORIZONS = ["Time horizon: Under 3 years", "Time horizon: 3-5 years", "Time horizon: 5-10 years", "Time horizon: 10+ years"]
LEAVES = ["Leave alone: Stocks", "Leave alone: Bonds", "Leave alone: Cash", "Leave alone: Real Estate", "Leave nothing out"]
CONFIRM = re.compile(r"^(confirm|yes|go ahead|confirmed|apply it)\W*$", re.I)


class Fake:
    def __init__(self, seed=1):
        self.rng = random.Random(seed)
        self.customers = {f"CUS-{n:04d}": {"cash": self.rng.choice([800.0, 5230.10, 12000.0, 90000.0]), "name": f"Customer {n}",
                                           "note": "Whale: 120 holdings" if n >= 58 else "12 holdings"} for n in range(1, 61)}
        self.prices = {s: round(self.rng.uniform(5, 400), 2) for s in SYMBOLS}
        self.holdings = {c: {s: self.rng.randint(1, 200) for s in self.rng.sample(SYMBOLS, 4)} for c in self.customers}
        self.pending = {c: [] for c in self.customers}       # customer -> pending trades (the first ones are seeded below, or not: `seed_pending`)
        self.tokens = {}                                      # token -> user id
        self.down = False                                     # the transport refuses to connect
        self.status_override = None                           # (status, paths) every call to those paths answers this status
        self.outcome = "ok"                                   # outcome of every assistant message
        self.hang = lambda customer, version: False           # chat requests that never get an answer
        self.chat_5xx = 0                                     # the next N chat requests answer 500
        self.config = {"enabled": True, "conversations_per_minute": 30, "transactions_per_minute": 20, "auto_stories": False,
                       "surge": {"volume": 1, "flows": {}, "balance_events_factor": 1, "valuation_per_minute": 0}}
        self.stats_posts, self.stats = [], {"conversations": 0, "turns": 0, "transactions": 0, "errors": 0}
        self.logins, self.chats, self.feedback, self.orders, self.transfers = [], [], [], [], []
        self.balance_calls, self.story_starts, self.run_posts = [], [], []
        self.stories = [{"name": n, "active": False} for n in STORIES]
        self.runs = []
        self.violations = []                                  # things the load generator sent that the cards did not offer
        self.conversations = {}                               # id -> state
        self.tickets = {}                                     # id -> {"kind", "checks"}
        self.msg_id = 0
        self.applied = []                                     # (customer, kind, ticket id) of every confirm that was applied
        self.token_ttl = None                                 # a token stops working after this many calls (401)
        self.token_uses = {}
        self.requests = []                                    # (method, path, headers) of every call that got past sign-in

    def seed_pending(self, per_customer=2):
        for c in self.customers:
            for _ in range(per_customer):
                self.pending[c].append(self.new_trade(c, "buy", self.rng.choice(SYMBOLS), self.rng.randint(1, 50), "pending"))

    def new_trade(self, customer, side, symbol, quantity, status):
        return {"id": f"TRD-{self.rng.randrange(10**7, 10**8)}", "side": side, "symbol": symbol, "quantity": quantity, "order_type": "market", "status": status,
                "filled_quantity": 0, "avg_price": None, "ticket_id": None, "reject_reason": None, "created_at": "2026-11-11T12:00:00Z", "updated_at": "2026-11-11T12:00:00Z"}

    def revoke_tokens(self):
        self.tokens.clear()

    def ticket(self, kind):
        tid = f"TKT-{self.rng.getrandbits(32):08X}"
        self.tickets[tid] = {"kind": kind, "checks": 0}
        return tid

    # --- the Concierge: understands what SPEC 3.3 lists ---

    def understand(self, text, conv):
        low = text.lower()
        if CONFIRM.match(text.strip()):
            return "confirm", None
        if low.strip().rstrip(".!") == "cancel":
            return "cancel", None
        if m := re.search(r"check ticket (TKT-\w+)", text, re.I):
            return "check_ticket", m.group(1)
        for phrase, kind in (("where is my transfer", "transfer"), ("did my trade go through", "trade"), ("did my rebalance finish", "rebalance")):
            if phrase in low:
                mine = [t for t in conv["tickets"] if self.tickets[t]["kind"] == kind]
                return "check_ticket", mine[-1] if mine else None
        if m := re.search(r"(?:change|set) trade (TRD-\d+) to (\d+)", text, re.I):
            return "change_trade", (m.group(1), int(m.group(2)))
        if m := re.search(r"make my (\S+) order (\d+) shares instead", text, re.I):
            return "change_trade", (m.group(1), int(m.group(2)))
        if m := re.search(r"^(add|withdraw) \$(\d+(?:\.\d\d)?) (?:to|from) my account\.?$", text.strip(), re.I):
            return "transfer_amount", (m.group(1).lower(), float(m.group(2)))
        if any(w in low for w in ("transfer money", "move some money", "transfer funds")):
            return "transfer_form", None
        for prefix, intent in (("risk level:", "rb_risk"), ("time horizon:", "rb_horizon"), ("leave alone:", "rb_leave"), ("leave nothing out", "rb_leave")):
            if low.startswith(prefix):
                return intent, text
        if "rebalance" in low:
            return "rb_start", None
        if any(w in low for w in ("worth", "valuation", "value all", "value every", "price everything")):
            return "valuation", None
        if any(w in low for w in ("pending trade", "trades waiting", "trades are pending", "orders haven't")):
            return "pending_trades", None
        if any(w in low for w in ("portfolio", "holdings", "what do i own", "allocation", "asset class", "split up")):
            return "portfolio", None
        return "general", None

    def answer(self, customer, conv, text, chip):
        """Returns (text, cards, intent). Checks that the message is something the last cards offered."""
        intent, arg = self.understand(text, conv)
        bad = lambda why: self.violations.append((customer, text, why))
        cards, reply = [], "OK."
        if intent == "confirm":
            if not conv["confirm"]:
                bad("confirm without an open confirm card")
                return "There is nothing to confirm.", [], "confirm_none"
            if not chip and not CONFIRM.match(text):
                bad("odd confirmation")
            kind, conv["confirm"] = conv["confirm"]["kind"], None
            tid = self.ticket(kind)
            conv["tickets"].append(tid)
            self.applied.append((customer, kind, tid))
            cards, reply = [{"type": "ticket", "ticket_id": tid, "kind": kind, "state": "queued"}], f"Done. Your ticket is {tid}."
        elif intent == "cancel":
            if not conv["confirm"]:
                bad("cancel without an open confirm card")
            conv["confirm"] = None
            reply = "Cancelled. Nothing was changed."
        elif intent == "check_ticket":
            if arg not in self.tickets or arg not in conv["tickets"]:
                bad(f"ticket {arg} was never given in this conversation")
                return "I cannot find that ticket.", [], "check_ticket_unknown"
            t = self.tickets[arg]
            t["checks"] += 1
            state = ["queued", "processing", "completed"][min(2, t["checks"] - 1)]
            cards, reply = [{"type": "ticket", "ticket_id": arg, "kind": t["kind"], "state": state}], f"Ticket {arg} is {state}."
        elif intent == "change_trade":
            ref, qty = arg
            trade = next((t for t in conv["offered_trades"] if ref in (t["id"], t["symbol"])), None)
            if trade is None:
                bad(f"trade {ref} was not offered in this conversation")
                return "I cannot find that trade.", [], "change_trade_unknown"
            conv["confirm"] = {"kind": "trade"}
            cards, reply = [{"type": "confirm", "kind": "trade", "title": f"Change {trade['symbol']} to {qty} shares", "lines": [trade["id"]]}], "Please confirm."
        elif intent == "transfer_form":
            cards, reply = [{"type": "transfer_form"}], "Here is the form."
        elif intent == "transfer_amount":
            conv["confirm"] = {"kind": "transfer"}
            word = "Deposit" if arg[0] == "add" else "Withdrawal"
            cards, reply = [{"type": "confirm", "kind": "transfer", "title": f"{word} ${arg[1]:.2f}", "lines": ["From your bank account"]}], "Please confirm."
        elif intent in ("rb_start", "rb_risk", "rb_horizon", "rb_leave"):
            ladder = {"rb_start": RISKS, "rb_risk": HORIZONS, "rb_horizon": LEAVES}
            if intent != "rb_start" and (not chip or text not in conv["offered_chips"]):
                bad(f"chip {text!r} was not offered")
            if intent == "rb_leave":
                conv["confirm"] = {"kind": "rebalance"}
                cards = [{"type": "rebalance_plan", "moves": [{"asset_class": "Stocks", "from": 62, "to": 55}]},
                         {"type": "confirm", "kind": "rebalance", "title": "Apply the plan", "lines": ["2 trades"]}]
                reply = "Here is the plan."
            else:
                options = self.rng.sample(ladder[intent], self.rng.randint(2, len(ladder[intent])))
                conv["offered_chips"] = set(options)
                cards, reply = [{"type": "chips", "options": options}], "Please choose."
        elif intent == "valuation":
            if customer >= "CUS-0058":
                return "Sorry, that was too much for me to read at once.", [], intent
            cards, reply = [{"type": "valuations", "count": 12, "total": 84211.55}], "Here are the values."
        elif intent == "pending_trades":
            conv["offered_trades"] = [{k: t[k] for k in ("id", "side", "symbol", "quantity", "status")} for t in self.pending[customer]]
            if conv["offered_trades"]:
                cards = [{"type": "trades", "items": conv["offered_trades"]}]
            reply = "Your pending trades."
        elif intent == "portfolio":
            cards, reply = [{"type": "allocation", "items": [{"asset_class": "Stocks", "percent": 55, "value": 46316.35}]}], "Here is your portfolio."
        else:
            reply = "That is a general answer."
        return reply, cards, intent


def make_app(fake):
    app = FastAPI()

    class ApiErr(Exception):
        def __init__(self, status, code, message=""):
            self.status, self.code, self.message = status, code, message

    @app.exception_handler(ApiErr)
    async def api_err(request, e):
        return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=e.status)

    @app.middleware("http")
    async def maybe_broken(request: Request, call_next):
        if fake.status_override and any(request.url.path.startswith(p) for p in fake.status_override[1]):
            return JSONResponse({"error": {"code": "broken", "message": "injected"}}, status_code=fake.status_override[0])
        return await call_next(request)

    def who(request, role):
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        user = fake.tokens.get(token)
        if user and fake.token_ttl:
            fake.token_uses[token] = fake.token_uses.get(token, 0) + 1
            if fake.token_uses[token] > fake.token_ttl:
                fake.tokens.pop(token)
                user = None
        if not user:
            raise ApiErr(401, "unauthorized", "sign in again")
        if not user.startswith(role):
            raise ApiErr(403, "forbidden", "wrong role")
        fake.requests.append((request.method, request.url.path, dict(request.headers)))
        return user

    @app.get("/api/personas")
    async def personas():
        return {"customers": [{"id": c, "role": "customer", "name": v["name"], "email": "x@example.com", "note": v["note"]} for c, v in fake.customers.items()], "staff": []}

    @app.post("/api/login")
    async def login(request: Request):
        user = (await request.json())["user_id"]
        if user not in fake.customers and user != "SYS-loadgen":
            raise ApiErr(404, "unknown_user")
        token = uuid.UUID(int=fake.rng.getrandbits(128)).hex
        fake.tokens[token] = user
        fake.logins.append(user)
        return {"token": token, "user": {"id": user}}

    @app.get("/api/summary")
    async def summary(request: Request):
        c = who(request, "CUS-")
        return {"customer": {"id": c}, "account": {"id": "ACC", "cash_balance": fake.customers[c]["cash"], "wallet_balance": 0, "invested_value": 1, "total_value": 1}, "allocation": []}

    @app.get("/api/holdings")
    async def holdings(request: Request):
        c = who(request, "CUS-")
        return [{"asset_id": f"AST-{i}", "symbol": s, "name": s, "asset_class": "Stocks", "quantity": q, "price": fake.prices[s], "market_value": q * fake.prices[s]}
                for i, (s, q) in enumerate(fake.holdings[c].items())]

    @app.get("/api/instruments")
    async def instruments(request: Request):
        who(request, "CUS-")
        return [{"symbol": s, "name": s, "asset_class": "Stocks", "price": p} for s, p in fake.prices.items()]

    @app.get("/api/trades")
    async def list_trades(request: Request, status: str = "all"):
        c = who(request, "CUS-")
        return fake.pending[c] if status in ("pending", "all") else []

    @app.post("/api/trades")
    async def post_trade(request: Request):
        c = who(request, "CUS-")
        b = await request.json()
        if b.get("symbol") not in fake.prices or not isinstance(b.get("quantity"), int) or b["quantity"] < 1 or b.get("side") not in ("buy", "sell") or b.get("mode") not in ("now", "pending"):
            raise ApiErr(400, "bad_order", "invalid order")
        fake.orders.append((c, b))
        trade = fake.new_trade(c, b["side"], b["symbol"], b["quantity"], "pending" if b["mode"] == "pending" else "queued")
        if b["mode"] == "pending":
            fake.pending[c].append(trade)
            return {"trade": trade, "ticket_id": None}
        return {"trade": trade, "ticket_id": fake.ticket("trade")}

    @app.post("/api/trades/{trade_id}/{action}")
    async def trade_action(request: Request, trade_id: str, action: str):
        c = who(request, "CUS-")
        trade = next((t for t in fake.pending[c] if t["id"] == trade_id), None)
        if trade is None or action not in ("submit", "cancel"):
            raise ApiErr(404, "not_found")
        fake.pending[c].remove(trade)
        fake.orders.append((c, {"action": action, "id": trade_id}))
        trade["status"] = "queued" if action == "submit" else "cancelled"
        return {"trade": trade, "ticket_id": fake.ticket("trade")} if action == "submit" else {"trade": trade}

    @app.post("/api/transfers")
    async def post_transfer(request: Request):
        c = who(request, "CUS-")
        b = await request.json()
        if b.get("direction") not in ("in", "out") or b.get("counterparty") not in ("bank", "card_account") or not isinstance(b.get("amount"), (int, float)) or b["amount"] <= 0:
            raise ApiErr(400, "bad_transfer")
        fake.transfers.append((c, b))
        return {"transfer": {"id": f"TRF-{fake.rng.randrange(10**7, 10**8)}", **b, "status": "queued"}, "ticket_id": fake.ticket("transfer")}

    @app.post("/api/chat")
    async def chat(request: Request):
        c = who(request, "CUS-")
        b = await request.json()
        version = request.headers.get("x-app-version")
        fake.chats.append({"customer": c, "conv": b.get("conversation_id"), "text": b["message"], "chip": b.get("chip"), "version": version})
        if fake.hang(c, version):
            await asyncio.sleep(3600)
        if fake.chat_5xx > 0:
            fake.chat_5xx -= 1
            raise ApiErr(500, "model_unavailable", "the Concierge could not answer")
        cid = b.get("conversation_id")
        if cid is None:
            cid = str(uuid.UUID(int=fake.rng.getrandbits(128)))
            fake.conversations[cid] = {"customer": c, "confirm": None, "tickets": [], "offered_trades": [], "offered_chips": set(), "intents": []}
        conv = fake.conversations.get(cid)
        if conv is None or conv["customer"] != c:
            raise ApiErr(404, "no_conversation")
        text, cards, intent = fake.answer(c, conv, b["message"], bool(b.get("chip")))
        conv["intents"].append(intent)
        fake.chats[-1].update(intent=intent, conv=cid)
        fake.msg_id += 1
        outcome = "context_overflow" if intent == "valuation" and not cards else fake.outcome
        return {"conversation_id": cid, "message": {"id": fake.msg_id, "role": "assistant", "text": text, "cards": cards, "outcome": outcome,
                                                       "trace_id": None, "feedback": None, "author": None, "created_at": "2026-11-11T12:00:00Z"}}

    @app.post("/api/messages/{message_id}/feedback")
    async def feedback(request: Request, message_id: int):
        c = who(request, "CUS-")
        rating = (await request.json())["rating"]
        if rating not in ("up", "down"):
            raise ApiErr(400, "bad_rating")
        fake.feedback.append((c, message_id, rating))
        return {"ok": True}

    # --- operations: SYS-loadgen only ---

    @app.get("/api/ops/loadgen")
    async def get_loadgen(request: Request):
        who(request, "SYS-")
        return {**fake.config, "stats": {**fake.stats, "last_seen_at": None}}

    @app.post("/api/ops/loadgen/stats")
    async def post_stats(request: Request):
        who(request, "SYS-")
        b = await request.json()
        if set(b) - set(fake.stats) or not all(isinstance(v, int) and v >= 0 for v in b.values()):
            raise ApiErr(400, "bad_stats")
        fake.stats_posts.append(b)
        for k, v in b.items():
            fake.stats[k] += v
        return {"ok": True}

    @app.post("/api/ops/balance-events")
    async def balance(request: Request):
        who(request, "SYS-")
        n = (await request.json())["count"]
        if not 1 <= n <= 100:
            raise ApiErr(400, "bad_count")
        fake.balance_calls.append(n)
        return {"published": n}

    @app.get("/api/ops/stories")
    async def stories(request: Request):
        who(request, "SYS-")
        return fake.stories

    @app.post("/api/ops/stories/{name}/start")
    async def start_story(request: Request, name: str):
        who(request, "SYS-")
        row = next((s for s in fake.stories if s["name"] == name), None)
        if row is None:
            raise ApiErr(404, "no_story")
        row["active"] = True
        fake.story_starts.append((name, (await request.json())["minutes"], time.monotonic()))
        return row

    @app.get("/api/ops/runs")
    async def runs(request: Request):
        who(request, "SYS-")
        return fake.runs

    @app.post("/api/ops/runs")
    async def start_run(request: Request):
        who(request, "SYS-")
        kind = (await request.json())["kind"]
        if kind not in ("settlement", "sweep", "pricing", "payroll"):
            raise ApiErr(400, "bad_kind")
        fake.run_posts.append(kind)
        row = {"id": len(fake.runs) + 1, "kind": kind, "status": "completed", "detail": "", "started_by": "SYS-loadgen"}
        fake.runs.insert(0, row)
        return row

    return app


class FakeTransport(httpx.ASGITransport):
    """Runs a client against the fake app in this process; `fake.down` makes every call fail to connect."""

    def __init__(self, fake):
        super().__init__(make_app(fake))
        self.fake = fake

    async def handle_async_request(self, request):
        if self.fake.down:
            raise httpx.ConnectError("the fake API is down", request=request)
        return await super().handle_async_request(request)
