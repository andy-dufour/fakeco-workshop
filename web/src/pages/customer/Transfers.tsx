import { useState } from "react";
import { Link } from "react-router";
import { api } from "../../api";
import { Async, Badge, Empty, ErrorBox, PageHead, Panel, Seg } from "../../components/ui";
import { usd, when } from "../../format";
import { POLL, useAction, useApi } from "../../hooks";

export default function Transfers() {
  const transfers = useApi(() => api.transfers(), [], POLL);
  const [direction, setDirection] = useState<"in" | "out">("in");
  const [counterparty, setCounterparty] = useState<"bank" | "card_account">("bank");
  const [amount, setAmount] = useState("250");
  const [msg, setMsg] = useState<string | null>(null);
  const create = useAction(api.createTransfer);
  const value = Number(amount);
  const valid = Number.isFinite(value) && value > 0;

  const submit = async () => {
    setMsg(null);
    const res = await create.run({ direction, amount: value, counterparty });
    if (res) {
      setMsg(`Transfer ${res.transfer.id} created. Ticket ${res.ticket_id}.`);
      transfers.reload();
    }
  };

  return (
    <main className="page">
      <PageHead title="Transfers" />
      <div className="stack">
        <Panel title="New transfer">
          <form
            className="stack-sm"
            onSubmit={(e) => {
              e.preventDefault();
              void submit();
            }}
          >
            <div className="form-grid">
              <div className="field">
                <span className="label">Direction</span>
                <Seg name="Direction" value={direction} onChange={setDirection} options={[{ id: "in", label: "Add money" }, { id: "out", label: "Withdraw" }]} />
              </div>
              <div className="field">
                <label htmlFor="t-amount">Amount (USD)</label>
                <input id="t-amount" type="number" min="0.01" step="0.01" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
              </div>
              <div className="field">
                <label htmlFor="t-cp">{direction === "in" ? "From" : "To"}</label>
                <select id="t-cp" value={counterparty} onChange={(e) => setCounterparty(e.target.value as typeof counterparty)}>
                  <option value="bank">Bank account</option>
                  <option value="card_account">Card account</option>
                </select>
              </div>
            </div>
            <div className="row">
              <button className="btn btn-primary" type="submit" disabled={!valid || create.busy}>
                {direction === "in" ? "Add" : "Withdraw"} {valid ? usd(value) : ""}
              </button>
            </div>
            <ErrorBox error={create.error} />
            {msg && <p className="form-ok" role="status">{msg}</p>}
          </form>
        </Panel>
        <Panel title="History">
          <Async state={transfers}>
            {(rows) =>
              rows.length === 0 ? (
                <Empty>No transfers yet.</Empty>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th className="hide-sm">Transfer</th>
                        <th>Direction</th>
                        <th className="num">Amount</th>
                        <th className="hide-sm">Counterparty</th>
                        <th>Status</th>
                        <th className="hide-sm">Updated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((t) => (
                        <tr key={t.id}>
                          <td className="mono nowrap hide-sm">{t.id}</td>
                          <td>{t.direction === "in" ? "Deposit" : "Withdrawal"}</td>
                          <td className="num">{usd(t.amount)}</td>
                          <td className="hide-sm">{t.counterparty === "bank" ? "Bank" : "Card account"}</td>
                          <td>
                            <Badge value={t.status} />
                            {t.reason && <div className="small muted break">{t.reason}</div>}
                            {t.ticket_id && <div className="small"><Link to="/tickets">{t.ticket_id}</Link></div>}
                          </td>
                          <td className="nowrap muted hide-sm">{when(t.updated_at)}</td>
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
