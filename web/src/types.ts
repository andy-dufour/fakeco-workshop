// Types for the public API, section 3 of the specification.

export type Role = "customer" | "support" | "operations" | "compliance";

export interface Person {
  id: string;
  role: Role;
  name: string;
  email: string;
  note: string;
  account_id?: string;
}

export interface AppConfig {
  service_name: string;
  version: string;
  otlp_path: string;
  environment: string;
  honeycomb_trace_url: string | null;
}

export interface Account {
  id: string;
  cash_balance: number;
  wallet_balance: number;
  invested_value: number;
  total_value: number;
}

export interface AllocationItem {
  asset_class: string;
  percent: number;
  value: number;
}

export interface Summary {
  customer: Person;
  account: Account;
  allocation: AllocationItem[];
}

export interface Holding {
  asset_id: string;
  symbol: string;
  name: string;
  asset_class: string;
  quantity: number;
  price: number;
  market_value: number;
}

export interface Instrument {
  symbol: string;
  name: string;
  asset_class: string;
  price: number;
}

export type TradeStatus = "pending" | "queued" | "filled" | "partially_filled" | "cancelled" | "rejected";

export interface Trade {
  id: string;
  side: "buy" | "sell";
  symbol: string;
  quantity: number;
  order_type: "market" | "limit";
  status: TradeStatus;
  filled_quantity: number;
  avg_price: number | null;
  ticket_id: string | null;
  reject_reason: string | null;
  created_at: string;
  updated_at: string;
}

export type TransferStatus = "proposed" | "queued" | "held" | "completed" | "failed" | "returned";

export interface Transfer {
  id: string;
  direction: "in" | "out";
  amount: number;
  counterparty: "bank" | "card_account";
  status: TransferStatus;
  ticket_id: string | null;
  reason: string | null;
  created_at: string;
  updated_at: string;
}

export type TicketState = "queued" | "processing" | "retrying" | "completed" | "failed" | "dead_lettered" | "held";

export interface TicketNote {
  author: string;
  text: string;
  created_at: string;
}

export interface Ticket {
  id: string;
  kind: "trade" | "transfer" | "rebalance";
  state: TicketState;
  attempt: number;
  queue: string;
  customer_id: string;
  ref_id: string;
  reason: string | null;
  queue_position: number | null;
  eta_seconds: number | null;
  notes: TicketNote[];
  created_at: string;
  updated_at: string;
}

export interface Notification {
  id: number;
  kind: string;
  text: string;
  created_at: string;
}

export type Card =
  | { type: "allocation"; items: AllocationItem[] }
  | { type: "trades"; items: { id: string; side: "buy" | "sell"; symbol: string; quantity: number; status: string }[] }
  | { type: "transfer_form" }
  | { type: "confirm"; kind: "transfer" | "trade" | "rebalance"; title: string; lines: string[] }
  | { type: "rebalance_plan"; moves: { asset_class: string; from: number; to: number }[] }
  | { type: "ticket"; ticket_id: string; kind: string; state: string }
  | { type: "chips"; options: string[] }
  | { type: "valuations"; count: number; total: number }
  | { type: string; [key: string]: unknown }; // unknown card types are ignored

export type Outcome = "ok" | "tool_timeout_recovered" | "context_overflow" | "agent_bug" | "error";

export interface ToolCall {
  agent: string;
  tool: string;
  arguments: Record<string, unknown>;
  result: string;
  error: string | null;
}

export interface Message {
  id: number;
  role: "user" | "assistant" | "support";
  text: string;
  cards: Card[];
  outcome: Outcome | null;
  trace_id: string | null;
  feedback: "up" | "down" | null;
  author: string | null;
  created_at: string;
  tool_calls?: ToolCall[];
}

export interface ConversationSummary {
  id: string;
  title: string;
  updated_at: string;
  message_count: number;
}

export interface Conversation {
  id: string;
  customer_id: string;
  messages: Message[];
}

export interface ConversationRow {
  id: string;
  customer_id: string;
  customer_name: string;
  title: string;
  last_outcome: Outcome | null;
  has_negative_feedback: boolean;
  message_count: number;
  updated_at: string;
}

export interface SupportConversation {
  id: string;
  customer_id: string;
  customer_name: string;
  messages: Message[];
}

export interface CustomerDetail {
  customer: Person;
  account: Account;
  allocation: AllocationItem[];
  trades: Trade[];
  transfers: Transfer[];
  tickets: Ticket[];
  conversations: ConversationRow[];
}

export interface Service {
  name: string;
  kind: "backend" | "agent" | "backoffice" | "edge";
  healthy: boolean;
  latency_ms: number;
}

export interface Queue {
  queue: string;
  ready: number;
  inflight: number;
  retry: number;
  dlq: number;
  oldest_ready_age_s: number;
  consumer: string;
  processed_5m: number;
  failed_5m: number;
}

export type QueueState = "ready" | "inflight" | "retry" | "dlq";

export interface QueueMessage {
  id: number;
  queue: string;
  state: QueueState;
  attempt: number;
  ticket_id: string | null;
  customer_id: string | null;
  last_error: string | null;
  enqueued_at: string;
  summary: string;
}

export interface Story {
  name: string;
  title: string;
  description: string;
  kind: "agent" | "backoffice";
  active: boolean;
  expires_at: string | null;
  faults: string[];
}

export interface Fault {
  name: string;
  description: string;
  active: boolean;
  params: Record<string, unknown> | null;
  story: string | null;
  expires_at: string | null;
}

export interface Loadgen {
  enabled: boolean;
  conversations_per_minute: number;
  transactions_per_minute: number;
  auto_stories: boolean;
  stats: {
    conversations: number;
    turns: number;
    transactions: number;
    errors: number;
    last_seen_at: string | null;
  };
}

export interface Run {
  id: number;
  kind: "settlement" | "sweep" | "pricing" | "payroll";
  status: "running" | "completed" | "failed";
  detail: string;
  started_at: string;
  ended_at: string | null;
  started_by: string;
}

export interface Hold {
  transfer: Transfer;
  customer: Person;
  ticket_id: string;
  risk_score: number;
  hold_reason: string;
}

export interface Restricted {
  symbol: string;
  reason: string;
  added_by: string;
  created_at: string;
}
