"""The model the agents call. It is scripted: no provider is called and no key is needed.

create() takes and returns the shape of the Anthropic Messages API, so a real provider can be put behind the same call later. What the scripted model
"decides" comes from agents/policies.py; this module simulates the rest of a model call: token counts, latency, the context window, HTTP 429 with a
retry, and a call that hangs. The only span it makes is the HTTP request to the provider, as an HTTP client library would.
"""
import asyncio, json, math, os, random, secrets

from opentelemetry.trace import SpanKind

from . import config, faults, telemetry

OVERHEAD_TOKENS = {"Concierge": 960}   # the tool definitions and framing of a request; the specialists have fewer tools
SIMULATED = {"app.model.simulated": True}
_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZabcdefghjkmnpqrstvwxyz0123456789"
# How much slower than Haiku a model family is, on every part of a call (prompt, reasoning, output), by the first family named in the model id.
# Roughly the ratio of output speeds on the provider: Haiku about 120 tokens/s, Sonnet about 70, Opus about 40. An unknown model counts as Haiku.
MODEL_WEIGHTS = {"haiku": 1.0, "sonnet": 1.7, "opus": 3.0}


class ModelError(Exception):
    """The provider rejected the request."""
    def __init__(self, error_type, message, status):
        super().__init__(message)
        self.error_type, self.status = error_type, status


def jd(x):
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str)


def new_id(prefix):
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(24))


def speed():
    """A factor on every simulated wait; the tests set it to 0."""
    return float(os.environ.get("FAKECO_MODEL_SPEED", "1"))


def weight(model_id):
    """The latency weight of a model id (MODEL_WEIGHTS): claude-opus-4-1-20250805 -> 3.0."""
    return next((w for family, w in MODEL_WEIGHTS.items() if family in model_id.lower()), 1.0)


def parts(messages):
    """Messages API messages as the parts format of the GenAI semantic conventions (what the spans record). Each tool result is a message of its own."""
    out = []
    for m in messages:
        blocks = m["content"] if isinstance(m["content"], list) else [{"type": "text", "text": m["content"]}]
        ps = []
        for b in blocks:
            if b["type"] == "text":
                ps.append({"type": "text", "content": b["text"]})
            elif b["type"] == "tool_use":
                ps.append({"type": "tool_call", "id": b["id"], "name": b["name"], "arguments": b["input"]})
            elif b["type"] == "tool_result":
                out.append({"role": "tool", "parts": [{"type": "tool_call_response", "id": b["tool_use_id"], "response": b["content"]}]})
        if ps:
            out.append({"role": m["role"], "parts": ps})
    return out


async def _post(status, seconds, error=None):
    """One HTTP request to the provider, as a span. Nothing is sent."""
    with telemetry.tracer().start_as_current_span("POST", kind=SpanKind.CLIENT, attributes={
            "http.request.method": "POST", "server.address": "api.anthropic.com", "url.path": "/v1/messages", "http.response.status_code": status, **SIMULATED}) as sp:
        await asyncio.sleep(seconds * speed())
        if error:
            telemetry.fail(sp, str(status), error)


async def create(agent, system, messages, tools, decide, *, stall=False):
    """One model call. `decide(system, messages, tools)` returns the content blocks of the answer.
    stall: the provider never answers (the caller gives up and cancels). Raises ModelError when the request is rejected."""
    in_tok = OVERHEAD_TOKENS.get(agent, 620) + max(1, len(jd(parts(messages))) // 4)
    name = config.model_for(agent)
    if stall:
        await asyncio.Event().wait()
    if in_tok > config.CONTEXT_LIMIT_TOKENS:   # rejected before anything is generated
        message = f"prompt is too long: {in_tok} tokens > {config.CONTEXT_LIMIT_TOKENS} maximum"
        await _post(400, random.uniform(.4, .9), message)
        raise ModelError("ContextWindowExceededError", message, 400)
    content = decide(system, messages, tools)
    out_tok = max(1, len(jd(content)) // 4)
    reason = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
    if await faults.hit("model_rate_limit"):
        await _post(429, random.uniform(.06, .15), "provider rate limit (HTTP 429)")
        await asyncio.sleep(random.uniform(.5, 1.5) * speed())
    # latency as on a real provider, for Haiku: reading the prompt (about 1 s per 25k input tokens), reasoning over the tool results that just came
    # back (about 1.3 s each), then writing the answer (6 ms per output token); a slower model takes its MODEL_WEIGHTS times as long
    results = sum(1 for b in messages[-1]["content"] if b.get("type") == "tool_result") if isinstance(messages[-1]["content"], list) else 0
    noise, factor = math.exp(random.gauss(0, .22)), await faults.factor("model_slow") * weight(name)
    first = (.6 + in_tok / 25_000 + 1.3 * results) * noise * factor   # until the first token
    await _post(200, first + 6 * out_tok / 1000 * noise * factor)
    return {"id": new_id("msg_"), "role": "assistant", "model": name, "content": content, "stop_reason": reason,
            "usage": {"input_tokens": in_tok, "output_tokens": out_tok}}
