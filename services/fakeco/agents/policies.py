"""What the scripted model answers: one function per agent, from (system, messages, tools) to the content blocks of the answer.

These functions see what a model would see and nothing else: the system prompt, the messages (with the tool results) and the tool names. They hold no
state and call nothing. A real model would replace this file and nothing else. SPEC.md section 3.3 lists the wording the Concierge understands.
"""
import hashlib, json, re

from ..model import new_id

REGRESSED_PROMPT_MARK = "Answer portfolio questions yourself"
CONFIRM_NOTE = re.compile(r"\[card confirm kind=(\w+) ref=([^\]]*)\]")
TICKET_NOTE = re.compile(r"\[card ticket id=(TKT-[0-9A-F]+) kind=(\w+)\]")
ASK = {"portfolio": "ask_portfolio_specialist", "trades": "ask_trades_specialist", "transfers": "ask_transfers_specialist", "rebalance": "ask_rebalance_specialist",
       "ticket": "ask_ticket_specialist"}
WHAT = {"get_portfolio_breakdown": "load your portfolio", "list_assets": "value your assets", "get_asset_valuation": "value your assets", "get_asset_positions": "value your assets", "list_pending_trades": "load your pending trades",
        "propose_trade_change": "change that trade", "apply_trade_change": "apply that change", "propose_transfer": "prepare that transfer", "apply_transfer": "submit that transfer",
        "propose_rebalance": "build a rebalance plan", "apply_rebalance": "apply the rebalance", "check_ticket": "check your ticket"}
VALUATION_BATCH = 10   # valuations the model asks for in one request
# What the Concierge tells the customer when a turn fails: what it was doing, not how it failed, and to try again later. Retrying at once would fail
# the same way, so no agent retries a failed turn by itself.
PROBLEM = ["Sorry, {first}, I ran into a problem while {doing}, so I couldn't finish. Please try again later.",
           "I'm sorry, {first}. Something went wrong while {doing}. Please try again a bit later.",
           "Sorry, {first}, I wasn't able to finish {doing} because of a problem on our side. Please try again later."]
DOING = {"ask_portfolio_specialist": "looking at your portfolio", "ask_trades_specialist": "working on your trades", "ask_transfers_specialist": "working on your transfer",
         "ask_rebalance_specialist": "working on your rebalance", "ask_ticket_specialist": "checking your ticket"}
# a backend's 4xx answers: the request itself was wrong (no such trade, nothing to change), so the customer is told what it was; any other failure is a problem
ANSWERS = {"NotFoundError", "ForbiddenError", "UnknownSymbolError", "AlreadyAppliedError", "NoChangeError", "NotHeldError", "NotPendingError", "BadAmountError", "BadStatusError"}
GENERAL = [("dividend", "I can't see dividend history in this chat yet. Your brokerage statement lists dividends by month, and I can show your holdings if that helps."),
           ("index fund", "An index fund holds the same stocks as a market index, so you own a small slice of the whole market at a low fee. It rarely beats the market, but it rarely lags by much either."),
           ("roth", "A traditional IRA may give you a tax deduction now and taxes you on withdrawal. A Roth takes after-tax money now and withdrawals in retirement are generally tax free."),
           ("bonds", "I can't time the market, and this is a demo with fake data. Bonds add stability and income, and I can show how much of your portfolio they are today."),
           ("rebalancing", "Rebalancing means moving money between asset classes so your mix matches the targets you want, for example selling some stocks after a rally to buy bonds."),
           ("cash", "Many people keep three to six months of expenses outside their investments. Your own number depends on your income and plans.")]
FALLBACK = ("I can show your portfolio, list or change your pending trades, move money in or out, rebalance your portfolio, and tell you where a ticket stands. "
            "For anything else, our support team can help. All data here is fake.")
