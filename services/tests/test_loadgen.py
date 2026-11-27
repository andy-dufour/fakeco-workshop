"""The load generator against a fake of the public API (tests/fake_api.py), in this process, with time sped up. No network."""
import asyncio, collections, contextlib, json, logging, re, socket, time

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fakeco import config
from fakeco.loadgen.api import app_version
from fakeco.loadgen.conversation import START_FLOWS, STARTER_CHIPS, VALUATION_ASKS, Behavior, pick_flow
from fakeco.loadgen.runner import LoadGen, Settings
from fakeco.loadgen.util import Clock
from fake_api import STORIES, Fake, FakeTransport

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("a test tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "FAKECO_EXPORT", "FAKECO_API_URL", "LOADGEN_SEED", "LOADGEN_MAX_CONCURRENT_CONVERSATIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FAKECO_LOCAL", "1")
    EXPORTER.clear()


# --- harness ---

@contextlib.asynccontextmanager
async def running(fake, speed=200, seed=7, **kw):
    """Starts a load generator on the fake API and yields it. At the end it must still be running (it never exits)."""
    async with httpx.AsyncClient(transport=FakeTransport(fake), base_url="http://fake") as client:
        lg = LoadGen(client, clock=Clock(speed), seed=seed, **kw)
        task = asyncio.create_task(lg.run())
        try:
            yield lg
            assert not task.done(), f"the load generator exited: {task.exception() if task.done() and not task.cancelled() else ''}"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def until(predicate, what, timeout=40):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, f"gave up waiting for: {what}"
        await asyncio.sleep(0.005)


async def quiesce(fake, lg):
    """Switches the load off, lets the work in flight finish, and reports the last counts."""
    fake.config["enabled"] = False
    await until(lambda: not lg.enabled and not lg.tasks, "the work in flight to finish")
    await lg.flush_stats()


def scenario(coro):
    return asyncio.run(coro)


def fake_with(**config_changes):
    fake = Fake()
    fake.config.update(config_changes)
    fake.seed_pending(1)
    return fake


def intents_by_conversation(fake):
    return [c["intents"] for c in fake.conversations.values()]


TAIL = r"(,check_ticket){0,2}"
FLOW_PATTERNS = {
    "portfolio": r"portfolio", "general": r"general", "valuation": r"valuation",
    "trades": rf"pending_trades,change_trade,((confirm{TAIL})|cancel)",
    "transfer": rf"transfer_form,transfer_amount,((confirm{TAIL})|cancel)",
    "transfer_direct": rf"transfer_amount,((confirm{TAIL})|cancel)",
    "rebalance": rf"rb_start,rb_risk,rb_horizon,rb_leave,((confirm{TAIL})|cancel)",
}


def flow_of(intents):
    found = [f for f, p in FLOW_PATTERNS.items() if re.fullmatch(p, ",".join(intents))]
    return found[0] if found else None


# --- tests ---

def test_every_flow_is_played_to_its_last_step_following_the_cards():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=0)
    fake.seed_pending(1)

    async def go():
        async with running(fake, behavior=Behavior(cancel=0)) as lg:
            await until(lambda: sum(lg.outcomes.values()) >= 120 and {f for f, _ in lg.outcomes} == set(START_FLOWS), "120 conversations of all flows")
            await quiesce(fake, lg)
            return lg
    lg = scenario(go())
    assert fake.violations == []   # every id and chip text sent was one the cards had offered
    assert {r for _, r in lg.outcomes} == {"done"}, lg.outcomes
    flows = collections.Counter()
    for intents in intents_by_conversation(fake):
        flow = flow_of(intents)
        assert flow, f"not a complete flow: {intents}"
        flows[flow] += 1
    assert set(flows) == set(START_FLOWS)
    assert sum(flows.values()) == sum(lg.outcomes.values())
    kinds = collections.Counter(kind for _, kind, _ in fake.applied)
    assert set(kinds) == {"trade", "transfer", "rebalance"}   # every confirm card was confirmed and gave a ticket


