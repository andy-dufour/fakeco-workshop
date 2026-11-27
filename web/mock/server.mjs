// Mock of the FakeCo Securities public API (specification section 3).
// Plain Node, no dependencies, everything in memory. It exists so the web
// frontend can be developed without the real backend; start it with
// `npm run mock` (or `npm run dev`, which starts it together with Vite).
//
// Not in the spec, and only for testing the frontend:
//   - a chat message containing "(fail)" gets a 503 the first time it is sent
//     in a conversation, so the error bubble and "Try again" can be shown.
// Environment: MOCK_PORT (default 8787), MOCK_DELAY_MS (chat delay, default 900).

import http from "node:http";
import { randomUUID, randomBytes } from "node:crypto";

const PORT = Number(process.env.MOCK_PORT ?? 8787);
const CHAT_DELAY = Number(process.env.MOCK_DELAY_MS ?? 900);

// ---------------------------------------------------------------- helpers

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const r2 = (n) => Math.round(n * 100) / 100;
const now = () => new Date().toISOString();
const ago = (s) => new Date(Date.now() - s * 1000).toISOString();
const hex = (n) => randomBytes(n).toString("hex");
const pad = (n, w) => String(n).padStart(w, "0");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let seq = 1000;
const nextId = () => ++seq;

// ------------------------------------------------------------------ people

const FIRST = ["Dana", "Marcus", "Priya", "Tomas", "Lena", "Omar", "Grace", "Hiro", "Sofia", "Andre", "Mei", "Callum", "Nadia", "Felix", "Ines", "Jamal", "Karin", "Luis", "Maya", "Noel"];
const LAST = ["Ruiz", "Okafor", "Shah", "Novak", "Fischer", "Haddad", "Bell", "Tanaka", "Costa", "Dubois", "Zhang", "Reid", "Petrov", "Lind", "Moreno", "Wright", "Berg", "Ortega", "Nair", "Quinn"];

const customers = [];
for (let i = 1; i <= 60; i++) {
  const name = `${FIRST[(i * 7) % FIRST.length]} ${LAST[(i * 11 + 3) % LAST.length]}`;
  customers.push({
    id: `CUS-${pad(i, 4)}`,
    role: "customer",
    name,
    email: `${name.toLowerCase().replace(" ", ".")}${i > 20 ? i : ""}@example.com`,
    note: "",
    account_id: `ACC-${pad(i, 4)}`,
  });
}
const staff = [
  { id: "SUP-ava", role: "support", name: "Ava Lindqvist", email: "ava@fakeco.example", note: "Support" },
  { id: "SUP-ben", role: "support", name: "Ben Carter", email: "ben@fakeco.example", note: "Support" },
  { id: "OPS-kai", role: "operations", name: "Kai Nakamura", email: "kai@fakeco.example", note: "Operations" },
  { id: "OPS-mia", role: "operations", name: "Mia Rossi", email: "mia@fakeco.example", note: "Operations" },
  { id: "CMP-lee", role: "compliance", name: "Lee Abara", email: "lee@fakeco.example", note: "Compliance" },
];
// The load generators sign in as SYS-loadgen; like the real API, it is not in GET /api/personas.
const people = new Map([...customers, ...staff, { id: "SYS-loadgen", role: "operations", name: "Load generator", email: "loadgen@fakeco.example", note: "" }].map((p) => [p.id, p]));
const isWhale = (c) => Number(c.id.slice(4)) >= 58;
const personOut = (p, withAccount = false) => {
  const { account_id, ...rest } = p;
  return withAccount && account_id ? { ...rest, account_id } : rest;
};

// ------------------------------------------------------------- instruments

const CLASS_SUFFIX = { Stocks: "EQ", Bonds: "BD", Cash: "MM", "Real Estate": "RE", Commodities: "CM" };
const instruments = [];
{
  const A = ["Nova", "Orbit", "Quanta", "Helio", "Terra", "Aero", "Blue", "Cobalt", "Delta", "Ember", "Fjord", "Glint", "Harbor", "Iron", "Juno", "Kite", "Lumen", "Maple", "Nimbus", "Onyx"];
  const B = ["dyne", "tech", "works", "labs", "ware", "gen", "link", "point", "forge", "grid"];
  const C = { Stocks: ["Corp", "Inc", "Holdings", "Group"], Bonds: ["Treasury 2031", "Muni Fund", "Corp Bond 2029", "Green Bond"], Cash: ["Money Market"], "Real Estate": ["REIT", "Property Trust"], Commodities: ["Gold Trust", "Oil Fund", "Copper ETF", "Grain Index"] };
  const rand = rng(42);
  const used = new Set();
  const add = (name, cls, price) => {
    const code = name.replace(/[^A-Za-z]/g, "").slice(0, 3).toUpperCase();
    let sym = `${code}-${CLASS_SUFFIX[cls]}`;
    for (let n = 2; used.has(sym); n++) sym = `${code}${n}-${CLASS_SUFFIX[cls]}`;
    used.add(sym);
    instruments.push({ symbol: sym, name, asset_class: cls, price });
  };
  add("Novadyne Corp", "Stocks", 121.33);
  add("Orbit Freight", "Stocks", 54.2); // ORB-EQ, used as a restricted symbol below
  const plan = [["Stocks", 90], ["Bonds", 25], ["Cash", 4], ["Real Estate", 15], ["Commodities", 14]];
  for (const [cls, count] of plan) {
    for (let i = 0; i < count; i++) {
      const base = `${A[Math.floor(rand() * A.length)]}${B[Math.floor(rand() * B.length)]}`;
      const suffix = C[cls][Math.floor(rand() * C[cls].length)];
      const price = cls === "Bonds" ? 90 + rand() * 20 : cls === "Cash" ? 1 : 8 + rand() * 340;
      add(`${base} ${suffix}`, cls, r2(price));
    }
  }
}
const bySymbol = new Map(instruments.map((i) => [i.symbol, i]));

// ---------------------------------------------------------- state: money

const holdingsOf = new Map(); // customer id -> [{symbol, quantity}]
const cashOf = new Map();
const walletOf = new Map();
for (const c of customers) {
  const n = Number(c.id.slice(4));
  const rand = rng(n * 977);
  const count = isWhale(c) ? 110 + Math.floor(rand() * 50) : 5 + Math.floor(rand() * 14);
  const picked = new Set();
  while (picked.size < count) picked.add(instruments[Math.floor(rand() * instruments.length)].symbol);
  holdingsOf.set(
    c.id,
    [...picked].map((symbol) => ({ symbol, quantity: Math.max(1, Math.round(rand() * (bySymbol.get(symbol).price > 100 ? 60 : 300))) })),
  );
  cashOf.set(c.id, r2(500 + rand() * 9000));
  walletOf.set(c.id, r2(rand() * 600));
  c.note = isWhale(c) ? `Whale: ${count} holdings` : `${count} holdings`;
}

function holdingRows(cid) {
  return holdingsOf
    .get(cid)
    .map((h) => {
      const ins = bySymbol.get(h.symbol);
      return {
        asset_id: `AST-${pad(instruments.indexOf(ins) + 100, 6)}`,
        symbol: ins.symbol,
        name: ins.name,
        asset_class: ins.asset_class,
        quantity: h.quantity,
        price: ins.price,
        market_value: r2(h.quantity * ins.price),
      };
    })
    .sort((a, b) => b.market_value - a.market_value);
}

function allocationOf(cid) {
  const values = { Stocks: 0, Bonds: 0, Cash: cashOf.get(cid), "Real Estate": 0, Commodities: 0 };
  for (const h of holdingRows(cid)) values[h.asset_class] += h.market_value;
  const total = Object.values(values).reduce((a, b) => a + b, 0);
  // Largest-remainder rounding so the percentages add up to 100.
  const raw = Object.entries(values).map(([asset_class, value]) => ({ asset_class, value: r2(value), exact: (value / total) * 100 }));
  const items = raw.map((x) => ({ ...x, percent: Math.floor(x.exact) }));
  let left = 100 - items.reduce((a, b) => a + b.percent, 0);
  [...items].sort((a, b) => b.exact - b.percent - (a.exact - a.percent)).forEach((x) => { if (left-- > 0) x.percent++; });
  return items.filter((x) => x.value > 0).map(({ asset_class, percent, value }) => ({ asset_class, percent, value }));
}

