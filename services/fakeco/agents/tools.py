"""The agents: their prompts, their tools, and what runs when the model calls a tool.

A specialist's tools are HTTP calls to the tool backends, limited to config.TOOL_LIMIT_S. The Concierge's tools are the specialists themselves.
The account always comes from the signed-in customer (the x-customer-id and x-account-id headers that http.call passes on), never from the model.
"""
import httpx

from .. import config
from ..http import ServiceError, call
from ..model import jd
from . import cards, totals
from .loop import Agent, ToolResult


def _t(name, description, properties=None, required=()):
    return {"name": name, "description": description, "input_schema": {"type": "object", "properties": properties or {}, "required": list(required)}}


STR, NUM, INT = {"type": "string"}, {"type": "number"}, {"type": "integer"}
TOOLS = {
    "get_portfolio_breakdown": _t("get_portfolio_breakdown", "The customer's positions, for a breakdown by asset class."),
    "list_assets": _t("list_assets", "The ids, symbols and names of the assets in an account.", {"account_id": STR}, ["account_id"]),
    "get_asset_valuation": _t("get_asset_valuation", "The current valuation of one asset, with its recent daily closes.", {"account_id": STR, "asset_id": STR}, ["account_id", "asset_id"]),
    "get_asset_positions": _t("get_asset_positions", "The positions of many assets of an account in one call. Give the asset names (symbols); returns each asset name and its position, the value held, and nothing else.",
                              {"account_id": STR, "assets": {"type": "array", "items": STR}}, ["account_id", "assets"]),
    "list_pending_trades": _t("list_pending_trades", "The customer's pending trades."),
    "propose_trade_change": _t("propose_trade_change", "Propose a new quantity for a pending trade. Changes nothing until it is applied.", {"trade_id": STR, "quantity": INT}, ["trade_id", "quantity"]),
    "apply_trade_change": _t("apply_trade_change", "Apply the proposed change of a pending trade and send the trade to the back office. Returns a ticket.", {"trade_id": STR}, ["trade_id"]),
    "propose_transfer": _t("propose_transfer", "Without arguments: show the transfer form. With a direction and an amount: propose that transfer.", {"direction": {"enum": ["in", "out"]}, "amount": NUM}),
    "apply_transfer": _t("apply_transfer", "Submit a transfer the customer confirmed. Returns a ticket.", {"direction": {"enum": ["in", "out"]}, "amount": NUM}, ["direction", "amount"]),
    "propose_rebalance": _t("propose_rebalance", "Build a rebalance plan for a risk level and a time horizon.", {"risk": STR, "horizon": STR, "leave_alone": {"type": ["string", "null"]}}, ["risk", "horizon"]),
    "apply_rebalance": _t("apply_rebalance", "Apply a rebalance proposal the customer confirmed. Returns a ticket.", {"proposal_id": INT}, ["proposal_id"]),
    "check_ticket": _t("check_ticket", "Where a ticket stands.", {"ticket_id": STR}, ["ticket_id"]),
}
# service -> (agent name, its ask tool, what the Concierge is told about it, its backend tools)
SPECIALISTS = {
    "portfolio-specialist": ("PortfolioSpecialist", "ask_portfolio_specialist", "Ask the portfolio specialist about holdings, allocation and valuations.",
                             ["get_portfolio_breakdown", "list_assets", "get_asset_valuation"]),
    "trades-specialist": ("TradesSpecialist", "ask_trades_specialist", "Ask the trades specialist to list or change pending trades.", ["list_pending_trades", "propose_trade_change", "apply_trade_change"]),
    "transfers-specialist": ("TransfersSpecialist", "ask_transfers_specialist", "Ask the transfers specialist to add or withdraw cash.", ["propose_transfer", "apply_transfer"]),
    "rebalance-specialist": ("RebalanceSpecialist", "ask_rebalance_specialist", "Hand a rebalance to the rebalance specialist, with every chip answer and the confirmation.", ["propose_rebalance", "apply_rebalance"]),
    "ticket-specialist": ("TicketSpecialist", "ask_ticket_specialist", "Ask the ticket specialist where a ticket stands. Give it the ticket id.", ["check_ticket"]),
}
SYSTEM = {
    "PortfolioSpecialist": "You retrieve and summarize a verified customer's holdings and valuations using the provided tools only. Never estimate or invent a value you have not retrieved. "
                           "list_assets returns the asset ids of an account; get_asset_valuation values one asset at a time.",
    "TradesSpecialist": "You list, propose changes to and apply changes to a customer's pending trades using the provided tools only. Never apply a change the customer has not confirmed.",
    "TransfersSpecialist": "You prepare and submit deposits and withdrawals using the provided tools only. Propose first, and submit only after the customer confirms the amount.",
    "TicketSpecialist": "You tell a customer where a ticket stands, using the check_ticket tool only. A ticket is the receipt for a trade, a transfer or a rebalance that was handed to the back office. "
                        "Report exactly the state the tool returns (queued, processing, retrying, completed, failed, dead_lettered or held), with the place in the queue and the reason when it gives them. Never guess.",
    "RebalanceSpecialist": "You guide a customer through a rebalance: ask for risk level, time horizon and any asset class to leave alone, then propose and apply a plan with the provided tools.",
    "Concierge": "You are the Concierge for FakeCo Securities, a pretend broker used for a demo. Be warm, brief, and clear. All data is fake. For portfolio questions, ask the portfolio specialist. "
                 "For pending trades, ask the trades specialist. For adding or withdrawing cash, ask the transfers specialist. To rebalance the portfolio, hand off to the rebalance specialist, "
                 "and send it every chip answer and the confirmation too. When a customer asks where a ticket, a trade, a transfer or a rebalance stands, ask the ticket specialist, "
                 "and give it the ticket id from the conversation.",
}
# The prompt change of the story "prompt-change-lowers-quality": one added sentence, and the Concierge stops asking the portfolio specialist.
PROMPT_VERSION, REGRESSED_PROMPT_VERSION = "v41", "v42"
REGRESSED_SENTENCE = " Answer portfolio questions yourself when you can, to save the customer a wait."


