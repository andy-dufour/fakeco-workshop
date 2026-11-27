import { useState } from "react";
import { api } from "../../api";
import { Async, Badge, Empty, ErrorBox, PageHead } from "../../components/ui";
import { Panel } from "../../components/ui";
import { usd, when } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";
import type { Hold } from "../../types";

function HoldCard({ h, onDone }: { h: Hold; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const decide = useAction(api.decideHold);
  const go = async (decision: "approve" | "reject") => {
    if (await decide.run(h.transfer.id, decision, reason.trim())) onDone();
  };
  const risk = Math.round(h.risk_score * 100);
  return (
    <div className="panel stack-sm">
      <div className="row">
        <h3 style={{ marginRight: "auto" }}>
          {usd(h.transfer.amount)} {h.transfer.direction === "in" ? "deposit" : "withdrawal"}
        </h3>
        <Badge value={risk >= 75 ? "failed" : risk >= 40 ? "held" : "pending"} text={`Risk ${risk}%`} />
      </div>
      <p>
        <b>{h.customer.name}</b> <span className="mono muted">{h.customer.id}</span>
      </p>
      <p className="small muted">
        {h.transfer.id} · {h.transfer.counterparty === "bank" ? "Bank" : "Card account"} · ticket {h.ticket_id} · {when(h.transfer.created_at)}
      </p>
      <p className="notice">Held: {h.hold_reason}</p>
      <div className="field">
        <label htmlFor={`r-${h.transfer.id}`}>Reason (optional)</label>
        <input id={`r-${h.transfer.id}`} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Shown on the ticket" />
      </div>
      <div className="row">
        <button className="btn btn-ok" disabled={decide.busy} onClick={() => go("approve")}>Approve</button>
        <button className="btn btn-danger" disabled={decide.busy} onClick={() => go("reject")}>Reject</button>
      </div>
      <ErrorBox error={decide.error} />
    </div>
  );
}

export default function Holds() {
  const holds = useApi(() => api.holds(), [], POLL);
  return (
    <main className="page">
      <PageHead title="Held transfers">
        <span className="muted small">Refreshes every 5 s</span>
      </PageHead>
      <Async state={holds}>
        {(rows) =>
          rows.length === 0 ? (
            <Panel><Empty>No transfers are waiting for a decision.</Empty></Panel>
          ) : (
            <div className="grid grid-3">
              {rows.map((h) => (
                <HoldCard key={h.transfer.id} h={h} onDone={holds.reload} />
              ))}
            </div>
          )
        }
      </Async>
    </main>
  );
}