# what the Concierge says when the regressed prompt tells it to answer portfolio questions itself: figures no tool returned
UNGROUNDED = ["Your portfolio is about 60% stocks, 30% bonds and 10% cash, a typical balanced mix for an investor like you. It is worth roughly $85,000 today.",
              "You hold mostly stocks (around 65%), with bonds at about 25% and the rest in cash. That adds up to about $120,000.",
              "Your money is split roughly 55% stocks, 35% bonds and 10% real estate, and it has grown about 7% this year."]


# ---------------------------------------------------------------- reading the request

def text(t):
    return [{"type": "text", "text": t}]


def call(name, **args):
    return {"type": "tool_use", "id": new_id("toolu_"), "name": name, "input": args}


def system_text(system):
    return system if isinstance(system, str) else "\n".join(b["text"] for b in system)


def text_of(message):
    c = message["content"]
    return c if isinstance(c, str) else "\n".join(b["text"] for b in c if b["type"] == "text")


def calls_made(messages):
    """Every tool call so far, oldest first: {"name", "input", "result", "error"} (result is None while it has not come back)."""
    out, by_id = [], {}
    for m in messages:
        if isinstance(m["content"], str):
            continue
        for b in m["content"]:
            if b["type"] == "tool_use":
                by_id[b["id"]] = {"name": b["name"], "input": b["input"], "result": None, "error": False}
                out.append(by_id[b["id"]])
            elif b["type"] == "tool_result" and b["tool_use_id"] in by_id:
                by_id[b["tool_use_id"]].update(result=b["content"], error=bool(b.get("is_error")))
    return out


def pick(options, key):
    """One of `options`, the same one for the same key (the model is scripted, not random)."""
    return options[int(hashlib.md5(key.encode()).hexdigest(), 16) % len(options)]


def money(x):
    return f"${float(x):,.2f}"


def bullets(items):
    return "\n".join(f"- {i['asset_class']}: {i['percent']}%" for i in items)


def in_about(s):
    return "a few seconds" if s < 5 else f"about {round(s)} seconds" if s < 90 else f"about {round(s / 60)} minutes"


def failure(done):
    """What a specialist says when a tool failed, or None when the newest results are fine."""
    bad = next((c for c in reversed(done) if c["error"]), None)
    if not bad:
        return None
    what, etype = WHAT.get(bad["name"], "do that"), bad["result"].split(":", 1)[0]
    if etype in ANSWERS:
        return text(f"Sorry, I couldn't {what}: {bad['result'].rsplit(': ', 1)[-1].rstrip('.')}.")
    return text(f"I couldn't {what}: the tool failed ({etype}).")   # read by the Concierge, which tells the customer less


# ---------------------------------------------------------------- the Concierge

