# FakeCo Securities demo: how the services work together

`../SPEC.md` is the public contract (the web app and the load generator). This file is the contract between the services. All of it is one Python package, `fakeco`, and one container image; a Deployment picks its service with `python -m fakeco.run <service>`.

## Shared code (do not change its interface; ask the main thread if it is wrong)

- `config.py`: `SERVICES` (name → kind, module, local port), `QUEUES` (queue → consumer service), `url(service)`, `MAX_ATTEMPTS` 3, `HOLD_AMOUNT_USD` 10,000, `TOOL_LIMIT_S` 8.
- `app.py`: `create_app(workers=(), on_start=None, needs_db=True)` returns a FastAPI app with telemetry, the pool, `/healthz`, and the workers as tasks. Every service module exposes `build()` that returns the app. `ApiError(status, code, message)` becomes `{"error": {...}}`. `who()` returns `(customer_id, account_id)` from the `x-customer-id` and `x-account-id` headers.
- `db.py`: `fetch`, `fetchrow`, `fetchval`, `execute(sql, *args, quiet=False)` with `%s` placeholders; each makes a CLIENT span `<OP> <table>` unless `quiet=True` (use quiet for polling and housekeeping only). `db.J(obj)` wraps a value for a jsonb column.
- `http.py`: `await call(service, method, path, json=, params=, timeout=, route=, raise_for_status=True)` returns the JSON body, raises `ServiceError(service, status, body)` on 4xx/5xx. It passes the trace context and the customer headers on.
- `telemetry.py`: `tracer()`, `meter()`, `fail(span, error_type, message)` (marks a span failed without raising).
- `faults.py`: `await faults.get(name)` (params or None), `await faults.hit(name, key="rate")` (params when the fault is on and the draw falls under its rate), `await faults.factor(name)` (the slow-down factor, 1 when off). Names and default params are in `stories.py`.
- `queue.py`: `await publish(queue, kind, payload, ticket_id=, customer_id=, summary=, delay_s=, attrs=)`; `worker(queue, handler, name="1", stuck_fault=None)` (pass `lambda: queue.worker(...)` in `create_app(workers=[...])`). A handler is `async def handle(m, span)`: `m` is the `queue_messages` row (`m["kind"]`, `m["payload"]`, `m["ticket_id"]`, `m["customer_id"]`, `m["attempt"]`, `m["replay"]`), `span` is the `process <queue>` CONSUMER span, the root of a new trace. It returns `"completed"`, `"failed"` (a business rejection, final) or `"held"`, or a tuple `(outcome, reason)`; it raises `queue.Retry(message, error_type)` for a technical failure (retried after a back-off; the third failure dead-letters). The worker then sets the ticket state through ticket-status-service. `await queue.set_ticket(ticket_id, "processing", attempt)` at the start of a handler.
- `tickets.py`: `await tickets.create(kind, queue, customer_id, account_id, ref_id)` returns the ticket id.

## Tables

See `fakeco/schema.sql`; `fakeco/seed.py` fills it. Every service uses the one database directly for its own tables. Reading another service's table is allowed where this file says so; otherwise call the service, because the call is what the demo shows in a trace.

One exception: the support, operations and compliance endpoints of `concierge-api` read (and for holds, faults, stories and settings, write) the tables directly. Their traces are short on purpose; the customer paths are the ones the demo follows through the services.

## Messages

- `orders` (consumer `order-router`)
  - kind `order`, payload `{trade_id, account_id, customer_id, leg: bool, parent_ticket_id: str|null}`. The message's `ticket_id` is the trade's ticket, or null for a leg of a rebalance (then `parent_ticket_id` is set). The `trades` row exists already with status `queued`.
  - kind `rebalance`, payload `{proposal_id, account_id, customer_id}`, with the rebalance ticket. The handler reads `rebalance_proposals.legs` (`[{"side", "symbol", "quantity"}]`, written by portfolio-service), inserts one `trades` row per leg (status `queued`, `parent_ticket_id`), publishes one `order` message per leg (kind `order`, `leg: true`), sets the proposal `applied`, and returns `completed`.
