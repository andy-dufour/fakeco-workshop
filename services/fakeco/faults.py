"""Fault flags: rows in the `faults` table that the operations page (or a story) turns on. Services read them with a short cache."""
import random, time

from . import db

_cache, _at = {}, 0.0
TTL_S = 3.0


async def _load():
    global _cache, _at
    if time.monotonic() - _at > TTL_S:
        rows = await db.fetch("SELECT name, params FROM faults WHERE active AND (starts_at IS NULL OR starts_at <= now()) AND (expires_at IS NULL OR expires_at > now())", quiet=True)
        _cache, _at = {r["name"]: _now(r["params"] or {}) for r in rows}, time.monotonic()
    return _cache


def _now(params):
    """A fault's params at this moment. `then` is a schedule for a story that changes one fault part way through:
    [{"at": unix seconds, "params": {...}}]; each entry whose time has come overrides what is before it."""
    out = {k: v for k, v in params.items() if k != "then"}
    for step in sorted(params.get("then") or [], key=lambda s: s["at"]):
        if step["at"] <= time.time():
            out.update(step["params"])
    return out


async def get(name):
    """The params of an active fault, or None."""
    return (await _load()).get(name)


async def hit(name, key="rate", default=1.0):
    """True when the fault is active and a draw falls under its rate. Returns the params then, else None."""
    p = await get(name)
    if p is not None and random.random() < float(p.get(key, default)):
        return p
    return None


async def factor(name, key="factor", default=1.0):
    """A slow-down factor: the fault's `factor` when active, else 1."""
    p = await get(name)
    return float(p.get(key, default)) if p is not None else 1.0
