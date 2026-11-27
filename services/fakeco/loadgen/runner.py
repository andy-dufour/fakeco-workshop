"""The load generator: polls its settings and starts conversations, transactions, balance events, stories and runs."""
import asyncio, collections, logging, os, random
from dataclasses import dataclass

import httpx

from .. import config
from .api import OPS_USER, Api
from .conversation import WHALES, Behavior, Conversation, num, pick_flow
from .transactions import Trader
from .util import Clock, Counters, names, poisson, span

log = logging.getLogger("loadgen")

POLL_S, STATS_S = 5, 15
BALANCE_EVENTS_PER_MIN = 4
STORY_EVERY_S, STORY_MINUTES = (600, 1200), (5, 10)
RUN_EVERY_S = 600
RUN_KINDS = ("settlement", "sweep", "pricing")
VALUATION_GAP_S = 240   # at most one valuation conversation per this long: a whale's runaway takes about 3 min and goes on after a customer gives up
DEFAULT_SETTINGS = {"enabled": False, "conversations_per_minute": 0, "transactions_per_minute": 0, "auto_stories": False,
                    "surge": {"volume": 1, "flows": {}, "balance_events_factor": 1, "valuation_per_minute": 0}}


@dataclass
class Settings:
    api_url: str
    max_concurrent: int = 30
    seed: int = None

    @classmethod
    def from_env(cls):
        e = os.environ
        return cls(api_url=e.get("FAKECO_API_URL") or config.url("concierge-api"),
                   max_concurrent=int(e.get("LOADGEN_MAX_CONCURRENT_CONVERSATIONS") or 30),
                   seed=int(e["LOADGEN_SEED"]) if e.get("LOADGEN_SEED") else None)