function accountOf(cid) {
  const invested = r2(holdingRows(cid).reduce((a, h) => a + h.market_value, 0));
  const cash = r2(cashOf.get(cid));
  const wallet = r2(walletOf.get(cid));
  return { id: people.get(cid).account_id, cash_balance: cash, wallet_balance: wallet, invested_value: invested, total_value: r2(invested + cash + wallet) };
}

// ------------------------------------------- state: trades, transfers, tickets

const trades = [];
const transfers = [];
const tickets = [];
const notifications = []; // {customer_id, ...}
const restricted = [
  { symbol: "ORB-EQ", reason: "Pending acquisition: insider list active", added_by: "CMP-lee", created_at: ago(86400 * 2) },
  { symbol: "QUA-EQ", reason: "Trading halt", added_by: "CMP-lee", created_at: ago(3600 * 5) },
];
const qmsgs = []; // queue messages

const QUEUES = ["orders", "transfers.outbound", "sweeps", "ach.returns", "settlement.instructions", "notifications"];
const CONSUMER = { orders: "order-router", "transfers.outbound": "ach-gateway", sweeps: "sweep-service", "ach.returns": "ach-gateway", "settlement.instructions": "settlement-service", notifications: "notification-service" };

function notify(cid, kind, text) {
  notifications.unshift({ id: nextId(), customer_id: cid, kind, text, created_at: now() });
}

function newTrade(cid, { side, symbol, quantity, status = "pending", created = now() }) {
  const t = {
    id: `TRD-${pad(1200 + trades.length + 1, 8)}`,
    customer_id: cid,
    side,
    symbol,
    quantity,
    order_type: "market",
    status,
    filled_quantity: 0,
    avg_price: null,
    ticket_id: null,
    reject_reason: null,
    created_at: created,
    updated_at: created,
  };
  trades.unshift(t);
  return t;
}

function newTicket(cid, kind, refId, queue, extra = {}) {
  const t = {
    id: `TKT-${hex(4).toUpperCase()}`,
    kind,
    state: "queued",
    attempt: 1,
    queue,
    customer_id: cid,
    ref_id: refId,
    reason: null,
    notes: [],
    created_at: now(),
    updated_at: now(),
    _since: Date.now(),
    ...extra,
  };
  tickets.unshift(t);
  if (t.state !== "held") enqueue(t);
  return t;
}

function enqueue(t) {
  qmsgs.push({ id: nextId(), queue: t.queue, state: "ready", attempt: t.attempt, ticket_id: t.id, customer_id: t.customer_id, last_error: null, enqueued_at: now(), summary: summaryOf(t) });
}

function summaryOf(t) {
  if (t.kind === "transfer") {
    const x = transfers.find((v) => v.id === t.ref_id);
    return x ? `${x.direction === "in" ? "deposit" : "withdraw"} $${x.amount.toFixed(2)}` : t.ref_id;
  }
  if (t.kind === "rebalance") return "rebalance orders";
  const x = trades.find((v) => v.id === t.ref_id);
  return x ? `${x.side} ${x.quantity} ${x.symbol}` : t.ref_id;
}

function setTicketState(t, state, reason = null) {
  t.state = state;
  t.reason = reason;
  t.updated_at = now();
  t._since = Date.now();
  const m = qmsgs.find((q) => q.ticket_id === t.id);
  if (!m) return;
  if (state === "completed" || state === "failed") qmsgs.splice(qmsgs.indexOf(m), 1);
  else {
    m.state = { queued: "ready", processing: "inflight", retrying: "retry", dead_lettered: "dlq" }[state] ?? m.state;
    m.attempt = t.attempt;
    if (reason) m.last_error = reason;
  }
}

function ticketOut(t) {
  const { _since, ...rest } = t;
  let queue_position = null;
  let eta_seconds = null;
  if (t.state === "queued") {
    const ahead = tickets.filter((x) => x.state === "queued" && x.queue === t.queue && x._since < t._since).length;
    queue_position = ahead + 1;
    eta_seconds = queue_position * 4;
  }
  return { ...rest, queue_position, eta_seconds };
}

function tradeOut(t) {
  const { customer_id, ...rest } = t;
  return rest;
}

function submitTrade(t) {
  const tk = newTicket(t.customer_id, "trade", t.id, "orders");
  t.status = "queued";
  t.ticket_id = tk.id;
  t.updated_at = now();
  return tk;
}

function createTransfer(cid, { direction, amount, counterparty }) {
  const x = {
    id: `TRF-${pad(1200 + transfers.length + 1, 8)}`,
    customer_id: cid,
    direction,
    amount: r2(amount),
    counterparty,
    status: "queued",
    ticket_id: null,
    reason: null,
    created_at: now(),
    updated_at: now(),
  };
  transfers.unshift(x);
  const held = x.amount > 10000;
  const tk = newTicket(cid, "transfer", x.id, "transfers.outbound", held ? { state: "held", reason: "amount over $10,000" } : {});
  x.ticket_id = tk.id;
  if (held) x.status = "held";
  return { transfer: x, ticket: tk };
}
const transferOut = (x) => {
  const { customer_id, ...rest } = x;
  return rest;
};

// ------------------------------------------------- simulation: faults, tickets

const FAULT_DEFS = [
  ["venue_slow", "Venue gateway answers slowly", { factor: 15 }, "venue-gateway"],
  ["venue_timeout", "Venue gateway times out on orders", { rate: 0.6 }, "venue-gateway"],
  ["ledger_lock", "Ledger rows stay locked", { seconds: 8 }, "ledger-service"],
  ["fraud_flaky", "Fraud service errors on some calls", { error_rate: 0.3 }, "fraud-service"],
  ["ach_down", "ACH gateway refuses connections", null, "ach-gateway"],
  ["sweep_stuck", "Sweep service stops consuming", null, "sweep-service"],
  ["wallet_stale", "Wallet balance is stale", { stale_seconds: 600 }, "wallet-balance-service"],
  ["market_data_lag", "Prices are delayed", { lag_seconds: 90 }, "market-data-service"],
  ["portfolio_slow", "Portfolio service is slow", { delay_ms: 8000 }, "portfolio-service"],
  ["trades_tool_timeout", "Trades tool call times out once", { timeout_ms: 5000 }, "trades-service"],
  ["transfers_bug", "Transfers specialist ignores the amount", null, "transfers-specialist"],
  ["rebalance_loop", "Rebalance specialist loops", { max_cycles: 12 }, "rebalance-specialist"],
  ["model_context_small", "Model context window is tiny", { tokens: 4000 }, "concierge-orchestrator"],
  ["settlement_fail", "Settlement rejects instructions", { fail_every: 3 }, "settlement-service"],
];
const faults = FAULT_DEFS.map(([name, description, params, service]) => ({ name, description, active: false, params, story: null, expires_at: null, _service: service }));
const faultOut = ({ _service, ...f }) => f;
const faultActive = (name) => faults.find((f) => f.name === name)?.active;

const STORY_DEFS = [
  ["venue-slow-afternoon", "Venue slow", "The venue answers 15 times slower than usual; orders pile up.", "backoffice", ["venue_slow", "venue_timeout"]],
  ["ledger-lock-contention", "Ledger lock contention", "Ledger rows stay locked and transfers wait.", "backoffice", ["ledger_lock"]],
  ["fraud-flaky", "Flaky fraud checks", "Fraud checks fail at random and tickets retry.", "backoffice", ["fraud_flaky"]],
  ["ach-outage", "ACH outage", "Withdrawals fail and fill the dead-letter queue.", "backoffice", ["ach_down"]],
  ["sweep-stuck", "Sweeps stuck", "The sweep queue stops draining.", "backoffice", ["sweep_stuck"]],
  ["stale-wallet", "Stale wallet balance", "Wallet balances lag behind the ledger.", "backoffice", ["wallet_stale"]],
  ["market-data-lag", "Market data lag", "Prices arrive 90 s late.", "backoffice", ["market_data_lag"]],
  ["settlement-failures", "Settlement failures", "The nightly settlement rejects instructions.", "backoffice", ["settlement_fail"]],
  ["slow-portfolio", "Slow portfolio tool", "Portfolio answers take 8 s and the Concierge retries.", "agent", ["portfolio_slow", "trades_tool_timeout"]],
  ["whale-context-overflow", "Whale context overflow", "Valuing every holding overflows the context window.", "agent", ["model_context_small"]],
  ["transfer-amount-bug", "Transfer amount bug", "The transfers specialist drops the amount.", "agent", ["transfers_bug"]],
  ["rebalance-loop", "Rebalance loop", "The rebalance specialist keeps looping.", "agent", ["rebalance_loop"]],
  ["trade-tool-timeout", "Trade tool timeout", "A tool call times out and the Concierge recovers.", "agent", ["trades_tool_timeout"]],
];
const stories = STORY_DEFS.map(([name, title, description, kind, fs]) => ({ name, title, description, kind, active: false, expires_at: null, faults: fs }));