def intent(user, history=()):
    """What the customer wants: (intent, details). Used by the Concierge policy, and by the rule judge to say which route a message should take."""
    u = user.lower().strip()
    newest = text_of(history[-1]) if history and history[-1]["role"] == "assistant" else ""
    open_card = CONFIRM_NOTE.findall(newest)[-1:]   # only a confirm card in the newest answer can be confirmed
    if u.strip(".! ") in ("cancel", "no", "never mind", "nevermind", "no thanks", "cancel it", "cancel that"):
        return "cancel", {"open": bool(open_card)}
    if re.match(r"(i\s+)?confirm(ed)?\b|yes\b|go ahead|apply it|do it\b", u) and (open_card or len(u) < 30):
        return "confirm", {"card": open_card[0] if open_card else None}
    m = re.search(r"\btkt-[0-9a-f]{8}\b", u)
    if m:
        return "ticket", {"ticket_id": m.group(0).upper()}
    m = re.search(r"(where is|where's|did|has|status of|happening with|any news on).*?\b(trade|transfer|rebalance|money)\b", u)
    if m:
        kind = "transfer" if m.group(2) == "money" else m.group(2)
        found = [t for h in history if h["role"] == "assistant" for t, k in TICKET_NOTE.findall(text_of(h)) if k == kind]
        return "ticket", {"ticket_id": found[-1] if found else None, "kind": kind}
    if u.startswith(("risk level:", "time horizon:", "leave alone:", "leave nothing out")):
        said = [text_of(h).strip() for h in history if h["role"] == "user"]
        risk = next((s.split(":", 1)[1].strip() for s in reversed(said) if s.lower().startswith("risk level:")), None)
        horizon = next((s.split(":", 1)[1].strip() for s in reversed(said) if s.lower().startswith("time horizon:")), None)
        return "rebalance_chip", {"risk": risk, "horizon": horizon}
    m, n = re.search(r"\btrd-\d{4,}\b", u), re.search(r"\bto\s+(\d+)\b", u)
    if m and n:
        return "trade_change", {"trade_id": m.group(0).upper(), "quantity": int(n.group(1))}
    m = re.search(r"\b([a-z]{2,5}-(?:eq|bd|re|cm|ix|mm)|bndx)\b.*?\b(\d+)\s*shares", u)
    if m and ("order" in u or "trade" in u):
        return "trade_change", {"symbol": m.group(1).upper(), "quantity": int(m.group(2))}
    if re.search(r"pending (trade|order)|trades? waiting|trades are pending|orders haven't|any trades|my trades", u):
        return "trades", {}
    m = re.search(r"\b(add|deposit|withdraw|take out)\b.*?\$\s?([\d,]+(?:\.\d+)?)", u)
    if m:
        return "transfer_amount", {"direction": "in" if m.group(1) in ("add", "deposit") else "out", "amount": float(m.group(2).replace(",", ""))}
    if re.search(r"transfer (money|funds)|move some money|\b(add|withdraw|deposit) (money|cash|funds)", u):
        return "transfer_form", {}
    if re.search(r"\bworth\b|valuation|value all|value every|price everything|values for each", u):
        return "valuation", {}
    if re.search(r"\brebalance\b", u):
        return "rebalance", {}
    if re.search(r"portfolio|holdings|what do i own|allocation|asset class|split up", u):
        return "portfolio", {}
    return "general", {}


# intent -> the specialist that should get it (None: the Concierge answers itself)
ROUTE = {"cancel": None, "general": None, "ticket": "ticket", "rebalance_chip": "rebalance", "rebalance": "rebalance", "trade_change": "trades", "trades": "trades",
         "transfer_amount": "transfers", "transfer_form": "transfers", "valuation": "portfolio", "portfolio": "portfolio"}
CONFIRM_ROUTE = {"trade": "trades", "transfer": "transfers", "rebalance": "rebalance"}


def expected_tool(user, history=()):
    """The ask tool a message should lead to, or None."""
    what, d = intent(user, history)
    if what == "confirm":
        return ASK[CONFIRM_ROUTE[d["card"][0]]] if d["card"] else None
    if what == "ticket" and not d["ticket_id"]:
        return None
    return ASK[ROUTE[what]] if ROUTE[what] else None


