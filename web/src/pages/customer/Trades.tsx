import { useMemo, useState } from "react";
import { Link } from "react-router";
import { api } from "../../api";
import { Async, Badge, Empty, ErrorBox, PageHead, Panel, Seg } from "../../components/ui";
import { num, usd, when } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";
import type { Trade } from "../../types";

const STATUSES = ["all", "pending", "queued", "filled", "cancelled", "rejected"] as const;

function OrderForm({ onDone }: { onDone: () => void }) {
  const instruments = useApi(() => api.instruments(), []);
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [symbol, setSymbol] = useState("");
  const [qty, setQty] = useState("10");
  const [msg, setMsg] = useState<string | null>(null);
  const create = useAction(api.createTrade);

  const inst = instruments.data?.find((i) => i.symbol === symbol.trim().toUpperCase());
  const quantity = Number(qty);
  const valid = !!inst && Number.isInteger(quantity) && quantity > 0;

  const submit = async (mode: "now" | "pending") => {
    if (!inst) return;
    setMsg(null);
    const res = await create.run({ side, symbol: inst.symbol, quantity, mode });
    if (res) {
      setMsg(
        res.ticket_id
          ? `Order ${res.trade.id} sent. Ticket ${res.ticket_id}.`
          : `Order ${res.trade.id} saved as pending. Submit it from the list when you are ready.`,
      );
      onDone();
    }
  };

  return (
    <Panel title="New order">
      <form
        className="stack-sm"
        onSubmit={(e) => {
          e.preventDefault();
          void submit("now");
        }}
      >
        <div className="form-grid">
          <div className="field">
            <span className="label">Side</span>
            <Seg name="Side" value={side} onChange={setSide} options={[{ id: "buy", label: "Buy" }, { id: "sell", label: "Sell" }]} />
          </div>
          <div className="field">
            <label htmlFor="o-symbol">Symbol</label>
            <input id="o-symbol" list="instruments" value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="NVD-EQ" autoComplete="off" />
            <datalist id="instruments">
              {instruments.data?.map((i) => (
                <option key={i.symbol} value={i.symbol}>{i.name}</option>
              ))}
            </datalist>
          </div>
          <div className="field">
            <label htmlFor="o-qty">Quantity</label>
            <input id="o-qty" type="number" min="1" step="1" inputMode="numeric" value={qty} onChange={(e) => setQty(e.target.value)} />
          </div>
        </div>
        {inst && (
          <p className="muted small">
            {inst.name} · {inst.asset_class} · {usd(inst.price)} · about {usd(inst.price * (quantity || 0))}
          </p>
        )}
        {symbol && !inst && instruments.data && <p className="muted small">Unknown symbol.</p>}
        <div className="row">
          <button className="btn btn-primary" type="submit" disabled={!valid || create.busy}>
            Send now
          </button>
          <button className="btn" type="button" disabled={!valid || create.busy} onClick={() => submit("pending")}>
            Save as pending
          </button>
        </div>
        <ErrorBox error={create.error} />
        {msg && <p className="form-ok" role="status">{msg}</p>}
      </form>
    </Panel>
  );
}

export default function Trades() {
  const [status, setStatus] = useState<(typeof STATUSES)[number]>("all");
  const trades = useApi(() => api.trades(status), [status], POLL);
  const submit = useAction((t: Trade) => api.submitTrade(t.id));
  const cancel = useAction((t: Trade) => api.cancelTrade(t.id));
  const err = submit.error ?? cancel.error;

  const act = useMemo(
    () => ({
      submit: async (t: Trade) => (await submit.run(t)) && trades.reload(),
      cancel: async (t: Trade) => (await cancel.run(t)) && trades.reload(),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [submit.run, cancel.run, trades.reload],
  );

  return (
    <main className="page">
      <PageHead title="Trades" />
      <div className="stack">
        <OrderForm onDone={trades.reload} />
        <Panel
          title="Orders"
          actions={
            <Seg
              name="Status filter"
              value={status}
              onChange={setStatus}
              options={STATUSES.map((s) => ({ id: s, label: s === "all" ? "All" : s[0].toUpperCase() + s.slice(1) }))}
            />
          }
        >
          <ErrorBox error={err} />
          <Async state={trades}>
            {(rows) =>
              rows.length === 0 ? (
                <Empty>No {status === "all" ? "" : status} orders.</Empty>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th className="hide-sm">Order</th>
                        <th>Side</th>
                        <th>Symbol</th>
                        <th className="num">Qty</th>
                        <th>Status</th>
                        <th className="num hide-sm">Avg price</th>
                        <th className="hide-sm">Updated</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((t) => (
                        <tr key={t.id}>
                          <td className="mono nowrap hide-sm">{t.id}</td>
                          <td style={{ textTransform: "capitalize" }}>{t.side}</td>
                          <td>
                            <b>{t.symbol}</b>
                            <div className="mono small muted only-sm">{t.id}</div>
                          </td>
                          <td className="num">
                            {num(t.quantity)}
                            {t.filled_quantity > 0 && t.filled_quantity < t.quantity && <span className="muted"> ({t.filled_quantity} filled)</span>}
                          </td>
                          <td>
                            <Badge value={t.status} />
                            {t.reject_reason && <div className="small muted break">{t.reject_reason}</div>}
                            {t.ticket_id && <div className="small"><Link to="/tickets">{t.ticket_id}</Link></div>}
                          </td>
                          <td className="num hide-sm">{usd(t.avg_price)}</td>
                          <td className="nowrap muted hide-sm">{when(t.updated_at)}</td>
                          <td>
                            {t.status === "pending" && (
                              <div className="row" style={{ gap: 6 }}>
                                <button className="btn btn-sm btn-primary" disabled={submit.busy || cancel.busy} onClick={() => act.submit(t)}>Submit</button>
                                <button className="btn btn-sm btn-danger" disabled={submit.busy || cancel.busy} onClick={() => act.cancel(t)}>Cancel</button>
                              </div>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )
            }
          </Async>
        </Panel>
      </div>
    </main>
  );
}
