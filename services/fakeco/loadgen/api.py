"""The public API as the load generator sees it: sign in, call with a token, sign in again on 401, never raise."""
import logging, zlib
from dataclasses import dataclass

import httpx

log = logging.getLogger("loadgen")

APP_VERSIONS = ("0.1.0-demo", "0.1.1-demo", "0.2.0-demo")  # the newest last
OPS_USER = "SYS-loadgen"


def app_version(customer_id):
    """Stable for a customer (a hash of the id, not of the run's seed): 75% have the newest release, 15% the one before, 10% the oldest."""
    n = zlib.crc32(customer_id.encode()) % 100
    return APP_VERSIONS[2] if n < 75 else APP_VERSIONS[1] if n < 90 else APP_VERSIONS[0]


@dataclass
class Reply:
    status: int          # 0: no HTTP answer (network error, timeout, sign-in failed)
    data: object = None  # the decoded JSON body
    error: str = ""

    @property
    def ok(self):
        return 200 <= self.status < 300


class Api:
    def __init__(self, client, counters):
        self.client, self.counters = client, counters
        self.tokens = {}

    async def login(self, user_id, count_errors=True):
        """Returns the token, or None. A failure is logged and counted, never raised."""
        r = await self._send("POST", "/api/login", None, json={"user_id": user_id})
        token = r.data.get("token") if r.ok and isinstance(r.data, dict) else None
        if token:
            self.tokens[user_id] = token
        else:
            self._failed(r, "POST /api/login", user_id, count_errors, False)
        return token

    async def call(self, user_id, method, path, *, json=None, params=None, timeout=15.0, count_errors=True, expect_4xx=False):
        """One authenticated call as `user_id`. On 401 it signs in again and repeats the call once. `expect_4xx`: a 4xx is the answer to a
        request that was meant to be refused (not an error). A chat call passes timeout=None: the caller sets its own deadline."""
        r = Reply(0, error="not signed in")
        for attempt in (1, 2):
            token = self.tokens.get(user_id) or await self.login(user_id, count_errors)
            if not token:
                return Reply(0, error="sign-in failed")
            r = await self._send(method, path, token, json=json, params=params, timeout=timeout, version=app_version(user_id) if user_id.startswith("CUS-") else None)
            if r.status != 401:
                break
            self.tokens.pop(user_id, None)   # expired or revoked: sign in again
            log.info("401 for %s on %s %s, signing in again", user_id, method, path)
        self._failed(r, f"{method} {path}", user_id, count_errors, expect_4xx)
        return r

    async def _send(self, method, path, token, *, json=None, params=None, timeout=15.0, version=None):
        headers = {"authorization": f"Bearer {token}"} if token else {}
        if version:
            headers["x-app-version"] = version
        try:
            resp = await self.client.request(method, path, json=json, params=params, headers=headers, timeout=httpx.Timeout(timeout, connect=5.0))
        except httpx.HTTPError as e:   # includes timeouts and connection errors
            return Reply(0, error=type(e).__name__)
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        error = ""
        if resp.status_code >= 400:
            e = data.get("error") if isinstance(data, dict) else None
            error = e.get("code", "") if isinstance(e, dict) else str(data)[:80]
        return Reply(resp.status_code, data, error)

    def _failed(self, r, what, user_id, count_errors, expect_4xx):
        if r.ok:
            return
        if expect_4xx and 400 <= r.status < 500:
            return
        log.warning("%s as %s failed: %s %s", what, user_id, r.status or "no answer", r.error)
        if count_errors:
            self.counters.errors += 1
