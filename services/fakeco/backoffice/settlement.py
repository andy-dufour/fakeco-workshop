"""settlement-service: stores settlement instructions as orders fill, and settles the pending ones in a run."""
import asyncio, datetime as dt, logging, math, os, random, time

from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from .. import customers, db, faults, queue, telemetry
from ..app import create_app
from ._common import around, outside, start_run

log = logging.getLogger(__name__)
CHUNK = 10


class Run(BaseModel):
    run_id: int


async def handle(m, span):
    p = m["payload"]
    span.set_attributes({"order.id": p["trade_id"], "order.symbol": p["symbol"], "order.notional_usd": p["notional"]})
    if await db.fetchval("SELECT 1 FROM settlement_instructions WHERE trade_id = %s", p["trade_id"]):   # delivered twice
        span.set_attribute("queue.duplicate", True)
        return "completed"
    await db.execute("INSERT INTO settlement_instructions (trade_id, symbol, notional) VALUES (%s, %s, %s)", p["trade_id"], p["symbol"], p["notional"])
    return "completed"


async def settle_chunk(rows, i, n, fail_rate):
    """Deliver one chunk of instructions to the clearing agency's custody account. A failed delivery is bought in on the market."""
    failed = False
    with telemetry.tracer().start_as_current_span("settle.chunk", attributes={"chunk.index": i, "chunk.of": n, "settlement.instructions": len(rows)}) as sp:
        await asyncio.sleep(around(.03))
        if fail_rate and random.random() < fail_rate:
            failed = True
            with telemetry.tracer().start_as_current_span("delivery.failed", attributes={"settlement.fail_reason": "counterparty_short"}) as d:
                await asyncio.sleep(around(.04))
                telemetry.fail(d, "SettlementFail", "failed delivery: counterparty short")
            with telemetry.tracer().start_as_current_span("buy_in", attributes={"settlement.buy_in": True}):
                await asyncio.sleep(around(.3))
            sp.set_attribute("settlement.failed", True)
        await db.execute("UPDATE settlement_instructions SET status = %s WHERE id = ANY(%s)", "bought_in" if failed else "settled", [r["id"] for r in rows])
    return failed


async def settlement(sp, run_id):
    """Settle the pending instructions in chunks of CHUNK: net per security, submit the file to the clearing agency, then book each chunk."""
    rows = await db.fetch("UPDATE settlement_instructions SET status = 'in_run', run_id = %s WHERE status = 'pending' RETURNING id, symbol, notional", run_id)
    sp.set_attributes({"messaging.system": "postgresql", "messaging.destination.name": "settlement.instructions", "messaging.operation.type": "process",
                       "messaging.consumer.group.name": "settlement-service-workers", "messaging.batch.message_count": len(rows)})
    net = {}
    for r in rows:
        net[r["symbol"]] = net.get(r["symbol"], 0) + r["notional"]
    for sym, notional in sorted(net.items()):
        with telemetry.tracer().start_as_current_span("net_position", attributes={"settlement.symbol": sym, "settlement.net_notional_usd": round(notional, 2)}):
            pass
    if rows:
        await outside("clearing.submit_file", "clearing-gateway", around(.4), **{"clearing.file_records": len(rows)})
    chunks = [rows[i:i + CHUNK] for i in range(0, len(rows), CHUNK)]
    p = await faults.get("settle_fail")
    rate = float(p.get("rate", .25)) if p is not None else 0.0
    fails = 0
    for i, chunk in enumerate(chunks):
        fails += await settle_chunk(chunk, i, len(chunks), rate)
    sp.set_attributes({"settlement.chunks": len(chunks), "settlement.failed_deliveries": fails, "queue.outcome": "completed"})
    return f"{len(rows)} instructions, {fails} fails"


