import { useState } from "react";
import { api } from "../../api";
import { TicketList } from "../../components/TicketList";
import { Async, PageHead, Panel } from "../../components/ui";
import { POLL, useApi } from "../../hooks";
import { TICKET_STATES } from "../support/Tickets";

export default function OpsTickets() {
  const [state, setState] = useState("");
  const tickets = useApi(() => api.opsTickets({ state: state || undefined, limit: 100 }), [state], POLL);
  const counts = new Map<string, number>();
  for (const t of tickets.data ?? []) counts.set(t.state, (counts.get(t.state) ?? 0) + 1);
  return (
    <main className="page">
      <PageHead title="Tickets by state">
        <span className="muted small">Refreshes every 5 s</span>
      </PageHead>
      <Panel>
        <div className="chips" style={{ marginBottom: 12 }}>
          <button className="chip" aria-pressed={state === ""} style={state === "" ? { background: "var(--accent)", color: "var(--accent-text)" } : undefined} onClick={() => setState("")}>All</button>
          {TICKET_STATES.map((s) => (
            <button key={s} className="chip" aria-pressed={state === s} style={state === s ? { background: "var(--accent)", color: "var(--accent-text)" } : undefined} onClick={() => setState(s)}>
              {s.replace(/_/g, " ")}
              {state === "" && counts.get(s) ? ` · ${counts.get(s)}` : ""}
            </button>
          ))}
        </div>
        <Async state={tickets}>{(rows) => <TicketList tickets={rows} />}</Async>
      </Panel>
    </main>
  );
}
