import { useState } from "react";
import { api } from "../../api";
import { Async, Badge, ErrorBox, PageHead } from "../../components/ui";
import { duration } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";
import type { Story } from "../../types";

function StoryCard({ s, onChange }: { s: Story; onChange: () => void }) {
  const [minutes, setMinutes] = useState("15");
  const start = useAction(api.opsStartStory);
  const stop = useAction(api.opsStopStory);
  const m = Number(minutes);
  const left = s.expires_at ? Math.max(0, (new Date(s.expires_at).getTime() - Date.now()) / 1000) : null;
  return (
    <div className={`panel story ${s.active ? "active" : ""}`}>
      <div className="row">
        <h3 style={{ marginRight: "auto" }}>{s.title}</h3>
        <span className="badge">{s.kind}</span>
        {s.active && <Badge value="held" text={left != null ? `Active · ${duration(left)} left` : "Active"} />}
      </div>
      <p className="muted small">{s.description}</p>
      <p className="small">
        Faults: {s.faults.map((f) => <code key={f} style={{ marginRight: 6 }}>{f}</code>)}
      </p>
      <div className="row">
        {s.active ? (
          <button className="btn btn-danger" disabled={stop.busy} onClick={async () => (await stop.run(s.name)) && onChange()}>
            Stop
          </button>
        ) : (
          <>
            <input
              type="number"
              min="1"
              max="240"
              inputMode="numeric"
              value={minutes}
              onChange={(e) => setMinutes(e.target.value)}
              aria-label={`Minutes for ${s.title}`}
              style={{ width: 84 }}
            />
            <span className="muted">min</span>
            <button className="btn btn-primary" disabled={start.busy || !(m >= 1)} onClick={async () => (await start.run(s.name, m)) && onChange()}>
              Start
            </button>
          </>
        )}
      </div>
      <ErrorBox error={start.error ?? stop.error} />
      <span className="mono muted small">{s.name}</span>
    </div>
  );
}

export default function Stories() {
  const stories = useApi(() => api.opsStories(), [], POLL);
  const stopAll = useAction(api.opsStopAll);
  const [msg, setMsg] = useState<string | null>(null);
  const active = stories.data?.filter((s) => s.active).length ?? 0;
  return (
    <main className="page">
      <PageHead title="Stories">
        <button
          className="btn btn-danger"
          disabled={stopAll.busy || active === 0}
          onClick={async () => {
            const r = await stopAll.run();
            if (r) {
              setMsg(`Stopped ${r.stopped}.`);
              stories.reload();
            }
          }}
        >
          Stop all{active ? ` (${active})` : ""}
        </button>
      </PageHead>
      <div className="stack">
        <ErrorBox error={stopAll.error} />
        {msg && <p className="form-ok" role="status">{msg}</p>}
        <p className="muted">A story switches on a set of faults for a while, then switches them off again.</p>
        <div>
          <Async state={stories}>
            {(rows) => (
              <div className="grid grid-3">
                {rows.map((s) => (
                  <StoryCard key={s.name} s={s} onChange={stories.reload} />
                ))}
              </div>
            )}
          </Async>
        </div>
      </div>
    </main>
  );
}