# --- position reconciliation and settlement cycles ---
# All day, settlement-service reconciles one account at a time against the custodian (about RECONCILE_EVERY_S apart, more often for accounts with
# many tax lots). Bronze accounts have no consolidated lot records, so their raw tax lots are read from lot-store a page at a time and merged.
# A settlement cycle opens on the clock every CYCLE_EVERY_MIN minutes. With the fault settlement_lot_cache_leak, the cycle holds what every
# merge gathers in one CycleResults and never lets go: it fills the pod's memory (mostly with the one account that has thousands of lots), each
# merge walks all of it, and Kubernetes kills the pod at its memory limit. A restarted process has no open cycle; the next cycle starts it again.
FAULT = "settlement_lot_cache_leak"
RECONCILE_EVERY_S = 2.0
CYCLE_EVERY_MIN = 120
MAX_IN_FLIGHT = 40
PAGE_LOTS = 1_000         # lots per lot-store page; one merge per page
HEAVY_LOTS = 9_000        # with memory full, an account with this many lots is slower by the fault's scan_s
LOTS_PER_WEIGHT = 400     # each LOTS_PER_WEIGHT lots make an account one more reconciliation's worth of work
MiB = 1 << 20
_cycle = {"id": None, "armed": False, "results": None}   # armed: the cycle opened while the fault was on
_bronze_lots_per_s = [1.0]   # Bronze lots read per second at the current account mix


