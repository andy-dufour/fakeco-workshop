"""The agent loop: model call, tool calls, model call, until the model answers. The tools one model call asked for run side by side.

Nothing here is traced yet: a run shows up only as the HTTP request that started it and the HTTP calls it made.
"""
import asyncio, dataclasses, logging, time

from opentelemetry import trace

from .. import model
from . import policies

log = logging.getLogger("fakeco.agents")
PRIORITY = ["ok", "error", "tool_timeout_recovered", "context_overflow", "agent_bug"]   # the worst outcome of a turn wins
MAX_CYCLES = 40
BUG_ANSWER = ("I couldn't total your positions. An unexpected error ({type}: {message}) happened in my own code after the portfolio data came back, "
              "so I have no figures to give you.")
FAILED_ANSWER = "Sorry, I ran into a problem and couldn't finish that. Please try again later."   # the Concierge's own call can fail too: nothing about how


def worst(*outcomes):
    return max(outcomes, key=PRIORITY.index)


@dataclasses.dataclass
class ToolResult:
    text: str                       # what the model reads
    error: tuple = None             # (type, message) when the tool failed
    outcome: str = "ok"
    cards: list = dataclasses.field(default_factory=list)
    data: object = None             # the backend's JSON, for the agent's own code
    calls: list = dataclasses.field(default_factory=list)   # the tool calls a specialist made (ask tools)


@dataclasses.dataclass
class Agent:
    name: str
    system: str
    tools: list                     # tool definitions (Messages API)
    execute: object                 # async (tool name, arguments) -> ToolResult
    after_tools: object = None      # the agent's own code on a tool result: (tool name, ToolResult) -> ToolResult; a bug in it is an agent bug


@dataclasses.dataclass
class Result:
    answer: str = ""
    cards: list = dataclasses.field(default_factory=list)
    outcome: str = "ok"
    error: tuple = None             # (type, message) when the agent itself failed
    calls: list = dataclasses.field(default_factory=list)   # [{"agent", "tool", "arguments", "result", "error"}], for the support transcript
    used: list = dataclasses.field(default_factory=list)    # names of the tools this agent called
    seen: list = dataclasses.field(default_factory=list)    # the text of every tool result this agent read
    span_context: object = None     # the span this run happened in: the request's own span
    started_at: int = None          # when this run started and ended (Unix ns); the evaluation records are stamped with the end
    ended_at: int = None


async def _tool(agent, use):
    name, args = use["name"], use["input"]
    try:
        return await agent.execute(name, args)
    except asyncio.CancelledError:
        raise
    except Exception as e:   # the tool code itself broke; the model is told, as with any failed tool
        log.exception("tool %s of %s failed", name, agent.name)
        return ToolResult(f"{type(e).__name__}: {e}", error=(type(e).__name__, str(e)), outcome="error")


async def run(agent, messages, base, *, system=None, stall=False, attrs=None, on_start=None):
    """One agent run for the newest message of `messages`. base and attrs: what is known about the run (the conversation, the customer,
    the prompt version). on_start: called with the span the run happens in, as soon as it starts."""
    system = system or agent.system
    res, msgs, cycles = Result(), list(messages), 0
    sp = trace.get_current_span()
    res.span_context, res.started_at = sp.get_span_context(), time.time_ns()
    if on_start:
        on_start(sp)
    while not res.answer and cycles < MAX_CYCLES:
        cycles += 1
        try:
            reply = await model.create(agent.name, system, msgs, agent.tools, policies.POLICY[agent.name], stall=stall and cycles == 1)
        except model.ModelError as e:   # the provider rejected the request: the run fails, and says so in its answer
            overflow = e.error_type == "ContextWindowExceededError"
            res.error, res.outcome, res.cards = (e.error_type, str(e)), worst(res.outcome, "context_overflow" if overflow else "error"), []
            res.answer = policies.overflow_answer(msgs) if overflow and agent.name != "Concierge" else FAILED_ANSWER
            break
        msgs.append({"role": "assistant", "content": reply["content"]})
        uses = [b for b in reply["content"] if b["type"] == "tool_use"]
        if not uses:
            res.answer = "\n".join(b["text"] for b in reply["content"] if b["type"] == "text")
            break
        results = await asyncio.gather(*(_tool(agent, u) for u in uses))
        for u, r in zip(uses, results):
            res.used.append(u["name"])
            res.calls.append({"agent": agent.name, "tool": u["name"], "arguments": u["input"], "result": r.text[:2000], "error": r.text[:500] if r.error else None})
            res.calls += r.calls
            res.outcome = worst(res.outcome, r.outcome)
        if agent.after_tools:
            try:
                results = [r if r.error else agent.after_tools(u["name"], r) for u, r in zip(uses, results)]
            except Exception as e:
                # a bug in the agent's own code. No further model call is made; the agent answers with the failure.
                log.exception("the code of %s failed after its tools", agent.name)
                res.outcome, res.cards = "agent_bug", []
                res.answer = BUG_ANSWER.format(type=type(e).__name__, message=e)
                break
        res.cards = [c for r in results if not r.error for c in r.cards]   # the cards of the newest tool results
        res.seen += [r.text for r in results]
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": u["id"], "content": r.text, **({"is_error": True} if r.error else {})}
                                                 for u, r in zip(uses, results)]})
    if not res.answer:
        res.answer, res.outcome = FAILED_ANSWER, worst(res.outcome, "error")
    res.ended_at = time.time_ns()
    return res