function startStory(s, minutes) {
  s.active = true;
  s.expires_at = new Date(Date.now() + minutes * 60000).toISOString();
  for (const n of s.faults) {
    const f = faults.find((x) => x.name === n);
    f.active = true;
    f.story = s.name;
    f.expires_at = s.expires_at;
  }
}
function stopStory(s) {
  s.active = false;
  s.expires_at = null;
  for (const n of s.faults) {
    const f = faults.find((x) => x.name === n);
    if (f.story === s.name) {
      f.active = false;
      f.story = null;
      f.expires_at = null;
    }
  }
}

const SERVICES = [
  ["web", "edge"], ["concierge-api", "edge"],
  ["concierge-orchestrator", "agent"], ["portfolio-specialist", "agent"], ["trades-specialist", "agent"], ["transfers-specialist", "agent"], ["rebalance-specialist", "agent"], ["ticket-specialist", "agent"],
  ["portfolio-service", "backend"], ["trades-service", "backend"], ["transfers-service", "backend"], ["ticket-status-service", "backend"],
  ["order-router", "backoffice"], ["ledger-service", "backoffice"], ["compliance-service", "backoffice"], ["venue-gateway", "backoffice"], ["fraud-service", "backoffice"], ["ach-gateway", "backoffice"], ["sweep-service", "backoffice"], ["wallet-balance-service", "backoffice"], ["settlement-service", "backoffice"], ["notification-service", "backoffice"], ["market-data-service", "backoffice"],
];
const BASE_LATENCY = Object.fromEntries(SERVICES.map(([n], i) => [n, 3 + ((i * 7) % 13)]));

function serviceRows() {
  return SERVICES.map(([name, kind]) => {
    const f = faults.filter((x) => x.active && x._service === name);
    const down = f.some((x) => ["ach_down", "sweep_stuck"].includes(x.name));
    const slow = f.length > 0;
    return { name, kind, healthy: !down, latency_ms: down ? 0 : Math.round(BASE_LATENCY[name] * (slow ? 25 : 1) + Math.random() * 3) };
  });
}

// Background queue traffic (other queues than the ones tickets use).
const bgTemplates = {
  sweeps: ["sweep cash to money market", "sweep wallet to cash"],
  "ach.returns": ["return R01 insufficient funds", "return R03 no account"],
  "settlement.instructions": ["settle 40 AST-000123", "settle 12 AST-000150"],
  notifications: ["order.confirmed email", "transfer.completed push"],
  orders: ["buy 25 NOV-EQ"],
  "transfers.outbound": ["withdraw $120.00"],
};
function seedBackground() {
  for (const q of QUEUES) {
    for (let i = 0; i < 2 + (QUEUES.indexOf(q) % 3); i++) {
      const tpl = bgTemplates[q];
      qmsgs.push({ id: nextId(), queue: q, state: "ready", attempt: 1, ticket_id: null, customer_id: customers[(i * 5 + QUEUES.indexOf(q)) % 60].id, last_error: null, enqueued_at: ago(10 + i * 15), summary: tpl[i % tpl.length] });
    }
  }
  const dead = [
    ["orders", "buy 40 HEL-EQ", "VenueTimeoutError: no fill report from venue after 30s"],
    ["orders", "sell 15 AER-EQ", "VenueTimeoutError: no fill report from venue after 30s"],
    ["transfers.outbound", "withdraw $1,200.00", "AchGatewayError: connection refused"],
    ["settlement.instructions", "settle 12 AST-000150", "SettlementRejected: account closed"],
  ];
  for (const [queue, summary, last_error] of dead) {
    qmsgs.push({ id: nextId(), queue, state: "dlq", attempt: 3, ticket_id: null, customer_id: customers[(qmsgs.length * 3) % 60].id, last_error, enqueued_at: ago(600 + qmsgs.length * 40), summary });
  }
  qmsgs.push({ id: nextId(), queue: "orders", state: "retry", attempt: 2, ticket_id: null, customer_id: "CUS-0007", last_error: "VenueTimeoutError: no fill report", enqueued_at: ago(50), summary: "buy 5 NVD-EQ" });
  qmsgs.push({ id: nextId(), queue: "orders", state: "inflight", attempt: 1, ticket_id: null, customer_id: "CUS-0012", last_error: null, enqueued_at: ago(4), summary: "sell 3 NVD-EQ" });
}

const counters = Object.fromEntries(QUEUES.map((q, i) => [q, { processed_5m: 20 + i * 9, failed_5m: i % 3 }]));

function tick() {
  const nowMs = Date.now();
  for (const t of tickets) {
    const age = (nowMs - t._since) / 1000;
    if (t.state === "queued" && age > 4 && !(t.queue === "sweeps" && faultActive("sweep_stuck")) && !(t.kind === "transfer" && faultActive("ach_down") && age < 20)) {
      setTicketState(t, "processing");
    } else if (t.state === "processing" && age > 4) finish(t);
    else if (t.state === "retrying" && age > 5) setTicketState(t, "queued");
  }
  // Background churn on the counters and queues.
  for (const q of QUEUES) {
    counters[q].processed_5m = Math.max(0, counters[q].processed_5m + Math.round(Math.random() * 4 - 1.6));
    counters[q].failed_5m = Math.max(0, counters[q].failed_5m + (Math.random() < 0.15 ? 1 : Math.random() < 0.2 ? -1 : 0));
  }
  for (const m of qmsgs) {
    if (m.ticket_id === null && m.state === "ready" && Math.random() < 0.05) qmsgs.splice(qmsgs.indexOf(m), 1);
  }
  if (Math.random() < 0.4) {
    const q = QUEUES[Math.floor(Math.random() * QUEUES.length)];
    qmsgs.push({ id: nextId(), queue: q, state: "ready", attempt: 1, ticket_id: null, customer_id: customers[Math.floor(Math.random() * 60)].id, last_error: null, enqueued_at: now(), summary: bgTemplates[q][0] });
  }
  // Expiry of stories.
  for (const s of stories) if (s.active && new Date(s.expires_at).getTime() < nowMs) stopStory(s);
  for (const f of faults) if (f.active && !f.story && f.expires_at && new Date(f.expires_at).getTime() < nowMs) Object.assign(f, { active: false, expires_at: null });
  // Runs finish.
  for (const r of runs) if (r.status === "running" && nowMs - new Date(r.started_at).getTime() > 8000) Object.assign(r, { status: faultActive("settlement_fail") && r.kind === "settlement" ? "failed" : "completed", detail: faultActive("settlement_fail") && r.kind === "settlement" ? "38 instructions, 12 fails" : `${30 + Math.floor(Math.random() * 20)} items, 0 fails`, ended_at: now() });
  // Load generator.
  if (loadgen.enabled) {
    loadgen.stats.conversations += Math.round(loadgen.conversations_per_minute / 20);
    loadgen.stats.turns += Math.round((loadgen.conversations_per_minute * 3) / 20);
    loadgen.stats.transactions += Math.round(loadgen.transactions_per_minute / 20);
    if (Math.random() < 0.05) loadgen.stats.errors++;
    loadgen.stats.last_seen_at = now();
  }
}