def test_cancel_at_a_confirm_card_ends_the_flow_without_a_ticket():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=0)

    async def go():
        async with running(fake, behavior=Behavior(cancel=1, status_ask=1)) as lg:
            await until(lambda: sum(lg.outcomes.values()) >= 60, "60 conversations")
            await quiesce(fake, lg)
            return lg
    lg = scenario(go())
    assert fake.violations == [] and fake.applied == []
    for intents in intents_by_conversation(fake):
        assert flow_of(intents), intents
        if len(intents) > 1 and intents[1] != "general":
            assert intents[-1] == "cancel"
    assert lg.outcomes["rebalance", "cancelled"] > 0 and ("rebalance", "done") not in lg.outcomes


def test_ticket_questions_use_the_ticket_of_the_card_and_stop_when_it_is_done():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=0)

    async def go():
        async with running(fake, behavior=Behavior(cancel=0, status_ask=1, status_ask_twice=1)) as lg:
            await until(lambda: len(fake.applied) >= 40, "40 confirmed cards")
            await quiesce(fake, lg)
    scenario(go())
    assert fake.violations == []   # "Check ticket TKT-..." named a ticket this conversation was given
    asked = collections.Counter(intents.count("check_ticket") for intents in intents_by_conversation(fake) if "confirm" in intents)
    assert asked[2] > 0 and asked[0] == 0 and max(asked) == 2
    assert {"check_ticket"} <= {c["intent"] for c in fake.chats}
    assert any(c["text"].startswith("Check ticket TKT-") for c in fake.chats)
    assert any(c["text"] in ("Where is my transfer?", "Did my trade go through?", "Did my rebalance finish?") for c in fake.chats)


def test_the_surge_shifts_the_flow_mix():
    import random
    rng = random.Random(3)

    def share(surge, flow, n=4000):
        return sum(pick_flow(rng, surge) == flow for _ in range(n)) / n
    base = share({"volume": 1, "flows": {}, "balance_events_factor": 1}, "rebalance")
    assert .07 < base < .13   # START_FLOWS: 10%
    surge = {"volume": 2.4, "flows": {"portfolio": 2.5, "rebalance": 3, "trades": 1.5}}
    assert share(surge, "rebalance") > 1.5 * base   # 0.30 of 1.74 against 0.10 of 1.00
    assert share(surge, "general") < share(None, "general")
    assert share({"flows": {"rebalance": 1000, "bogus": 5}}, "rebalance") > .9

    fake = fake_with(conversations_per_minute=12, transactions_per_minute=0)
    fake.config["surge"] = {"volume": 1, "flows": {"rebalance": 40}, "balance_events_factor": 1}

    async def go():
        async with running(fake) as lg:
            await until(lambda: len(fake.conversations) >= 100, "100 conversations")
            await quiesce(fake, lg)
    scenario(go())
    firsts = collections.Counter(flow_of(i[:1] + ["x"]) or i[0] for i in intents_by_conversation(fake))
    assert firsts["rb_start"] > 0.6 * len(fake.conversations)


def test_the_surge_volume_multiplies_the_conversation_rate():
    def conversations(volume):
        fake = fake_with(conversations_per_minute=6, transactions_per_minute=0)
        fake.config["surge"] = {"volume": volume, "flows": {}, "balance_events_factor": 1}

        async def go():
            async with running(fake, speed=120) as lg:
                t0 = lg.clock.now()
                await until(lambda: lg.clock.now() - t0 > 600, "10 minutes")
                return len(fake.conversations)
        return scenario(go())
    low, high = conversations(1), conversations(5)
    assert 35 < low < 90, low           # 6 a minute for 10 minutes
    assert high > 2.5 * low, (low, high)


