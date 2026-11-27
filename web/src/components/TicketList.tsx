import { Fragment, type ReactNode } from "react";
import { duration, when } from "../format";
import type { Ticket } from "../types";
import { Badge, Empty } from "./ui";

/** Ticket table shared by customer, support and operations pages. */
export function TicketList({
  tickets,
  showCustomer = true,
  renderNotes,
}: {
  tickets: Ticket[];
  showCustomer?: boolean;
  renderNotes?: (t: Ticket) => ReactNode;
}) {
  if (tickets.length === 0) return <Empty>No tickets.</Empty>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Ticket</th>
            <th className="hide-sm">Kind</th>
            <th>State</th>
            {showCustomer && <th className="hide-sm">Customer</th>}
            <th className="hide-sm">Reference</th>
            <th className="hide-sm">Queue</th>
            <th className="hide-sm">Updated</th>
          </tr>
        </thead>
        <tbody>
          {tickets.map((t) => (
            <Fragment key={t.id}>
              <tr>
                <td className="mono nowrap">
                  {t.id}
                  <div className="small muted only-sm" style={{ textTransform: "capitalize" }}>{t.kind} · {t.ref_id}</div>
                </td>
                <td className="hide-sm" style={{ textTransform: "capitalize" }}>{t.kind}</td>
                <td>
                  <Badge value={t.state} />
                  {t.attempt > 1 && <span className="muted small"> attempt {t.attempt}</span>}
                  {t.reason && <div className="small muted break">{t.reason}</div>}
                  {t.queue_position != null && (
                    <div className="small muted">
                      #{t.queue_position} in line{t.eta_seconds != null ? `, about ${duration(t.eta_seconds)}` : ""}
                    </div>
                  )}
                </td>
                {showCustomer && <td className="mono nowrap hide-sm">{t.customer_id}</td>}
                <td className="mono nowrap hide-sm">{t.ref_id}</td>
                <td className="nowrap hide-sm">{t.queue}</td>
                <td className="nowrap muted hide-sm">{when(t.updated_at)}</td>
              </tr>
              {(t.notes.length > 0 || renderNotes) && (
                <tr>
                  <td colSpan={showCustomer ? 7 : 6} style={{ background: "var(--surface-2)" }}>
                    {t.notes.map((n, i) => (
                      <p key={i} className="small break">
                        <b>{n.author}</b> <span className="muted">{when(n.created_at)}</span>: {n.text}
                      </p>
                    ))}
                    {renderNotes?.(t)}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}
