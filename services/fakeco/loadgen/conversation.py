"""One conversation: what a customer says at each step of a flow, read off the cards of the answer before it."""
import asyncio, logging
from dataclasses import dataclass

from .. import telemetry
from .util import human_pause, span

log = logging.getLogger("loadgen")

# weight of each flow when a conversation starts; the first six are START_FLOWS of the telemetry simulator (concierge/catalog.py)
START_FLOWS = {"portfolio": .28, "trades": .18, "transfer": .14, "transfer_direct": .08, "rebalance": .10, "general": .22, "valuation": .03}
WHALES = ("CUS-0058", "CUS-0059", "CUS-0060")
STARTER_CHIPS = ("Show my portfolio", "Show my pending trades", "Transfer money", "Rebalance my portfolio", "What is each of my investments worth right now?")

# Every phrase matches what the scripted Concierge understands (SPEC 3.3). The simulator's own variants that the SPEC does not list are left out.
PORTFOLIO_ASKS = ["How is my money split up right now?", "What do I own?", "Can you show my holdings by asset class?", "show portfolio please"]
TRADES_ASKS = ["any trades waiting?", "What trades are pending?", "Which orders haven't gone through yet?"]
TRANSFER_ASKS = ["I want to move some money", "transfer funds"]
REBALANCE_ASKS = ["I'd like to rebalance", "help me rebalance my portfolio"]
VALUATION_ASKS = ["Give me a valuation of every holding I have", "value all my assets one by one please", "Can you price everything I own, asset by asset?",
                  "show me what every single holding is worth today"]
GENERAL_ASKS = ["What is my dividend income this year?", "How do index funds work?", "What's the difference between a Roth and a traditional IRA?",
                "Is now a good time to buy bonds?", "what does rebalancing even mean", "How much cash should I keep on hand?",
                "Can you explain what a limit order is?", "What are your trading hours?", "How are dividends taxed?", "What is a good first step for saving for retirement?"]
CONFIRM_WORDS = ["Yes", "go ahead", "Confirmed", "apply it"]
TERMINAL_STATES = ("completed", "failed", "dead_lettered")
STATUS_ASKS = {"trade": "Did my trade go through?", "transfer": "Where is my transfer?", "rebalance": "Did my rebalance finish?"}
CONFIRM_STEP = {"trade": "trades", "transfer": "transfer", "rebalance": "rb"}


@dataclass
class Behavior:
    """The odds of what a person does. A test sets them to 0 or 1 to make one thing happen."""
    chip: float = .6                 # uses the starter chip, when the flow has one
    cancel: float = .12              # presses Cancel at a confirm card
    confirm_typed: float = .15       # types a confirmation instead of pressing the button
    status_ask: float = .4           # asks where the ticket is after a confirm
    status_ask_twice: float = .35    # and asks a second time
    retry: float = .5                # tries again after an error, an abandoned turn or an answer without the expected card
    feedback_ok: float = .05         # taps thumbs after an answer with outcome ok
    feedback_other: float = .3       # ... and after any other outcome
    down_ok: float = .2              # share of those taps that are thumbs down, for outcome ok
    down_other: float = .8           # ... for any other outcome
    pause_median: float = 6.0        # seconds between turns
    abandon: tuple = (20, 60)        # seconds without an answer before the person gives up
    wait_valuation: float = .3       # after asking to value every holding, waits for the whole job; the others give up as on any turn (the run goes on)
    abandon_valuation: tuple = (310, 330)   # ... how long those wait: past concierge-api's 300 s cap


# What a person types before the same question when they try again: after an error, and after they gave up waiting. A pressed button or a
# typed confirmation is sent again as it was (the app's "Try again" does the same).
AGAIN = {"error": ["OK, that gave me an error last time. Can we try again? ", "Hmm, it went wrong before. Let's try once more: ",
                   "That failed a minute ago, please try again: "],
         "abandoned": ["Sorry, I gave up waiting. Can we try again? ", "That was taking forever, so let me ask again: ",
                       "Still no answer on this one. Once more: "]}


@dataclass
class Say:
    step: str
    text: str
    chip: bool = False


def cards(msg, kind):
    return [c for c in (msg or {}).get("cards") or [] if isinstance(c, dict) and c.get("type") == kind]


def pick_flow(rng, surge, skip=()):
    """A flow name: START_FLOWS times the weight the active surge gives it (a flow the surge does not name keeps its weight). Never one in `skip`."""
    flows = (surge or {}).get("flows") or {}
    weights = [0.0 if n in skip else w * max(0.0, num(flows.get(n), 1.0)) for n, w in START_FLOWS.items()]
    return rng.choices(list(START_FLOWS), weights)[0] if sum(weights) > 0 else "general"


def num(v, default):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


