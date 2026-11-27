import { useState } from "react";
import { Link } from "react-router";
import { api } from "../../api";
import { Async, Donut, Empty, PageHead, Panel, Stat } from "../../components/ui";
import { num, usd, when } from "../../format";
import { POLL, useApi } from "../../hooks";

export default function Dashboard() {
  const summary = useApi(() => api.summary(), [], 30000);
  const holdings = useApi(() => api.holdings(), [], 30000);
  const notes = useApi(() => api.notifications(), [], POLL);
  const [all, setAll] = useState(false);

  return (
    <main className="page">
      <PageHead title="Dashboard">
        <Link className="btn btn-primary" to="/chat">Ask the Concierge</Link>
        <Link className="btn" to="/trades">New order</Link>
      </PageHead>
      <div className="stack">
        <Async state={summary}>
          {(s) => (
            <>
              <Panel>
                <div className="grid grid-4 stats-2">
                  <Stat big label="Total value" value={usd(s.account.total_value)} />
                  <Stat label="Invested" value={usd(s.account.invested_value)} />
                  <Stat label="Cash" value={usd(s.account.cash_balance)} />
                  <Stat label="Wallet" value={usd(s.account.wallet_balance)} />
                </div>
                <p className="muted small" style={{ marginTop: 10 }}>
                  Account {s.account.id}
                </p>
              </Panel>
              <div className="grid grid-main">
                <Panel title="Allocation">
                  <Donut items={s.allocation} center={usd(s.account.total_value).replace(/\.\d+$/, "")} />
                </Panel>
                <Panel title="Notifications">
                  <Async state={notes}>
                    {(list) =>
                      list.length === 0 ? (
                        <Empty>Nothing yet.</Empty>
                      ) : (
                        <ul className="list" style={{ maxHeight: 260, overflowY: "auto" }}>
                          {list.slice(0, 8).map((n) => (
                            <li key={n.id}>
                              <span className="break">{n.text}</span>
                              <span className="muted small nowrap">{when(n.created_at)}</span>
                            </li>
                          ))}
                        </ul>
                      )
                    }
                  </Async>
                </Panel>
              </div>
            </>
          )}
        </Async>
        <Panel
          title={`Holdings${holdings.data ? ` (${num(holdings.data.length)})` : ""}`}
          actions={
            holdings.data && holdings.data.length > 12 ? (
              <button className="btn btn-sm" onClick={() => setAll(!all)}>
                {all ? "Show fewer" : "Show all"}
              </button>
            ) : null
          }
        >
          <Async state={holdings}>
            {(rows) =>
              rows.length === 0 ? (
                <Empty>No holdings.</Empty>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Symbol</th>
                        <th>Name</th>
                        <th>Class</th>
                        <th className="num">Qty</th>
                        <th className="num">Price</th>
                        <th className="num">Value</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(all ? rows : rows.slice(0, 12)).map((h) => (
                        <tr key={h.asset_id}>
                          <td className="nowrap"><b>{h.symbol}</b></td>
                          <td>{h.name}</td>
                          <td>{h.asset_class}</td>
                          <td className="num">{num(h.quantity)}</td>
                          <td className="num">{usd(h.price)}</td>
                          <td className="num">{usd(h.market_value)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            }
          </Async>
        </Panel>
      </div>
    </main>
  );
}