def test_the_valuation_rush_starts_valuation_conversations_beyond_the_usual_gap():
    def valuations(per_minute):
        fake = fake_with(conversations_per_minute=0, transactions_per_minute=0)
        fake.config["surge"] = {"volume": 1, "flows": {}, "balance_events_factor": 1, "valuation_per_minute": per_minute}

        async def go():
            async with running(fake, speed=120) as lg:
                t0 = lg.clock.now()
                await until(lambda: lg.clock.now() - t0 > 600, "10 minutes")
                return [c["customer"] for c in fake.chats if c["text"] in VALUATION_ASKS or c["text"] in STARTER_CHIPS[-1:]]
        return scenario(go())
    assert valuations(0) == []                    # no rush, and the conversations setting is 0: nothing starts
    asked = valuations(3)
    assert 18 < len(asked) < 45, len(asked)       # 3 a minute for 10 minutes; the usual gap would allow 3
    assert len(set(asked)) > 3 and {"CUS-0058", "CUS-0059", "CUS-0060"} & set(asked)   # whales and other customers


def test_disabled_stops_new_work_and_enabling_starts_it():
    fake = fake_with(enabled=False, conversations_per_minute=12, transactions_per_minute=12, auto_stories=True)
    fake.config["surge"]["balance_events_factor"] = 100

    async def go():
        async with running(fake, speed=500) as lg:
            await until(lambda: len(fake.stats_posts) >= 3, "three reports")
            t0 = lg.clock.now()
            await until(lambda: lg.clock.now() - t0 > 1500, "25 minutes")   # long enough for a story, a run, a balance event
            assert (fake.chats, fake.orders, fake.transfers, fake.balance_calls, fake.story_starts, fake.run_posts) == ([], [], [], [], [], [])
            assert all(set(p.values()) == {0} for p in fake.stats_posts)   # still reports: the zeros tell the API it is alive
            fake.config["enabled"] = True
            await until(lambda: fake.chats and fake.orders and fake.balance_calls, "work after enabling")
    scenario(go())


def test_abandoned_chats_are_counted_not_errors():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=0)
    fake.hang = lambda customer, version: version == "0.2.0-demo"   # the newest app never gets an answer (fault app_hang_on_send)

    async def go():
        async with running(fake, behavior=Behavior(retry=0)) as lg:
            await until(lambda: lg.counters.abandoned >= 8 and lg.outcomes["general", "done"] + lg.outcomes["portfolio", "done"] >= 3, "abandoned turns and finished ones")
            await quiesce(fake, lg)
            return lg
    lg = scenario(go())
    assert lg.counters.errors == 0 and fake.stats["errors"] == 0
    assert lg.counters.abandoned > 0 and lg.counters.turns >= lg.counters.abandoned
    assert any(r == "abandoned" for _, r in lg.outcomes)
    assert {c["version"] for c in fake.chats if c["version"] != "0.2.0-demo"} <= {"0.1.0-demo", "0.1.1-demo"}
    spans = [s for s in EXPORTER.get_finished_spans() if s.name == "loadgen chat turn"]
    abandoned = [s for s in spans if s.attributes.get("error.type") == "Abandoned"]
    assert len(abandoned) == lg.counters.abandoned and all(s.status.status_code != trace.StatusCode.ERROR for s in abandoned)


def test_the_app_version_is_stable_per_customer_and_mostly_the_newest():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=0)

    async def go():
        async with running(fake) as lg:
            await until(lambda: len(fake.chats) >= 400, "400 chat calls")
            await quiesce(fake, lg)
    scenario(go())
    versions = collections.defaultdict(set)
    for c in fake.chats:
        versions[c["customer"]].add(c["version"])
    assert all(len(v) == 1 for v in versions.values())
    assert all(app_version(c) in v for c, v in versions.items())
    newest = sum(app_version(f"CUS-{n:04d}") == "0.2.0-demo" for n in range(1, 61)) / 60
    assert .55 < newest < .9


def test_pending_trades_are_made_when_a_customer_has_none():
    fake = Fake()   # nobody has a pending trade
    fake.config.update(conversations_per_minute=12, transactions_per_minute=0)
    fake.config["surge"]["flows"] = {"trades": 100}

    async def go():
        async with running(fake, behavior=Behavior(cancel=0)) as lg:
            await until(lambda: sum(lg.outcomes.values()) >= 20, "20 conversations")
            await quiesce(fake, lg)
            return lg
    lg = scenario(go())
    assert lg.outcomes["trades", "done"] > 10 and fake.violations == []


