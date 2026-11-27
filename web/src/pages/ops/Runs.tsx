import { useState } from "react";
import { api } from "../../api";
import { Async, Badge, Empty, ErrorBox, PageHead, Panel } from "../../components/ui";
import { when } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";
import type { Run } from "../../types";

export default function Runs() {
  const runs = useApi(() => api.opsRuns(), [], POLL);
  const start = useAction(api.opsStartRun);
  const [kind, setKind] = useState<Run["kind"]>("settlement");
  return (
    <main className="page">
      <PageHead title="Nightly runs" />
      <div className="stack">
        <Panel title="Start a run">
          <div className="row">
            <div className="field" style={{ minWidth: 180 }}>
              <label htmlFor="rk">Kind</label>
              <select id="rk" value={kind} onChange={(e) => setKind(e.target.value as Run["kind"])}>
                <option value="settlement">Settlement</option>
                <option value="sweep">Sweep</option>
                <option value="pricing">Pricing</option>
                <option value="payroll">Payroll deposits</option>
              </select>
            </div>
            <button
              className="btn btn-primary"
              style={{ alignSelf: "flex-end" }}
              disabled={start.busy}
              onClick={async () => (await start.run(kind)) && runs.reload()}
            >
              Start {kind === "payroll" ? "payroll deposits" : kind}
            </button>
          </div>
          {kind === "payroll" && <p className="muted small" style={{ marginTop: 8 }}>Posts payroll deposits to about 40 customers. Each deposit puts a message on the sweeps queue.</p>}
          <ErrorBox error={start.error} />
        </Panel>
        <Panel title="Recent runs">
          <Async state={runs}>
            {(rows) =>
              rows.length === 0 ? (
                <Empty>No runs yet.</Empty>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr><th>ID</th><th>Kind</th><th>Status</th><th>Detail</th><th>Started</th><th>Ended</th><th>By</th></tr>
                    </thead>
                    <tbody>
                      {rows.map((r) => (
                        <tr key={r.id}>
                          <td className="mono">{r.id}</td>
                          <td>{r.kind === "payroll" ? "Payroll deposits" : <span style={{ textTransform: "capitalize" }}>{r.kind}</span>}</td>
                          <td><Badge value={r.status} /></td>
                          <td className="break">{r.detail}</td>
                          <td className="nowrap muted">{when(r.started_at)}</td>
                          <td className="nowrap muted">{r.ended_at ? when(r.ended_at) : "–"}</td>
                          <td className="mono">{r.started_by}</td>
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