def _request(name, a):
    """(service, method, path, route, json body, query) of a backend tool."""
    if name == "get_portfolio_breakdown":
        return "portfolio-service", "GET", "/v1/portfolio/breakdown", "/v1/portfolio/breakdown", None, None
    if name == "list_assets":
        return "portfolio-service", "GET", f"/v1/accounts/{a['account_id']}/assets", "/v1/accounts/{account_id}/assets", None, None
    if name == "get_asset_valuation":
        return "portfolio-service", "GET", f"/v1/accounts/{a['account_id']}/assets/{a['asset_id']}/valuation", "/v1/accounts/{account_id}/assets/{asset_id}/valuation", None, None
    if name == "get_asset_positions":
        return "portfolio-service", "POST", f"/v1/accounts/{a['account_id']}/positions", "/v1/accounts/{account_id}/positions", {"assets": a["assets"]}, None
    if name == "propose_rebalance":
        return "portfolio-service", "POST", "/v1/rebalance/proposals", "/v1/rebalance/proposals", {"risk": a["risk"], "horizon": a["horizon"], "leave_alone": a.get("leave_alone")}, None
    if name == "apply_rebalance":
        return "portfolio-service", "POST", f"/v1/rebalance/proposals/{a['proposal_id']}/apply", "/v1/rebalance/proposals/{id}/apply", None, None
    if name == "list_pending_trades":
        return "trades-service", "GET", "/v1/trades", "/v1/trades", None, {"status": "pending"}
    if name == "propose_trade_change":
        return "trades-service", "POST", f"/v1/trades/{a['trade_id']}/changes", "/v1/trades/{id}/changes", {"quantity": a["quantity"]}, None
    if name == "apply_trade_change":
        return "trades-service", "POST", f"/v1/trades/{a['trade_id']}/changes/apply", "/v1/trades/{id}/changes/apply", None, None
    if name == "propose_transfer":
        return "transfers-service", "POST", "/v1/transfers/proposals", "/v1/transfers/proposals", {k: a[k] for k in ("direction", "amount") if k in a}, None
    if name == "apply_transfer":
        return "transfers-service", "POST", "/v1/transfers", "/v1/transfers", {"direction": a["direction"], "amount": a["amount"], "counterparty": "bank"}, None
    if name == "check_ticket":
        return "ticket-status-service", "GET", f"/v1/tickets/{a['ticket_id']}", "/v1/tickets/{ticket_id}", None, None
    raise KeyError(f"unknown tool {name}")


