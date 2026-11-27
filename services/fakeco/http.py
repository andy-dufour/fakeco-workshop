"""Calls between services. The httpx instrumentation makes the CLIENT span and passes the trace context; this adds who the customer is."""
import contextvars

import httpx
from opentelemetry import trace

from . import config

# who the request is for; set by the service base from the x-customer-id and x-account-id headers, and sent on to the next service
customer = contextvars.ContextVar("customer", default=None)   # {"customer_id", "account_id"}
_client = None


class ServiceError(Exception):
    def __init__(self, service, status, body):
        super().__init__(f"{service} answered HTTP {status}: {str(body)[:200]}")
        self.service, self.status, self.body = service, status, body


def client():
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=3.0), limits=httpx.Limits(max_connections=100, keepalive_expiry=4.0))  # shorter than uvicorn's 5 s keep-alive, so a pooled connection is never reused as the server closes it
    return _client


async def call(service, method, path, *, json=None, params=None, timeout=None, route=None, headers=None, raise_for_status=True):
    """Call another service and return its JSON. `route` is the path template, for the span name (low cardinality)."""
    h = dict(headers or {})
    who = customer.get()
    if who:
        h.setdefault("x-customer-id", who["customer_id"])
        h.setdefault("x-account-id", who["account_id"])
    kw = {"timeout": timeout} if timeout is not None else {}
    r = await client().request(method, config.url(service) + path, json=json, params=params, headers=h, **kw)
    sp = trace.get_current_span()
    if route and sp.is_recording():
        sp.set_attribute("app.peer.route", route)
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    if raise_for_status and r.status_code >= 400:
        raise ServiceError(service, r.status_code, body)
    return body
