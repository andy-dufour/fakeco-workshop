import { Link, useParams } from "react-router";
import { api } from "../../api";
import { Async, Badge, Donut, Empty, PageHead, Panel, Stat } from "../../components/ui";
import { TicketList } from "../../components/TicketList";
import { TicketNoteForm } from "../../components/TicketNoteForm";
import { num, usd, when } from "../../format";
import { POLL, useApi } from "../../hooks";

export default function Customer() {
  const { id = "" } = useParams();
  const detail = useApi(() => api.supportCustomer(id), [id], POLL);
  return (
    <main className="page">
      <Async state={detail}>
        {(d) => (
          <>
            <PageHead title={d.customer.name}>
              <span className="mono muted">{d.customer.id}</span>
              <Link className="btn btn-sm" to="/support">← All customers</Link>
            </PageHead>
            <div className="stack">
              <div className="grid grid-main">
                <Panel title="Profile and account">
                  <div className="grid grid-2 stats-2">
                    <Stat label="Email" value={<span className="small break">{d.customer.email}</span>} />
                    <Stat label="Note" value={<span className="small">{d.customer.note || "–"}</span>} />
                    <Stat label="Account" value={<span className="mono small">{d.account.id}</span>} />
                    <Stat label="Total value" value={usd(d.account.total_value)} />
                    <Stat label="Invested" value={usd(d.account.invested_value)} />
                    <Stat label="Cash" value={usd(d.account.cash_balance)} />
                    <Stat label="Wallet" value={usd(d.account.wallet_balance)} />
                  </div>
                </Panel>
                <Panel title="Allocation">
                  <Donut items={d.allocation} compact />
                </Panel>
              </div>

              <Panel title="Trades">
                {d.trades.length === 0 ? (
                  <Empty>No trades.</Empty>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr><th>Order</th><th>Side</th><th>Symbol</th><th className="num">Qty</th><th>Status</th><th>Updated</th></tr>
                      </thead>
                      <tbody>
                        {d.trades.map((t) => (
                          <tr key={t.id}>
                            <td className="mono nowrap">{t.id}</td>
                            <td style={{ textTransform: "capitalize" }}>{t.side}</td>
                            <td><b>{t.symbol}</b></td>
                            <td className="num">{num(t.quantity)}</td>
                            <td><Badge value={t.status} />{t.reject_reason && <div className="small muted">{t.reject_reason}</div>}</td>
                            <td className="muted nowrap">{when(t.updated_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>

              <Panel title="Transfers">
                {d.transfers.length === 0 ? (
                  <Empty>No transfers.</Empty>
                ) : (
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr><th>Transfer</th><th>Direction</th><th className="num">Amount</th><th>Counterparty</th><th>Status</th><th>Updated</th></tr>
                      </thead>
                      <tbody>
                        {d.transfers.map((t) => (
                          <tr key={t.id}>
                            <td className="mono nowrap">{t.id}</td>
                            <td>{t.direction === "in" ? "Deposit" : "Withdrawal"}</td>
                            <td className="num">{usd(t.amount)}</td>
                            <td>{t.counterparty === "bank" ? "Bank" : "Card account"}</td>
                            <td><Badge value={t.status} />{t.reason && <div className="small muted">{t.reason}</div>}</td>
                            <td className="muted nowrap">{when(t.updated_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Panel>

              <Panel title="Tickets">
                <TicketList
                  tickets={d.tickets}
                  showCustomer={false}
                  renderNotes={(t) => <TicketNoteForm ticketId={t.id} onAdded={detail.reload} />}
                />
              </Panel>

              <Panel
                title="Conversations"
                actions={<Link className="btn btn-sm" to={`/support/conversations?customer_id=${d.customer.id}`}>All conversations</Link>}
              >
                {d.conversations.length === 0 ? (
                  <Empty>No conversations.</Empty>
                ) : (
                  <ul className="list">
                    {d.conversations.map((c) => (
                      <li key={c.id}>
                        <Link className="list-link" to={`/support/conversations/${c.id}`}>
                          <b className="break">{c.title}</b>
                          <span className="muted small">{c.message_count} messages · {when(c.updated_at)}</span>
                        </Link>
                        <span className="row">
                          {c.has_negative_feedback && <Badge value="error" text="Thumbs down" />}
                          {c.last_outcome && <Badge value={c.last_outcome} />}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>
            </div>
          </>
        )}
      </Async>
    </main>
  );
}
