"""The evaluation records of a Concierge answer must sit on the Concierge's span: 100 ms before its end, never after it or before its start."""
import asyncio

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fakeco import model, telemetry
from fakeco.agents import evals, loop


@pytest.mark.parametrize("model_seconds, before_end_ms", [(.25, 100), (.02, None)])   # None: the span is shorter than 100 ms, so its start
def test_evaluation_records_are_stamped_100_ms_before_the_end_of_the_concierge_span(monkeypatch, model_seconds, before_end_ms):
    spans, records = InMemorySpanExporter(), InMemoryLogRecordExporter()
    tracing, logging = TracerProvider(), LoggerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(spans))
    logging.add_log_record_processor(SimpleLogRecordProcessor(records))
    monkeypatch.setattr(telemetry, "tracer", lambda: tracing.get_tracer("test"))
    monkeypatch.setattr(telemetry, "logger", logging.get_logger)

    async def answer(*args, **kwargs):
        await asyncio.sleep(model_seconds)
        return {"content": [{"type": "text", "text": "An index fund holds every stock of an index."}]}
    monkeypatch.setattr(model, "create", answer)

    async def turn():
        agent = loop.Agent("Concierge", "You are the Concierge.", [], None)
        res = await loop.run(agent, [{"role": "user", "content": "How do index funds work?"}], {})
        await asyncio.sleep(.02)   # the judge runs after the span has ended
        evals.emit("CNV-1", "How do index funds work?", [], res)
    asyncio.run(turn())

    span = next(s for s in spans.get_finished_spans() if s.name == "invoke_agent Concierge")
    logs = [r.log_record for r in records.get_finished_logs()]
    assert sorted(r.attributes["gen_ai.evaluation.name"] for r in logs) == ["completeness", "hallucination", "relevance"]
    for r in logs:
        assert r.span_id == span.context.span_id
        expected = span.end_time - before_end_ms * 1_000_000 if before_end_ms else span.start_time
        assert span.start_time <= r.timestamp == expected < span.end_time
        assert r.attributes["meta.time_since_span_start_ms"] == round((expected - span.start_time) / 1e6, 3)
        assert r.attributes["evaluation.judged_at_unix_nano"] > span.end_time   # when the judge ran is kept, apart from where the record is drawn
        assert "gen_ai.operation.name" not in r.attributes