function finish(t) {
  if (t.kind === "trade") {
    const tr = trades.find((x) => x.id === t.ref_id);
    if (tr) {
      const ins = bySymbol.get(tr.symbol);
      if (restricted.some((r) => r.symbol === tr.symbol)) {
        tr.status = "rejected";
        tr.reject_reason = "Symbol is on the restricted list";
        setTicketState(t, "failed", "Symbol is on the restricted list");
        tr.updated_at = now();
        notify(tr.customer_id, "order.rejected", `Your ${tr.side} of ${tr.quantity} ${tr.symbol} was rejected: restricted symbol.`);
        return;
      }
      if (faultActive("venue_timeout") && Math.random() < 0.6) {
        t.attempt++;
        if (t.attempt > 3) {
          setTicketState(t, "dead_lettered", "VenueTimeoutError: no fill report from venue after 30s");
          tr.updated_at = now();
        } else setTicketState(t, "retrying", "VenueTimeoutError: no fill report from venue after 30s");
        return;
      }
      const cost = r2(tr.quantity * ins.price);
      const holds = holdingsOf.get(tr.customer_id);
      const h = holds.find((x) => x.symbol === tr.symbol);
      if (tr.side === "buy") {
        if (cashOf.get(tr.customer_id) < cost) {
          tr.status = "rejected";
          tr.reject_reason = "Insufficient cash";
          setTicketState(t, "failed", "Insufficient cash");
          tr.updated_at = now();
          notify(tr.customer_id, "order.rejected", `Your buy of ${tr.quantity} ${tr.symbol} was rejected: insufficient cash.`);
          return;
        }
        cashOf.set(tr.customer_id, r2(cashOf.get(tr.customer_id) - cost));
        if (h) h.quantity += tr.quantity;
        else holds.push({ symbol: tr.symbol, quantity: tr.quantity });
      } else {
        if (!h || h.quantity < tr.quantity) {
          tr.status = "rejected";
          tr.reject_reason = "Not enough shares to sell";
          setTicketState(t, "failed", "Not enough shares to sell");
          tr.updated_at = now();
          notify(tr.customer_id, "order.rejected", `Your sell of ${tr.quantity} ${tr.symbol} was rejected: not enough shares.`);
          return;
        }
        h.quantity -= tr.quantity;
        if (h.quantity === 0) holds.splice(holds.indexOf(h), 1);
        cashOf.set(tr.customer_id, r2(cashOf.get(tr.customer_id) + cost));
      }
      tr.status = "filled";
      tr.filled_quantity = tr.quantity;
      tr.avg_price = ins.price;
      tr.updated_at = now();
      notify(tr.customer_id, "order.confirmed", `Your ${tr.side} of ${tr.quantity} ${tr.symbol} was filled at $${ins.price.toFixed(2)}.`);
    }
  } else if (t.kind === "transfer") {
    const x = transfers.find((v) => v.id === t.ref_id);
    if (x) {
      if (x.direction === "out" && faultActive("ach_down")) {
        t.attempt++;
        if (t.attempt > 3) setTicketState(t, "dead_lettered", "AchGatewayError: connection refused");
        else setTicketState(t, "retrying", "AchGatewayError: connection refused");
        return;
      }
      if (x.direction === "out" && cashOf.get(x.customer_id) < x.amount) {
        x.status = "failed";
        x.reason = "Insufficient cash";
        x.updated_at = now();
        setTicketState(t, "failed", "Insufficient cash");
        notify(x.customer_id, "transfer.failed", `Your withdrawal of $${x.amount.toFixed(2)} failed: insufficient cash.`);
        return;
      }
      cashOf.set(x.customer_id, r2(cashOf.get(x.customer_id) + (x.direction === "in" ? x.amount : -x.amount)));
      x.status = "completed";
      x.updated_at = now();
      notify(x.customer_id, "transfer.completed", `Your ${x.direction === "in" ? "deposit" : "withdrawal"} of $${x.amount.toFixed(2)} completed.`);
    }
  }
  setTicketState(t, "completed");
}

// ----------------------------------------------------------------- seed data

const runs = [
  { id: 12, kind: "settlement", status: "completed", detail: "38 instructions, 2 fails", started_at: ago(86400 + 60), ended_at: ago(86400 + 20), started_by: "OPS-kai" },
  { id: 11, kind: "pricing", status: "completed", detail: "152 prices", started_at: ago(86400 + 4000), ended_at: ago(86400 + 3990), started_by: "OPS-mia" },
  { id: 10, kind: "sweep", status: "failed", detail: "ledger lock timeout", started_at: ago(172800), ended_at: ago(172790), started_by: "OPS-kai" },
];
const loadgen = { enabled: true, conversations_per_minute: 6, transactions_per_minute: 4, auto_stories: false, stats: { conversations: 120, turns: 400, transactions: 80, errors: 3, last_seen_at: ago(4) } };

for (const c of customers) {
  const rand = rng(Number(c.id.slice(4)) * 31);
  const pick = () => holdingsOf.get(c.id)[Math.floor(rand() * holdingsOf.get(c.id).length)].symbol;
  const filled = newTrade(c.id, { side: "buy", symbol: pick(), quantity: 5 + Math.floor(rand() * 20), status: "filled", created: ago(86400 * (1 + rand() * 6)) });
  filled.filled_quantity = filled.quantity;
  filled.avg_price = bySymbol.get(filled.symbol).price;
  newTrade(c.id, { side: rand() < 0.5 ? "buy" : "sell", symbol: pick(), quantity: 1 + Math.floor(rand() * 8), status: "pending", created: ago(3600 * (1 + rand() * 20)) });
  if (rand() < 0.4) newTrade(c.id, { side: "buy", symbol: pick(), quantity: 2 + Math.floor(rand() * 8), status: "pending", created: ago(1800) });
  const x = { id: `TRF-${pad(1200 + transfers.length + 1, 8)}`, customer_id: c.id, direction: "in", amount: r2(100 + rand() * 900), counterparty: "bank", status: "completed", ticket_id: null, reason: null, created_at: ago(86400 * 3), updated_at: ago(86400 * 3) };
  transfers.unshift(x);
  notifications.push({ id: nextId(), customer_id: c.id, kind: "transfer.completed", text: `Your deposit of $${x.amount.toFixed(2)} completed.`, created_at: ago(86400 * 3) });
  notifications.push({ id: nextId(), customer_id: c.id, kind: "order.confirmed", text: `Your buy of ${filled.quantity} ${filled.symbol} was filled at $${filled.avg_price.toFixed(2)}.`, created_at: ago(86400 * 2) });
}
notifications.sort((a, b) => b.created_at.localeCompare(a.created_at));
{
  // A few tickets in different states, a held transfer, and a rejected trade.
  const mk = (cid, kind, state, extra = {}) => {
    let tk;
    if (kind === "trade") tk = submitTrade(newTrade(cid, { side: "buy", symbol: "NVD-EQ", quantity: 10 }));
    else tk = createTransfer(cid, { direction: "out", amount: 1200, counterparty: "bank" }).ticket;
    setTicketState(tk, state, extra.reason ?? null);
    if (extra.attempt) {
      tk.attempt = extra.attempt;
      const m = qmsgs.find((q) => q.ticket_id === tk.id);
      if (m) m.attempt = extra.attempt;
    }
    return tk;
  };
  mk("CUS-0003", "trade", "dead_lettered", { reason: "VenueTimeoutError: no fill report from venue after 30s", attempt: 3 });
  mk("CUS-0005", "transfer", "retrying", { reason: "AchGatewayError: connection refused", attempt: 2 });
  const held = createTransfer("CUS-0009", { direction: "out", amount: 15000, counterparty: "bank" });
  createTransfer("CUS-0021", { direction: "in", amount: 25000, counterparty: "card_account" });
  createTransfer("CUS-0033", { direction: "out", amount: 12400, counterparty: "bank" });
  held.ticket.notes.push({ author: "SUP-ava", text: "Customer called to ask about the hold; told them compliance reviews within a day.", created_at: ago(1800) });
  const rej = newTrade("CUS-0004", { side: "buy", symbol: "ORB-EQ", quantity: 20, status: "rejected" });
  rej.reject_reason = "Symbol is on the restricted list";
}
seedBackground();

// ----------------------------------------------------------- conversations

const failedOnce = new Set();
const conversations = new Map(); // id -> {id, customer_id, title, messages, pending, updated_at, failedOnce:Set}

