import { useState } from "react";
import { Link, useParams } from "react-router";
import { api } from "../../api";
import { useAuth } from "../../auth";
import { CardView } from "../../components/Cards";
import { MessageText } from "../../components/MessageText";
import { Async, Badge, ErrorBox, PageHead, Panel } from "../../components/ui";
import { when } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";
import type { Message } from "../../types";

const noop = () => {};
const readOnlyActions = { send: noop, fill: noop, disabled: true, confirmLive: false };

function ToolCalls({ m }: { m: Message }) {
  const calls = m.tool_calls ?? [];
  if (calls.length === 0) return null;
  return (
    <details className="details">
      <summary>{calls.length} tool call{calls.length === 1 ? "" : "s"}</summary>
      <div className="details-body">
        {calls.map((c, i) => (
          <div className="tool" key={i}>
            <div className="tool-name">
              <span className="mono">{c.tool}</span>
              <span className="muted">{c.agent}</span>
              {c.error && <Badge value="error" text="error" />}
            </div>
            <div className="muted small">Arguments</div>
            <pre>{JSON.stringify(c.arguments, null, 2)}</pre>
            {c.error ? (
              <>
                <div className="small" style={{ color: "var(--bad)" }}>Error</div>
                <pre>{c.error}</pre>
              </>
            ) : null}
            <div className="muted small">Result</div>
            <pre>{c.result}</pre>
          </div>
        ))}
      </div>
    </details>
  );
}

export default function Transcript() {
  const { id = "" } = useParams();
  const { config } = useAuth();
  const conv = useApi(() => api.supportConversation(id), [id], POLL);
  const [text, setText] = useState("");
  const reply = useAction(api.supportReply);

  const send = async () => {
    if (!text.trim()) return;
    if (await reply.run(id, text.trim())) {
      setText("");
      conv.reload();
    }
  };

  return (
    <main className="page">
      <Async state={conv}>
        {(c) => (
          <>
            <PageHead title={`Conversation with ${c.customer_name}`}>
              <Link className="btn btn-sm" to={`/support/customers/${c.customer_id}`}>{c.customer_id}</Link>
              <Link className="btn btn-sm" to="/support/conversations">← Conversations</Link>
            </PageHead>
            <div className="stack">
              <Panel>
                <div className="transcript">
                  {c.messages.map((m) => (
                    <div key={m.id} className={`msg msg-${m.role}`}>
                      <div className={`bubble bubble-${m.role}`}>
                        {m.role === "support" && <div className="bubble-author">Support{m.author ? ` · ${m.author}` : ""}</div>}
                        <MessageText text={m.text} />
                      </div>
                      {m.cards.map((card, i) => (
                        <CardView key={i} card={card} actions={readOnlyActions} readOnly />
                      ))}
                      <div className="msg-meta">
                        <span>{m.role === "user" ? "Customer" : m.role === "assistant" ? "Concierge" : "Support"} · {when(m.created_at)}</span>
                        {m.outcome && m.outcome !== "ok" && <Badge value={m.outcome} />}
                        {m.role === "assistant" && m.outcome === "ok" && <Badge value="ok" text="ok" />}
                        {m.feedback && <Badge value={m.feedback === "down" ? "error" : "ok"} text={m.feedback === "down" ? "👎 thumbs down" : "👍 thumbs up"} />}
                        {config?.honeycomb_trace_url && m.trace_id && (
                          <a href={config.honeycomb_trace_url.replace("{trace_id}", m.trace_id)} target="_blank" rel="noreferrer">View trace</a>
                        )}
                      </div>
                      {m.role === "assistant" && <ToolCalls m={m} />}
                    </div>
                  ))}
                </div>
              </Panel>
              <Panel title="Reply to the customer">
                <form
                  className="stack-sm"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void send();
                  }}
                >
                  <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="The customer sees this in their chat as a message from support." aria-label="Reply" />
                  <div className="row">
                    <button className="btn btn-primary" disabled={reply.busy || !text.trim()}>Send reply</button>
                  </div>
                  <ErrorBox error={reply.error} />
                </form>
              </Panel>
            </div>
          </>
        )}
      </Async>
    </main>
  );
}
