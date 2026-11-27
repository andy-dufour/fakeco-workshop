# FakeCo Securities

A pretend broker for the Honeycomb Goal Agents workshop. It has a web app, a chat with a "Concierge" agent, five specialist agents, 22 small services, queues and a load generator that plays 60 customers. Nothing in it reaches a real trading system, bank or model provider: the money, the venue, the bank and the model are all simulated.

| Where | What |
| --- | --- |
| `SPEC.md` | The public API, the people, the chat cards and the telemetry conventions |
| `services/INTERNALS.md` | Tables, queue messages, internal routes and the back-office processes |
| `services/fakeco/` | All Python code. One image; `python -m fakeco.run <service>` starts one service |
| `services/fakeco/agents/` | The agent loop, the tools, and `policies.py`: what the scripted model "decides" |
| `services/fakeco/model.py` | The simulated model call: tokens, latency, HTTP 429, the context window |
| `web/` | The React app and its nginx image |
| `k8s/` | Manifests for the namespace `fakeco`. `services.generated.yaml` is written by `scripts/render_services.py` |

## People

There are no passwords: the sign-in page lists everyone, and each browser tab keeps its own person.

- Account holders `CUS-0001` to `CUS-0060`
- Support `SUP-ava`, `SUP-ben`
- Operations `OPS-kai`, `OPS-mia`: service health, queues, the load generator
- Compliance `CMP-lee`

## Run it on Kubernetes

```sh
scripts/build-images.sh        # fakeco-services:dev and fakeco-web:dev, saved to .build/
kubectl apply -k k8s
```

The web app is a NodePort on 30080.

## Telemetry

Every service sends OTLP/HTTP to `http://otel-collector:4318` (`OTEL_EXPORTER_OTLP_ENDPOINT` in `k8s/config.yaml`). **There is no collector in `k8s/`: you deploy one.** It needs a Service named `otel-collector` in the namespace `fakeco`.

`concierge-api` reads `HONEYCOMB_CONFIGURATION_KEY` and `HONEYCOMB_API_URL` from the Secret `honeycomb` when it exists, for "View trace" links and deploy markers.

## The model

No model provider is called. `model.py` `create()` has the request and response shape of the Anthropic Messages API, and `agents/policies.py` decides what the "model" answers from the message and the tool results. The agent loop, the tool calls, the specialists and every back-end request are real, and a model call takes as long as on a real provider. Token counts are computed from the real history, so a long conversation costs more.

## Checks

```sh
cd services
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
FAKECO_LOCAL=1 .venv/bin/python -m pytest tests -q
```