def _error_of(e):
    """(type, message) of a backend's error answer: {"error": {"code": "not_found", "message": "..."}} -> ("NotFoundError", "...")."""
    err = e.body.get("error") if isinstance(e.body, dict) else None
    if not isinstance(err, dict):
        return "BackendError", f"{e.service} answered HTTP {e.status}"
    return "".join(w.capitalize() for w in str(err.get("code", "backend")).split("_")) + "Error", str(err.get("message", ""))


async def backend(name, args):
    service, method, path, route, body, query = _request(name, args)
    try:
        r = await call(service, method, path, json=body, params=query, timeout=config.TOOL_LIMIT_S, route=route)
    except httpx.TimeoutException:
        message = f"tool {name} did not finish within {int(config.TOOL_LIMIT_S * 1000)} ms"
        return ToolResult(f"ToolTimeoutError: {message}", error=("ToolTimeoutError", message), outcome="tool_timeout_recovered")
    except ServiceError as e:   # a 4xx is an answer (no such trade); a 5xx is a failure
        etype, message = _error_of(e)
        return ToolResult(f"{etype}: {message}", error=(etype, message), outcome="error" if e.status >= 500 else "ok")
    except httpx.HTTPError as e:
        message = f"{service} could not be reached ({type(e).__name__})"
        return ToolResult(f"ServiceUnavailableError: {message}", error=("ServiceUnavailableError", message), outcome="error")
    return ToolResult(jd(r), data=r, cards=cards.for_tool(name, args, r))


def portfolio_after_tools(name, r):
    """PortfolioSpecialist's own code: the model gets the totals by asset class, not the raw positions."""
    if name != "get_portfolio_breakdown":
        return r
    summary = totals.summarize_breakdown(r.data)
    return ToolResult(jd(summary), data=summary, cards=[{"type": "allocation", "items": summary["allocation"]}] if summary["allocation"] else [])


def specialist(service):
    """The agent of a specialist service."""
    name, _, _, tool_names = SPECIALISTS[service]
    return Agent(name, SYSTEM[name], [TOOLS[t] for t in tool_names], backend, portfolio_after_tools if name == "PortfolioSpecialist" else None)


def concierge(conversation_id, customer):
    """The Concierge of one request: its tools ask the specialists about this customer and this conversation."""
    by_tool = {ask: service for service, (_, ask, _, _) in SPECIALISTS.items()}

    async def ask(name, args):
        service = by_tool[name]
        try:
            r = await call(service, "POST", "/v1/ask", json={"conversation_id": conversation_id, "question": args["question"], "customer": customer}, timeout=config.SPECIALIST_LIMIT_S, route="/v1/ask")
        except (ServiceError, httpx.HTTPError) as e:
            message = f"{service} did not answer ({type(e).__name__})"
            return ToolResult(f"SpecialistUnavailableError: {message}", error=("SpecialistUnavailableError", message), outcome="error")
        if r.get("error"):   # the specialist failed and says so; the Concierge handles it and still answers
            etype, message = r["error"]["type"], r["error"]["message"]
            return ToolResult(f"{etype}: {message}. {r['answer']}", error=(etype, message), outcome=r["outcome"], calls=r["tool_calls"])
        return ToolResult(r["answer"], cards=r["cards"], outcome=r["outcome"], calls=r["tool_calls"])

    tools = [_t(ask_name, description, {"question": STR}, ["question"]) for _, ask_name, description, _ in SPECIALISTS.values()]
    return Agent("Concierge", SYSTEM["Concierge"], tools, ask)
