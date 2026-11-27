import type { ReactNode } from "react";
import type { ApiError } from "../api";
import { ASSET_COLORS, label, usd } from "../format";
import type { AllocationItem } from "../types";

const TONES: Record<string, string> = {
  filled: "ok",
  completed: "ok",
  ok: "ok",
  approved: "ok",
  pending: "info",
  proposed: "info",
  queued: "accent",
  processing: "accent",
  running: "accent",
  partially_filled: "accent",
  retrying: "warn",
  held: "warn",
  tool_timeout_recovered: "warn",
  cancelled: "",
  returned: "warn",
  rejected: "bad",
  failed: "bad",
  dead_lettered: "bad",
  error: "bad",
  context_overflow: "bad",
  agent_bug: "bad",
};

export function Badge({ value, text }: { value: string; text?: string }) {
  const tone = TONES[value] ?? "";
  return <span className={`badge ${tone ? `badge-${tone}` : ""}`}>{text ?? label(value)}</span>;
}

export function Loading({ text = "Loading…" }: { text?: string }) {
  return (
    <div className="loading" role="status">
      <span className="spinner" aria-hidden="true" />
      {text}
    </div>
  );
}

export function ErrorBox({ error }: { error: ApiError | string | null | undefined }) {
  if (!error) return null;
  return (
    <div className="alert" role="alert">
      {typeof error === "string" ? error : error.message}
    </div>
  );
}

/** Shows loading text on first load, an error if there is no data yet, else children. */
export function Async<T>({
  state,
  children,
}: {
  state: { data: T | undefined; error: ApiError | null; loading: boolean };
  children: (data: T) => ReactNode;
}) {
  if (state.data === undefined) {
    return state.error ? <ErrorBox error={state.error} /> : <Loading />;
  }
  return (
    <>
      {state.error && <ErrorBox error={state.error} />}
      {children(state.data)}
    </>
  );
}

export function Panel({
  title,
  actions,
  children,
  className = "",
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || actions) && (
        <div className="panel-head">
          {title && <h2>{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({ label: l, value, big }: { label: string; value: ReactNode; big?: boolean }) {
  return (
    <div className="stat">
      <span className="stat-label">{l}</span>
      <span className={`stat-value ${big ? "big" : ""}`}>{value}</span>
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function PageHead({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      <h1>{title}</h1>
      {children}
    </div>
  );
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: { id: T; label: string }[];
  value: T;
  onChange: (t: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={value === t.id} onClick={() => onChange(t.id)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Seg<T extends string>({
  options,
  value,
  onChange,
  name,
}: {
  options: { id: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  name: string;
}) {
  return (
    <div className="seg" role="group" aria-label={name}>
      {options.map((o) => (
        <button key={o.id} type="button" aria-pressed={value === o.id} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** Donut chart drawn with SVG circles and stroke-dasharray. */
export function Donut({
  items,
  center,
  compact,
}: {
  items: AllocationItem[];
  center?: string;
  compact?: boolean;
}) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const total = items.reduce((s, i) => s + Math.max(i.percent, 0), 0) || 1;
  let offset = 0;
  return (
    <div className="donut-wrap">
      <svg
        className={`donut ${compact ? "donut-compact" : ""}`}
        viewBox="0 0 140 140"
        role="img"
        aria-label={`Allocation: ${items.map((i) => `${i.asset_class} ${i.percent}%`).join(", ")}`}
      >
        <circle cx="70" cy="70" r={r} fill="none" stroke="var(--surface-2)" strokeWidth="22" />
        {items.map((i) => {
          const len = (Math.max(i.percent, 0) / total) * c;
          const el = (
            <circle
              key={i.asset_class}
              cx="70"
              cy="70"
              r={r}
              fill="none"
              stroke={ASSET_COLORS[i.asset_class] ?? "var(--muted)"}
              strokeWidth="22"
              strokeDasharray={`${len} ${c - len}`}
              strokeDashoffset={-offset}
              transform="rotate(-90 70 70)"
            />
          );
          offset += len;
          return el;
        })}
        {center && (
          <text x="70" y="75" textAnchor="middle" fontSize="15" fontWeight="700">
            {center}
          </text>
        )}
      </svg>
      <ul className="legend">
        {items.map((i) => (
          <li key={i.asset_class}>
            <span className="swatch" style={{ background: ASSET_COLORS[i.asset_class] ?? "var(--muted)" }} />
            <span className="lg-name">{i.asset_class}</span>
            <span className="muted">{i.percent}%</span>
            <span className="num">{usd(i.value)}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export const ThumbUp = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M7 10v11H3V10zM7 10l4-8a2.5 2.5 0 0 1 2.5 2.7L13 8h6.2a2 2 0 0 1 2 2.4l-1.4 8a2 2 0 0 1-2 1.6H7" />
  </svg>
);
export const ThumbDown = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M17 14V3h4v11zM17 14l-4 8a2.5 2.5 0 0 1-2.5-2.7L11 16H4.8a2 2 0 0 1-2-2.4l1.4-8A2 2 0 0 1 6.2 4H17" />
  </svg>
);