def test_transactions_are_mostly_affordable_and_a_few_are_not():
    fake = fake_with(conversations_per_minute=0, transactions_per_minute=120)

    async def go():
        async with running(fake, speed=50) as lg:
            await until(lambda: lg.counters.transactions >= 800, "800 transactions")
            await quiesce(fake, lg)
    scenario(go())
    orders = [(c, b) for c, b in fake.orders if "side" in b]
    assert {b["mode"] for _, b in orders} == {"now", "pending"}
    assert .1 < sum(b["mode"] == "pending" for _, b in orders) / len(orders) < .3
    actions = collections.Counter(b["action"] for _, b in fake.orders if "action" in b)
    assert actions["submit"] > 0 and actions["cancel"] > 0   # some pending trades are sent on or cancelled; the rest wait
    bad = collections.Counter()
    for c, b in orders:
        held = fake.holdings[c].get(b["symbol"], 0)
        if b["symbol"] == "ORB-EQ":
            bad["restricted"] += 1
        elif b["side"] == "sell" and b["quantity"] > held:
            bad["sell_not_held" if not held else "sell_too_much"] += 1
        elif b["side"] == "buy" and b["quantity"] * fake.prices[b["symbol"]] > fake.customers[c]["cash"]:
            bad["buy_over_cash"] += 1
    assert bad["restricted"] > 0 and bad["sell_not_held"] > 0 and bad["buy_over_cash"] > 0
    assert bad["sell_too_much"] == 0
    assert sum(bad.values()) / len(orders) < .2
    amounts = [b["amount"] for _, b in fake.transfers]
    ordinary = [a for a in amounts if a < 10_000]
    assert len(amounts) > 100 and all(100 <= a <= 1000 for a in ordinary if a >= 100)
    assert sum(a >= 10_000 for a in amounts) > 0 and sum(a >= 10_000 for a in amounts) / len(amounts) < .1   # rare
    assert {b["counterparty"] for _, b in fake.transfers} == {"bank", "card_account"}
    assert {b["direction"] for _, b in fake.transfers} == {"in", "out"}
    withdrawals = [(c, b["amount"]) for c, b in fake.transfers if b["direction"] == "out" and b["amount"] < 10_000]
    assert all(a <= fake.customers[c]["cash"] for c, a in withdrawals)   # an ordinary withdrawal is covered


def test_balance_events_follow_the_base_rate_and_the_factor_in_calls_of_at_most_20():
    def events(factor, seconds, speed):
        fake = fake_with(conversations_per_minute=0, transactions_per_minute=0)
        fake.config["surge"]["balance_events_factor"] = factor

        async def go():
            async with running(fake, speed=speed) as lg:
                t0 = lg.clock.now()
                await until(lambda: lg.clock.now() - t0 > seconds, "the time to pass")
                elapsed = lg.clock.now() - t0
                return sum(fake.balance_calls), max(fake.balance_calls, default=0), elapsed
        return scenario(go())
    # Ten calls of 20 take real time that the clock multiplies by its speed; at 500 a look took 5 to 13 clock
    # seconds, so 30 seconds held only one to three looks and the uncounted last one decided the ratio.
    total, biggest, elapsed = events(300, 30, speed=100)   # 4 a minute times 300 = 20 a second
    assert biggest == 20 and .6 < total / (20 * elapsed) < 1.4, (total, elapsed)
    total, biggest, elapsed = events(1, 900, speed=500)    # 4 a minute: 60 in 15 minutes
    assert 40 < total < 80 and biggest <= 20, (total, elapsed)