function msg(conv, role, text, extra = {}) {
  const m = { id: nextId(), role, text, cards: [], outcome: null, trace_id: null, feedback: null, author: null, created_at: now(), ...extra };
  conv.messages.push(m);
  conv.updated_at = m.created_at;
  return m;
}
const newConv = (cid, title) => {
  const c = { id: randomUUID(), customer_id: cid, title, messages: [], pending: null, updated_at: now(), failedOnce: new Set() };
  conversations.set(c.id, c);
  return c;
};

function seedConversations() {
  const specs = [
    ["CUS-0001", "Show my portfolio", "ok", null],
    ["CUS-0002", "Show my pending trades", "tool_timeout_recovered", null],
    ["CUS-0006", "Transfer money", "ok", "down"],
    ["CUS-0008", "Rebalance my portfolio", "agent_bug", "down"],
    ["CUS-0010", "Show my portfolio", "ok", "up"],
    ["CUS-0013", "What is each of my investments worth right now?", "ok", null],
    ["CUS-0058", "What is each of my investments worth right now?", "context_overflow", "down"],
    ["CUS-0059", "What is each of my investments worth right now?", "context_overflow", null],
    ["CUS-0015", "Check ticket", "error", "down"],
  ];
  specs.forEach(([cid, title, outcome, fb], i) => {
    const c = newConv(cid, title);
    const t0 = Date.now() - (i + 1) * 3600 * 1000 * 2;
    const userText = title === "Check ticket" ? "Check ticket TKT-00000000" : title;
    const u = msg(c, "user", userText, { created_at: new Date(t0).toISOString() });
    const reply = respond(c, userText, outcome);
    const a = msg(c, "assistant", reply.text, { cards: reply.cards, outcome, trace_id: hex(16), feedback: fb, created_at: new Date(t0 + 4000).toISOString(), tool_calls: reply.tool_calls });
    c.updated_at = a.created_at;
    void u;
  });
}

// ----------------------------------------------------------- scripted Concierge

function toolCall(agent, tool, args, result, error = null) {
  return { agent, tool, arguments: args, result: typeof result === "string" ? result : JSON.stringify(result), error };
}

const moneyRe = /^(Add|Withdraw) \$([\d,]+(?:\.\d+)?) (?:to|from) my account\.?$/i;

/** Returns {text, cards, tool_calls, outcome?} for a user message. May set conv.pending. */
function respond(conv, text, forcedOutcome) {
  const cid = conv.customer_id;
  const c = people.get(cid);
  const t = text.trim();
  const out = (text, cards = [], tool_calls = [], outcome = "ok") => ({ text, cards, tool_calls, outcome });

  if (/^show my portfolio/i.test(t)) {
    const alloc = allocationOf(cid);
    const acc = accountOf(cid);
    const calls = [toolCall("PortfolioSpecialist", "get_portfolio_breakdown", { account_id: acc.id }, { total_value: acc.total_value, allocation: alloc })];
    if (forcedOutcome === "tool_timeout_recovered") calls.unshift(toolCall("PortfolioSpecialist", "get_portfolio_breakdown", { account_id: acc.id }, "", "ToolTimeout: portfolio-service did not answer in 5000 ms"));
    if (forcedOutcome === "agent_bug") return out("Here is your portfolio. Your allocation is 0% stocks, 0% bonds.", [], calls, "agent_bug");
    return out(`Your account ${acc.id} is worth ${usd(acc.total_value)}. Here is how it is spread:`, [{ type: "allocation", items: alloc }], calls, forcedOutcome ?? "ok");
  }
  if (/^show my pending trades/i.test(t)) {
    const pending = trades.filter((x) => x.customer_id === cid && x.status === "pending");
    const calls = [toolCall("TradesSpecialist", "list_trades", { customer_id: cid, status: "pending" }, pending.map((p) => p.id), forcedOutcome === "tool_timeout_recovered" ? "ToolTimeout: trades-service did not answer in 5000 ms" : null)];
    if (forcedOutcome === "tool_timeout_recovered") calls.push(toolCall("TradesSpecialist", "list_trades", { customer_id: cid, status: "pending" }, pending.map((p) => p.id)));
    if (pending.length === 0) return out("You have no pending trades.", [], calls, forcedOutcome ?? "ok");
    return out(`You have ${pending.length} pending trade${pending.length === 1 ? "" : "s"}. Tap one to change it.`, [{ type: "trades", items: pending.map((p) => ({ id: p.id, side: p.side, symbol: p.symbol, quantity: p.quantity, status: p.status })) }], calls, forcedOutcome ?? "ok");
  }
  if (/^transfer money/i.test(t)) {
    if (forcedOutcome === "agent_bug") return out("Done. I moved $0.00 to your account.", [], [toolCall("TransfersSpecialist", "propose_transfer", { amount: 0 }, "ok")], "agent_bug");
    return out("Sure. Choose a direction and an amount.", [{ type: "transfer_form" }], [], forcedOutcome ?? "ok");
  }
  const mm = moneyRe.exec(t);
  if (mm) {
    const direction = /^add/i.test(mm[1]) ? "in" : "out";
    const amount = Number(mm[2].replace(/,/g, ""));
    conv.pending = { kind: "transfer", direction, amount };
    return out(
      `Please confirm this transfer.`,
      [{ type: "confirm", kind: "transfer", title: `${direction === "in" ? "Deposit" : "Withdrawal"} ${usd(amount)}`, lines: [direction === "in" ? "From your bank account" : "To your bank account", amount > 10000 ? "Amounts over $10,000 are reviewed by compliance" : "Usually completes in a few minutes"] }],
      [toolCall("TransfersSpecialist", "propose_transfer", { direction, amount, counterparty: "bank" }, { proposed: true })],
    );
  }
  if (/^rebalance my portfolio/i.test(t)) {
    if (forcedOutcome === "agent_bug") return out("I rebalanced your portfolio. Cycle limit reached after 12 attempts.", [], [toolCall("RebalanceSpecialist", "plan_rebalance", { risk: null }, "loop detected")], "agent_bug");
    return out("What risk level should I aim for?", [{ type: "chips", options: ["Risk level: conservative", "Risk level: moderate", "Risk level: aggressive"] }], []);
  }
  const rl = /^Risk level: (\w+)/i.exec(t);
  if (rl) {
    const alloc = allocationOf(cid);
    const target = { conservative: { Stocks: 30, Bonds: 45, Cash: 10, "Real Estate": 10, Commodities: 5 }, moderate: { Stocks: 55, Bonds: 25, Cash: 5, "Real Estate": 10, Commodities: 5 }, aggressive: { Stocks: 75, Bonds: 5, Cash: 3, "Real Estate": 10, Commodities: 7 } }[rl[1].toLowerCase()] ?? {};
    const moves = Object.keys(CLASS_SUFFIX).map((k) => ({ asset_class: k, from: alloc.find((a) => a.asset_class === k)?.percent ?? 0, to: target[k] ?? 0 })).filter((m) => m.from !== m.to);
    conv.pending = { kind: "rebalance" };
    return out(`Here is a ${rl[1].toLowerCase()} plan.`, [{ type: "rebalance_plan", moves }, { type: "confirm", kind: "rebalance", title: "Rebalance to a " + rl[1].toLowerCase() + " mix", lines: [`${moves.length} asset classes change`, "Orders are sent to the back office"] }], [toolCall("RebalanceSpecialist", "plan_rebalance", { risk: rl[1] }, { moves })]);
  }
  if (/^what is each of my investments worth/i.test(t)) {
    const rows = holdingRows(cid);
    if (isWhale(c) || forcedOutcome === "context_overflow") {
      return out(
        "I could not finish that. There are too many holdings for me to value in one go, so I stopped. Ask me about one asset class at a time and I will do that.",
        [],
        [toolCall("PortfolioSpecialist", "get_holding_value", { asset_id: rows[0].asset_id }, "(140 more calls)", "ContextWindowOverflow: prompt exceeds the model's context window")],
        "context_overflow",
      );
    }
    return out(`I valued all ${rows.length} of your holdings.`, [{ type: "valuations", count: rows.length, total: r2(rows.reduce((a, h) => a + h.market_value, 0)) }], rows.slice(0, 3).map((h) => toolCall("PortfolioSpecialist", "get_holding_value", { asset_id: h.asset_id }, { market_value: h.market_value })));
  }
  const ct = /^Check ticket (TKT-\w+)/i.exec(t);
  if (ct) {
    const tk = tickets.find((x) => x.id === ct[1] && x.customer_id === cid);
    if (forcedOutcome === "error" || !tk) {
      return out(forcedOutcome === "error" ? "Sorry, the Concierge could not answer." : `I could not find ticket ${ct[1]}.`, [], [toolCall("TicketSpecialist", "get_ticket_status", { ticket_id: ct[1] }, "", "NotFound")], forcedOutcome ?? "ok");
    }
    return out(`Ticket ${tk.id} is ${tk.state.replace(/_/g, " ")}${tk.queue_position ? `, number ${ticketOut(tk).queue_position} in line` : ""}.`, [{ type: "ticket", ticket_id: tk.id, kind: tk.kind, state: tk.state }], [toolCall("TicketSpecialist", "get_ticket_status", { ticket_id: tk.id }, { state: tk.state })]);
  }
  const ch = /^Change trade (TRD-\d+) to\s*(.*)$/i.exec(t);
  if (ch) {
    const tr = trades.find((x) => x.id === ch[1] && x.customer_id === cid && x.status === "pending");
    if (!tr) return out(`I could not find a pending trade ${ch[1]}.`);
    const q = /(\d+)/.exec(ch[2]);
    const side = /sell/i.test(ch[2]) ? "sell" : /buy/i.test(ch[2]) ? "buy" : tr.side;
    if (!q && side === tr.side) return out(`What should trade ${tr.id} change to? For example “sell 5”.`);
    conv.pending = { kind: "trade", id: tr.id, side, quantity: q ? Number(q[1]) : tr.quantity };
    return out("Please confirm the change and send the order.", [{ type: "confirm", kind: "trade", title: `${side === "buy" ? "Buy" : "Sell"} ${conv.pending.quantity} ${tr.symbol}`, lines: [`Replaces ${tr.id}`, "Sent to the back office right away"] }], [toolCall("TradesSpecialist", "update_trade", { trade_id: tr.id, side, quantity: conv.pending.quantity }, { ok: true })]);
  }
  if (/^confirm$/i.test(t)) {
    const p = conv.pending;
    conv.pending = null;
    if (!p) return out("There is nothing to confirm right now.");
    if (p.kind === "transfer") {
      const { transfer, ticket } = createTransfer(cid, { direction: p.direction, amount: p.amount, counterparty: "bank" });
      return out(`Done. I started transfer ${transfer.id}.`, [{ type: "ticket", ticket_id: ticket.id, kind: "transfer", state: ticket.state }], [toolCall("TransfersSpecialist", "submit_transfer", { transfer_id: transfer.id }, { ticket_id: ticket.id })]);
    }
    if (p.kind === "trade") {
      const tr = trades.find((x) => x.id === p.id);
      tr.side = p.side;
      tr.quantity = p.quantity;
      const tk = submitTrade(tr);
      return out(`Done. Your order ${tr.id} is with the back office.`, [{ type: "ticket", ticket_id: tk.id, kind: "trade", state: tk.state }], [toolCall("TradesSpecialist", "submit_trade", { trade_id: tr.id }, { ticket_id: tk.id })]);
    }
    const tk = newTicket(cid, "rebalance", `RBL-${hex(3).toUpperCase()}`, "orders");
    return out("Done. I sent the rebalance orders.", [{ type: "ticket", ticket_id: tk.id, kind: "rebalance", state: tk.state }], [toolCall("RebalanceSpecialist", "submit_rebalance", {}, { ticket_id: tk.id })]);
  }
  if (/^cancel$/i.test(t)) {
    const had = conv.pending;
    conv.pending = null;
    return out(had ? "Okay, I cancelled that. Nothing was sent." : "There is nothing to cancel right now.");
  }
  return out("I can show your portfolio and pending trades, move money, rebalance, and check on tickets. What would you like to do?", [{ type: "chips", options: ["Show my portfolio", "Show my pending trades", "Transfer money", "Rebalance my portfolio"] }]);
}
const usd = (n) => n.toLocaleString("en-US", { style: "currency", currency: "USD" });

