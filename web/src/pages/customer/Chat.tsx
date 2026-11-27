import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api";
import { useAuth } from "../../auth";
import { CardView, type CardActions } from "../../components/Cards";
import { MessageText } from "../../components/MessageText";
import { ErrorBox, ThumbDown, ThumbUp } from "../../components/ui";
import { when } from "../../format";
import { POLL, useApi } from "../../hooks";
import type { Message } from "../../types";

const STARTERS: { label: string; text: string }[] = [
  { label: "Show my portfolio", text: "Show my portfolio" },
  { label: "Show my pending trades", text: "Show my pending trades" },
  { label: "Transfer money", text: "Transfer money" },
  { label: "Rebalance my portfolio", text: "Rebalance my portfolio" },
  { label: "Value every holding", text: "What is each of my investments worth right now?" },
];

type Phase = { kind: "idle" } | { kind: "waiting"; since: number } | { kind: "failed"; error: string };

function Waiting({ since }: { since: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const s = Math.floor((now - since) / 1000);
  return (
    <div className="msg msg-assistant" role="status" aria-live="polite">
      <div className="bubble bubble-assistant typing">
        <span className="dots" aria-hidden="true"><i /><i /><i /></span>
        <span>
          {s < 8 ? "The Concierge is thinking…" : `Still working… ${s} s. Hard questions can take up to a minute.`}
        </span>
      </div>
    </div>
  );
}

export default function Chat() {
  const { config } = useAuth();
  const conversations = useApi(() => api.conversations(), []);
  const [convId, setConvId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [pendingUser, setPendingUser] = useState<{ text: string; chip: boolean } | null>(null);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [view, setView] = useState<"list" | "chat">("chat");
  const [draft, setDraft] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const waiting = phase.kind === "waiting";
  // Guards against a slow reply landing in a conversation the user has left.
  const epoch = useRef(0);

  const open = useCallback(
    async (id: string | null) => {
      epoch.current++;
      setConvId(id);
      setPendingUser(null);
      setPhase({ kind: "idle" });
      setLoadError(null);
      setView("chat");
      if (!id) {
        setMessages([]);
        return;
      }
      try {
        const c = await api.conversation(id);
        setMessages(c.messages);
      } catch (e) {
        setLoadError(e instanceof Error ? e.message : String(e));
      }
    },
    [],
  );

  // Support replies arrive by polling the open conversation.
  useEffect(() => {
    if (!convId || phase.kind !== "idle") return;
    const myEpoch = epoch.current;
    const t = setInterval(async () => {
      try {
        const c = await api.conversation(convId);
        if (epoch.current !== myEpoch) return;
        setMessages((old) => (old.length === c.messages.length && old.every((m, i) => m.id === c.messages[i].id && m.feedback === c.messages[i].feedback) ? old : c.messages));
      } catch {
        /* try again on the next tick */
      }
    }, POLL);
    return () => clearInterval(t);
  }, [convId, phase.kind]);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, pendingUser, phase.kind, view]);

  const send = useCallback(
    async (text: string, chip: boolean) => {
      const body = text.trim();
      if (!body || waiting) return;
      const myEpoch = epoch.current;
      setPendingUser({ text: body, chip });
      setPhase({ kind: "waiting", since: Date.now() });
      setDraft("");
      try {
        const res = await api.chat({ conversation_id: convId, message: body, chip });
        if (epoch.current !== myEpoch) return;
        setConvId(res.conversation_id);
        // The user's message is now on the server; show it with the reply.
        try {
          const c = await api.conversation(res.conversation_id);
          if (epoch.current !== myEpoch) return;
          setMessages(c.messages.some((m) => m.id === res.message.id) ? c.messages : [...c.messages, res.message]);
        } catch {
          setMessages((old) => [
            ...old,
            { id: -Date.now(), role: "user", text: body, cards: [], outcome: null, trace_id: null, feedback: null, author: null, created_at: new Date().toISOString() },
            res.message,
          ]);
        }
        setPendingUser(null);
        setPhase({ kind: "idle" });
        conversations.reload();
      } catch (e) {
        if (epoch.current !== myEpoch) return;
        setPhase({ kind: "failed", error: e instanceof Error ? e.message : String(e) });
      }
    },
    [convId, waiting, conversations],
  );

  const rate = async (m: Message, rating: "up" | "down") => {
    const before = m.feedback;
    setMessages((old) => old.map((x) => (x.id === m.id ? { ...x, feedback: rating } : x)));
    try {
      await api.feedback(m.id, rating);
    } catch {
      setMessages((old) => old.map((x) => (x.id === m.id ? { ...x, feedback: before } : x)));
    }
  };

  // The newest confirm card is live until the user answers (any later user message).
  const liveConfirm = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === "user") return null;
      if (messages[i].cards.some((c) => c.type === "confirm")) return messages[i].id;
    }
    return null;
  }, [messages]);
  const answeredPending = pendingUser !== null;

  const actions = (m: Message): CardActions => ({
    send: (t) => void send(t, true),
    fill: (t) => {
      setDraft(t + " ");
      input.current?.focus();
    },
    disabled: waiting,
    confirmLive: liveConfirm === m.id && !answeredPending,
  });

  const empty = messages.length === 0 && !pendingUser;
  const title = conversations.data?.find((c) => c.id === convId)?.title ?? "New conversation";

  return (
    <main className="page chat-page">
      <div className="chat-layout" data-view={view}>
        <aside className="chat-side" aria-label="Conversations">
          <div className="chat-side-head">
            <h2>Conversations</h2>
            <button className="btn btn-sm btn-primary" onClick={() => void open(null)}>New chat</button>
          </div>
          <ul className="conv-list">
            {conversations.data?.length === 0 && <li className="empty">No conversations yet.</li>}
            {conversations.data?.map((c) => (
              <li key={c.id}>
                <button className="conv-item" aria-current={c.id === convId} onClick={() => void open(c.id)}>
                  <span className="conv-title">{c.title}</span>
                  <span className="muted small">{c.message_count} messages · {when(c.updated_at)}</span>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <section className="chat-main" aria-label="Concierge chat">
          <div className="chat-toolbar">
            <button className="btn btn-sm" onClick={() => setView("list")}>☰ Chats</button>
            <span className="conv-title">{title}</span>
            <button className="btn btn-sm" onClick={() => void open(null)}>New</button>
          </div>
          {convId && (
            <div className="chat-idbar">
              <span className="muted small">Conversation ID</span>
              <code data-testid="conversation-id">{convId}</code>
              <button
                className="btn btn-sm"
                onClick={() => {
                  void navigator.clipboard?.writeText(convId).then(() => {
                    setCopied(true);
                    setTimeout(() => setCopied(false), 1500);
                  }, () => {});
                }}
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
          )}
          <div className="chat-scroll" ref={scroller} aria-live="off">
            {loadError && <ErrorBox error={loadError} />}
            {empty && (
              <div className="chat-welcome">
                <h2>Hi, I’m your FakeCo Concierge</h2>
                <p className="muted">Ask about your portfolio, trades and transfers, or start with one of these.</p>
                <div className="chips">
                  {STARTERS.map((s) => (
                    <button key={s.label} className="chip" disabled={waiting} onClick={() => void send(s.text, true)}>
                      {s.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((m) => (
              <div key={m.id} className={`msg msg-${m.role}`}>
                {m.text && (
                  <div className={`bubble bubble-${m.role}`}>
                    {m.role === "support" && <div className="bubble-author">Support{m.author ? ` · ${m.author}` : ""}</div>}
                    <MessageText text={m.text} />
                  </div>
                )}
                {m.cards.map((c, i) => (
                  <CardView key={i} card={c} actions={actions(m)} />
                ))}
                {m.role === "assistant" && m.id > 0 && (
                  <div className="msg-meta">
                    <button className="thumb" data-rating="up" aria-pressed={m.feedback === "up"} aria-label="Good answer" onClick={() => void rate(m, "up")}>
                      <ThumbUp />
                    </button>
                    <button className="thumb" data-rating="down" aria-pressed={m.feedback === "down"} aria-label="Bad answer" onClick={() => void rate(m, "down")}>
                      <ThumbDown />
                    </button>
                    {config?.honeycomb_trace_url && m.trace_id && (
                      <a href={config.honeycomb_trace_url.replace("{trace_id}", m.trace_id)} target="_blank" rel="noreferrer">
                        View trace
                      </a>
                    )}
                  </div>
                )}
              </div>
            ))}
            {pendingUser && (
              <div className="msg msg-user">
                <div className="bubble bubble-user"><MessageText text={pendingUser.text} /></div>
              </div>
            )}
            {phase.kind === "waiting" && <Waiting since={phase.since} />}
            {phase.kind === "failed" && pendingUser && (
              <div className="msg msg-assistant" role="alert">
                <div className="bubble bubble-error">
                  <p><b>The Concierge could not answer.</b></p>
                  <p className="small">{phase.error}</p>
                  <div style={{ marginTop: 8 }}>
                    <button className="btn btn-sm" onClick={() => void send(pendingUser.text, pendingUser.chip)}>Try again</button>
                  </div>
                </div>
              </div>
            )}
          </div>
          <form
            className="composer"
            onSubmit={(e) => {
              e.preventDefault();
              void send(draft, false);
            }}
          >
            <textarea
              ref={input}
              rows={1}
              value={draft}
              placeholder="Message the Concierge…"
              aria-label="Message"
              disabled={waiting}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void send(draft, false);
                }
              }}
            />
            <button className="btn btn-primary" type="submit" disabled={waiting || !draft.trim()}>
              Send
            </button>
          </form>
        </section>
      </div>
    </main>
  );
}