- `transfers.outbound` (consumer `transfers-service`; the handler lives in `fakeco/backoffice/transfer_worker.py` as `async def handle(m, span)`): kind `transfer`, payload `{transfer_id, approved: bool}`. The `transfers` row exists with status `queued`.
- `sweeps` (consumer `sweep-service`, one worker, `stuck_fault="sweep_worker_stuck"`): kind `sweep`, payload `{account_id, customer_id, cause: "payroll"|"card_purchase"|"trade"|"transfer", delta: number}`. No ticket.
- `ach.returns` (consumer `ach-gateway`): kind `ach_return`, payload `{transfer_id, code: "R01"|"R02"|"R03"}`. No ticket; put `ticket.parent_id` on the span.
- `settlement.instructions` (consumer `settlement-service`): kind `instruction`, payload `{trade_id, symbol, notional}`. The worker stores it in `settlement_instructions` (status `pending`); a settlement run settles the pending ones.
- `notifications` (consumer `notification-service`): kind is the notification kind (`order.confirmed`, `order.rejected`, `transfer.completed`, `transfer.held`, `transfer.failed`, `transfer.returned`, `sweep.completed`), payload `{customer_id, text}`. The worker inserts a `notifications` row.

## Internal HTTP routes

Tool backends (the main thread writes these): `portfolio-service` (`GET /v1/portfolio/breakdown`, `GET /v1/accounts/{account_id}/assets`, `GET /v1/accounts/{account_id}/assets/{asset_id}/valuation`, `POST /v1/rebalance/proposals`, `POST /v1/rebalance/proposals/{id}/apply`), `trades-service` (`GET /v1/trades`, `POST /v1/trades`, `POST /v1/trades/{id}/changes`, `POST /v1/trades/{id}/changes/apply`, `POST /v1/trades/{id}/submit`, `POST /v1/trades/{id}/cancel`), `transfers-service` (`POST /v1/transfers/proposals`, `POST /v1/transfers`, `POST /v1/transfers/{id}/decision`), `ticket-status-service` (`GET` and `PUT /v1/tickets/{id}`).

Back office:

- `ledger-service`
  - `POST /v1/ledger/check-buying-power` `{account_id, amount}` → `{ok: bool, available: number, shortfall: number}`
  - `POST /v1/ledger/check-position` `{account_id, symbol, quantity}` → `{ok: bool, held: number}`
  - `POST /v1/ledger/post` `{account_id, ref_id, entries: [{entry_type, amount}], position: {symbol, delta_quantity}|null}` → `{posted: n, cash_balance}`. `amount` is signed and applied to `accounts.cash_balance`; `position` changes `holdings` (insert the row with a new `asset_id` from `asset_seq` as `AST-%06d` when the account does not hold the symbol; delete it at zero). One database transaction.
  - `GET /v1/ledger/records?ref_id=` → `{entries: [{id, account_id, entry_type, amount, created_at}]}`
  - `POST /v1/balance-events` `{count}` → `{published: n}`: for `count` random customers, a payroll deposit (credit to the wallet) or a card purchase (debit), then a `sweeps` message each. The load generator calls it through `concierge-api`.
  - `POST /v1/runs` `{run_id}`: the `payroll` run (about 40 customers get a payroll deposit; one `sweeps` message each).
- `compliance-service`: `POST /v1/compliance/screen-order` `{account_id, symbol, side, quantity}` → `{allowed: bool, reason: str|null}` (reads `restricted_list`); `POST /v1/compliance/aml-screen` `{transfer_id, account_id, amount, direction}` → `{cleared: bool, reason: str|null}`.
- `fraud-service`: `POST /v1/fraud/score-transfer` `{transfer_id, account_id, amount, direction}` → `{score: 0..1}`.
- `venue-gateway`: `POST /v1/venue/orders` `{trade_id, symbol, side, quantity, price}` → `{venue, status: "filled"|"partially_filled", fills: [{quantity, price}]}`. `venue_slow` multiplies its time; `venue_timeout` makes it hold the request past the caller's 6 s limit (the order-router raises `Retry(..., "VenueTimeoutError")`).
- `ach-gateway`: `POST /v1/ach/entries` `{transfer_id, direction, amount}` → `{entry_id, batch_id}`; HTTP 503 for the `bank_down` share (the transfer worker raises `Retry(..., "BankUnavailableError")`). When `ach_returns` hits, it publishes an `ach.returns` message with `delay_s` 20 to 90.
- `wallet-balance-service` (the card account): `GET /v1/wallets/{account_id}` → `{balance, floor, target}`; `POST /v1/wallets/{account_id}/credit` and `/debit` `{amount, ref_id}` → `{balance}`.
- `market-data-service`: `GET /v1/quotes/{symbol}` → `{symbol, price}`. A ticker task moves `instruments.price` every 15 s (a small random walk; `market_sell_off` adds its `drift_pct` per tick).
- Runs: `concierge-api` inserts the `runs` row (status `running`) and calls `POST /v1/runs` `{run_id}` on `settlement-service` (kind `settlement`), `sweep-service` (`sweep`), `market-data-service` (`pricing`) or `ledger-service` (`payroll`). The service answers 202 at once, does the work in a task under a span of its own, and then sets `status`, `detail` and `ended_at` on the row.

