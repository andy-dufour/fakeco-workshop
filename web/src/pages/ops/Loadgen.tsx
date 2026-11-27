import { useEffect, useState } from "react";
import { api } from "../../api";
import { Async, ErrorBox, PageHead, Panel, Stat } from "../../components/ui";
import { num, when } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";

export default function Loadgen() {
  const lg = useApi(() => api.opsLoadgen(), [], POLL);
  const save = useAction(api.opsSetLoadgen);
  const [conv, setConv] = useState("");
  const [tx, setTx] = useState("");
  const [dirty, setDirty] = useState(false);
  const d = lg.data;

  useEffect(() => {
    if (d && !dirty) {
      setConv(String(d.conversations_per_minute));
      setTx(String(d.transactions_per_minute));
    }
  }, [d, dirty]);

  const valid = (s: string) => s.trim() !== "" && Number.isInteger(Number(s)) && Number(s) >= 0 && Number(s) <= 120;

  const apply = async (b: Parameters<typeof api.opsSetLoadgen>[0]) => {
    const r = await save.run(b);
    if (r) {
      lg.setData(r);
      setDirty(false);
    }
  };

  return (
    <main className="page">
      <PageHead title="Load generator" />
      <div className="stack">
        <Async state={lg}>
          {(g) => (
            <>
              <Panel title="Settings">
                <div className="stack-sm">
                  <label className="row-nowrap">
                    <input type="checkbox" checked={g.enabled} disabled={save.busy} onChange={(e) => apply({ enabled: e.target.checked })} />
                    <b>Enabled</b>
                  </label>
                  <label className="row-nowrap">
                    <input type="checkbox" checked={g.auto_stories} disabled={save.busy} onChange={(e) => apply({ auto_stories: e.target.checked })} />
                    <span><b>Start stories automatically</b> <span className="muted small">(the load generator switches stories on and off by itself)</span></span>
                  </label>
                  <form
                    className="form-grid"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void apply({ conversations_per_minute: Number(conv), transactions_per_minute: Number(tx) });
                    }}
                  >
                    <div className="field">
                      <label htmlFor="lg-c">Conversations per minute (0–120)</label>
                      <input id="lg-c" type="number" min="0" max="120" inputMode="numeric" value={conv} onChange={(e) => { setConv(e.target.value); setDirty(true); }} />
                    </div>
                    <div className="field">
                      <label htmlFor="lg-t">Transactions per minute (0–120)</label>
                      <input id="lg-t" type="number" min="0" max="120" inputMode="numeric" value={tx} onChange={(e) => { setTx(e.target.value); setDirty(true); }} />
                    </div>
                    <div>
                      <button className="btn btn-primary" disabled={save.busy || !dirty || !valid(conv) || !valid(tx)}>Apply rates</button>
                    </div>
                  </form>
                  <ErrorBox error={save.error} />
                </div>
              </Panel>
              <Panel title="Stats">
                <div className="grid grid-4 stats-2">
                  <Stat label="Conversations" value={num(g.stats.conversations)} />
                  <Stat label="Turns" value={num(g.stats.turns)} />
                  <Stat label="Transactions" value={num(g.stats.transactions)} />
                  <Stat label="Errors" value={<span style={{ color: g.stats.errors ? "var(--bad)" : undefined }}>{num(g.stats.errors)}</span>} />
                </div>
                <p className="muted small" style={{ marginTop: 10 }}>Last activity: {g.stats.last_seen_at ? when(g.stats.last_seen_at) : "never"}</p>
              </Panel>
            </>
          )}
        </Async>
      </div>
    </main>
  );
}