def test_an_auto_story_starts_only_when_asked_and_when_none_is_active():
    fake = fake_with(conversations_per_minute=0, transactions_per_minute=0, auto_stories=True)

    async def go():
        async with running(fake, speed=1500) as lg:
            t0 = time.monotonic()
            await until(lambda: fake.story_starts, "the first story")
            name, minutes, at = fake.story_starts[0]
            assert name in STORIES and 5 <= minutes <= 10
            assert (at - t0) * 1500 >= 590   # 10 to 20 minutes after the start
            await until(lambda: lg.clock.now() > (at - t0) * 1500 + 2400, "40 more minutes")
            assert len(fake.story_starts) == 1   # it is still active: no second one
            fake.stories[STORIES.index(name)]["active"] = False
            await until(lambda: len(fake.story_starts) == 2, "the second story")
    scenario(go())

    fake = fake_with(conversations_per_minute=0, transactions_per_minute=0, auto_stories=False)

    async def go_off():
        async with running(fake, speed=1500) as lg:
            await until(lambda: lg.clock.now() > 2400, "40 minutes")
            assert fake.story_starts == []
    scenario(go_off())


def test_runs_start_about_every_ten_minutes_whatever_auto_stories_says():
    fake = fake_with(conversations_per_minute=0, transactions_per_minute=0, auto_stories=False)

    async def go():
        async with running(fake, speed=1500) as lg:
            await until(lambda: lg.clock.now() > 5400, "90 minutes")
    scenario(go())
    assert 2 <= len(fake.run_posts) <= 25   # 9 expected
    assert set(fake.run_posts) <= {"settlement", "sweep", "pricing"}


def test_stats_deltas_add_up():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=12)

    async def go():
        async with running(fake) as lg:
            await until(lambda: len(fake.stats_posts) >= 4 and lg.counters.transactions >= 40, "four reports")
            await quiesce(fake, lg)
            return lg
    lg = scenario(go())
    placed = [o for o in fake.orders if "side" in o[1]] + fake.transfers
    assert fake.stats == {"conversations": len(fake.conversations), "turns": len(fake.chats), "transactions": len(placed), "errors": 0}
    assert fake.stats == {k: getattr(lg.counters, k) for k in fake.stats}   # what the API summed is what the load generator counted
    assert len(fake.stats_posts) >= 4 and any(p["turns"] for p in fake.stats_posts)


def test_counts_survive_a_failed_report():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=6)
    fake.status_override = (503, ["/api/ops/loadgen/stats"])

    async def go():
        async with running(fake) as lg:
            await until(lambda: lg.counters.turns >= 30, "30 turns")
            await asyncio.sleep(0.1)   # a few reports fail
            assert fake.stats_posts == []
            fake.status_override = None
            await until(lambda: fake.stats_posts, "a report")
            await quiesce(fake, lg)
            return lg
    lg = scenario(go())
    assert fake.stats == {k: getattr(lg.counters, k) for k in fake.stats} and fake.stats["turns"] >= 30   # nothing was lost
    assert fake.stats["errors"] == 0   # a failed report is logged, not counted


def test_the_api_down_at_start_and_5xx_do_not_stop_it():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=6)
    fake.down = True

    async def go():
        async with running(fake) as lg:
            await asyncio.sleep(0.2)   # 200 seconds of the load generator's time with no API
            assert fake.chats == [] and not lg.customers
            fake.down = False
            await until(lambda: lg.outcomes and lg.counters.turns > 10, "the first conversations")
            fake.chat_5xx = 8   # the Concierge cannot answer
            fake.status_override = (503, ["/api/trades", "/api/transfers"])
            await until(lambda: fake.chat_5xx == 0, "the 5xx answers to be used up")
            await asyncio.sleep(0.1)
            fake.status_override = None
            before = lg.counters.errors
            await until(lambda: len(fake.orders) > 0 and lg.counters.turns > 40, "work again after the 5xx")
            await quiesce(fake, lg)
            return lg, before
    lg, before = scenario(go())
    assert before >= 8 and fake.stats["errors"] == lg.counters.errors >= 8
    assert fake.stats["turns"] == len(fake.chats), (fake.stats, len(fake.chats), lg.counters)
    assert any(r == "done" for _, r in lg.outcomes)


