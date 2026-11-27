import { api } from "../../api";
import { TicketList } from "../../components/TicketList";
import { PageHead, Panel } from "../../components/ui";
import { FINAL_TICKET_STATES, duration } from "../../format";
import { POLL, useApi } from "../../hooks";
import { Async } from "../../components/ui";
import type { Ticket } from "../../types";

const open = (t: Ticket) => !FINAL_TICKET_STATES.includes(t.state);

export default function Tickets() {
  // Refresh every 5 s while any ticket is not in a final state.
  const tickets = useApi(() => api.tickets(), [], (d) => (!d || d.some(open) ? POLL : 0));
  return (
    <main className="page">
      <PageHead title="Tickets">
        {tickets.data?.some(open) && <span className="muted small">Refreshing every {duration(POLL / 1000)} while a ticket is open</span>}
      </PageHead>
      <Panel>
        <Async state={tickets}>{(rows) => <TicketList tickets={rows} showCustomer={false} />}</Async>
      </Panel>
    </main>
  );
}
