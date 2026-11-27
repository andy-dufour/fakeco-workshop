"""The fault settlement_lot_cache_leak (fakeco/backoffice/settlement.py): one Bronze customer with thousands of lots, and a settlement cycle that
keeps every merge until the fault is turned off. No database, no network, no real waits."""
import asyncio

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fakeco import faults, telemetry
from fakeco.backoffice import _common, settlement
from fakeco.seed import plans


def test_exactly_one_customer_has_thousands_of_lots_and_is_bronze():
    p = plans()
    heavy = [(plan, lots) for plan, lots in p if lots >= 1_000]
    assert len(heavy) == 1 and heavy[0][0] == "bronze"
    assert sum(plan == "bronze" for plan, _ in p) >= 8   # other Bronze customers too, so the plan is the first BubbleUp lead and the customer the second


@pytest.fixture
def run(monkeypatch):
    spans, tp = InMemorySpanExporter(), TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(telemetry, "tracer", lambda: tp.get_tracer("test"))
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))
    monkeypatch.setattr(_common.asyncio, "sleep", lambda s: real_sleep(0))
    monkeypatch.setattr(settlement, "_cgroup", lambda v2, v1: {"memory.max": 300 << 20, "memory.current": 100 << 20}[v2])
    monkeypatch.setattr(settlement, "_bronze_lots_per_s", [1e6])   # small records, so the test holds a few KiB
    monkeypatch.setattr(settlement, "_cycle", {"id": None, "armed": False, "results": None})
    state = {"fault": {"minutes": 20, "scan_s": 6}}

    async def get(name):
        return state["fault"] if name == settlement.FAULT else None
    monkeypatch.setattr(faults, "get", get)
    return spans, state


def test_the_cycle_keeps_every_merge_and_each_merge_scans_all_of_it_until_the_fault_is_off(run):
    spans, state = run
    heavy, small = {"id": "ACC-0036", "tax_lots": 2_500}, {"id": "ACC-0003", "tax_lots": 91}
    settlement.open_cycle(0, "clock", fault_on=False)   # a cycle that opened while the fault was off holds nothing
    asyncio.run(settlement.match_lots(small))
    assert settlement._cycle["results"] is None
    spans.clear()

    settlement.open_cycle(0, "manual run", fault_on=True)
    asyncio.run(settlement.match_lots(heavy))
    asyncio.run(settlement.match_lots(small))
    merges = [s.attributes for s in spans.get_finished_spans() if s.name == "merge lots"]
    assert [m["lot_store.lots"] for m in merges] == [1000, 1000, 500, 91]
    assert [m["settlement.cycle_results.records_scanned"] for m in merges] == [0, 1000, 2000, 2500]   # everything held, not just this account
    assert merges[-1]["settlement.cycle_results.records"] == 2591
    held = settlement._cycle["results"].bytes
    assert held == 2591 * settlement._cycle["results"].record_bytes and held > 0

    state["fault"] = None   # the fixed release
    asyncio.run(settlement.match_lots(small))
    assert settlement._cycle["results"] is None
    assert "settlement.cycle_results.records" not in [s for s in spans.get_finished_spans() if s.name == "merge lots"][-1].attributes
