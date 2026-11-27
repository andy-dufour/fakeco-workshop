import { NavLink, Outlet, Link, useLocation } from "react-router";
import { useAuth } from "../auth";
import type { Role } from "../types";

const NAV: Record<Role, { to: string; label: string; end?: boolean }[]> = {
  customer: [
    { to: "/", label: "Dashboard", end: true },
    { to: "/chat", label: "Concierge" },
    { to: "/trades", label: "Trades" },
    { to: "/transfers", label: "Transfers" },
    { to: "/tickets", label: "Tickets" },
  ],
  support: [
    { to: "/support", label: "Customers", end: true },
    { to: "/support/conversations", label: "Conversations" },
    { to: "/support/tickets", label: "Tickets" },
  ],
  operations: [
    { to: "/ops", label: "Health & queues", end: true },
    { to: "/ops/stories", label: "Stories" },
    { to: "/ops/faults", label: "Fault flags" },
    { to: "/ops/loadgen", label: "Load generator" },
    { to: "/ops/runs", label: "Nightly runs" },
    { to: "/ops/tickets", label: "Tickets" },
  ],
  compliance: [
    { to: "/compliance", label: "Held transfers", end: true },
    { to: "/compliance/restricted", label: "Restricted list" },
  ],
};

export const ROLE_LABEL: Record<Role, string> = {
  customer: "Account holder",
  support: "Support",
  operations: "Operations",
  compliance: "Compliance",
};

export function initials(name: string) {
  return name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

export function Avatar({ name, role }: { name: string; role: Role }) {
  return (
    <span className="avatar" data-role={role} aria-hidden="true">
      {initials(name)}
    </span>
  );
}

export default function Layout() {
  const { user, config } = useAuth();
  const { pathname } = useLocation();
  const fill = pathname === "/chat";
  if (!user) return null;
  return (
    <div className={`app ${fill ? "app-fill" : ""}`}>
      <header className="topbar">
        <div className="topbar-row">
          <Link to="/signin" className="brand" aria-label="FakeCo Securities">
            <span className="brand-mark" aria-hidden="true">F</span>
            <span className="brand-text-long">FakeCo Securities</span>
            <span className="brand-text-short">FakeCo</span>
          </Link>
          <span className="spacer" />
          <div className="who" data-role={user.role} aria-label="Signed in as">
            <Avatar name={user.name} role={user.role} />
            <div className="who-text">
              <span className="who-name">{user.name}</span>
              <span className="who-role">
                {ROLE_LABEL[user.role]}<span className="who-id"> · {user.id}</span>
              </span>
            </div>
          </div>
          <Link to="/signin" className="btn btn-sm btn-switch">
            Switch<span className="btn-label-long">person</span>
          </Link>
        </div>
        <nav className="nav" aria-label="Main">
          {NAV[user.role].map((n) => (
            <NavLink key={n.to} to={n.to} end={n.end}>
              {n.label}
            </NavLink>
          ))}
        </nav>
      </header>
      {config && config.environment !== "demo" && <div className="banner">Environment: {config.environment}</div>}
      <Outlet />
    </div>
  );
}
