"""Small helpers: a clock that can run fast (tests), the counters that go to the API, and random draws."""
import asyncio, contextlib, math, time
from dataclasses import dataclass

from opentelemetry import context as otel_context

from .. import telemetry


class Clock:
    """Time in seconds that passes `speed` times as fast as real time. Every wait and every timeout of the load generator goes through it."""

    def __init__(self, speed=1.0):
        self.speed, self._t0 = float(speed), time.monotonic()

    def now(self):
        return (time.monotonic() - self._t0) * self.speed

    async def sleep(self, seconds):
        await asyncio.sleep(max(0.0, seconds) / self.speed)

    async def wait_for(self, awaitable, seconds):
        """Raises asyncio.TimeoutError after `seconds` of clock time."""
        return await asyncio.wait_for(awaitable, max(0.0, seconds) / self.speed)


SENT = ("conversations", "turns", "transactions", "errors")


@dataclass
class Counters:
    """Running totals. `abandoned` is kept here only (the API has no field for it); the other four are the body of POST /api/ops/loadgen/stats."""
    conversations: int = 0
    turns: int = 0
    transactions: int = 0
    errors: int = 0
    abandoned: int = 0

    def since(self, reported):
        """The four reported numbers minus what an earlier report already carried."""
        return {k: getattr(self, k) - getattr(reported, k) for k in SENT}


def poisson(rng, lam):
    """A Poisson draw: Knuth's method for small means, a normal approximation for large ones."""
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def human_pause(rng, median=6.0, lo=2.0, hi=30.0):
    """Seconds a person takes before the next message: log-normal around `median`."""
    return min(hi, max(lo, rng.lognormvariate(math.log(median), 0.5)))


names = {}   # customer id -> full name, filled from /api/personas


@contextlib.contextmanager
def span(name, customer=None, flow=None, step=None, chip=None, **attrs):
    """A span of the load generator's own, the root of one trace (the httpx CLIENT spans of the calls inside it are its children)."""
    a = {"app.synthetic": True, "app.customer.id": customer, "app.customer.name": names.get(customer), "app.loadgen.flow": flow, "app.loadgen.step": step, "app.chat.chip": chip, **attrs}
    a = {k: v for k, v in a.items() if v is not None}
    with telemetry.tracer().start_as_current_span(name, context=otel_context.Context(), attributes=a) as sp:
        yield sp