def test_a_401_means_sign_in_again():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=6)
    fake.token_ttl = 3   # every token stops working after three calls

    async def go():
        async with running(fake) as lg:
            await until(lambda: sum(lg.outcomes.values()) >= 20 and len(fake.orders) >= 10, "conversations and orders")
            await quiesce(fake, lg)
            return lg
    lg = scenario(go())
    assert len(fake.logins) > sum(lg.outcomes.values()) + 20   # more sign-ins than conversations: the tokens ran out in the middle of flows
    assert lg.counters.errors == 0 and fake.violations == []
    assert lg.outcomes["rebalance", "done"] + lg.outcomes["transfer", "done"] > 0


def test_thumbs_down_come_more_often_after_an_answer_that_is_not_ok():
    def ratings(outcome):
        fake = fake_with(conversations_per_minute=12, transactions_per_minute=0)
        fake.outcome = outcome

        async def go():
            async with running(fake) as lg:
                await until(lambda: len(fake.feedback) >= 25, "25 ratings")
                await quiesce(fake, lg)
        scenario(go())
        return collections.Counter(r for _, _, r in fake.feedback)
    assert ratings("tool_timeout_recovered")["down"] > 0.6 * 25   # 30% tap, 80% of the taps are down
    ok = ratings("ok")                                            # 5% tap, 20% of the taps are down
    assert ok["up"] > ok["down"]


def test_spans_are_roots_with_the_attributes_of_the_script_and_nothing_forbidden():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=12)

    async def go():
        async with running(fake) as lg:
            await until(lambda: sum(lg.outcomes.values()) >= 25 and lg.counters.transactions >= 25, "work")
            await quiesce(fake, lg)
    scenario(go())
    spans = EXPORTER.get_finished_spans()
    turns = [s for s in spans if s.name == "loadgen chat turn"]
    txns = [s for s in spans if s.name in ("loadgen trade", "loadgen transfer")]
    assert turns and txns
    for s in turns + txns:
        assert s.parent is None   # the load generator is the client at the start of each trace
        a = s.attributes
        assert a["app.synthetic"] is True and re.fullmatch(r"CUS-00\d\d", a["app.customer.id"])
        assert a["app.customer.name"] == fake.customers[a["app.customer.id"]]["name"]
        assert a["app.loadgen.flow"] and a["app.loadgen.step"]
    assert all(isinstance(s.attributes["app.chat.chip"], bool) for s in turns)
    assert {s.attributes["app.loadgen.flow"] for s in turns} <= set(START_FLOWS)
    assert {s.attributes["app.loadgen.flow"] for s in txns} <= {"txn_trade", "txn_transfer"} | set(START_FLOWS)
    for s in spans:
        assert "gen_ai.operation.name" not in s.attributes
        assert "mo" "ov" not in json.dumps(dict(s.attributes), default=str).lower() + s.name.lower()


def test_settings_come_from_the_environment_and_import_has_no_side_effects(monkeypatch):
    from fakeco import telemetry
    assert telemetry._done is False and not any(h.__class__.__name__ == "LoggingHandler" for h in logging.getLogger().handlers)
    s = Settings.from_env()
    assert (s.api_url, s.max_concurrent, s.seed) == (config.url("concierge-api"), 30, None)
    monkeypatch.setenv("FAKECO_API_URL", "http://example.invalid:1234")
    monkeypatch.setenv("LOADGEN_MAX_CONCURRENT_CONVERSATIONS", "7")
    monkeypatch.setenv("LOADGEN_SEED", "42")
    s = Settings.from_env()
    assert (s.api_url, s.max_concurrent, s.seed) == ("http://example.invalid:1234", 7, 42)


def test_no_more_than_the_maximum_run_at_once():
    fake = fake_with(conversations_per_minute=12, transactions_per_minute=0)
    peak = 0

    async def go():
        nonlocal peak
        async with running(fake, max_concurrent=3, behavior=Behavior(pause_median=20)) as lg:
            await until(lambda: lg.skipped > 5, "arrivals to be turned away")
            for _ in range(50):
                peak = max(peak, lg.conversations_running)
                await asyncio.sleep(0.002)
            await quiesce(fake, lg)
    scenario(go())
    assert 0 < peak <= 3
