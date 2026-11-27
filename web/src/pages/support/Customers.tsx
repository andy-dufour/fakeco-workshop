import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api } from "../../api";
import { Async, Empty, PageHead, Panel } from "../../components/ui";
import { useApi } from "../../hooks";

export default function Customers() {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);
  const list = useApi(() => api.supportCustomers(debounced), [debounced]);

  return (
    <main className="page">
      <PageHead title="Customers" />
      <Panel>
        <div className="field" style={{ maxWidth: 480, marginBottom: 12 }}>
          <label htmlFor="q">Search by name, ID or email</label>
          <input id="q" type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Dana, CUS-0042…" autoComplete="off" autoFocus />
        </div>
        <Async state={list}>
          {(rows) =>
            rows.length === 0 ? (
              <Empty>No customers match.</Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>ID</th>
                      <th>Name</th>
                      <th>Email</th>
                      <th>Note</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((c) => (
                      <tr key={c.id}>
                        <td className="mono nowrap"><Link to={`/support/customers/${c.id}`}>{c.id}</Link></td>
                        <td><Link to={`/support/customers/${c.id}`}>{c.name}</Link></td>
                        <td className="muted">{c.email}</td>
                        <td>{c.note}</td>
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
