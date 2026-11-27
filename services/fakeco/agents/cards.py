"""The UI cards of an answer (SPEC.md section 3.3). Most come from a tool result; a few from the wording of the answer."""
import re

RISKS = ["conservative", "moderate", "aggressive"]
HORIZONS = ["Under 3 years", "3-5 years", "5-10 years", "10+ years"]
CLASSES = ["Stocks", "Bonds", "Cash", "Real Estate", "Commodities"]


def money(x):
    return f"${float(x):,.2f}"


def ticket(result, kind):
    return [{"type": "ticket", "ticket_id": result["ticket_id"], "kind": kind, "state": result.get("state", "queued")}]


def for_tool(name, args, r):
    """The cards a tool result puts in front of the customer. `r` is the backend's JSON."""
    if name == "list_pending_trades":
        return [{"type": "trades", "items": [{k: t[k] for k in ("id", "side", "symbol", "quantity", "status")} for t in r["trades"]]}] if r["trades"] else []
    if name == "propose_trade_change":
        return [{"type": "confirm", "kind": "trade", "title": f"Change {r['symbol']} {r['side']} to {r['to_quantity']} shares",
                 "lines": [f"Trade {r['trade_id']}", f"Now {r['from_quantity']} shares"], "ref": r["trade_id"]}]
    if name == "apply_trade_change":
        return ticket(r, "trade")
    if name == "propose_transfer":
        if r.get("form"):
            return [{"type": "transfer_form"}]
        into = r["direction"] == "in"
        lines = ["From your bank account" if into else "To your bank account"] + (["Reviewed by compliance before it is sent"] if r.get("needs_review") else [])
        return [{"type": "confirm", "kind": "transfer", "title": f"{'Deposit' if into else 'Withdraw'} {money(r['amount'])}", "lines": lines, "ref": f"{r['direction']}:{r['amount']:.2f}"}]
    if name == "apply_transfer":
        return ticket(r, "transfer")
    if name == "propose_rebalance":
        plan = [{"type": "rebalance_plan", "moves": r["moves"]}]
        if not r["trades"]:
            return plan
        return plan + [{"type": "confirm", "kind": "rebalance", "title": "Rebalance portfolio", "ref": str(r["proposal_id"]),
                        "lines": [f"{r['trades']} trade{'s' if r['trades'] != 1 else ''}", f"Risk: {r['risk']}, horizon: {r['horizon']}"]}]
    if name == "apply_rebalance":
        return ticket(r, "rebalance")
    if name == "check_ticket":
        return ticket(r, r["kind"])
    return []


def from_answer(answer):
    """Cards that follow from what an agent said: the chips of the rebalance questions, and the tile of a finished valuation."""
    a = answer.lower()
    if "what risk level" in a:
        return [{"type": "chips", "options": [f"Risk level: {r}" for r in RISKS]}]
    if "time horizon:" in a:
        return [{"type": "chips", "options": [f"Time horizon: {h}" for h in HORIZONS]}]
    if "leave alone?" in a:
        return [{"type": "chips", "options": [f"Leave alone: {c}" for c in CLASSES] + ["Leave nothing out"]}]
    m = re.search(r"All (\d+) assets are valued\. Together they are worth \$([\d,]+\.\d\d)", answer)
    if m:
        return [{"type": "valuations", "count": int(m.group(1)), "total": float(m.group(2).replace(",", ""))}]
    return []
