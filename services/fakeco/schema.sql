-- FakeCo Securities demo: all state lives here. Money is double precision on purpose (a fake broker; it keeps JSON simple).
CREATE TABLE settings (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE customers (id text PRIMARY KEY, name text NOT NULL, first_name text NOT NULL, email text NOT NULL, whale boolean NOT NULL DEFAULT false,
    funded boolean NOT NULL DEFAULT false, wallet_floor float8 NOT NULL DEFAULT 100, wallet_target float8 NOT NULL DEFAULT 1000, app_version text NOT NULL DEFAULT '0.2.0-demo',
    plan text NOT NULL DEFAULT 'silver');   -- bronze | silver | gold
CREATE TABLE staff (id text PRIMARY KEY, name text NOT NULL, role text NOT NULL, email text NOT NULL, hidden boolean NOT NULL DEFAULT false);
CREATE TABLE sessions (token text PRIMARY KEY, user_id text NOT NULL, role text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE accounts (id text PRIMARY KEY, customer_id text NOT NULL REFERENCES customers, cash_balance float8 NOT NULL, wallet_balance float8 NOT NULL, mmf_balance float8 NOT NULL DEFAULT 0,
    tax_lots int NOT NULL DEFAULT 0);   -- open tax lots, which settlement-service reconciles
CREATE TABLE instruments (symbol text PRIMARY KEY, name text NOT NULL, asset_class text NOT NULL, price float8);
CREATE TABLE price_history (symbol text NOT NULL, trade_date date NOT NULL, close float8 NOT NULL, volume bigint NOT NULL, PRIMARY KEY (symbol, trade_date));
CREATE TABLE holdings (asset_id text PRIMARY KEY, account_id text NOT NULL REFERENCES accounts, symbol text NOT NULL REFERENCES instruments, quantity float8 NOT NULL, UNIQUE (account_id, symbol));
CREATE SEQUENCE asset_seq START 100000;
CREATE TABLE account_values (account_id text NOT NULL, as_of timestamptz NOT NULL DEFAULT now(), value float8 NOT NULL);

CREATE SEQUENCE trade_seq START 1000;
CREATE TABLE trades (id text PRIMARY KEY, account_id text NOT NULL, customer_id text NOT NULL, side text NOT NULL, symbol text NOT NULL, quantity int NOT NULL,
    order_type text NOT NULL DEFAULT 'market', status text NOT NULL DEFAULT 'pending', filled_quantity int NOT NULL DEFAULT 0, avg_price float8, ticket_id text, parent_ticket_id text,
    reject_reason text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON trades (account_id, status);
CREATE TABLE trade_changes (id bigserial PRIMARY KEY, trade_id text NOT NULL, account_id text NOT NULL, new_quantity int NOT NULL, status text NOT NULL DEFAULT 'proposed', created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE executions (id bigserial PRIMARY KEY, trade_id text NOT NULL, quantity int NOT NULL, price float8 NOT NULL, venue text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

CREATE SEQUENCE transfer_seq START 1000;
CREATE TABLE transfers (id text PRIMARY KEY, account_id text NOT NULL, customer_id text NOT NULL, direction text NOT NULL, amount float8 NOT NULL, counterparty text NOT NULL DEFAULT 'bank',
    status text NOT NULL DEFAULT 'proposed', ticket_id text, reason text, risk_score float8, hold_reason text, decided_by text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON transfers (account_id, status);
CREATE TABLE ach_entries (id bigserial PRIMARY KEY, transfer_id text NOT NULL, entry_type text NOT NULL, batch_id text, status text NOT NULL DEFAULT 'submitted', return_code text, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE rebalance_proposals (id bigserial PRIMARY KEY, account_id text NOT NULL, customer_id text NOT NULL, risk text, horizon text, leave_alone text, moves jsonb NOT NULL, legs jsonb NOT NULL DEFAULT '[]',
    status text NOT NULL DEFAULT 'proposed', ticket_id text,
    created_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE tickets (id text PRIMARY KEY, kind text NOT NULL, state text NOT NULL DEFAULT 'queued', attempt int NOT NULL DEFAULT 1, queue text NOT NULL, customer_id text NOT NULL, account_id text NOT NULL, ref_id text,
    reason text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON tickets (customer_id, created_at DESC);
CREATE TABLE ticket_notes (id bigserial PRIMARY KEY, ticket_id text NOT NULL REFERENCES tickets, author text NOT NULL, text text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());

-- The message queues. state: ready -> inflight -> done | failed | retry (waits for available_at) | dlq.
CREATE TABLE queue_messages (id bigserial PRIMARY KEY, queue text NOT NULL, state text NOT NULL DEFAULT 'ready', attempt int NOT NULL DEFAULT 1, kind text NOT NULL, payload jsonb NOT NULL,
    traceparent text, ticket_id text, customer_id text, summary text, last_error text, replay boolean NOT NULL DEFAULT false, depth_at_enqueue int NOT NULL DEFAULT 0,
    enqueued_at timestamptz NOT NULL DEFAULT now(), available_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz);
CREATE INDEX ON queue_messages (queue, state, available_at);
CREATE INDEX ON queue_messages (finished_at);
CREATE TABLE worker_heartbeats (worker text PRIMARY KEY, queue text NOT NULL, beat_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE processed_messages (idempotency_key text PRIMARY KEY, processed_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE ledger_entries (id bigserial PRIMARY KEY, account_id text NOT NULL, entry_type text NOT NULL, amount float8 NOT NULL, ref_id text, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON ledger_entries (ref_id);
CREATE TABLE settlement_instructions (id bigserial PRIMARY KEY, trade_id text NOT NULL, symbol text NOT NULL, notional float8 NOT NULL, status text NOT NULL DEFAULT 'pending', run_id bigint, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE restricted_list (symbol text PRIMARY KEY, reason text NOT NULL, added_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE notifications (id bigserial PRIMARY KEY, customer_id text NOT NULL, kind text NOT NULL, text text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON notifications (customer_id, id DESC);
CREATE TABLE runs (id bigserial PRIMARY KEY, kind text NOT NULL, status text NOT NULL DEFAULT 'running', detail text, started_at timestamptz NOT NULL DEFAULT now(), ended_at timestamptz, started_by text NOT NULL);

CREATE TABLE conversations (id text PRIMARY KEY, customer_id text NOT NULL, title text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON conversations (customer_id, updated_at DESC);
CREATE TABLE messages (id bigserial PRIMARY KEY, conversation_id text NOT NULL REFERENCES conversations, role text NOT NULL, text text NOT NULL, cards jsonb NOT NULL DEFAULT '[]', outcome text, trace_id text,
    feedback text, author text, tool_calls jsonb NOT NULL DEFAULT '[]', created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX ON messages (conversation_id, id);

-- Fault flags (what a story turns on). A fault is in force when active and now is between starts_at and expires_at (null: open ended).
CREATE TABLE faults (name text PRIMARY KEY, description text NOT NULL, active boolean NOT NULL DEFAULT false, params jsonb NOT NULL DEFAULT '{}', default_params jsonb NOT NULL DEFAULT '{}',
    story text, starts_at timestamptz, expires_at timestamptz);
CREATE TABLE stories (name text PRIMARY KEY, title text NOT NULL, description text NOT NULL, kind text NOT NULL, position int NOT NULL, parts jsonb NOT NULL, active boolean NOT NULL DEFAULT false,
    started_at timestamptz, expires_at timestamptz, started_by text);