class CycleResults:
    """What the faulty release keeps for the whole cycle: a record for every lot of every Bronze reconciliation, duplicates and all, never freed.
    A record holds the whole lot-store row, its page and the custodian's answer, so it is far larger than the lot. record_bytes is sized so that
    the memory limit is reached after about the fault's `minutes`."""

    def __init__(self, minutes):
        used = _cgroup("memory.current", "memory.usage_in_bytes") or int(open("/proc/self/statm").read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
        self.headroom = max(32 * MiB, (_cgroup("memory.max", "memory.limit_in_bytes") or 300 * MiB) - used)
        self.record_bytes = max(16, int(self.headroom * 1.05 / (minutes * 60 * _bronze_lots_per_s[0])))
        self.pages, self.records, self.bytes = [], 0, 0

    def add(self, lots):
        self.pages.append(b"L" * (lots * self.record_bytes))   # written, not just reserved, so it counts against the container's memory
        self.records += lots
        self.bytes += lots * self.record_bytes

    def full(self):
        return min(1.0, self.bytes / self.headroom)


def _cgroup(v2, v1):
    """A number of bytes from the container's memory cgroup (v2, else v1), or None when there is no limit or no cgroup (one machine)."""
    for path in (f"/sys/fs/cgroup/{v2}", f"/sys/fs/cgroup/memory/{v1}"):
        try:
            v = open(path).read().strip()
        except OSError:
            continue
        if v.isdigit() and int(v) < 1 << 50:
            return int(v)
    return None


async def match_lots(a):
    """Read a Bronze account's raw tax lots from lot-store a page at a time and merge each page. With the fault, each merge adds the page to the
    cycle's CycleResults and checks it for duplicates against everything held so far, so every merge is slower than the one before."""
    p = await faults.get(FAULT)
    if p is None:
        _cycle.update(armed=False, results=None)   # the fixed release keeps nothing after a merge
    elif _cycle["armed"] and _cycle["results"] is None:
        _cycle["results"] = CycleResults(float(p.get("minutes", 20)))
    res = _cycle["results"]
    pages = max(1, math.ceil(a["tax_lots"] / PAGE_LOTS))
    for n in range(1, pages + 1):
        lots = min(PAGE_LOTS, a["tax_lots"] - (n - 1) * PAGE_LOTS)
        await outside("lot_store.read", "lot-store", around(.012, .35), **{"lot_store.page": n, "lot_store.pages": pages, "lot_store.lots": lots})
        with telemetry.tracer().start_as_current_span("merge lots", attributes={"lot_store.page": n, "lot_store.lots": lots}) as m:
            if res is None:
                await asyncio.sleep(around(.004))
                continue
            scanned = res.records   # the duplicate check walks every record held, not just this account's
            res.add(lots)
            m.set_attributes({"settlement.cycle_results.records": res.records, "settlement.cycle_results.records_scanned": scanned,
                              "settlement.cycle_results.bytes": res.bytes, "settlement.cycle_results.pages": len(res.pages)})
            # the walk of everything held, and the garbage collector's walks of a heap this size: a wait here, so the rest of the service keeps
            # working. A bigger account walks longer, though not in proportion; its pages share the time.
            await asyncio.sleep(around(.004) + around(float(p.get("scan_s", 6)) * res.full() ** 2 * min(1.0, a["tax_lots"] / HEAVY_LOTS) ** .3 / pages, .15))
    if res is not None and res.bytes > res.headroom * 1.4:   # no memory limit stopped it (one machine, no cgroup): end the process as the limit would
        log.error("cycle results at %d MiB: out of memory", res.bytes // MiB)
        os._exit(137)


async def reconcile(a):
    """One account against the custodian: our positions, the custodian's, and for Bronze accounts the raw tax lots."""
    attrs = {"account.id": a["id"], **await customers.attrs(a["customer_id"]), "app.customer.plan": a["plan"], "settlement.tax_lots": a["tax_lots"],
             "settlement.lot_source": "lot-store" if a["plan"] == "bronze" else "consolidated"}
    if _cycle["id"]:
        attrs["settlement.cycle.id"] = _cycle["id"]
    with telemetry.tracer().start_as_current_span("reconcile account", attributes=attrs) as sp:
        rows = await db.fetch("SELECT symbol, quantity FROM holdings WHERE account_id = %s", a["id"])
        await outside("custodian.positions", "custodian-gateway", around(.06, .35), **{"custodian.positions": len(rows)})
        with telemetry.tracer().start_as_current_span("match tax lots", attributes={"settlement.tax_lots": a["tax_lots"]}):
            if a["plan"] == "bronze":
                await match_lots(a)
            else:
                await asyncio.sleep(around(.02))
        sp.set_attribute("settlement.positions", len(rows))


async def reconcile_loop():
    """Starts reconciliations at random (Poisson) times while the load generator is on, each in its own task and trace."""
    accounts, weights, enabled, at, running = [], [], False, -1e9, set()
    while True:
        await asyncio.sleep(random.expovariate(1 / RECONCILE_EVERY_S))
        try:
            if time.monotonic() - at > 30:
                accounts = await db.fetch("SELECT a.id, a.customer_id, c.plan, a.tax_lots FROM accounts a JOIN customers c ON c.id = a.customer_id ORDER BY a.id", quiet=True)
                enabled = bool(((await db.fetchval("SELECT value FROM settings WHERE key = 'loadgen'", quiet=True)) or {}).get("enabled"))
                weights = [1 + r["tax_lots"] / LOTS_PER_WEIGHT for r in accounts]
                _bronze_lots_per_s[0] = max(1.0, sum(w * r["tax_lots"] for w, r in zip(weights, accounts) if r["plan"] == "bronze") / (sum(weights) or 1) / RECONCILE_EVERY_S)
                at = time.monotonic()
            if not (enabled and accounts) or len(running) >= MAX_IN_FLIGHT:
                continue
            a = random.choices(accounts, weights)[0]
            t = asyncio.create_task(reconcile(a))
            running.add(t)
            t.add_done_callback(running.discard)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reconciliation failed")


def open_cycle(at, trigger, fault_on):
    """Open a settlement cycle. One that opens while the fault is on keeps every Bronze merge in memory from now on (see match_lots)."""
    cid = "CYC-" + dt.datetime.fromtimestamp(at, dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    with telemetry.tracer().start_as_current_span("open settlement cycle", attributes={"settlement.cycle.id": cid, "settlement.cycle.trigger": trigger}):
        _cycle["id"] = cid
        _cycle["armed"] = _cycle["armed"] or fault_on
    log.info("settlement cycle %s open (%s)", cid, trigger)


async def cycle_loop():
    """Opens a settlement cycle on the clock. A process that starts in the middle of a period waits for the next one (or a manual settlement run)."""
    seen = None
    while True:
        try:
            p = await faults.get(FAULT)
            every = float((p or {}).get("every_minutes", CYCLE_EVERY_MIN)) * 60
            now = (every, int(time.time() // every))
            if seen and seen[0] == now[0] and seen[1] != now[1]:
                open_cycle(now[1] * every, "clock", p is not None)
            seen = now
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("settlement cycle: unexpected error")
        await asyncio.sleep(5)


def build():
    app = create_app(workers=[lambda: queue.worker("settlement.instructions", handle), reconcile_loop, cycle_loop])

    @app.post("/v1/runs")
    async def runs(b: Run):
        # a settlement run that a person starts on the operations page opens a cycle at once; the load generator's routine runs do not
        if await db.fetchval("SELECT started_by FROM runs WHERE id = %s", b.run_id) not in (None, "SYS-loadgen"):
            open_cycle(time.time(), "manual run", await faults.get(FAULT) is not None)
        return start_run(b.run_id, "process settlement.instructions", lambda sp: settlement(sp, b.run_id), kind=SpanKind.CONSUMER)

    return app
