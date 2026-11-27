import { useState } from "react";
import { api } from "../../api";
import { Async, Empty, ErrorBox, PageHead, Panel } from "../../components/ui";
import { when } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";

export default function Restricted() {
  const list = useApi(() => api.restricted(), [], POLL);
  const [symbol, setSymbol] = useState("");
  const [reason, setReason] = useState("");
  const add = useAction(api.addRestricted);
  const remove = useAction(api.removeRestricted);

  return (
    <main className="page">
      <PageHead title="Restricted list" />
      <div className="stack">
        <Panel title="Add a symbol">
          <form
            className="stack-sm"
            onSubmit={async (e) => {
              e.preventDefault();
              const r = await add.run(symbol.trim().toUpperCase(), reason.trim());
              if (r) {
                list.setData(r);
                setSymbol("");
                setReason("");
              }
            }}
          >
            <div className="form-grid">
              <div className="field">
                <label htmlFor="rs">Symbol</label>
                <input id="rs" value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="ORB-EQ" autoComplete="off" />
              </div>
              <div className="field" style={{ gridColumn: "span 2" }}>
                <label htmlFor="rr">Reason</label>
                <input id="rr" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Why orders are blocked" />
              </div>
              <div>
                <button className="btn btn-primary" disabled={add.busy || !symbol.trim() || !reason.trim()}>Add</button>
              </div>
            </div>
            <p className="muted small">Orders in a restricted symbol are rejected by the back office.</p>
            <ErrorBox error={add.error} />
          </form>
        </Panel>
        <Panel title="Restricted symbols">
          <ErrorBox error={remove.error} />
          <Async state={list}>
            {(rows) =>
              rows.length === 0 ? (
                <Empty>No restricted symbols.</Empty>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr><th>Symbol</th><th>Reason</th><th>Added by</th><th>Added</th><th></th></tr>
                    </thead>
                    <tbody>
                      {rows.map((r) => (
                        <tr key={r.symbol}>
                          <td><b>{r.symbol}</b></td>
                          <td className="break">{r.reason}</td>
                          <td className="mono">{r.added_by}</td>
                          <td className="nowrap muted">{when(r.created_at)}</td>
                          <td>
                            <button
                              className="btn btn-sm btn-danger"
                              disabled={remove.busy}
                              onClick={async () => {
                                const next = await remove.run(r.symbol);
                                if (next) list.setData(next);
                              }}
                            >
                              Remove
                            </button>
                          </td>
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
