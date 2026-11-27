"""The agent services. concierge-orchestrator runs the Concierge (POST /v1/chat); each specialist service runs one specialist (POST /v1/ask)."""
import asyncio, datetime as dt, hashlib

from fastapi import Request
from opentelemetry import trace
from pydantic import BaseModel

from .. import config, faults
from ..app import ClientGone, create_app, or_disconnect
from . import cards, evals, loop, policies, tools

HISTORY_MESSAGES = 20
MAX_OUTLIVING = 3   # valuation runs that go on after their caller hung up, at a time, per pod; past that a run stops with its caller
_outliving = set()


class Customer(BaseModel):
    id: str
    account_id: str
    first_name: str
    name: str


class Turn(BaseModel):
    role: str
    text: str
    cards: list = []


class Chat(BaseModel):
    conversation_id: str
    message: str
    chip: bool = False
    history: list[Turn] = []
    customer: Customer
    app_version: str | None = None


class Ask(BaseModel):
    conversation_id: str
    question: str
    customer: Customer


def _base(conversation_id, customer):
    return {"gen_ai.conversation.id": conversation_id, "gen_ai.provider.name": config.PROVIDER, "app.customer.id": customer.id, "app.customer.name": customer.name}


def as_messages(history):
    """The stored conversation as model messages. A card the customer can act on is written into the text as a note, because the model reads only text:
    `[card confirm kind=transfer ref=in:250.00]`, `[card ticket id=TKT-7F3A9C21 kind=transfer]`. Messages a support person wrote are left out."""
    out = []
    for h in history[-HISTORY_MESSAGES:]:
        if h.role not in ("user", "assistant"):
            continue
        notes = [f"[card confirm kind={c['kind']} ref={c.get('ref', '')}]" for c in h.cards if c.get("type") == "confirm"]
        notes += [f"[card ticket id={c['ticket_id']} kind={c['kind']}]" for c in h.cards if c.get("type") == "ticket"]
        out.append({"role": h.role, "content": "\n".join([h.text] + notes)})
    return out


def _share(key, rate):
    """True for the same `rate` share of keys every time (a release that reaches some conversations, not a coin toss per message)."""
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate


def build():
    service = config.service_name()
    app = create_app()

    if service == "concierge-orchestrator":
        @app.post("/v1/chat")
        async def chat(body: Chat, request: Request):
            c = body.customer
            agent = tools.concierge(body.conversation_id, c.model_dump())
            regress = await faults.get("prompt_regression")
            regressed = regress is not None and _share(body.conversation_id, float(regress.get("rate", 1)))
            version = tools.REGRESSED_PROMPT_VERSION if regressed else tools.PROMPT_VERSION
            system = [{"type": "text", "text": agent.system + (tools.REGRESSED_SENTENCE if regressed else "")},
                      {"type": "text", "text": f"The verified customer is {c.name} (first name {c.first_name}), customer {c.id}, account {c.account_id}. Today is {dt.date.today().isoformat()}."}]
            # the app release that hangs on send: the newest one (a request without the header counts as the newest)
            stall = (body.app_version or config.LATEST_APP_VERSION) == config.LATEST_APP_VERSION and await faults.hit("app_hang_on_send") is not None
            trace.get_current_span().set_attributes({"app.chat.chip": body.chip, "app.version": body.app_version or config.LATEST_APP_VERSION})
            history = as_messages(body.history)
            messages = history + [{"role": "user", "content": body.message}]
            res = await or_disconnect(request, loop.run(agent, messages, _base(body.conversation_id, c), system=system, stall=stall, attrs={"app.prompt.version": version}))
            evals.emit(body.conversation_id, body.message, history, res, {"app.prompt.version": version, "app.customer.id": c.id, "app.customer.name": c.name})
            return {"answer": res.answer, "cards": res.cards, "outcome": res.outcome, "tool_calls": res.calls, "trace_id": f"{res.span_context.trace_id:032x}"}
    else:

        @app.post("/v1/ask")
        async def ask(body: Ask, request: Request):
            started = []
            agent = tools.specialist(service)
            run = loop.run(agent, [{"role": "user", "content": body.question}], _base(body.conversation_id, body.customer), on_start=started.append)
            if agent.name == "PortfolioSpecialist" and policies.intent(body.question)[0] == "valuation":
                # a valuation run, as in a real agent server: when the caller hangs up (the customer gave up, or the Concierge stopped waiting), the run
                # goes on to the end, valuing assets and paying for model calls nobody reads. It ends after the request that started it.
                task = asyncio.create_task(run)
                try:
                    res = await or_disconnect(request, asyncio.shield(task))
                except ClientGone:
                    if len(_outliving) >= MAX_OUTLIVING:   # enough runs already go on for nobody: this one stops as any other does
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    else:
                        _outliving.add(task)
                        task.add_done_callback(lambda t: (_outliving.discard(t), t.cancelled() or t.exception()))
                        if started:
                            started[0].set_attribute("app.outlived_cancelled_parent", True)
                    raise
            else:
                res = await or_disconnect(request, run)
            found = [] if res.error or res.outcome == "agent_bug" else res.cards + cards.from_answer(res.answer)
            return {"answer": res.answer, "cards": found, "outcome": res.outcome, "tool_calls": res.calls,
                    "error": {"type": res.error[0], "message": res.error[1]} if res.error else None}

    return app