class LoadGen:
    def __init__(self, client, *, max_concurrent=30, seed=None, clock=None, behavior=None):
        self.clock, self.behavior = clock or Clock(), behavior or Behavior()
        self.rng = random.Random(seed)
        # one random source per job, so that with a seed each job draws the same numbers whatever the other jobs do
        self.rngs = {n: random.Random(self.rng.getrandbits(64)) for n in ("conversations", "transactions", "balance", "stories", "runs", "valuations")}
        self.max_concurrent = max_concurrent
        self.counters, self.reported = Counters(), Counters()
        self.api = Api(client, self.counters)
        self.trader = Trader(self)
        self.settings = dict(DEFAULT_SETTINGS)
        self.customers, self.whales = [], []
        self.busy = set()                  # customers in a conversation now
        self.tasks = set()                 # conversations and transactions in flight
        self.conversations_running = 0
        self.outcomes = collections.Counter()   # (flow, result) of the conversations that ended
        self.stats_lock = asyncio.Lock()
        self.balance_at = None             # clock time of the last look for balance events
        self.story_state, self.run_state = {}, {}   # when the next auto story and the next run are due
        self.skipped = 0                   # arrivals dropped because too many conversations were running
        self.valuation_at = float("-inf")  # clock time of the last valuation conversation

    # --- settings ---

    @property
    def enabled(self):
        return bool(self.settings.get("enabled"))

    def surge(self):
        s = self.settings.get("surge")
        return s if isinstance(s, dict) else DEFAULT_SETTINGS["surge"]

    def rate(self, key, volume=1.0):
        """Events per second for a per-minute setting, 0 when the load generator is switched off."""
        return max(0.0, num(self.settings.get(key), 0.0)) * volume / 60 if self.enabled else 0.0

    def valuation_rate(self):
        """Extra valuation conversations per second while the fault asset_valuation_rush is on (the surge says how many a minute)."""
        return max(0.0, num(self.surge().get("valuation_per_minute"), 0.0)) / 60 if self.enabled else 0.0

    async def poll_settings(self):
        r = await self.api.call(OPS_USER, "GET", "/api/ops/loadgen", count_errors=False)
        if r.ok and isinstance(r.data, dict):
            self.settings = {**DEFAULT_SETTINGS, **{k: r.data[k] for k in DEFAULT_SETTINGS if k in r.data}}
        else:
            log.warning("could not read the settings (%s); keeping the last ones", r.status or r.error)

    async def load_customers(self):
        try:
            people = (await self.api.client.get("/api/personas", timeout=10)).json()["customers"]
            self.customers = sorted(p["id"] for p in people)
            names.update({p["id"]: p["name"] for p in people})
            self.whales = sorted(p["id"] for p in people if str(p.get("note", "")).startswith("Whale")) or [w for w in WHALES if w in self.customers]
        except Exception as e:   # the API is down or answered something else; the next poll tries again
            log.warning("could not read the customers: %s", type(e).__name__)

    # --- starting work ---

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def start_conversation(self, rush=False):
        """rush: a valuation conversation of the asset_valuation_rush fault, which is not held back by VALUATION_GAP_S."""
        if self.conversations_running >= self.max_concurrent:
            self.skipped += 1
            return
        rng = self.rngs["valuations" if rush else "conversations"]
        flow = "valuation" if rush else pick_flow(rng, self.surge())
        if flow == "valuation" and not rush:
            if self.clock.now() - self.valuation_at < VALUATION_GAP_S:   # too soon after the last one: another flow instead
                flow = pick_flow(rng, self.surge(), skip=("valuation",))
            else:
                self.valuation_at = self.clock.now()
        pool = self.whales if flow == "valuation" and self.whales and rng.random() < .6 else self.customers
        free = [c for c in pool if c not in self.busy] or [c for c in self.customers if c not in self.busy]
        if not free:
            self.skipped += 1
            return
        customer = rng.choice(free)
        self.busy.add(customer)
        self.conversations_running += 1
        self.spawn(self.conversation(customer, flow, random.Random(rng.getrandbits(64))))

    async def conversation(self, customer, flow, rng):
        try:
            await Conversation(self, customer, flow, rng).play()
        except Exception:
            log.exception("conversation of %s (%s) failed", customer, flow)
        finally:
            self.busy.discard(customer)
            self.conversations_running -= 1

    def start_transaction(self):
        if len(self.tasks) >= self.max_concurrent * 3 or not self.customers:
            return
        rng = self.rngs["transactions"]
        customer, rng = rng.choice(self.customers), random.Random(rng.getrandbits(64))
        self.spawn(self.transaction(customer, rng))

    async def transaction(self, customer, rng):
        try:
            if rng.random() < .65:
                await self.trader.trade_and_follow_up(customer, rng)
            else:
                await self.trader.transfer(customer, rng)
        except Exception:
            log.exception("transaction of %s failed", customer)

    # --- the loops: each runs for ever ---

    async def forever(self, name, step):
        while True:
            try:
                await step()
            except Exception:
                log.exception("%s failed; going on", name)
                await self.clock.sleep(1)

    async def poll_loop(self):
        if not self.customers:
            await self.load_customers()
        await self.poll_settings()
        await self.clock.sleep(POLL_S)

    async def arrivals(self, rng, rate, start):
        """Poisson arrivals at a rate that may change at any time: draw a gap; when it is longer than a second, wait that second and draw again."""
        r = rate()
        if r <= 0:
            return await self.clock.sleep(1)
        gap = rng.expovariate(r)
        if gap > 1:
            return await self.clock.sleep(1)
        await self.clock.sleep(gap)
        if rate() > 0 and self.customers:
            start()

    async def balance_loop(self):
        """Every couple of seconds: the events that fall in the time since the last look (a Poisson draw), sent in calls of at most 20."""
        await self.clock.sleep(2)
        now = self.clock.now()
        dt, self.balance_at = min(10.0, now - self.balance_at) if self.balance_at is not None else 2.0, now
        factor = max(0.0, num(self.surge().get("balance_events_factor"), 1.0))
        n = poisson(self.rngs["balance"], BALANCE_EVENTS_PER_MIN / 60 * factor * dt) if self.enabled else 0
        if n:
            await self.trader.balance_events(n)

    def story_wait(self):
        return self.clock.now() + self.rngs["stories"].uniform(*STORY_EVERY_S)

    async def story_loop(self):
        state = self.story_state
        await self.clock.sleep(5)
        if not (self.enabled and self.settings.get("auto_stories")):
            state["next"] = None   # the countdown starts when the switch goes on
            return
        if state.get("next") is None:
            state["next"] = self.story_wait()
        if self.clock.now() < state["next"]:
            return
        with span("loadgen stories", None, "auto_story", "start"):
            r = await self.api.call(OPS_USER, "GET", "/api/ops/stories")
            stories = r.data if r.ok and isinstance(r.data, list) else []
            if not stories or any(s.get("active") for s in stories):
                state["next"] = self.clock.now() + 60   # one is running (or the list is not there): look again in a minute
                return
            name = self.rngs["stories"].choice([s["name"] for s in stories])
            minutes = self.rngs["stories"].randint(*STORY_MINUTES)
            log.info("starting story %s for %d minutes", name, minutes)
            await self.api.call(OPS_USER, "POST", f"/api/ops/stories/{name}/start", json={"minutes": minutes})
        state["next"] = self.story_wait()

    async def run_loop(self):
        state = self.run_state
        await self.clock.sleep(5)
        if state.get("next") is None:
            state["next"] = self.clock.now() + self.rngs["runs"].expovariate(1 / RUN_EVERY_S)
        if self.clock.now() < state["next"]:
            return
        state["next"] = self.clock.now() + self.rngs["runs"].expovariate(1 / RUN_EVERY_S)
        if not self.enabled:
            return
        kind = self.rngs["runs"].choice(RUN_KINDS)
        with span("loadgen runs", None, "run", "start", **{"app.run.kind": kind}):
            r = await self.api.call(OPS_USER, "GET", "/api/ops/runs")
            if r.ok and isinstance(r.data, list) and any(x.get("kind") == kind and x.get("status") == "running" for x in r.data):
                return   # that run is still going
            await self.api.call(OPS_USER, "POST", "/api/ops/runs", json={"kind": kind})

    async def stats_loop(self):
        await self.clock.sleep(STATS_S)
        await self.flush_stats()

    async def flush_stats(self):
        """Reports the counts since the last successful report. Always sent, even when all zero: it also tells the API that the load generator is alive."""
        async with self.stats_lock:   # one report at a time, or the same counts would be sent twice
            delta = self.counters.since(self.reported)
            r = await self.api.call(OPS_USER, "POST", "/api/ops/loadgen/stats", json=delta, count_errors=False)
            if r.ok:
                for k, v in delta.items():
                    setattr(self.reported, k, getattr(self.reported, k) + v)
            else:
                log.warning("could not report the counts (%s); they go with the next report", r.status or r.error)

    async def run(self):
        loops = {"settings": self.poll_loop, "stats": self.stats_loop, "balance events": self.balance_loop,
                 "stories": self.story_loop, "runs": self.run_loop,
                 "conversation arrivals": lambda: self.arrivals(
                     self.rngs["conversations"], lambda: self.rate("conversations_per_minute", max(0.0, num(self.surge().get("volume"), 1.0))), self.start_conversation),
                 "valuation arrivals": lambda: self.arrivals(self.rngs["valuations"], self.valuation_rate, lambda: self.start_conversation(rush=True)),
                 "transaction arrivals": lambda: self.arrivals(self.rngs["transactions"], lambda: self.rate("transactions_per_minute"), self.start_transaction)}
        tasks = [asyncio.create_task(self.forever(name, step)) for name, step in loops.items()]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks + list(self.tasks):
                t.cancel()


async def serve(settings):
    async with httpx.AsyncClient(base_url=settings.api_url, limits=httpx.Limits(max_connections=200)) as client:
        lg = LoadGen(client, max_concurrent=settings.max_concurrent, seed=settings.seed)
        log.info("load generator started against %s", settings.api_url)
        await lg.run()