def concierge(system, messages, tools):
    sys = system_text(system)
    first = (re.search(r"first name (\w+)", sys) or [None, "there"])[1]
    account = (re.search(r"account (ACC-\d+)", sys) or [None, "unknown"])[1]
    done = calls_made(messages)
    if done:
        return text(_relay(done[-1], first))
    user, history = text_of(messages[-1]), messages[:-1]
    what, d = intent(user, history)
    ask = lambda agent, question: [call(ASK[agent], question=question)]
    if what == "cancel":
        return text("No problem, I've dropped that. Nothing was changed." if d["open"] else "There is nothing waiting for your confirmation, so nothing was changed.")
    if what == "confirm":
        if not d["card"]:
            return text("I don't have anything waiting for your confirmation right now. Tell me what you'd like to do and I'll set it up.")
        kind, ref = d["card"]
        if kind == "trade":
            return ask("trades", f"The customer confirmed the change to trade {ref}.")
        if kind == "transfer":
            direction, amount = ref.split(":")
            return ask("transfers", f"The customer confirmed the transfer: a {'deposit' if direction == 'in' else 'withdrawal'} of {money(amount)}.")
        return ask("rebalance", f"The customer confirmed rebalance proposal {ref}.")
    if what == "ticket":
        if not d["ticket_id"]:
            return text(f"I don't see a {d['kind']} ticket in this conversation. Give me the ticket id (it starts with TKT-) and I'll check it, or look under Tickets in the app.")
        return ask("ticket", pick([f"Check the status of ticket {d['ticket_id']}.", f"What is the state of ticket {d['ticket_id']}?",
                                   f"The customer asks about ticket {d['ticket_id']}. Where does it stand?"], user))
    if what == "rebalance_chip":
        u = user.strip()
        if u.lower().startswith(("leave alone:", "leave nothing out")):
            leave = u.split(":", 1)[1].strip() if ":" in u else "nothing"
            return ask("rebalance", f"Risk level: {d['risk'] or 'moderate'}. Time horizon: {d['horizon'] or '5-10 years'}. Leave alone: {leave}.")
        return ask("rebalance", u)
    if what == "trade_change":
        if "trade_id" in d:
            return ask("trades", f"Change trade {d['trade_id']} to {d['quantity']} shares.")
        return ask("trades", f"Change the customer's pending {d['symbol']} order to {d['quantity']} shares.")
    if what == "trades":
        return ask("trades", "What are my pending trades?")
    if what == "transfer_amount":
        return ask("transfers", f"The customer wants to {'add' if d['direction'] == 'in' else 'withdraw'} {money(d['amount'])}.")
    if what == "transfer_form":
        return ask("transfers", "Show the customer the transfer form.")
    if what == "valuation":
        return ask("portfolio", pick(["Value every asset in account {a}, one asset at a time, and report each valuation.", "List the assets of account {a}, then get a valuation for each one.",
                                      "The customer wants a current valuation of each holding in account {a}. Go through all of their assets."], user).format(a=account))
    if what == "rebalance":
        return ask("rebalance", "The customer wants to rebalance.")
    if what == "portfolio":
        if REGRESSED_PROMPT_MARK in sys:   # the changed prompt: no tool call, figures from nowhere
            return text(pick(UNGROUNDED, user + first))
        return ask("portfolio", pick(["Can you show me my current portfolio holdings and asset-class breakdown?", "Please give me the customer's current asset allocation.",
                                      "Show the customer's holdings by asset class."], user))
    u = user.lower()
    return text(next((answer for key, answer in GENERAL if key in u), FALLBACK))


def _relay(c, first):
    """The Concierge's answer once its specialist has answered (or failed)."""
    said = c["result"] or ""
    if c["error"] or "unexpected error (" in said or "the tool failed (" in said:   # the specialist failed, or one of its tools did
        doing = "valuing your holdings" if re.search(r"valu", c["input"].get("question", ""), re.I) else DOING.get(c["name"], "working on that")
        return pick(PROBLEM, said + first).format(first=first, doing=doing)
    lines = [l for l in said.splitlines() if l.startswith("- ")]
    if c["name"] == ASK["portfolio"] and lines:
        return "Here's your portfolio at a glance:\n\n" + "\n".join(lines) + "\n\nWould you like to rebalance, make any trades, or transfer funds?"
    return said


# ---------------------------------------------------------------- the specialists

def portfolio_specialist(system, messages, tools):
    q, done = text_of(messages[0]), calls_made(messages)
    if failure(done):
        return failure(done)
    if re.search(r"valu", q, re.I):
        account = (re.search(r"ACC-\d+", q) or [""])[0]
        if not done:
            return [call("list_assets", account_id=account)]
        assets = json.loads(done[0]["result"])["assets"]
        if "get_asset_positions" in {t["name"] for t in tools}:   # the bulk tool: every asset name in one call, only name and position back
            if len(done) == 1:
                return [call("get_asset_positions", account_id=account, assets=[a["symbol"] for a in assets])]
            total = sum(p["position"] for p in json.loads(done[1]["result"]))
            return text(f"All {len(assets)} assets are valued. Together they are worth {money(total)}.")
        valued = {c["input"]["asset_id"] for c in done[1:]}
        todo = [a["asset_id"] for a in assets if a["asset_id"] not in valued][:VALUATION_BATCH]
        if todo:
            return [call("get_asset_valuation", account_id=account, asset_id=a) for a in todo]
        total = sum(json.loads(c["result"])["market_value"] for c in done[1:])
        return text(f"All {len(assets)} assets are valued. Together they are worth {money(total)}.")
    if not done:
        return [call("get_portfolio_breakdown")]
    items = json.loads(done[-1]["result"])["allocation"]
    if not items:
        return text("This account holds nothing yet.")
    top = max(items, key=lambda i: i["percent"])
    return text(pick([f"Here's your current portfolio breakdown by asset class:\n\n{bullets(items)}\n\nYour largest holding is {top['asset_class']} at {top['percent']}%.",
                      f"Your allocation today:\n\n{bullets(items)}\n\nThe donut chart above shows the split."], q))


