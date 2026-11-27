// Typed client for the public API (specification section 3). Paths and
// fields are exactly the ones in the spec.
import type * as T from "./types";

const TOKEN_KEY = "fakeco.token";
const USER_KEY = "fakeco.user";

/** The token lives in sessionStorage so two browser tabs can be two people. */
export const session = {
  get token() {
    return sessionStorage.getItem(TOKEN_KEY);
  },
  get user(): T.Person | null {
    try {
      return JSON.parse(sessionStorage.getItem(USER_KEY) ?? "null");
    } catch {
      return null;
    }
  },
  set(token: string, user: T.Person) {
    sessionStorage.setItem(TOKEN_KEY, token);
    sessionStorage.setItem(USER_KEY, JSON.stringify(user));
  },
  clear() {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem(USER_KEY);
  },
};

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

let onUnauthorized: () => void = () => {};
/** Called when any call comes back 401 (sign in again). */
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

async function call<R>(method: string, path: string, body?: unknown): Promise<R> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const token = session.token;
  if (token) headers.Authorization = `Bearer ${token}`;
  let res: Response;
  try {
    res = await fetch(`/api${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network", "Could not reach the server.");
  }
  if (!res.ok) {
    let code = "error";
    let message = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      code = data?.error?.code ?? code;
      message = data?.error?.message ?? message;
    } catch {
      /* not JSON (for example a proxy error page) */
    }
    if (res.status === 401 && code === "gate_required") {
      // the demo gate's sign-in ran out (it is not the app's own sign-in): go back to its form and then here again
      window.location.assign(`/gate/login?next=${encodeURIComponent(window.location.pathname)}`);
    } else if (res.status === 401 && token) onUnauthorized();
    throw new ApiError(res.status, code, message);
  }
  return (await res.json()) as R;
}

const get = <R>(path: string) => call<R>("GET", path);
const post = <R>(path: string, body?: unknown) => call<R>("POST", path, body ?? {});
const put = <R>(path: string, body: unknown) => call<R>("PUT", path, body);
const del = <R>(path: string) => call<R>("DELETE", path);

export function qs(params: Record<string, string | number | boolean | undefined | null>) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "" && v !== "all") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const api = {
  // 3.1 Session
  config: () => get<T.AppConfig>("/config"),
  personas: () => get<{ customers: T.Person[]; staff: T.Person[] }>("/personas"),
  login: (user_id: string) => post<{ token: string; user: T.Person }>("/login", { user_id }),
  me: () => get<T.Person>("/me"),

  // 3.2 Customer
  summary: () => get<T.Summary>("/summary"),
  holdings: () => get<T.Holding[]>("/holdings"),
  instruments: () => get<T.Instrument[]>("/instruments"),
  trades: (status = "all") => get<T.Trade[]>(`/trades${qs({ status })}`),
  createTrade: (b: { side: "buy" | "sell"; symbol: string; quantity: number; mode: "now" | "pending" }) =>
    post<{ trade: T.Trade; ticket_id: string | null }>("/trades", b),
  submitTrade: (id: string) => post<{ trade: T.Trade; ticket_id: string }>(`/trades/${id}/submit`),
  cancelTrade: (id: string) => post<{ trade: T.Trade }>(`/trades/${id}/cancel`),
  transfers: () => get<T.Transfer[]>("/transfers"),
  createTransfer: (b: { direction: "in" | "out"; amount: number; counterparty: "bank" | "card_account" }) =>
    post<{ transfer: T.Transfer; ticket_id: string }>("/transfers", b),
  tickets: () => get<T.Ticket[]>("/tickets"),
  ticket: (id: string) => get<T.Ticket>(`/tickets/${id}`),
  notifications: () => get<T.Notification[]>("/notifications"),

  // 3.3 Chat
  conversations: () => get<T.ConversationSummary[]>("/conversations"),
  conversation: (id: string) => get<T.Conversation>(`/conversations/${id}`),
  chat: (b: { conversation_id: string | null; message: string; chip: boolean }) =>
    post<{ conversation_id: string; message: T.Message }>("/chat", b),
  feedback: (messageId: number, rating: "up" | "down") =>
    post<{ ok: boolean }>(`/messages/${messageId}/feedback`, { rating }),

  // 3.4 Support
  supportCustomers: (q = "") => get<T.Person[]>(`/support/customers${qs({ q })}`),
  supportCustomer: (id: string) => get<T.CustomerDetail>(`/support/customers/${id}`),
  supportConversations: (f: { customer_id?: string; outcome?: string; feedback?: string; limit?: number }) =>
    get<T.ConversationRow[]>(`/support/conversations${qs(f)}`),
  supportConversation: (id: string) => get<T.SupportConversation>(`/support/conversations/${id}`),
  supportReply: (id: string, text: string) =>
    post<{ message: T.Message }>(`/support/conversations/${id}/reply`, { text }),
  supportTickets: (f: { state?: string; customer_id?: string; limit?: number }) =>
    get<T.Ticket[]>(`/support/tickets${qs(f)}`),
  addTicketNote: (id: string, text: string) => post<T.Ticket>(`/support/tickets/${id}/notes`, { text }),

  // 3.5 Operations
  opsServices: () => get<T.Service[]>("/ops/services"),
  opsQueues: () => get<T.Queue[]>("/ops/queues"),
  opsQueueMessages: (queue: string, state: T.QueueState, limit = 50) =>
    get<T.QueueMessage[]>(`/ops/queues/${queue}/messages${qs({ state, limit })}`),
  opsReplay: (queue: string, b: { message_ids: number[] } | { all: true }) =>
    post<{ replayed: number }>(`/ops/queues/${queue}/replay`, b),
  opsStories: () => get<T.Story[]>("/ops/stories"),
  opsStartStory: (name: string, minutes: number) => post<T.Story>(`/ops/stories/${name}/start`, { minutes }),
  opsStopStory: (name: string) => post<T.Story>(`/ops/stories/${name}/stop`),
  opsStopAll: () => post<{ stopped: number }>("/ops/stories/stop-all"),
  opsFaults: () => get<T.Fault[]>("/ops/faults"),
  opsSetFault: (name: string, b: { active: boolean; params: Record<string, unknown> | null; minutes: number | null }) =>
    put<T.Fault>(`/ops/faults/${name}`, b),
  opsLoadgen: () => get<T.Loadgen>("/ops/loadgen"),
  opsSetLoadgen: (b: Partial<Pick<T.Loadgen, "enabled" | "conversations_per_minute" | "transactions_per_minute" | "auto_stories">>) =>
    put<T.Loadgen>("/ops/loadgen", b),
  opsRuns: () => get<T.Run[]>("/ops/runs"),
  opsStartRun: (kind: T.Run["kind"]) => post<T.Run>("/ops/runs", { kind }),
  opsTickets: (f: { state?: string; limit?: number }) => get<T.Ticket[]>(`/ops/tickets${qs(f)}`),

  // 3.6 Compliance
  holds: () => get<T.Hold[]>("/compliance/holds"),
  decideHold: (transferId: string, decision: "approve" | "reject", reason: string) =>
    post<{ transfer: T.Transfer }>(`/compliance/holds/${transferId}/decision`, { decision, reason }),
  restricted: () => get<T.Restricted[]>("/compliance/restricted"),
  addRestricted: (symbol: string, reason: string) => post<T.Restricted[]>("/compliance/restricted", { symbol, reason }),
  removeRestricted: (symbol: string) => del<T.Restricted[]>(`/compliance/restricted/${encodeURIComponent(symbol)}`),
};