// ---------------------------------------------------------------- HTTP layer

class HttpError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const tokens = new Map(); // token -> person id
const routes = [];
function route(method, pattern, roles, handler) {
  const keys = [];
  const re = new RegExp("^" + pattern.replace(/\{(\w+)\}/g, (_, k) => (keys.push(k), "([^/]+)")) + "$");
  routes.push({ method, re, keys, roles, handler });
}

const need = (cond, msg = "Bad request.") => {
  if (!cond) throw new HttpError(400, "bad_request", msg);
};
const find = (arr, pred, what) => {
  const v = arr.find(pred);
  if (!v) throw new HttpError(404, "not_found", `${what} not found.`);
  return v;
};
const limitOf = (q, def, max = 100) => Math.min(Number(q.get("limit")) || def, max);

// ---- 3.1 Session
route("GET", "/api/config", null, () => ({ service_name: "concierge-web", version: "0.1.0", otlp_path: "/otlp", environment: "demo", honeycomb_trace_url: process.env.MOCK_TRACE_URL ?? null }));
route("GET", "/api/personas", null, () => ({ customers: customers.map((c) => personOut(c)), staff: staff.map((s) => personOut(s)) }));
route("POST", "/api/login", null, ({ body }) => {
  const p = people.get(body?.user_id);
  if (!p) throw new HttpError(404, "unknown_user", "Unknown user.");
  const token = `mock.${p.id}.${hex(8)}`;
  tokens.set(token, p.id);
  return { token, user: personOut(p) };
});
route("GET", "/api/me", "any", ({ user }) => personOut(user, true));

// ---- 3.2 Customer
const C = ["customer"];
route("GET", "/api/summary", C, ({ user }) => ({ customer: personOut(user), account: accountOf(user.id), allocation: allocationOf(user.id) }));
route("GET", "/api/holdings", C, ({ user }) => holdingRows(user.id));
route("GET", "/api/instruments", C, () => instruments);
route("GET", "/api/trades", C, ({ user, query }) => {
  const status = query.get("status") ?? "all";
  return trades.filter((t) => t.customer_id === user.id && (status === "all" || t.status === status)).slice(0, 100).map(tradeOut);
});
route("POST", "/api/trades", C, ({ user, body }) => {
  need(["buy", "sell"].includes(body?.side), "side must be buy or sell.");
  const ins = bySymbol.get(body?.symbol);
  need(ins, "Unknown symbol.");
  need(Number.isInteger(body?.quantity) && body.quantity > 0, "quantity must be a positive whole number.");
  need(["now", "pending"].includes(body?.mode), "mode must be now or pending.");
  const t = newTrade(user.id, { side: body.side, symbol: ins.symbol, quantity: body.quantity });
  const tk = body.mode === "now" ? submitTrade(t) : null;
  return { trade: tradeOut(t), ticket_id: tk?.id ?? null };
});
route("POST", "/api/trades/{id}/submit", C, ({ user, params }) => {
  const t = find(trades, (x) => x.id === params.id && x.customer_id === user.id, "Trade");
  if (t.status !== "pending") throw new HttpError(409, "not_pending", "Only pending trades can be submitted.");
  const tk = submitTrade(t);
  return { trade: tradeOut(t), ticket_id: tk.id };
});
route("POST", "/api/trades/{id}/cancel", C, ({ user, params }) => {
  const t = find(trades, (x) => x.id === params.id && x.customer_id === user.id, "Trade");
  if (t.status !== "pending") throw new HttpError(409, "not_pending", "Only pending trades can be cancelled.");
  t.status = "cancelled";
  t.updated_at = now();
  return { trade: tradeOut(t) };
});
route("GET", "/api/transfers", C, ({ user }) => transfers.filter((x) => x.customer_id === user.id).slice(0, 100).map(transferOut));
route("POST", "/api/transfers", C, ({ user, body }) => {
  need(["in", "out"].includes(body?.direction), "direction must be in or out.");
  need(typeof body?.amount === "number" && body.amount > 0, "amount must be positive.");
  need(["bank", "card_account"].includes(body?.counterparty), "counterparty must be bank or card_account.");
  const { transfer, ticket } = createTransfer(user.id, body);
  return { transfer: transferOut(transfer), ticket_id: ticket.id };
});
route("GET", "/api/tickets", C, ({ user }) => tickets.filter((t) => t.customer_id === user.id).slice(0, 100).map(ticketOut));
route("GET", "/api/tickets/{id}", C, ({ user, params }) => ticketOut(find(tickets, (t) => t.id === params.id && t.customer_id === user.id, "Ticket")));
route("GET", "/api/notifications", C, ({ user }) => notifications.filter((n) => n.customer_id === user.id).slice(0, 50).map(({ customer_id, ...n }) => n));

