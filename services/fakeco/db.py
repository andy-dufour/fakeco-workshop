"""Postgres access. Every statement gets a CLIENT span named "<OPERATION> <table>", as in the simulator."""
import asyncio, logging, re

from opentelemetry.trace import SpanKind
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import config, telemetry

log = logging.getLogger("fakeco.db")
_pool = None
_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE)\s+([a-z_]+)", re.I)
J = Jsonb  # wrap a dict or list that goes into a jsonb column


async def open_pool(wait_for_schema=True):
    """Open the pool, and wait until the seed job has made the schema (the services start in any order)."""
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(config.database_url(), min_size=1, max_size=8, open=False, kwargs={"row_factory": dict_row, "autocommit": True})
        await _pool.open()
    while wait_for_schema:
        try:
            async with _pool.connection() as c:
                if await (await c.execute("SELECT 1 FROM settings WHERE key = 'schema_version'")).fetchone():
                    return
        except Exception as e:  # the database is not up yet, or the table is not there yet
            log.info("waiting for the database: %s", str(e).splitlines()[0] if str(e) else type(e).__name__)
        await asyncio.sleep(2)


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def pool():
    return _pool


async def _run(sql, args, how, quiet=False, delay_s=0.0, attrs=None, latency_s=0.0):
    """delay_s: the statement takes this much longer (a slow database, for the fault flags): pg_sleep on the same connection, inside the span.
    latency_s: a store that is far away: a wait inside the span before a connection is taken, so it holds no connection."""
    op = sql.lstrip().split(None, 1)[0].upper()
    m = _TABLE.search(sql)
    table = m.group(1) if m else "?"
    if quiet:  # housekeeping (queue polls, fault flags): no span, or the traces fill up with noise
        async with _pool.connection() as c:
            return await how(await c.execute(sql, args))
    with telemetry.tracer().start_as_current_span(f"{op} {table}", kind=SpanKind.CLIENT, attributes={
            "db.system.name": "postgresql", "db.operation.name": op, "db.collection.name": table, "db.query.text": " ".join(sql.split())[:1000], **(attrs or {})}) as sp:
        try:
            if latency_s > 0:
                await asyncio.sleep(latency_s)
            async with _pool.connection() as c:
                if delay_s > 0:
                    await c.execute("SELECT pg_sleep(%s)", (float(delay_s),))
                out = await how(await c.execute(sql, args))
        except asyncio.CancelledError:
            telemetry.fail(sp, "CancelledError", "query cancelled: the caller went away")
            raise
        if isinstance(out, list):
            sp.set_attribute("db.response.returned_rows", len(out))
        return out


async def _all(cur): return await cur.fetchall()
async def _one(cur): return await cur.fetchone()
async def _count(cur): return cur.rowcount


async def fetch(sql, *args, quiet=False, delay_s=0.0, attrs=None, latency_s=0.0): return await _run(sql, args, _all, quiet, delay_s, attrs, latency_s)
async def fetchrow(sql, *args, quiet=False, delay_s=0.0, attrs=None): return await _run(sql, args, _one, quiet, delay_s, attrs)
async def execute(sql, *args, quiet=False, delay_s=0.0, attrs=None): return await _run(sql, args, _count, quiet, delay_s, attrs)


async def fetchval(sql, *args, quiet=False):
    row = await fetchrow(sql, *args, quiet=quiet)
    return next(iter(row.values())) if row else None
