"""When the caller of portfolio-specialist hangs up, a valuation run goes on to its answer (tagged app.outlived_cancelled_parent, at most
MAX_OUTLIVING at a time); every other run stops with its caller."""
import asyncio, socket

import httpx, uvicorn
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fakeco import db, faults, model, telemetry
from fakeco.agents import service, tools
from fakeco.agents.loop import ToolResult

VALUE = "Value every asset in account ACC-{}, one asset at a time, and report each valuation."


def test_valuation_runs_outlive_a_caller_that_hangs_up_and_other_runs_do_not(monkeypatch):
    spans, tp = InMemorySpanExporter(), TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setenv("SERVICE_NAME", "portfolio-specialist")
    monkeypatch.setattr(telemetry, "tracer", lambda: tp.get_tracer("test"))
    monkeypatch.setattr(telemetry, "init", lambda name: None)

    async def nothing():
        pass
    monkeypatch.setattr(db, "open_pool", nothing)
    monkeypatch.setattr(db, "close_pool", nothing)

    async def no_flags(name):
        return None
    monkeypatch.setattr(faults, "get", no_flags)

    async def create(agent, system, messages, tools_, decide, **kw):   # four valuation cycles of .2 s, then the answer
        n = sum(1 for m in messages if m["role"] == "assistant")
        await asyncio.sleep(.2)
        if n < 4:
            return {"content": [{"type": "tool_use", "id": f"t{n}", "name": "get_asset_valuation", "input": {"account_id": "ACC-1", "asset_id": f"AST-{n}"}}]}
        return {"content": [{"type": "text", "text": "Here are your valuations."}]}
    monkeypatch.setattr(model, "create", create)

    async def backend(name, args):
        return ToolResult("{}")
    monkeypatch.setattr(tools, "backend", backend)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    asks = [VALUE.format(i) for i in range(service.MAX_OUTLIVING + 1)] + ["Show the portfolio breakdown by asset class"]

    async def main():
        srv = uvicorn.Server(uvicorn.Config(service.build(), port=port, log_level="warning"))
        task = asyncio.create_task(srv.serve())
        while not srv.started:
            await asyncio.sleep(.02)

        async def hang_up(q):   # the caller gives up after .3 s, long before the run's 1 s
            async with httpx.AsyncClient() as c:
                try:
                    await c.post(f"http://127.0.0.1:{port}/v1/ask", timeout=.3,
                                 json={"conversation_id": "c", "question": q, "customer": {"id": "CUS-1", "account_id": "ACC-1", "first_name": "A", "name": "A B"}})
                except httpx.TimeoutException:
                    pass
        await asyncio.gather(*(hang_up(q) for q in asks))
        await asyncio.sleep(1.5)
        srv.should_exit = True
        await task
    asyncio.run(main())

    runs = [s for s in spans.get_finished_spans() if s.name == "invoke_agent PortfolioSpecialist"]
    assert len(runs) == len(asks)
    went_on = [s for s in runs if s.attributes.get("app.outlived_cancelled_parent")]
    assert len(went_on) == service.MAX_OUTLIVING
    assert all("Here are your valuations." in s.attributes["gen_ai.output.messages"] and s.status.is_ok for s in went_on)
    stopped = [s for s in runs if s not in went_on]
    assert all(s.attributes.get("error.type") == "CancelledError" for s in stopped)
    assert any("portfolio breakdown" in s.attributes["gen_ai.input.messages"] for s in stopped)
