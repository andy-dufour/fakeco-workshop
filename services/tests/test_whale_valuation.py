"""A whale's valuation: the per-asset tool makes the history grow until the model rejects it. Also the markers of a flag change and the
service.version stamp. No database, no network, no waits."""
import asyncio, re

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fakeco import api, config, db, faults, honeycomb, telemetry
from fakeco.agents import loop, tools
from fakeco.agents.loop import ToolResult
from fakeco.model import jd

ASSETS = [{"asset_id": f"AST-{i:06d}", "symbol": f"SYM{i}", "name": f"Symbol {i} Holdings"} for i in range(120)]
QUESTION = "Value every asset in account ACC-0058, one asset at a time, and report each valuation."


async def fake_backend(name, args):
    """What portfolio-service answers: the shapes of backends/portfolio.py."""
    if name == "list_assets":
        r = {"account_id": args["account_id"], "assets": ASSETS}
    elif name == "get_asset_valuation":
        a = next(a for a in ASSETS if a["asset_id"] == args["asset_id"])
        r = {**a, "quantity": 10, "price": 5.0, "market_value": 50.0, "as_of": "2026-11-19", "closes": [{"date": f"2025-{d:05d}", "close": 5.0 + d / 1000} for d in range(300)]}
    elif name == "get_asset_positions":
        r = [{"asset": s, "position": 50.0} for s in args["assets"]]
    else:
        raise KeyError(name)
    return ToolResult(jd(r), data=r)


def whale_turn(monkeypatch):
    """(result, input tokens of every model call) of a valuation run over 120 assets."""
    spans, tp = InMemorySpanExporter(), TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(telemetry, "tracer", lambda: tp.get_tracer("test"))
    monkeypatch.setenv("FAKECO_MODEL_SPEED", "0")
    monkeypatch.setattr(tools, "backend", fake_backend)

    async def no_faults(name):
        return None
    monkeypatch.setattr(faults, "get", no_faults)
    res = asyncio.run(loop.run(tools.specialist("portfolio-specialist"), [{"role": "user", "content": QUESTION}], {}))
    chats = [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]
    return res, [s.attributes["gen_ai.usage.input_tokens"] for s in chats]


def test_whale_valuation(monkeypatch):
    res, tokens = whale_turn(monkeypatch)
    if "get_asset_positions" in res.used:   # one bulk call: a few thousand tokens
        assert res.used == ["list_assets", "get_asset_positions"] and res.outcome == "ok"
        assert res.answer == "All 120 assets are valued. Together they are worth $6,000.00."
        assert max(tokens) < config.CONTEXT_LIMIT_TOKENS / 20
        bulk = next(c for c in res.calls if c["tool"] == "get_asset_positions")
        assert bulk["arguments"]["assets"] == [a["symbol"] for a in ASSETS]
        assert set(re.findall(r'"(\w+)":', bulk["result"])) == {"asset", "position"}
    else:                                   # one asset at a time: the history grows by every valuation until the model rejects it
        assert set(res.used) == {"list_assets", "get_asset_valuation"}
        assert res.outcome == "context_overflow" and max(tokens) > config.CONTEXT_LIMIT_TOKENS


def test_flag_changes_make_markers_that_name_nothing(monkeypatch):
    made = []

    async def marker(message, kind, start, end=None):
        made.append((message, kind))
    monkeypatch.setattr(honeycomb, "marker", marker)
    stored, was = [], telemetry.version()

    async def execute(sql, *args, **kw):
        stored.append(args[0].obj)
    monkeypatch.setattr(db, "execute", execute)
    for _ in range(2):   # on, then off
        asyncio.run(api.change_marker("venue_slow", 0))
    assert made == [("configuration change", "config")] * 2
    assert stored == [] and telemetry.version() == was


def test_spans_leave_with_the_current_service_version(monkeypatch):
    inner = InMemorySpanExporter()
    tp = TracerProvider(resource=telemetry.resource("svc"))
    tp.add_span_processor(SimpleSpanProcessor(telemetry.VersionStamp(inner)))
    monkeypatch.setattr(telemetry, "_version", config.VERSION)
    for v in (config.VERSION, "9c41ab0", "e07f3d2"):
        telemetry.set_version(v)
        with tp.get_tracer("t").start_as_current_span("x"):
            pass
    out = inner.get_finished_spans()
    assert [s.resource.attributes["service.version"] for s in out] == [config.VERSION, "9c41ab0", "e07f3d2"]
    assert {s.resource.attributes["service.name"] for s in out} == {"svc"}   # the rest of the resource stays
