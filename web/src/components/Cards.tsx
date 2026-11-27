// Renders the card types of specification section 3.3. Unknown types render nothing.
import { useState } from "react";
import { Donut, Badge } from "./ui";
import { num, usd } from "../format";
import type { Card } from "../types";

export interface CardActions {
  /** Send a message as if the user pressed a button (chip: true). */
  send: (text: string) => void;
  /** Put text in the message box without sending. */
  fill: (text: string) => void;
  /** Buttons are off while a reply is pending. */
  disabled: boolean;
  /** Only the newest confirm card has live buttons. */
  confirmLive: boolean;
}

function TransferForm({ a }: { a: CardActions }) {
  const [direction, setDirection] = useState<"in" | "out">("in");
  const [amount, setAmount] = useState("250");
  const value = Number(amount);
  const ok = Number.isFinite(value) && value > 0;
  return (
    <form
      className="ccard stack-sm"
      onSubmit={(e) => {
        e.preventDefault();
        if (!ok) return;
        const money = `$${value.toFixed(2)}`;
        a.send(direction === "in" ? `Add ${money} to my account.` : `Withdraw ${money} from my account.`);
      }}
    >
      <h3>Transfer money</h3>
      <div className="seg" role="group" aria-label="Direction">
        <button type="button" aria-pressed={direction === "in"} onClick={() => setDirection("in")}>Add money</button>
        <button type="button" aria-pressed={direction === "out"} onClick={() => setDirection("out")}>Withdraw</button>
      </div>
      <div className="field">
        <label htmlFor="card-amount">Amount (USD)</label>
        <input id="card-amount" type="number" min="0.01" step="0.01" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
      </div>
      <button className="btn btn-primary" type="submit" disabled={!ok || a.disabled}>
        {direction === "in" ? "Add" : "Withdraw"} {ok ? usd(value) : ""}
      </button>
    </form>
  );
}

export function CardView({ card, actions: a, readOnly }: { card: Card; actions: CardActions; readOnly?: boolean }) {
  const off = a.disabled || !!readOnly;
  switch (card.type) {
    case "allocation": {
      const c = card as Extract<Card, { type: "allocation" }>;
      return (
        <div className="ccard">
          <h3>Allocation</h3>
          <Donut items={c.items} compact />
        </div>
      );
    }
    case "trades": {
      const c = card as Extract<Card, { type: "trades" }>;
      return (
        <div className="ccard">
          <h3>Trades</h3>
          {c.items.length === 0 && <p className="muted">No trades.</p>}
          {c.items.map((t) => (
            <button key={t.id} className="row-item" disabled={off} onClick={() => a.fill(`Change trade ${t.id} to`)} title="Change this trade">
              <span style={{ flex: 1, minWidth: 0 }}>
                <b style={{ textTransform: "capitalize" }}>{t.side}</b> {num(t.quantity)} {t.symbol}
                <span className="muted small mono" style={{ display: "block" }}>{t.id}</span>
              </span>
              <Badge value={t.status} />
            </button>
          ))}
          {c.items.length > 0 && <p className="muted small" style={{ marginTop: 6 }}>Tap a trade to change it.</p>}
        </div>
      );
    }
    case "transfer_form":
      return readOnly ? (
        <div className="ccard muted">Transfer form</div>
      ) : (
        <TransferForm a={a} />
      );
    case "confirm": {
      const c = card as Extract<Card, { type: "confirm" }>;
      const live = a.confirmLive && !off;
      return (
        <div className={`ccard confirm-card ${a.confirmLive ? "" : "stale"}`}>
          <h3>
            {c.title} <Badge value="pending" text={c.kind} />
          </h3>
          {c.lines.length > 0 && (
            <ul className="confirm-lines">
              {c.lines.map((l, i) => (
                <li key={i}>{l}</li>
              ))}
            </ul>
          )}
          <div className="row">
            <button className="btn btn-primary" disabled={!live} onClick={() => a.send("Confirm")}>Confirm</button>
            <button className="btn" disabled={!live} onClick={() => a.send("Cancel")}>Cancel</button>
          </div>
        </div>
      );
    }
    case "rebalance_plan": {
      const c = card as Extract<Card, { type: "rebalance_plan" }>;
      return (
        <div className="ccard">
          <h3>Rebalance plan</h3>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Asset class</th>
                  <th className="num">From</th>
                  <th className="num">To</th>
                </tr>
              </thead>
              <tbody>
                {c.moves.map((m) => (
                  <tr key={m.asset_class}>
                    <td>{m.asset_class}</td>
                    <td className="num">{m.from}%</td>
                    <td className="num">
                      <b>{m.to}%</b>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      );
    }
    case "ticket": {
      const c = card as Extract<Card, { type: "ticket" }>;
      return (
        <div className="ccard">
          <div className="row">
            <b className="mono">{c.ticket_id}</b>
            <span className="muted" style={{ textTransform: "capitalize" }}>{c.kind}</span>
            <Badge value={c.state} />
          </div>
          <div style={{ marginTop: 8 }}>
            <button className="btn btn-sm" disabled={off} onClick={() => a.send(`Check ticket ${c.ticket_id}`)}>Where is it?</button>
          </div>
        </div>
      );
    }
    case "chips": {
      const c = card as Extract<Card, { type: "chips" }>;
      return (
        <div className="chips">
          {c.options.map((o) => (
            <button key={o} className="chip" disabled={off} onClick={() => a.send(o)}>{o}</button>
          ))}
        </div>
      );
    }
    case "valuations": {
      const c = card as Extract<Card, { type: "valuations" }>;
      return (
        <div className="ccard valuation">
          <span className="stat-label">Valued {num(c.count)} holdings</span>
          <span className="stat-value">{usd(c.total)}</span>
        </div>
      );
    }
    default:
      return null;
  }
}