// ---- 3.3 Chat
const convRow = (c) => ({ id: c.id, title: c.title, updated_at: c.updated_at, message_count: c.messages.length });
const publicMsg = ({ tool_calls, ...m }) => m;
route("GET", "/api/conversations", C, ({ user }) =>
  [...conversations.values()].filter((c) => c.customer_id === user.id).sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 30).map(convRow),
);
route("GET", "/api/conversations/{id}", C, ({ user, params }) => {
  const c = find([...conversations.values()], (x) => x.id === params.id && x.customer_id === user.id, "Conversation");
  return { id: c.id, customer_id: c.customer_id, messages: c.messages.map(publicMsg) };
});
route("POST", "/api/chat", C, async ({ user, body }) => {
  need(typeof body?.message === "string" && body.message.trim(), "message is required.");
  const text = body.message.trim();
  const failKey = `${body.conversation_id}:${text}`;
  if (/\(fail\)/i.test(text) && !failedOnce.has(failKey)) {
    failedOnce.add(failKey);
    await sleep(CHAT_DELAY);
    throw new HttpError(503, "model_unavailable", "The model did not answer.");
  }
  let conv;
  if (body.conversation_id) conv = find([...conversations.values()], (x) => x.id === body.conversation_id && x.customer_id === user.id, "Conversation");
  else conv = newConv(user.id, text.slice(0, 60));
  const slow = /what is each of my investments/i.test(text) ? 3000 : CHAT_DELAY;
  await sleep(slow + Math.random() * 400);
  msg(conv, "user", text);
  const r = respond(conv, text);
  const m = msg(conv, "assistant", r.text, { cards: r.cards, outcome: r.outcome, trace_id: hex(16), tool_calls: r.tool_calls });
  return { conversation_id: conv.id, message: publicMsg(m) };
});
route("POST", "/api/messages/{id}/feedback", C, ({ user, params, body }) => {
  need(["up", "down"].includes(body?.rating), "rating must be up or down.");
  for (const c of conversations.values()) {
    const m = c.messages.find((x) => String(x.id) === params.id);
    if (m) {
      if (c.customer_id !== user.id) break;
      m.feedback = body.rating;
      return { ok: true };
    }
  }
  throw new HttpError(404, "not_found", "Message not found.");
});

// ---- 3.4 Support
const S = ["support", "operations"]; // operations may read too
const SW = ["support"];
const convSummary = (c) => {
  const assistant = c.messages.filter((m) => m.role === "assistant");
  return { id: c.id, customer_id: c.customer_id, customer_name: people.get(c.customer_id).name, title: c.title, last_outcome: assistant.at(-1)?.outcome ?? null, has_negative_feedback: c.messages.some((m) => m.feedback === "down"), message_count: c.messages.length, updated_at: c.updated_at };
};
route("GET", "/api/support/customers", S, ({ query }) => {
  const q = (query.get("q") ?? "").toLowerCase();
  return customers.filter((c) => !q || [c.name, c.id, c.email].some((f) => f.toLowerCase().includes(q))).slice(0, 60).map((c) => personOut(c));
});
route("GET", "/api/support/customers/{id}", S, ({ params }) => {
  const c = find(customers, (x) => x.id === params.id, "Customer");
  return {
    customer: personOut(c),
    account: accountOf(c.id),
    allocation: allocationOf(c.id),
    trades: trades.filter((t) => t.customer_id === c.id).slice(0, 10).map(tradeOut),
    transfers: transfers.filter((t) => t.customer_id === c.id).slice(0, 10).map(transferOut),
    tickets: tickets.filter((t) => t.customer_id === c.id).slice(0, 10).map(ticketOut),
    conversations: [...conversations.values()].filter((x) => x.customer_id === c.id).sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 10).map(convSummary),
  };
});
route("GET", "/api/support/conversations", S, ({ query }) => {
  const cid = query.get("customer_id");
  const outcome = query.get("outcome");
  const down = query.get("feedback") === "down";
  return [...conversations.values()]
    .filter((c) => (!cid || c.customer_id === cid) && (!outcome || c.messages.some((m) => m.outcome === outcome)) && (!down || c.messages.some((m) => m.feedback === "down")))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, limitOf(query, 50))
    .map(convSummary);
});
route("GET", "/api/support/conversations/{id}", S, ({ params }) => {
  const c = find([...conversations.values()], (x) => x.id === params.id, "Conversation");
  return { id: c.id, customer_id: c.customer_id, customer_name: people.get(c.customer_id).name, messages: c.messages.map((m) => (m.role === "assistant" ? { ...m, tool_calls: m.tool_calls ?? [] } : m)) };
});
route("POST", "/api/support/conversations/{id}/reply", SW, ({ user, params, body }) => {
  need(typeof body?.text === "string" && body.text.trim(), "text is required.");
  const c = find([...conversations.values()], (x) => x.id === params.id, "Conversation");
  return { message: msg(c, "support", body.text.trim(), { author: user.id }) };
});
route("GET", "/api/support/tickets", S, ({ query }) => {
  const state = query.get("state");
  const cid = query.get("customer_id");
  return tickets.filter((t) => (!state || t.state === state) && (!cid || t.customer_id === cid)).slice(0, limitOf(query, 50)).map(ticketOut);
});
route("POST", "/api/support/tickets/{id}/notes", SW, ({ user, params, body }) => {
  need(typeof body?.text === "string" && body.text.trim(), "text is required.");
  const t = find(tickets, (x) => x.id === params.id, "Ticket");
  t.notes.push({ author: user.id, text: body.text.trim(), created_at: now() });
  return ticketOut(t);
});