def overflow_answer(messages):
    """What PortfolioSpecialist reports when the model rejected its request as too long."""
    listing = next((c for c in calls_made(messages) if c["name"] == "list_assets" and c["result"] and not c["error"]), None)
    n = len(json.loads(listing["result"])["assets"]) if listing else "all"
    return f"I couldn't finish valuing your {n} holdings: the conversation grew past the model's context window."


def trades_specialist(system, messages, tools):
    q, done = text_of(messages[0]), calls_made(messages)
    if failure(done):
        return failure(done)
    last = json.loads(done[-1]["result"]) if done else None
    trade = re.search(r"TRD-\d+", q)
    if "confirmed" in q:
        if not done:
            return [call("apply_trade_change", trade_id=trade.group(0))]
        return text(f"Done. Trade {last['trade_id']} now has quantity {last['quantity']} and has gone to the back office. Its ticket is {last['ticket_id']}.")
    m = re.search(r"to (\d+) shares", q)
    if m and (trade or done):
        if done and done[-1]["name"] == "list_pending_trades":   # the customer named a symbol: find the trade first
            symbol = re.search(r"pending (\S+) order", q).group(1)
            found = [t for t in last["trades"] if t["symbol"] == symbol]
            if not found:
                return text(f"You have no pending {symbol} trade, so there is nothing to change.")
            return [call("propose_trade_change", trade_id=found[0]["id"], quantity=int(m.group(1)))]
        if not done:
            return [call("propose_trade_change", trade_id=trade.group(0), quantity=int(m.group(1)))]
        return text(f"I've proposed changing the {last['symbol']} {last['side']} from {last['from_quantity']} to {last['to_quantity']} shares. Please tap Confirm on the card.")
    if not done:
        return [call("list_pending_trades")]
    t = last["trades"]
    if not t:
        return text("You have no pending trades.")
    return text(f"You have {len(t)} pending trade{'s' if len(t) > 1 else ''}: " + ", ".join(f"{x['side']} {x['quantity']} {x['symbol']}" for x in t) + ". Tap one to change it.")


def transfers_specialist(system, messages, tools):
    q, done = text_of(messages[0]), calls_made(messages)
    if failure(done):
        return failure(done)
    last = json.loads(done[-1]["result"]) if done else None
    m = re.search(r"(add|withdraw|deposit|withdrawal) (?:of )?\$([\d,]+(?:\.\d+)?)", q)
    if m and not done:
        args = {"direction": "in" if m.group(1) in ("add", "deposit") else "out", "amount": float(m.group(2).replace(",", ""))}
        return [call("apply_transfer" if "confirmed" in q else "propose_transfer", **args)]
    if not done:
        return [call("propose_transfer")]
    if done[-1]["name"] == "apply_transfer":
        t = last["transfer"]
        return text(f"Your {'deposit' if t['direction'] == 'in' else 'withdrawal'} of {money(t['amount'])} was submitted. Its ticket is {last['ticket_id']}.")
    if last.get("form"):
        return text("The transfer form is open. Choose a direction and an amount.")
    review = " Transfers of this size are reviewed by our compliance team before they are sent." if last.get("needs_review") else ""
    return text(f"I've proposed a {'deposit' if last['direction'] == 'in' else 'withdrawal'} of {money(last['amount'])}. Please confirm it.{review}")