## What each back-office process does

Model each one on its builder in `concierge/processes.py` (same span names and attributes where the real work has a counterpart; real calls and queries replace the simulated durations): `order_execution`, `rebalance_execute`, `cash_transfer`, `ach_return`, `cash_sweep`, `pricing_run`, `sweep_run`, `settlement_run`. `messaging.system` is `postgresql` here, not `kafka`.

- Order (order-router): set the ticket `processing`; validate; buying power (buy) or position (sell) at ledger-service: reject as `insufficient_buying_power` or `insufficient_position` (a leg of a rebalance skips the buying-power check); compliance screen: reject as `restricted_security`; venue order; record the executions; post to the ledger (cash and position); set the trade `filled` or `partially_filled` with `filled_quantity` and `avg_price`; publish the settlement instruction, a notification and a `sweeps` message. A rejection sets the trade `rejected` with `reject_reason`, publishes `order.rejected`, and returns `("failed", reason)`.
- Transfer (transfer_worker): set the ticket `processing`; validate; daily limit; AML screen; fraud score. When the message is not `approved` and the amount is at least `HOLD_AMOUNT_USD` or the score is at least 0.8: set the transfer `held` with `risk_score` and `hold_reason`, publish `transfer.held`, return `("held", reason)`. Otherwise: counterparty `card_account` moves money between the wallet and the cash balance at once; `bank` goes through ach-gateway. A withdrawal over the cash balance is rejected (`insufficient_funds`). Then post to the ledger, set the transfer `completed`, publish a notification and a `sweeps` message.
- ACH return (ach-gateway): reverse the ledger posting, set the transfer `returned` with the code as `reason`, publish `transfer.returned`.
- Sweep (sweep-service): when the wallet is under its floor, move cash to it up to the target (and the other way when it is far over the target), with the card network's program-limit check (`card_network_slow` multiplies its time).
- Settlement run: settle the pending instructions in chunks of about 10; `settle_fail` makes a chunk's delivery fail and causes a buy-in. `detail` such as `38 instructions, 2 fails`.
- Reconciliation (settlement-service, always, while the load generator is on): about every 2 s, one account against the custodian, in a trace of its own (`reconcile account`, with `app.customer.plan` and `settlement.tax_lots`). Accounts with more tax lots (`accounts.tax_lots`) come up more often. Bronze accounts (`customers.plan`) have no consolidated lot records: their lots are read from lot-store 1,000 at a time (`lot_store.read`), and each page is merged (`merge lots`). One Bronze customer has about 9,600 lots; everyone else has a few hundred at most.
- Settlement cycle (settlement-service): opens on the clock every 120 minutes, or at once when a person (not `SYS-loadgen`) starts a settlement run (`open settlement cycle`, `settlement.cycle.trigger` `clock` or `manual run`); a process that starts in the middle of a period waits for the next one. Only a cycle that opens while the fault is on leaks. With `settlement_lot_cache_leak` on, the cycle keeps every merged page in one `cycle_results` collection that it never frees, and each merge walks all of it (`settlement.cycle_results.records_scanned`). Bronze reconciliations slow down, most of all for the 9,600-lot customer, while the container's memory climbs and Kubernetes OOM-kills the pod after about the fault's `minutes`. The params are `every_minutes` (the cycle period), `minutes` (to the crash) and `scan_s` (how much slower the 9,600-lot account is when memory is full). Turning the fault off frees the collection.
- Pricing run: write today's close for every instrument to `price_history` (upsert) and a row per account to `account_values`.

## Telemetry rules

- Spans come from real work: one span per step, an explicit status, `telemetry.fail` for failures. Put `ticket.id`, `account.id`, `app.customer.id`, `app.customer.name` (`customers.attrs()` gives both) and the business ids (`order.id`, `transfer.id`) on the root span of a process.
- Never put the word "moov" in any span, log, metric or attribute.
- The orb's shell sets `OTEL_EXPORTER_OTLP_ENDPOINT` to Honeycomb. Run everything with `FAKECO_LOCAL=1` (never with `FAKECO_EXPORT=1`), so nothing is sent.
