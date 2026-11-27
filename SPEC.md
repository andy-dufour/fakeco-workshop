# FakeCo Securities demo system: specification

A fake broker ("FakeCo Securities") that runs on Kubernetes. Nothing in it touches a real bank, venue or trading system. It exists so that a presenter can sign in as different people (account holders, support, operations, compliance), use it, break it, and watch the telemetry in Honeycomb.

This file is the contract between the parts. The public HTTP API in section 3 is what the web frontend and the load generator use.

## 1. Parts

- `web`: nginx with the React single-page app. It proxies `/api/` to `concierge-api` and `/otlp/` to the OpenTelemetry Collector (browser telemetry). A sidecar, the demo gate, asks for a username and password before the pages and `/api/` (not `/otlp/`): see README.md, "The demo gate".
- `concierge-api`: the only public API. Sign-in, the chat endpoint, and the customer, support, operations and compliance endpoints.
- Agents (one Deployment each): `concierge-orchestrator` (the Concierge) and `portfolio-specialist`, `trades-specialist`, `transfers-specialist`, `rebalance-specialist`, `ticket-specialist`. The model is a deterministic scripted model by default, so no provider key is needed.
- Tool backends: `portfolio-service`, `trades-service`, `transfers-service`, `ticket-status-service`.
- Back office: `order-router`, `ledger-service`, `compliance-service`, `venue-gateway`, `fraud-service`, `ach-gateway`, `sweep-service`, `wallet-balance-service`, `settlement-service`, `notification-service`, `market-data-service`.
- `postgres` (state and the message queues), `otel-collector` (sends everything to Honeycomb), `loadgen` (random chats and transactions).

## 2. People

Sign-in is fake: pick a person, get a token. Roles: `customer`, `support`, `operations`, `compliance`.

- Customers: `CUS-0001` to `CUS-0060`, each with one account `ACC-0001` to `ACC-0060`. `CUS-0058`, `CUS-0059` and `CUS-0060` are "whales" with more than 100 holdings.
- Staff: `SUP-ava`, `SUP-ben` (support), `OPS-kai`, `OPS-mia` (operations), `CMP-lee` (compliance).

## 3. Public API

Base path `/api`. Through the public web pod, every call also needs the demo gate's token (`X-Gate-Token`, or its cookie) and fails with `401` and the code `gate_required` without one; the rest of this section is the API behind the gate. JSON in and out. Every call except `GET /api/personas`, `POST /api/login` and `GET /api/config` needs `Authorization: Bearer <token>`. Errors are `{"error": {"code": "string", "message": "string"}}` with a 4xx or 5xx status; 401 means sign in again, 403 means the role may not do this. Times are ISO 8601 UTC strings. Money is a number in US dollars.

### 3.1 Session

- `GET /api/config` → `{"service_name": "concierge-web", "version": "0.1.0", "otlp_path": "/otlp", "environment": "demo", "honeycomb_trace_url": null}`. `version` is `0.1.0` until a release flag changes, then its build id. `honeycomb_trace_url` is null or a template with `{trace_id}`; when set, show a "View trace" link next to every assistant message.
- `GET /api/personas` → `{"customers": [Person], "staff": [Person]}`; `Person` is `{"id": "CUS-0001", "role": "customer", "name": "Dana Ruiz", "email": "dana.ruiz@example.com", "note": "12 holdings"}`. `customers` holds all 60. Whales have a `note` that begins with "Whale".
- `POST /api/login` `{"user_id": "CUS-0001"}` → `{"token": "…", "user": Person}`.
- `GET /api/me` → `Person` plus, for a customer, `"account_id": "ACC-0001"`.

### 3.2 Customer (role `customer`)

