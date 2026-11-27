import { useState } from "react";
import { api } from "../../api";
import { Async, Badge, Empty, ErrorBox, PageHead, Panel, Seg } from "../../components/ui";
import { duration, num, when } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";
import type { Queue, QueueState } from "../../types";

const STATES: { id: QueueState; label: string }[] = [
  { id: "ready", label: "Ready" },
  { id: "inflight", label: "In flight" },
  { id: "retry", label: "Retry" },
  { id: "dlq", label: "Dead letters" },
];

function Messages({ queue, initial }: { queue: Queue; initial: QueueState }) {
  const [state, setState] = useState<QueueState>(initial);
  const msgs = useApi(() => api.opsQueueMessages(queue.queue, state, 50), [queue.queue, state], POLL);
  const replay = useAction(api.opsReplay);
  const [done, setDone] = useState<string | null>(null);

  const run = async (b: { message_ids: number[] } | { all: true }) => {
    setDone(null);
    const r = await replay.run(queue.queue, b);
    if (r) {
      setDone(`Replayed ${r.replayed} message${r.replayed === 1 ? "" : "s"}.`);
      msgs.reload();
    }
  };

  return (
    <Panel
      title={`${queue.queue}: messages`}
      actions={
        <>
          <Seg name="Message state" value={state} onChange={setState} options={STATES} />
          {state === "dlq" && (
            <button className="btn btn-sm btn-primary" disabled={replay.busy || queue.dlq === 0} onClick={() => run({ all: true })}>
              Replay all dead letters
            </button>
          )}
        </>
      }
    >
      <ErrorBox error={replay.error} />
      {done && <p className="form-ok" role="status">{done}</p>}
      <Async state={msgs}>
        {(rows) =>
          rows.length === 0 ? (
            <Empty>No {STATES.find((s) => s.id === state)?.label.toLowerCase()} messages.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Summary</th>
                    <th className="num">Attempt</th>
                    <th>Ticket</th>
                    <th>Customer</th>
                    <th>Last error</th>
                    <th>Enqueued</th>
                    {state === "dlq" && <th></th>}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((m) => (
                    <tr key={m.id}>
                      <td className="mono">{m.id}</td>
                      <td>{m.summary}</td>
                      <td className="num">{m.attempt}</td>
                      <td className="mono nowrap">{m.ticket_id ?? "–"}</td>
                      <td className="mono nowrap">{m.customer_id ?? "–"}</td>
                      <td className="break small" style={{ maxWidth: 280 }}>{m.last_error ?? "–"}</td>
                      <td className="nowrap muted">{when(m.enqueued_at)}</td>
                      {state === "dlq" && (
                        <td>
                          <button className="btn btn-sm" disabled={replay.busy} onClick={() => run({ message_ids: [m.id] })}>Replay</button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Async>
    </Panel>
  );
}

export default function Overview() {
  const services = useApi(() => api.opsServices(), [], POLL);
  const queues = useApi(() => api.opsQueues(), [], POLL);
  const [selected, setSelected] = useState("orders");
  const [initial, setInitial] = useState<QueueState>("ready");
  const sel = queues.data?.find((q) => q.queue === selected) ?? queues.data?.[0];

  return (
    <main className="page">
      <PageHead title="Health and queues">
        <span className="muted small">Refreshes every 5 s</span>
      </PageHead>
      <div className="stack">
        <Panel
          title="Services"
          actions={services.data && <Badge value={services.data.every((s) => s.healthy) ? "ok" : "error"} text={`${services.data.filter((s) => s.healthy).length}/${services.data.length} healthy`} />}
        >
          <Async state={services}>
            {(rows) => (
              <div className="svc-grid">
                {[...rows].sort((a, b) => Number(a.healthy) - Number(b.healthy) || a.name.localeCompare(b.name)).map((s) => (
                  <div key={s.name} className={`svc ${s.healthy ? "" : "svc-bad"}`}>
                    <span className={`dot ${s.healthy ? "dot-ok" : "dot-bad"}`} aria-label={s.healthy ? "healthy" : "unhealthy"} />
                    <span className="svc-text">
                      <span className="svc-name" title={s.name}>{s.name}</span>
                      <span className="muted small">{s.kind}</span>
                    </span>
                    <span className="muted small nowrap">{s.healthy ? `${s.latency_ms} ms` : "down"}</span>
                  </div>
                ))}
              </div>
            )}
          </Async>
        </Panel>

        <Panel title="Queues">
          <Async state={queues}>
            {(rows) => (
              <div className="grid grid-3">
                {rows.map((q) => (
                  <button
                    key={q.queue}
                    className="panel queue-card"
                    aria-pressed={sel?.queue === q.queue}
                    onClick={() => {
                      setSelected(q.queue);
                      setInitial("ready");
                    }}
                  >
                    <div className="row">
                      <b>{q.queue}</b>
                      <span className="muted small">→ {q.consumer}</span>
                    </div>
                    <div className="counts">
                      <div><b>{num(q.ready)}</b><span>Ready</span></div>
                      <div><b>{num(q.inflight)}</b><span>In flight</span></div>
                      <div><b>{num(q.retry)}</b><span>Retry</span></div>
                      <div className={q.dlq > 0 ? "dlq-hot" : ""}><b>{num(q.dlq)}</b><span>Dead</span></div>
                    </div>
                    <div className="muted small">
                      Oldest ready: {q.ready ? duration(q.oldest_ready_age_s) : "–"} · 5 min: {q.processed_5m} done, {q.failed_5m} failed
                    </div>
                  </button>
                ))}
              </div>
            )}
          </Async>
        </Panel>

        {sel && (
          <>
            <div className="row">
              {sel.dlq > 0 && (
                <button className="btn btn-sm btn-danger" onClick={() => setInitial("dlq")}>
                  Show {sel.dlq} dead letter{sel.dlq === 1 ? "" : "s"}
                </button>
              )}
            </div>
            <Messages key={`${sel.queue}-${initial}`} queue={sel} initial={initial} />
          </>
        )}
      </div>
    </main>
  );
}
