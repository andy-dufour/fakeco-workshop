import { useState } from "react";
import { api } from "../../api";
import { TicketList } from "../../components/TicketList";
import { TicketNoteForm } from "../../components/TicketNoteForm";
import { Async, PageHead, Panel } from "../../components/ui";
import { POLL, useApi } from "../../hooks";

export const TICKET_STATES = ["queued", "processing", "retrying", "completed", "failed", "dead_lettered", "held"];

export default function SupportTickets() {
  const [state, setState] = useState("");
  const [customer, setCustomer] = useState("");
  const tickets = useApi(() => api.supportTickets({ state: state || undefined, customer_id: customer.trim() || undefined, limit: 100 }), [state, customer], POLL);
  return (
    <main className="page">
      <PageHead title="Tickets" />
      <Panel>
        <div className="form-grid" style={{ marginBottom: 12 }}>
          <div className="field">
            <label htmlFor="ts">State</label>
            <select id="ts" value={state} onChange={(e) => setState(e.target.value)}>
              <option value="">Any state</option>
              {TICKET_STATES.map((s) => (
                <option key={s} value={s}>{s.replace(/_/g, " ")}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="tc">Customer ID</label>
            <input id="tc" value={customer} onChange={(e) => setCustomer(e.target.value)} placeholder="CUS-0001" />
          </div>
        </div>
        <Async state={tickets}>
          {(rows) => <TicketList tickets={rows} renderNotes={(t) => <TicketNoteForm ticketId={t.id} onAdded={tickets.reload} />} />}
        </Async>
      </Panel>
    </main>
  );
}