- `GET /api/summary` → `{"customer": Person, "account": {"id": "ACC-0001", "cash_balance": 5230.10, "wallet_balance": 310.00, "invested_value": 84211.55, "total_value": 89441.65}, "allocation": [{"asset_class": "Stocks", "percent": 55, "value": 46316.35}]}`. Asset classes: Stocks, Bonds, Cash, Real Estate, Commodities.
- `GET /api/holdings` → `[{"asset_id": "AST-000123", "symbol": "NVD-EQ", "name": "Novadyne Corp", "asset_class": "Stocks", "quantity": 40, "price": 121.33, "market_value": 4853.20}]`
- `GET /api/instruments` → `[{"symbol": "NVD-EQ", "name": "Novadyne Corp", "asset_class": "Stocks", "price": 121.33}]` (about 150)
- `GET /api/trades?status=pending|queued|filled|cancelled|rejected|all` (default `all`, newest first, at most 100) → `[Trade]`. `Trade` is `{"id": "TRD-00001234", "side": "buy"|"sell", "symbol": "NVD-EQ", "quantity": 10, "order_type": "market"|"limit", "status": "pending"|"queued"|"filled"|"partially_filled"|"cancelled"|"rejected", "filled_quantity": 0, "avg_price": null, "ticket_id": null, "reject_reason": null, "created_at": "…", "updated_at": "…"}`. A `pending` trade waits for the customer; a `queued` trade is with the back office.
- `POST /api/trades` `{"side": "buy", "symbol": "NVD-EQ", "quantity": 10, "mode": "now"|"pending"}` → `{"trade": Trade, "ticket_id": "TKT-…"|null}`. `now` sends it to the back office (ticket), `pending` saves it as a pending trade.
- `POST /api/trades/{id}/submit` → `{"trade": Trade, "ticket_id": "TKT-…"}` (sends a pending trade to the back office). `POST /api/trades/{id}/cancel` → `{"trade": Trade}` (pending trades only).
- `GET /api/transfers` → `[Transfer]`. `Transfer` is `{"id": "TRF-00001234", "direction": "in"|"out", "amount": 250.0, "counterparty": "bank"|"card_account", "status": "proposed"|"queued"|"held"|"completed"|"failed"|"returned", "ticket_id": "TKT-…"|null, "reason": null, "created_at": "…", "updated_at": "…"}`. `in` is a deposit into the brokerage account.
- `POST /api/transfers` `{"direction": "in", "amount": 250, "counterparty": "bank"}` → `{"transfer": Transfer, "ticket_id": "TKT-…"}`
- `GET /api/tickets` → `[Ticket]` (the customer's, newest first, at most 100). `GET /api/tickets/{id}` → `Ticket`. `Ticket` is `{"id": "TKT-7F3A9C21", "kind": "trade"|"transfer"|"rebalance", "state": "queued"|"processing"|"retrying"|"completed"|"failed"|"dead_lettered"|"held", "attempt": 1, "queue": "orders", "customer_id": "CUS-0001", "ref_id": "TRD-00001234", "reason": null, "queue_position": 3|null, "eta_seconds": 12|null, "notes": [{"author": "SUP-ava", "text": "…", "created_at": "…"}], "created_at": "…", "updated_at": "…"}`.
- `GET /api/notifications` → `[{"id": 1, "kind": "order.confirmed", "text": "Your buy of 10 NVD-EQ was filled at $121.40.", "created_at": "…"}]` (newest first, at most 50)

### 3.3 Chat (role `customer`)

- `GET /api/conversations` → `[{"id": "…", "title": "Show my portfolio", "updated_at": "…", "message_count": 4}]` (the customer's, newest first, at most 30)
- `GET /api/conversations/{id}` → `{"id": "…", "customer_id": "CUS-0001", "messages": [Message]}`
- `POST /api/chat` `{"conversation_id": "…"|null, "message": "Show my portfolio", "chip": true}` → `{"conversation_id": "…", "message": Message}`. A null `conversation_id` starts a conversation. `chip` says the text came from a button, not the keyboard. The call can take 1 s to 5 min (a runaway valuation takes about 3 min). Any 5xx or network error means "the Concierge could not answer"; show that in the chat and let the user try again.
- `POST /api/messages/{id}/feedback` `{"rating": "up"|"down"}` → `{"ok": true}`

`Message` is `{"id": 123, "role": "user"|"assistant"|"support", "text": "…", "cards": [Card], "outcome": "ok"|"tool_timeout_recovered"|"context_overflow"|"agent_bug"|"error"|null, "trace_id": "32 hex"|null, "feedback": "up"|"down"|null, "author": "SUP-ava"|null, "created_at": "…"}`. `support` messages are written by a support person into the customer's conversation; the customer's chat shows them (poll `GET /api/conversations/{id}` every 5 s while the chat is open). Text may contain newlines and `- ` bullet lines.

`Card` is one of:

- `{"type": "allocation", "items": [{"asset_class": "Stocks", "percent": 55, "value": 46316.35}]}`: draw a donut chart with a legend.
- `{"type": "trades", "items": [{"id": "TRD-…", "side": "buy", "symbol": "NVD-EQ", "quantity": 10, "status": "pending"}]}`: a list. Tapping a row puts `Change trade TRD-… to ` in the message box.
- `{"type": "transfer_form"}`: a small form with a direction (Add money or Withdraw) and an amount. Submitting sends the message `Add $250.00 to my account.` or `Withdraw $250.00 from my account.`.
- `{"type": "confirm", "kind": "transfer"|"trade"|"rebalance", "title": "Deposit $250.00", "lines": ["From your bank account"]}`: a card with Confirm and Cancel buttons. Confirm sends the message `Confirm`; Cancel sends `Cancel`. Only the newest confirm card of a conversation has live buttons.
- `{"type": "rebalance_plan", "moves": [{"asset_class": "Stocks", "from": 62, "to": 55}]}`: a table of percentage moves.
- `{"type": "ticket", "ticket_id": "TKT-…", "kind": "transfer", "state": "queued"}`: a ticket badge. It has a button "Where is it?" that sends `Check ticket TKT-…`.
- `{"type": "chips", "options": ["Risk level: conservative", "Risk level: moderate"]}`: buttons that send their text with `chip: true`.
- `{"type": "valuations", "count": 12, "total": 84211.55}`: a small summary tile.

Unknown card types are ignored. A new conversation shows four starter chips: "Show my portfolio", "Show my pending trades", "Transfer money", "Rebalance my portfolio", and a fifth, "Value every holding", which sends `What is each of my investments worth right now?`.

What the Concierge understands (the model is scripted, so wording matters a little; matching ignores case):

- Portfolio: any message with "portfolio", "holdings", "what do I own", "allocation", "asset class" or "split up".
- Valuation of every holding: "worth", "valuation", "value all", "value every", "price everything".
- Pending trades: "pending trade", "trades waiting", "trades are pending", "orders haven't".
- Change a pending trade: `Change trade TRD-00001234 to 20 shares`, `set trade TRD-00001234 to 20`, or `Make my NVD-EQ order 20 shares instead`. The answer carries a confirm card.
- Transfer: "transfer money", "move some money", "transfer funds" (the answer carries the form); `Add $250.00 to my account.` or `Withdraw $250.00 from my account.` (the answer carries a confirm card).
- Rebalance: "rebalance", then the chips `Risk level: moderate`, `Time horizon: 5-10 years`, `Leave alone: Bonds` or `Leave nothing out` (the answer carries a plan and a confirm card).
- `Confirm` (also "yes", "go ahead", "confirmed", "apply it") applies the newest open confirm card; the answer carries a ticket card. `Cancel` drops it.
- Ticket status: `Check ticket TKT-7F3A9C21`, or "where is my transfer", "did my trade go through", "did my rebalance finish" (the newest ticket of that kind in the conversation).
- Anything else gets a general answer without tools.

### 3.4 Support (role `support`; operations may read too)

- `GET /api/support/customers?q=` → `[Person]` (name, id or email contains `q`; at most 60)
- `GET /api/support/customers/{id}` → `{"customer": Person, "account": {…as in summary…}, "allocation": […], "trades": [Trade], "transfers": [Transfer], "tickets": [Ticket], "conversations": [ConversationRow]}` (10 newest of each list)
- `GET /api/support/conversations?customer_id=&outcome=&feedback=&limit=` → `[ConversationRow]`; `ConversationRow` is `{"id": "…", "customer_id": "CUS-0001", "customer_name": "Dana Ruiz", "title": "…", "last_outcome": "ok", "has_negative_feedback": false, "message_count": 4, "updated_at": "…"}`. `outcome` filters on any message with that outcome; `feedback=down` on thumbs-down.
- `GET /api/support/conversations/{id}` → `{"id": "…", "customer_id": "…", "customer_name": "…", "messages": [Message + {"tool_calls": [{"agent": "PortfolioSpecialist", "tool": "get_portfolio_breakdown", "arguments": {…}, "result": "string, shortened to 2,000 characters", "error": null}]}]}`
- `POST /api/support/conversations/{id}/reply` `{"text": "…"}` → `{"message": Message}` (role `support`)
- `GET /api/support/tickets?state=&customer_id=&limit=` → `[Ticket]`
- `POST /api/support/tickets/{id}/notes` `{"text": "…"}` → `Ticket`

### 3.5 Operations (role `operations`)

- `GET /api/ops/services` → `[{"name": "portfolio-service", "kind": "backend"|"agent"|"backoffice"|"edge", "healthy": true, "latency_ms": 4}]`
- `GET /api/ops/queues` → `[{"queue": "orders", "ready": 3, "inflight": 1, "retry": 0, "dlq": 2, "oldest_ready_age_s": 14.2, "consumer": "order-router", "processed_5m": 40, "failed_5m": 1}]`. Queues: `orders`, `transfers.outbound`, `sweeps`, `ach.returns`, `settlement.instructions`, `notifications`.
- `GET /api/ops/queues/{queue}/messages?state=ready|inflight|retry|dlq&limit=` → `[{"id": 991, "queue": "orders", "state": "dlq", "attempt": 3, "ticket_id": "TKT-…"|null, "customer_id": "CUS-0001"|null, "last_error": "VenueTimeoutError: …"|null, "enqueued_at": "…", "summary": "buy 10 NVD-EQ"}]`
- `POST /api/ops/queues/{queue}/replay` `{"message_ids": [991]}` or `{"all": true}` → `{"replayed": 2}` (moves dead letters back to the queue)
- `GET /api/ops/stories` → `[{"name": "venue-slow-afternoon", "title": "Venue slow", "description": "…", "kind": "agent"|"backoffice", "active": false, "expires_at": null, "faults": ["venue_slow", "venue_timeout"]}]` (13 stories)
- `POST /api/ops/stories/{name}/start` `{"minutes": 15}` → the story row. `POST /api/ops/stories/{name}/stop` → the story row. `POST /api/ops/stories/stop-all` → `{"stopped": 3}`.
- `GET /api/ops/faults` → `[{"name": "venue_slow", "description": "…", "active": false, "params": {"factor": 15}, "story": null|"venue-slow-afternoon", "expires_at": null}]`. `PUT /api/ops/faults/{name}` `{"active": true, "params": {…}|null, "minutes": 10|null}` → the fault row.
- `GET /api/ops/loadgen` → `{"enabled": true, "conversations_per_minute": 6, "transactions_per_minute": 4, "auto_stories": false, "stats": {"conversations": 120, "turns": 400, "transactions": 80, "errors": 3, "last_seen_at": "…"|null}}`. The object may also hold `"surge"` (what the active stories ask of the load generator); the web app ignores it. `PUT /api/ops/loadgen` with any of `enabled`, `conversations_per_minute` (0-120), `transactions_per_minute` (0-120), `auto_stories` → the same object.
- `GET /api/ops/runs` → `[{"id": 12, "kind": "settlement"|"sweep"|"pricing"|"payroll", "status": "running"|"completed"|"failed", "detail": "38 instructions, 2 fails", "started_at": "…", "ended_at": "…"|null, "started_by": "OPS-kai"}]` (20 newest). `POST /api/ops/runs` `{"kind": "settlement"}` → the run row (status `running`; poll the list). A `payroll` run posts payroll deposits to about 40 customers; each one puts a message on the `sweeps` queue.
- `GET /api/ops/tickets?state=&limit=` → `[Ticket]`

The load generator signs in as the hidden operations person `SYS-loadgen` (not in `GET /api/personas`) and also uses:

- `POST /api/ops/balance-events` `{"count": 5}` → `{"published": 5}`: that many payroll deposits or card purchases on random customers' card accounts; each one puts a message on the `sweeps` queue.
- `POST /api/ops/loadgen/stats` `{"conversations": 1, "turns": 3, "transactions": 0, "errors": 0}` → `{"ok": true}`: adds these numbers to the totals of `GET /api/ops/loadgen` and sets `last_seen_at`.
- `surge` in `GET /api/ops/loadgen` is `{"volume": 2.4, "flows": {"portfolio": 2.5, "rebalance": 3, "trades": 1.5}, "balance_events_factor": 60, "valuation_per_minute": 2.5}`: multiply the conversations per minute by `volume`, the weight of each named flow by its number, and the balance events per minute by `balance_events_factor`; and start that many more conversations a minute that ask for a valuation of every holding, whatever `conversations_per_minute` is (the fault `asset_valuation_rush`). Without a surge it is `{"volume": 1, "flows": {}, "balance_events_factor": 1, "valuation_per_minute": 0}`.
- Chat requests may carry the header `x-app-version` (`0.1.0-demo`, `0.1.1-demo` or `0.2.0-demo`, the newest). While the fault `app_hang_on_send` is on, a share of the chat requests from the newest version (or without the header) get no answer: the server holds the request until the client gives up.

### 3.6 Compliance (role `compliance`)

- `GET /api/compliance/holds` → `[{"transfer": Transfer, "customer": Person, "ticket_id": "TKT-…", "risk_score": 0.83, "hold_reason": "amount over $10,000"}]`
- `POST /api/compliance/holds/{transfer_id}/decision` `{"decision": "approve"|"reject", "reason": "…"}` → `{"transfer": Transfer}`
- `GET /api/compliance/restricted` → `[{"symbol": "ORB-EQ", "reason": "…", "added_by": "CMP-lee", "created_at": "…"}]`. `POST /api/compliance/restricted` `{"symbol": "ORB-EQ", "reason": "…"}` → the list. `DELETE /api/compliance/restricted/{symbol}` → the list. Orders in a restricted symbol are rejected by the back office.

## 4. Telemetry conventions

- Every service sends OTLP over HTTP to the collector (`OTEL_EXPORTER_OTLP_ENDPOINT`); the collector sends to Honeycomb. The browser sends OTLP/HTTP JSON to `/otlp/v1/traces` and `/otlp/v1/logs` (same origin) and propagates W3C `traceparent` on `/api` calls, so one trace runs from the click to the database.
- Browser resource: `service.name` `concierge-web`. Put `app.customer.id` and `app.customer.name` (or `app.staff.id`), `app.persona.role` and `session.id` on every browser span. The span of a chat send is named `POST /api/chat` like the others, with `app.chat.chip` true or false.
- Agent spans follow the simulator in `concierge/`: `invoke_agent <Agent>`, `execute_event_loop_cycle`, `chat <model>`, `execute_tool <tool>`, with `gen_ai.*` attributes; `gen_ai.conversation.id` only on agent, cycle, model and tool spans. Evaluation results are log records named `gen_ai.evaluation.result` and never set `gen_ai.operation.name`.
- Queue spans: `publish <queue>` (PRODUCER) and `process <queue>` (CONSUMER, a new trace linked to the producer) with `messaging.*`, `queue.*` and `ticket.id`.
- Never put the word "moov" in telemetry.
