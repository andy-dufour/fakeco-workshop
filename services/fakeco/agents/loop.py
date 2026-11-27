"""The agent loop: model call, tool calls, model call, until the model answers. The spans are those of the simulator in concierge/turn.py:

    invoke_agent <Agent>
      execute_event_loop_cycle          one per model call
        chat <model>                    (model.py)
        execute_tool <tool>             the tools one model call asked for run side by side
"""
import asyncio, dataclasses, logging

from .. import config, model, telemetry
from ..model import jd
from . import policies

log = logging.getLogger("fakeco.agents")
PRIORITY = ["ok", "error", "tool_timeout_recovered", "context_overflow", "agent_bug"]   # the worst outcome of a turn wins
MAX_CYCLES = 40
MAX_RESULT_ATTR = 30_000
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
    span_context: object = None
    started_at: int = None          # when this agent's span started and ended (Unix ns); the evaluation records are stamped with the end
    ended_at: int = None


async def _tool(agent, use, common):
    name, args = use["name"], use["input"]
    with telemetry.tracer().start_as_current_span(f"execute_tool {name}", record_exception=False, set_status_on_exception=False, attributes={
            **common, "gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.call.id": use["id"], "gen_ai.tool.call.arguments": jd(args)}) as sp:
        try:
            r = await agent.execute(name, args)
        except asyncio.CancelledError:
            telemetry.fail(sp, "CancelledError", "request cancelled by client")
            raise
        except Exception as e:   # the tool code itself broke; the model is told, as with any failed tool
            log.exception("tool %s of %s failed", name, agent.name)
            sp.record_exception(e)
            r = ToolResult(f"{type(e).__name__}: {e}", error=(type(e).__name__, str(e)), outcome="error")
        if r.error:
            sp.set_attribute("error.type", r.error[0])
            sp.set_status(telemetry.trace.Status(telemetry.trace.StatusCode.ERROR, r.text[:500]))
        else:
            sp.set_attribute("gen_ai.tool.call.result", r.text[:MAX_RESULT_ATTR])
        return r


async def run(agent, messages, base, *, system=None, stall=False, attrs=None, on_start=None):
    """One agent run for the newest message of `messages`. base: the attributes every agent, cycle, model and tool span carries.
    on_start: called with the `invoke_agent` span as soon as it starts."""
    tr, system = telemetry.tracer(), system or agent.system
    common = {**base, **(attrs or {}), "gen_ai.agent.name": agent.name}
    res, msgs, cycles, tool_calls = Result(), list(messages), 0, 0
    with tr.start_as_current_span(f"invoke_agent {agent.name}", record_exception=False, set_status_on_exception=False, attributes={
            **common, "gen_ai.operation.name": "invoke_agent", "gen_ai.request.model": config.model_for(agent.name), "gen_ai.input.messages": jd(model.parts(msgs[-1:])),
            "gen_ai.system_instructions": jd([{"type": "text", "content": policies.system_text(system)}])}) as sp:
        res.span_context = sp.get_span_context()
        if on_start:
            on_start(sp)
        try:
            while not res.answer and cycles < MAX_CYCLES:
                cycles += 1
                with tr.start_as_current_span("execute_event_loop_cycle", record_exception=False, set_status_on_exception=False,
                                              attributes={**common, "gen_ai.operation.name": "execute_event_loop_cycle"}) as cyc:
                    try:
                        try:
                            reply = await model.create(agent.name, system, msgs, agent.tools, policies.POLICY[agent.name], attrs=common, stall=stall and cycles == 1)
                        except model.ModelError as e:   # the provider rejected the request: the run fails, and says so in its answer
                            telemetry.fail(cyc, e.error_type, str(e))
                            overflow = e.error_type == "ContextWindowExceededError"
                            res.error, res.outcome, res.cards = (e.error_type, str(e)), worst(res.outcome, "context_overflow" if overflow else "error"), []
                            res.answer = policies.overflow_answer(msgs) if overflow and agent.name != "Concierge" else FAILED_ANSWER
                            break
                        msgs.append({"role": "assistant", "content": reply["content"]})
                        uses = [b for b in reply["content"] if b["type"] == "tool_use"]
                        if not uses:
                            res.answer = "\n".join(b["text"] for b in reply["content"] if b["type"] == "text")
                            break
                        tool_calls += len(uses)
                        results = await asyncio.gather(*(_tool(agent, u, common) for u in uses))
                        for u, r in zip(uses, results):
                            res.used.append(u["name"])
                            res.calls.append({"agent": agent.name, "tool": u["name"], "arguments": u["input"], "result": r.text[:2000], "error": r.text[:500] if r.error else None})
                            res.calls += r.calls
                            res.outcome = worst(res.outcome, r.outcome)
                        if agent.after_tools:
                            try:
                                results = [r if r.error else agent.after_tools(u["name"], r) for u, r in zip(uses, results)]
                            except Exception as e:
                                # a bug in the agent's own code: recorded once, on the cycle it happened in. No further model call is made; the agent
                                # answers with the failure, and neither it nor the agents above it carry an error.
                                cyc.record_exception(e)
                                cyc.set_attribute("error.type", type(e).__name__)
                                cyc.set_status(telemetry.trace.Status(telemetry.trace.StatusCode.ERROR, f"{type(e).__name__}: {e}"))
                                res.outcome, res.cards = "agent_bug", []
                                res.answer = BUG_ANSWER.format(type=type(e).__name__, message=e)
                                break
                        res.cards = [c for r in results if not r.error for c in r.cards]   # the cards of the newest tool results
                        res.seen += [r.text for r in results]
                        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": u["id"], "content": r.text, **({"is_error": True} if r.error else {})}
                                                                 for u, r in zip(uses, results)]})
                    except asyncio.CancelledError:
                        telemetry.fail(cyc, "CancelledError", "request cancelled by client")
                        raise
            if not res.answer:
                res.answer, res.outcome = FAILED_ANSWER, worst(res.outcome, "error")
        except asyncio.CancelledError:
            telemetry.fail(sp, "CancelledError", "request cancelled by client")
            raise
        finally:
            sp.set_attributes({"agent.loop.cycles": cycles, "agent.loop.tool_calls": tool_calls})
        sp.set_attributes({"gen_ai.output.messages": jd([{"role": "assistant", "parts": [{"type": "text", "content": res.answer}], "finish_reason": "end_turn"}]), "app.outcome": res.outcome})
        if res.error:
            telemetry.fail(sp, *res.error)
    res.started_at, res.ended_at = getattr(sp, "start_time", None), getattr(sp, "end_time", None)   # a span that is not recorded has neither
    return res