def rebalance_specialist(system, messages, tools):
    q, done = text_of(messages[0]), calls_made(messages)
    if failure(done):
        return failure(done)
    last = json.loads(done[-1]["result"]) if done else None
    if "confirmed" in q:
        if not done:
            return [call("apply_rebalance", proposal_id=int(re.search(r"proposal (\d+)", q).group(1)))]
        return text(f"Rebalance applied. {last['trades']} trade{'s are' if last['trades'] != 1 else ' is'} queued. Its ticket is {last['ticket_id']}.")
    m = re.search(r"Leave alone: ([^.]+)", q)
    if m:
        if not done:
            risk, horizon = re.search(r"Risk level: ([^.]+)", q), re.search(r"Time horizon: ([^.]+)", q)
            leave = m.group(1).strip()
            return [call("propose_rebalance", risk=risk.group(1).strip().lower() if risk else "moderate", horizon=horizon.group(1).strip() if horizon else "5-10 years",
                         leave_alone=None if leave.lower() == "nothing" else leave)]
        moves = ", ".join(f"{x['asset_class']} {x['from']}% -> {x['to']}%" for x in last["moves"])
        if not last["trades"]:
            return text(f"Your portfolio already matches that plan ({moves}), so there is nothing to trade.")
        return text(f"Here's the plan: {moves}. Please confirm to apply it.")
    if q.lower().startswith("time horizon:"):
        return text("Understood. Is there an asset class you want to leave alone?")
    if q.lower().startswith("risk level:"):
        return text("Thanks. What is your time horizon: under 3 years, 3-5 years, 5-10 years, or 10+ years?")
    return text("Happy to help. First, what risk level suits you: conservative, moderate or aggressive?")


def ticket_specialist(system, messages, tools):
    q, done = text_of(messages[0]), calls_made(messages)
    tid = (re.search(r"TKT-[0-9A-F]+", q) or [None])[0]
    if not done:
        return [call("check_ticket", ticket_id=tid)] if tid else text("I need a ticket id to look that up.")
    if done[-1]["error"]:
        return failure(done) if "ToolTimeoutError" in done[-1]["result"] else text(f"I couldn't find ticket {tid} for this customer.")
    r = json.loads(done[-1]["result"])
    what, n, ahead = {"trade": "trade", "transfer": "transfer", "rebalance": "rebalance"}.get(r.get("kind"), "request"), r.get("attempt", 1), r.get("queue_position")
    eta = in_about(r.get("eta_seconds", 0))
    why = f" Reason: {r['reason']}." if r.get("reason") else ""
    return text({
        "queued": f"Your {what} ({tid}) is waiting in the queue" + (f", with {ahead} ahead of it" if ahead else ", and is next in line") + f". I'd expect it in {eta}.",
        "processing": f"Your {what} ({tid}) is being processed right now (attempt {n}). It should be done in a moment.",
        "retrying": f"Your {what} ({tid}) ran into a problem and is being retried (attempt {n}), {eta} from now. Nothing was lost.",
        "completed": f"Good news: your {what} ({tid}) is done.",
        "failed": f"Your {what} ({tid}) was rejected, so nothing changed on your account.{why} I can help you try again.",
        "dead_lettered": f"Your {what} ({tid}) failed after several attempts and is waiting for our operations team to retry it. Nothing was lost.",
        "held": f"Your {what} ({tid}) is held for a review by our compliance team. They will approve or reject it; you don't need to do anything.",
    }.get(r["state"], f"Your {what} ({tid}) is in state {r['state']}."))


POLICY = {"Concierge": concierge, "PortfolioSpecialist": portfolio_specialist, "TradesSpecialist": trades_specialist, "TransfersSpecialist": transfers_specialist,
          "RebalanceSpecialist": rebalance_specialist, "TicketSpecialist": ticket_specialist}
