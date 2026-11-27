import { Link, useSearchParams } from "react-router";
import { api } from "../../api";
import { Async, Badge, Empty, PageHead, Panel } from "../../components/ui";
import { when } from "../../format";
import { POLL, useApi } from "../../hooks";

export const OUTCOMES = ["ok", "tool_timeout_recovered", "context_overflow", "agent_bug", "error"];

export default function Conversations() {
  const [params, setParams] = useSearchParams();
  const outcome = params.get("outcome") ?? "";
  const down = params.get("feedback") === "down";
  const customer = params.get("customer_id") ?? "";

  const set = (k: string, v: string) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v);
    else p.delete(k);
    setParams(p, { replace: true });
  };

  const list = useApi(
    () => api.supportConversations({ customer_id: customer || undefined, outcome: outcome || undefined, feedback: down ? "down" : undefined, limit: 100 }),
    [customer, outcome, down],
    POLL,
  );

  return (
    <main className="page">
      <PageHead title="Conversations" />
      <Panel>
        <div className="form-grid" style={{ marginBottom: 12 }}>
          <div className="field">
            <label htmlFor="f-outcome">Outcome</label>
            <select id="f-outcome" value={outcome} onChange={(e) => set("outcome", e.target.value)}>
              <option value="">Any outcome</option>
              {OUTCOMES.map((o) => (
                <option key={o} value={o}>{o.replace(/_/g, " ")}</option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="f-cust">Customer ID</label>
            <input id="f-cust" value={customer} onChange={(e) => set("customer_id", e.target.value.trim())} placeholder="CUS-0001" />
          </div>
          <label className="row-nowrap" style={{ minHeight: 38 }}>
            <input type="checkbox" checked={down} onChange={(e) => set("feedback", e.target.checked ? "down" : "")} />
            <span>Thumbs-down only</span>
          </label>
        </div>
        <Async state={list}>
          {(rows) =>
            rows.length === 0 ? (
              <Empty>No conversations match.</Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Customer</th><th>Title</th><th>Last outcome</th><th className="num hide-sm">Messages</th><th className="hide-sm">Updated</th></tr>
                  </thead>
                  <tbody>
                    {rows.map((c) => (
                      <tr key={c.id}>
                        <td className="nowrap">
                          {c.customer_name}
                          <div className="mono small muted">{c.customer_id}</div>
                        </td>
                        <td><Link to={`/support/conversations/${c.id}`}>{c.title}</Link></td>
                        <td>
                          <span className="row">
                            {c.last_outcome ? <Badge value={c.last_outcome} /> : <span className="muted">–</span>}
                            {c.has_negative_feedback && <Badge value="error" text="👎" />}
                          </span>
                        </td>
                        <td className="num hide-sm">{c.message_count}</td>
                        <td className="nowrap muted hide-sm">{when(c.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </Async>
      </Panel>
    </main>
  );
}
