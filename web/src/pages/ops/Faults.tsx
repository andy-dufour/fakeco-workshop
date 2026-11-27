import { useEffect, useState } from "react";
import { api } from "../../api";
import { Async, Badge, ErrorBox, PageHead } from "../../components/ui";
import { duration } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";
import type { Fault } from "../../types";

const pretty = (p: Fault["params"]) => (p == null ? "" : JSON.stringify(p, null, 2));

function FaultRow({ f, onChange }: { f: Fault; onChange: () => void }) {
  const server = pretty(f.params);
  const [draft, setDraft] = useState(server);
  const [minutes, setMinutes] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const save = useAction(api.opsSetFault);
  const dirty = draft !== server;

  // Follow the server while the person is not editing.
  useEffect(() => {
    if (!dirty) setDraft(server);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [server]);

  const parse = (): { ok: true; value: Record<string, unknown> | null } | { ok: false } => {
    if (!draft.trim()) return { ok: true, value: null };
    try {
      const v = JSON.parse(draft);
      if (typeof v !== "object" || Array.isArray(v) || v === null) throw new Error("Params must be a JSON object.");
      return { ok: true, value: v };
    } catch (e) {
      setParseError(e instanceof Error ? e.message : String(e));
      return { ok: false };
    }
  };

  const apply = async (active: boolean) => {
    setParseError(null);
    const p = parse();
    if (!p.ok) return;
    const mins = minutes.trim() ? Number(minutes) : null;
    if (mins !== null && !(mins > 0)) {
      setParseError("Minutes must be a positive number.");
      return;
    }
    if (await save.run(f.name, { active, params: p.value, minutes: active ? mins : null })) onChange();
  };

  const left = f.expires_at ? Math.max(0, (new Date(f.expires_at).getTime() - Date.now()) / 1000) : null;

  return (
    <div className="panel story">
      <div className="row">
        <h3 className="mono" style={{ marginRight: "auto" }}>{f.name}</h3>
        {f.story && <span className="badge badge-info">story: {f.story}</span>}
        <Badge value={f.active ? "held" : "cancelled"} text={f.active ? `On${left != null ? ` · ${duration(left)} left` : ""}` : "Off"} />
      </div>
      <p className="muted small">{f.description}</p>
      <div className="field">
        <label htmlFor={`p-${f.name}`}>Params (JSON)</label>
        <textarea id={`p-${f.name}`} className="json-edit" rows={Math.min(8, Math.max(2, draft.split("\n").length))} value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="no params" spellCheck={false} />
      </div>
      <div className="row">
        <button className={`btn ${f.active ? "btn-danger" : "btn-primary"}`} disabled={save.busy} onClick={() => apply(!f.active)}>
          {f.active ? "Turn off" : "Turn on"}
        </button>
        {f.active && (
          <button className="btn" disabled={save.busy || !dirty} onClick={() => apply(true)}>Save params</button>
        )}
        {!f.active && (
          <>
            <input type="number" min="1" inputMode="numeric" placeholder="min" value={minutes} onChange={(e) => setMinutes(e.target.value)} aria-label={`Minutes for ${f.name}`} style={{ width: 84 }} />
            <span className="muted small">minutes (empty: until switched off)</span>
          </>
        )}
      </div>
      <ErrorBox error={parseError ?? save.error} />
    </div>
  );
}

export default function Faults() {
  const faults = useApi(() => api.opsFaults(), [], POLL);
  return (
    <main className="page">
      <PageHead title="Fault flags">
        <span className="muted small">
          {faults.data ? `${faults.data.filter((f) => f.active).length} of ${faults.data.length} on · ` : ""}refreshes every 5 s
        </span>
      </PageHead>
      <Async state={faults}>
        {(rows) => (
          <div className="grid grid-2">
            {rows.map((f) => (
              <FaultRow key={f.name} f={f} onChange={faults.reload} />
            ))}
          </div>
        )}
      </Async>
    </main>
  );
}