// ---- 3.5 Operations
const O = ["operations"];
route("GET", "/api/ops/services", O, () => serviceRows());
route("GET", "/api/ops/queues", O, () =>
  QUEUES.map((queue) => {
    const ms = qmsgs.filter((m) => m.queue === queue);
    const ready = ms.filter((m) => m.state === "ready");
    const oldest = ready.length ? Math.max(...ready.map((m) => (Date.now() - new Date(m.enqueued_at).getTime()) / 1000)) : 0;
    return { queue, ready: ready.length, inflight: ms.filter((m) => m.state === "inflight").length, retry: ms.filter((m) => m.state === "retry").length, dlq: ms.filter((m) => m.state === "dlq").length, oldest_ready_age_s: Math.round(oldest * 10) / 10, consumer: CONSUMER[queue], ...counters[queue] };
  }),
);
route("GET", "/api/ops/queues/{queue}/messages", O, ({ params, query }) => {
  need(QUEUES.includes(params.queue), "Unknown queue.");
  const state = query.get("state") ?? "ready";
  need(["ready", "inflight", "retry", "dlq"].includes(state), "Unknown state.");
  return qmsgs.filter((m) => m.queue === params.queue && m.state === state).sort((a, b) => b.id - a.id).slice(0, limitOf(query, 50, 200));
});
route("POST", "/api/ops/queues/{queue}/replay", O, ({ params, body }) => {
  need(QUEUES.includes(params.queue), "Unknown queue.");
  const dead = qmsgs.filter((m) => m.queue === params.queue && m.state === "dlq" && (body?.all === true || (body?.message_ids ?? []).includes(m.id)));
  for (const m of dead) {
    m.state = "ready";
    m.attempt = 1;
    m.enqueued_at = now();
    const tk = tickets.find((t) => t.id === m.ticket_id);
    if (tk) {
      tk.attempt = 1;
      tk.state = "queued";
      tk.reason = null;
      tk._since = Date.now();
      tk.updated_at = now();
    }
  }
  return { replayed: dead.length };
});
route("GET", "/api/ops/stories", O, () => stories);
route("POST", "/api/ops/stories/stop-all", O, () => {
  const on = stories.filter((s) => s.active);
  on.forEach(stopStory);
  return { stopped: on.length };
});
route("POST", "/api/ops/stories/{name}/start", O, ({ params, body }) => {
  const s = find(stories, (x) => x.name === params.name, "Story");
  need(Number(body?.minutes) > 0, "minutes must be positive.");
  startStory(s, Number(body.minutes));
  return s;
});
route("POST", "/api/ops/stories/{name}/stop", O, ({ params }) => {
  const s = find(stories, (x) => x.name === params.name, "Story");
  stopStory(s);
  return s;
});
route("GET", "/api/ops/faults", O, () => faults.map(faultOut));
route("PUT", "/api/ops/faults/{name}", O, ({ params, body }) => {
  const f = find(faults, (x) => x.name === params.name, "Fault");
  need(typeof body?.active === "boolean", "active is required.");
  if ("params" in body && body.params !== null) need(typeof body.params === "object" && !Array.isArray(body.params), "params must be an object or null.");
  if ("params" in body) f.params = body.params;
  f.active = body.active;
  if (!f.active) {
    f.story = null;
    f.expires_at = null;
  } else f.expires_at = body.minutes ? new Date(Date.now() + body.minutes * 60000).toISOString() : f.story ? f.expires_at : null;
  return faultOut(f);
});
route("GET", "/api/ops/loadgen", O, () => loadgen);
route("PUT", "/api/ops/loadgen", O, ({ body }) => {
  for (const k of ["conversations_per_minute", "transactions_per_minute"]) {
    if (k in body) need(Number.isInteger(body[k]) && body[k] >= 0 && body[k] <= 120, `${k} must be 0 to 120.`);
  }
  for (const k of ["enabled", "auto_stories"]) if (k in body) need(typeof body[k] === "boolean", `${k} must be true or false.`);
  for (const k of ["enabled", "conversations_per_minute", "transactions_per_minute", "auto_stories"]) if (k in body) loadgen[k] = body[k];
  return loadgen;
});
route("POST", "/api/ops/loadgen/stats", O, ({ body }) => {
  for (const k of ["conversations", "turns", "transactions", "errors"]) if (Number.isInteger(body?.[k]) && body[k] >= 0) loadgen.stats[k] += body[k];
  loadgen.stats.last_seen_at = now();
  return { ok: true };
});
route("GET", "/api/ops/runs", O, () => runs.slice(0, 20));
route("POST", "/api/ops/runs", O, ({ user, body }) => {
  need(["settlement", "sweep", "pricing", "payroll"].includes(body?.kind), "kind must be settlement, sweep, pricing or payroll.");
  const r = { id: Math.max(...runs.map((x) => x.id)) + 1, kind: body.kind, status: "running", detail: "started", started_at: now(), ended_at: null, started_by: user.id };
  runs.unshift(r);
  if (body.kind === "payroll") {
    // About 40 payroll deposits, each one a message on the sweeps queue.
    for (let i = 0; i < 40; i++) {
      const cid = customers[(i * 3) % 60].id;
      qmsgs.push({ id: nextId(), queue: "sweeps", state: "ready", attempt: 1, ticket_id: null, customer_id: cid, last_error: null, enqueued_at: now(), summary: `payroll deposit $${(1500 + i * 37).toFixed(2)}` });
    }
  }
  return r;
});
route("GET", "/api/ops/tickets", O, ({ query }) => {
  const state = query.get("state");
  return tickets.filter((t) => !state || t.state === state).slice(0, limitOf(query, 50)).map(ticketOut);
});

// ---- 3.6 Compliance
const K = ["compliance"];
route("GET", "/api/compliance/holds", K, () =>
  transfers
    .filter((x) => x.status === "held")
    .map((x) => ({ transfer: transferOut(x), customer: personOut(people.get(x.customer_id)), ticket_id: x.ticket_id, risk_score: Math.min(0.99, r2(0.4 + x.amount / 40000)), hold_reason: "amount over $10,000" })),
);
route("POST", "/api/compliance/holds/{id}/decision", K, ({ user, params, body }) => {
  need(["approve", "reject"].includes(body?.decision), "decision must be approve or reject.");
  const x = find(transfers, (v) => v.id === params.id && v.status === "held", "Held transfer");
  const tk = tickets.find((t) => t.id === x.ticket_id);
  const note = body.reason ? ` ${body.reason}` : "";
  if (tk) tk.notes.push({ author: user.id, text: `${body.decision === "approve" ? "Approved" : "Rejected"}.${note}`, created_at: now() });
  if (body.decision === "approve") {
    x.status = "queued";
    if (tk) {
      tk.state = "queued";
      tk._since = Date.now();
      tk.reason = null;
      enqueue(tk);
    }
  } else {
    x.status = "failed";
    x.reason = body.reason || "Rejected by compliance";
    if (tk) {
      tk.state = "failed";
      tk.reason = x.reason;
    }
    notify(x.customer_id, "transfer.failed", `Your ${x.direction === "in" ? "deposit" : "withdrawal"} of $${x.amount.toFixed(2)} was rejected by compliance.`);
  }
  x.updated_at = now();
  if (tk) tk.updated_at = now();
  return { transfer: transferOut(x) };
});
route("GET", "/api/compliance/restricted", K, () => restricted);
route("POST", "/api/compliance/restricted", K, ({ user, body }) => {
  const symbol = String(body?.symbol ?? "").toUpperCase();
  need(bySymbol.has(symbol), "Unknown symbol.");
  need(typeof body?.reason === "string" && body.reason.trim(), "reason is required.");
  if (!restricted.some((r) => r.symbol === symbol)) restricted.unshift({ symbol, reason: body.reason.trim(), added_by: user.id, created_at: now() });
  return restricted;
});
route("DELETE", "/api/compliance/restricted/{symbol}", K, ({ params }) => {
  const i = restricted.findIndex((r) => r.symbol === decodeURIComponent(params.symbol));
  if (i >= 0) restricted.splice(i, 1);
  return restricted;
});

// ---- server
function send(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) });
  res.end(body);
}
const err = (res, status, code, message) => send(res, status, { error: { code, message } });

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://mock");
  try {
    if (url.pathname === "/healthz") return send(res, 200, { ok: true });
    let match = null;
    let pathMatched = false;
    for (const r of routes) {
      const m = r.re.exec(url.pathname);
      if (!m) continue;
      pathMatched = true;
      if (r.method !== req.method) continue;
      match = { r, params: Object.fromEntries(r.keys.map((k, i) => [k, m[i + 1]])) };
      break;
    }
    if (!match) return pathMatched ? err(res, 405, "method_not_allowed", "Method not allowed.") : err(res, 404, "not_found", "No such endpoint.");

    let user = null;
    if (match.r.roles) {
      const token = (req.headers.authorization ?? "").replace(/^Bearer /, "");
      const id = tokens.get(token);
      user = id ? people.get(id) : null;
      if (!user) return err(res, 401, "unauthorized", "Sign in again.");
      if (match.r.roles !== "any" && !match.r.roles.includes(user.role)) return err(res, 403, "forbidden", `The ${user.role} role may not do this.`);
    }
    let body;
    if (req.method === "POST" || req.method === "PUT") {
      const chunks = [];
      for await (const c of req) chunks.push(c);
      const raw = Buffer.concat(chunks).toString();
      try {
        body = raw ? JSON.parse(raw) : {};
      } catch {
        return err(res, 400, "bad_json", "Body is not valid JSON.");
      }
    }
    const out = await match.r.handler({ user, params: match.params, query: url.searchParams, body });
    send(res, 200, out);
  } catch (e) {
    if (e instanceof HttpError) return err(res, e.status, e.code, e.message);
    console.error(e);
    err(res, 500, "internal", "Mock server error.");
  }
});

seedConversations();
setInterval(tick, 2000).unref();
server.listen(PORT, () => console.log(`Mock API on http://localhost:${PORT}`));
