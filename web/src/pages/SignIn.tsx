import { useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { api } from "../api";
import { Avatar, ROLE_LABEL } from "../components/Layout";
import { Async, ErrorBox } from "../components/ui";
import { homeFor, useAuth } from "../auth";
import { useApi } from "../hooks";
import type { Person, Role } from "../types";

function PersonaButton({ p, current, busy, onPick }: { p: Person; current: boolean; busy: boolean; onPick: (p: Person) => void }) {
  return (
    <button
      className="persona"
      data-role={p.role}
      aria-current={current}
      disabled={busy}
      onClick={() => onPick(p)}
      title={`${p.name} (${p.id})`}
    >
      <Avatar name={p.name} role={p.role} />
      <span className="persona-text">
        <strong>{p.name}</strong>
        <span>
          {p.id}
          {p.note ? ` · ${p.note}` : ""}
        </span>
      </span>
    </button>
  );
}

export default function SignIn() {
  const { user, signIn } = useAuth();
  const navigate = useNavigate();
  const personas = useApi(() => api.personas(), []);
  const [q, setQ] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const pick = async (p: Person) => {
    setBusy(true);
    setError(null);
    try {
      const me = await signIn(p.id);
      navigate(homeFor[me.role], { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  const staffByRole = useMemo(() => {
    const m = new Map<Role, Person[]>();
    for (const s of personas.data?.staff ?? []) m.set(s.role, [...(m.get(s.role) ?? []), s]);
    return m;
  }, [personas.data]);

  const customers = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (personas.data?.customers ?? []).filter(
      (c) => !needle || [c.name, c.id, c.email, c.note].some((f) => f.toLowerCase().includes(needle)),
    );
  }, [personas.data, q]);

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-row">
          <span className="brand">
            <span className="brand-mark" aria-hidden="true">F</span>
            FakeCo Securities
          </span>
          <span className="spacer" />
          {user && (
            <span className="muted small">
              Signed in as <b>{user.name}</b>
            </span>
          )}
        </div>
      </header>
      <main className="page">
        <div className="signin-hero">
          <h1>Who are you today?</h1>
          <p>Pick a person to sign in. There are no passwords; this is a demo. Each browser tab keeps its own person.</p>
        </div>
        <ErrorBox error={error} />
        <Async state={personas}>
          {() => (
            <>
              {(["support", "operations", "compliance"] as Role[]).map((role) => (
                <section key={role} aria-labelledby={`h-${role}`}>
                  <div className="section-title">
                    <h2 id={`h-${role}`}>{ROLE_LABEL[role]}</h2>
                  </div>
                  <div className="persona-grid">
                    {(staffByRole.get(role) ?? []).map((p) => (
                      <PersonaButton key={p.id} p={p} current={user?.id === p.id} busy={busy} onPick={pick} />
                    ))}
                  </div>
                </section>
              ))}
              <section aria-labelledby="h-customer">
                <div className="section-title">
                  <h2 id="h-customer">Account holders</h2>
                  <span className="muted small">{customers.length} of {personas.data?.customers.length}</span>
                </div>
                <div className="field" style={{ maxWidth: 420, marginBottom: 12 }}>
                  <label htmlFor="cust-search" className="label">Search by name, ID or email</label>
                  <input
                    id="cust-search"
                    type="search"
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    placeholder="Dana, CUS-0042, whale…"
                    autoComplete="off"
                  />
                </div>
                <div className="persona-grid">
                  {customers.map((p) => (
                    <PersonaButton key={p.id} p={p} current={user?.id === p.id} busy={busy} onPick={pick} />
                  ))}
                </div>
                {customers.length === 0 && <p className="empty">Nobody matches “{q}”.</p>}
              </section>
            </>
          )}
        </Async>
      </main>
    </div>
  );
}
