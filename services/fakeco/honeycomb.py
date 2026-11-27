"""What concierge-api does in Honeycomb besides sending telemetry: a link to a trace, and a marker when a story starts.

Both are optional and best effort. They need HONEYCOMB_CONFIGURATION_KEY (a configuration key that may create markers); without it, or when
Honeycomb does not answer, the link is left out and no marker is made. The key is never logged.
"""
import asyncio, logging, os, time

import httpx
from opentelemetry.instrumentation.utils import suppress_instrumentation

log = logging.getLogger("fakeco.honeycomb")
RETRY_S = 300
_info, _tried_at = {}, {}   # per target name and key: a team that gets a new key is checked again


def _targets():
    """(name, API URL, configuration key) of every destination that has a key."""
    found = [("main", os.environ.get("HONEYCOMB_API_URL", "https://api.honeycomb.io").rstrip("/"), os.environ.get("HONEYCOMB_CONFIGURATION_KEY", "").strip())]
    return [t for t in found if t[2]]


def _ui(api):
    return os.environ.get("HONEYCOMB_UI_URL", "https://ui.eu1.honeycomb.io" if ".eu1." in api else "https://ui.honeycomb.io").rstrip("/")


async def _request(target, method, path, json=None):
    _, api, key = target
    with suppress_instrumentation():   # no spans for calls to the telemetry vendor
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.request(method, api + path, json=json, headers={"X-Honeycomb-Team": key})
            r.raise_for_status()
            return r.json()


async def info(target):
    """{"team": slug, "environment": slug} of the target's configuration key, or None. The environment comes from the key, never from a setting."""
    name, cache = target[0], (target[0], target[2])
    if _info.get(cache) or time.monotonic() - _tried_at.get(cache, 0) < RETRY_S and cache in _tried_at:
        return _info.get(cache)
    _tried_at[cache] = time.monotonic()
    try:
        a = await _request(target, "GET", "/1/auth")
        found = {"team": a["team"]["slug"], "environment": a["environment"]["slug"]}
    except Exception as e:
        log.warning("Honeycomb (%s) did not accept the configuration key (%s); no trace links or markers there", name, type(e).__name__)
        return None
    _info[cache] = found
    return found


async def trace_url():
    """A link template with {trace_id} into the main environment, or None."""
    main = [t for t in _targets() if t[0] == "main"]
    i = main and await info(main[0])
    return f"{_ui(main[0][1])}/{i['team']}/environments/{i['environment']}/trace?trace_id={{trace_id}}" if i else None


async def marker(message, kind, start, end=None):
    """A marker on every dataset of each environment that has a configuration key, all at once. Returns True when every one of them took it."""
    body = {"message": message[:200], "type": kind, "start_time": int(start)}
    if end:
        body["end_time"] = int(end)

    async def one(t):
        if not await info(t):
            return False
        try:
            await _request(t, "POST", "/1/markers/__all__", json=body)
            return True
        except Exception as e:
            log.warning("Honeycomb (%s) did not take the marker (%s)", t[0], type(e).__name__)
            return False

    targets = _targets()
    took = await asyncio.gather(*(one(t) for t in targets))
    return bool(targets) and all(took)


if __name__ == "__main__":
    # python -m fakeco.honeycomb <type> <message> [start epoch seconds]: a marker from now (or start) to now
    import sys
    logging.basicConfig()
    kind, message, *start = sys.argv[1:]
    now = time.time()
    took = asyncio.run(marker(message, kind, float(start[0]) if start else now, now if start else None))
    print("marker made" if took else "no marker (no configuration key, or Honeycomb did not take it)")