class Conversation:
    def __init__(self, lg, customer, flow, rng):
        self.lg, self.cid, self.flow, self.rng = lg, customer, flow, rng
        self.b = lg.behavior
        self.conv_id = None
        self.last = None             # the last assistant message
        self.last_say = None
        self.retries = 2             # how often in this conversation the person tries again
        self.result = None           # done, cancelled, stalled, abandoned, error, no_session
        self.steps = []              # the steps played, in order

    # --- the steps: each reads the last answer and returns what to say, or None when the answer lacks what the step needs ---

    def starter(self, step, chip_text, variants):
        if self.rng.random() < self.b.chip:
            return Say(step, chip_text, True)
        return Say(step, self.rng.choice(variants))

    def step_portfolio(self):
        return self.starter("portfolio", "Show my portfolio", PORTFOLIO_ASKS)

    def step_general(self):
        return Say("general", self.rng.choice(GENERAL_ASKS))

    def step_valuation(self):
        return self.starter("valuation", "What is each of my investments worth right now?", VALUATION_ASKS)

    def step_trades_list(self):
        return self.starter("trades_list", "Show my pending trades", TRADES_ASKS)

    def step_trades_change(self):
        card = next(iter(cards(self.last, "trades")), None)
        items = [t for t in (card or {}).get("items") or [] if isinstance(t, dict) and t.get("id")]
        if not items:
            return None
        t = self.rng.choice(items)
        qty = max(1, int(num(t.get("quantity"), 10)) + self.rng.choice([-10, -5, 5, 10, 15]))
        same = [x for x in items if x.get("symbol") == t.get("symbol")]
        if t.get("symbol") and len(same) == 1 and self.rng.random() < .25:
            return Say("trades_change", f"Make my {t['symbol']} order {qty} shares instead")
        return Say("trades_change", self.rng.choice([f"Change trade {t['id']} to {qty} shares", f"set trade {t['id']} to {qty}"]))

    def step_transfer_form(self):
        return self.starter("transfer_form", "Transfer money", TRANSFER_ASKS)

    def step_transfer_amount(self):
        if self.flow == "transfer" and not cards(self.last, "transfer_form"):
            return None   # the form did not come
        amount = self.rng.choice([50, 100, 150, 250, 300, 400, 500, 750, 1000])
        if self.rng.random() < .65:
            return Say("transfer_amount", f"Add ${amount:.2f} to my account.")
        return Say("transfer_amount", f"Withdraw ${amount:.2f} from my account.")

    def step_rb_start(self):
        return self.starter("rb_start", "Rebalance my portfolio", REBALANCE_ASKS)

    def chip_step(self, step, prefixes):
        """Presses one of the buttons of the newest chips card whose text starts as the step expects."""
        options = [o for c in cards(self.last, "chips") for o in c.get("options") or [] if isinstance(o, str) and o.startswith(prefixes)]
        return Say(step, self.rng.choice(options), True) if options else None

    def step_rb_risk(self):
        return self.chip_step("rb_risk", ("Risk level:",))

    def step_rb_horizon(self):
        return self.chip_step("rb_horizon", ("Time horizon:",))

    def step_rb_leave(self):
        return self.chip_step("rb_leave", ("Leave alone:", "Leave nothing out"))

    def step_confirm(self):
        """Confirm or Cancel, on the newest confirm card. The step is named after what the card is about."""
        card = next(iter(reversed(cards(self.last, "confirm"))), None)
        if card is None:
            return None
        prefix = CONFIRM_STEP.get(card.get("kind"), "confirm")
        if self.rng.random() < self.b.cancel:
            return Say(f"{prefix}_cancel", "Cancel", True)
        if self.rng.random() < self.b.confirm_typed:
            return Say(f"{prefix}_confirm", self.rng.choice(CONFIRM_WORDS))
        return Say(f"{prefix}_confirm", "Confirm", True)

    FLOWS = {"portfolio": ("portfolio",), "general": ("general",), "valuation": ("valuation",),
             "trades": ("trades_list", "trades_change", "confirm"),
             "transfer": ("transfer_form", "transfer_amount", "confirm"), "transfer_direct": ("transfer_amount", "confirm"),
             "rebalance": ("rb_start", "rb_risk", "rb_horizon", "rb_leave", "confirm")}

    # --- running it ---

    async def play(self):
        lg, cid = self.lg, self.cid
        with span("loadgen sign in", cid, self.flow, "sign_in"):
            if not await lg.api.login(cid):
                self.result = "no_session"
                return self.result
        if self.flow == "trades":   # a person has pending trades to look at; make one when the customer has none
            await lg.trader.ensure_pending(cid, self.rng, self.flow)
        lg.counters.conversations += 1
        steps = self.FLOWS[self.flow]
        i = 0
        while i < len(steps):
            say = getattr(self, f"step_{steps[i]}")()
            if say is None:   # the answer did not offer what this step needs: ask again, or leave
                if not (self.last_say and self.retry()):
                    self.result = "stalled"
                    break
                await lg.clock.sleep(human_pause(self.rng, self.b.pause_median))
                if await self.ask(self.last_say) is None:
                    break
                continue
            if await self.ask(say) is None:
                break
            i += 1
            if i < len(steps):
                await self.react()
            if say.text == "Cancel":
                self.result = "cancelled"
                break
        else:
            self.result = "done"
        if self.result == "done":
            await self.ask_where_is_it()
        lg.outcomes[self.flow, self.result] += 1
        log.info("conversation %s %s: %s after %s", cid, self.flow, self.result, ", ".join(self.steps))
        return self.result

    def retry(self):
        if self.retries > 0 and self.rng.random() < self.b.retry:
            self.retries -= 1
            return True
        return False

    async def ask(self, say):
        """Sends a message and returns the answer. After an error or an abandoned turn the person may try again; None means they left."""
        question = say.text
        while True:
            msg, status = await self.send(say)
            if msg is not None:
                self.last, self.last_say = msg, say
                self.steps.append(say.step)
                return msg
            if not self.retry():
                self.result = status
                return None
            await self.lg.clock.sleep(human_pause(self.rng, self.b.pause_median))
            if status in AGAIN and not say.chip and not say.step.endswith(("_confirm", "_cancel")):
                say = Say(say.step, self.rng.choice(AGAIN[status]) + question)

    async def send(self, say):
        """One chat turn. Returns (message, "ok"), or (None, "abandoned") when no answer came within the person's patience, or (None, "error")."""
        lg = self.lg
        waits = say.step == "valuation" and self.rng.random() < self.b.wait_valuation
        deadline = self.rng.uniform(*(self.b.abandon_valuation if waits else self.b.abandon))
        with span("loadgen chat turn", self.cid, self.flow, say.step, chip=say.chip) as sp:
            lg.counters.turns += 1
            try:
                r = await lg.clock.wait_for(lg.api.call(self.cid, "POST", "/api/chat", timeout=None, json={
                    "conversation_id": self.conv_id, "message": say.text, "chip": say.chip}), deadline)
            except asyncio.TimeoutError:
                lg.counters.abandoned += 1
                sp.set_attribute("error.type", "Abandoned")
                return None, "abandoned"
            msg = r.data.get("message") if r.ok and isinstance(r.data, dict) else None
            if not isinstance(msg, dict):
                telemetry.fail(sp, str(r.status) if r.status else (r.error or "NoAnswer"), "the Concierge could not answer")
                return None, "error"
            self.conv_id = r.data.get("conversation_id") or self.conv_id
            sp.set_attributes({"app.conversation.id": self.conv_id or "", "app.chat.outcome": msg.get("outcome") or ""})
            return msg, "ok"

    async def react(self):
        """Between two turns: maybe a thumb on the answer, and the pause of a person who reads it."""
        lg, msg = self.lg, self.last
        pause = human_pause(self.rng, self.b.pause_median)
        ok = msg.get("outcome") in (None, "ok")
        if msg.get("id") is not None and self.rng.random() < (self.b.feedback_ok if ok else self.b.feedback_other):
            tap = min(pause, self.rng.uniform(1, 5))
            await lg.clock.sleep(tap)
            pause -= tap
            rating = "down" if self.rng.random() < (self.b.down_ok if ok else self.b.down_other) else "up"
            with span("loadgen feedback", self.cid, self.flow, "feedback"):
                await lg.api.call(self.cid, "POST", f"/api/messages/{msg['id']}/feedback", json={"rating": rating})
        await lg.clock.sleep(pause)

    async def ask_where_is_it(self):
        """After a confirm that gave a ticket, sometimes ask where it is, once or twice."""
        ticket = next(iter(cards(self.last, "ticket")), None)
        if not ticket or not ticket.get("ticket_id") or self.rng.random() >= self.b.status_ask:
            return
        for n in range(2 if self.rng.random() < self.b.status_ask_twice else 1):
            if ticket.get("state") in TERMINAL_STATES:
                return
            await self.lg.clock.sleep(self.rng.uniform(5, 20) if n == 0 else self.rng.uniform(10, 45))
            tid = ticket["ticket_id"]
            say = self.rng.choice([Say("check_ticket", f"Check ticket {tid}", True), Say("check_ticket", f"Check ticket {tid}"),
                                   Say("check_ticket", STATUS_ASKS.get(ticket.get("kind"), f"Check ticket {tid}"))])
            msg, _ = await self.send(say)
            if msg is None:
                return
            self.last = msg
            self.steps.append(say.step)
            await self.react()
            ticket = next(iter(cards(msg, "ticket")), ticket)
