"""Evaluation of each Concierge answer, as log records named gen_ai.evaluation.result on the Concierge's span (as concierge/evals.py does).

The judge is a set of rules, not a model: it costs nothing and it reads what a judge would read: the customer's message, the tools that were used,
their results and the answer. The records never set gen_ai.operation.name.
"""
import re, time

from opentelemetry._logs import LogRecord, SeverityNumber
from opentelemetry.trace import TraceFlags

from .. import config, telemetry
from . import policies

JUDGE = {"evaluation.judge.type": "rule", "evaluation.judge.version": "1"}
COMPLETENESS = {"ok": 1.0, "tool_timeout_recovered": 0.4, "context_overflow": 0.2, "agent_bug": 0.1, "error": 0.1}
FIGURE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?|\d+(?:\.\d+)?\s?%|\b\d{3,}\b")
BEFORE_END_NS = 100_000_000   # the evaluation records sit this long before the end of the Concierge's span
LOGGER = "concierge.evaluations"


def _digits(s):
    return re.sub(r"[^\d.]", "", s).rstrip(".")


def unsupported_figures(answer, seen):
    """Dollar amounts, percentages and long numbers in the answer that are in no tool result the Concierge read."""
    have = {_digits(f) for text in seen for f in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}
    have |= {d.split(".")[0] for d in have}
    return [f for f in FIGURE.findall(answer) if _digits(f) not in have and _digits(f).split(".")[0] not in have]


def judge(user, history, result):
    """{name: (score, explanation)} for one Concierge run (loop.Result)."""
    expected = policies.expected_tool(user, history)
    comp = COMPLETENESS[result.outcome]
    if expected and expected not in result.used:
        rel, why_rel = 0.3, f"The request needed {expected}, but the Concierge used {', '.join(result.used) or 'no tool'}."
    elif not expected and result.used:
        rel, why_rel = 0.5, f"The request needed no specialist, but the Concierge used {', '.join(result.used)}."
    else:
        rel, why_rel = 1.0, f"Routing matches the request: {expected or 'answered directly'}."
    bad = unsupported_figures(result.answer, result.seen + [user])
    hal = max(0.0, round(.6 - .1 * len(bad), 2)) if bad else 1.0   # 1 = no unsupported claims
    return {"completeness": (comp, f"The agent {'completed' if comp >= .7 else 'did not complete'} the request. Outcome: {result.outcome}."),
            "relevance": (rel, why_rel),
            "hallucination": (hal, "No unsupported claims." if not bad else f"The answer states figures that are in no tool result: {', '.join(bad[:6])}.")}


def emit(conversation_id, user, history, result, attrs=None):
    """Three log records on the Concierge's agent span, stamped 100 ms before that span ended (at its start when it is shorter than that).

    A record stamped when the judge ran falls just after the span, and one at the very end sits on its edge in a trace view. The time the judge ran is in
    evaluation.judged_at_unix_nano. meta.time_since_span_start_ms is the field Honeycomb adds to a span event that arrives inside a span; a log
    record does not get it, so it is set here."""
    ctx, now = result.span_context, time.time_ns()
    at = max(result.started_at or 0, result.ended_at - BEFORE_END_NS) if result.ended_at else now
    logger = telemetry.logger(LOGGER)   # this process's logger: the records carry the service.name of the span they sit on
    offset = {"meta.time_since_span_start_ms": round((at - result.started_at) / 1e6, 3)} if result.started_at else {}
    for name, (score, why) in judge(user, history, result).items():
        good = score >= .7
        label = "pass" if good else "fail"
        logger.emit(LogRecord(
            timestamp=at, observed_timestamp=now, trace_id=ctx.trace_id, span_id=ctx.span_id, trace_flags=TraceFlags(TraceFlags.SAMPLED), event_name="gen_ai.evaluation.result",
            severity_number=SeverityNumber.INFO, severity_text="INFO", body=f"{name}: {label} ({score})", attributes={
                **(attrs or {}), "event.name": "gen_ai.evaluation.result", "gen_ai.conversation.id": conversation_id, "gen_ai.agent.name": "Concierge", "gen_ai.provider.name": config.PROVIDER,
                "gen_ai.request.model": config.model_for("Concierge"), "gen_ai.evaluation.name": name, "gen_ai.evaluation.score.value": score, "gen_ai.evaluation.score.label": label,
                "gen_ai.evaluation.explanation": why, "evaluation.judged_at_unix_nano": now, **offset, **JUDGE}))
