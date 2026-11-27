# FakeCo Securities: web frontend

The browser app of the demo broker. React 18, TypeScript, Vite and react-router; no UI framework and no chart library. The build is plain static files. The API it speaks is section 3 of the specification (SPEC.md), and its telemetry follows section 4.

## Run it

```sh
npm install
npm run dev        # Vite on :5173 and the mock API on :8787 (/api is proxied to it)
```

Open http://localhost:5173 and pick a person. Other commands:

```sh
npm run build      # type check (tsc --noEmit), then vite build into dist/
npm run typecheck  # type check only
npm run mock       # only the mock API (mock/server.mjs, plain Node, no dependencies)
npm run preview    # serve dist/ (no /api proxy)
```

Environment variables for development: `API_TARGET` (send `/api` to a real backend instead of the mock), `OTLP_TARGET` (collector for `/otlp`, default `http://localhost:4318`; nothing needs to listen there), `MOCK_PORT` (default 8787), `MOCK_DELAY_MS` (chat delay, default 900).

The mock keeps everything in memory and resets when it restarts. It implements every endpoint of section 3, simulates tickets moving through their states, and answers the chat starter chips with every card type. One thing in it is not in the spec: a chat message that contains `(fail)` gets a 503 the first time, so you can show the error bubble and "Try again".

## Docker

```sh
docker build -t fakeco-securities-web .
docker run --rm -p 8080:8080 -p 8081:8081 \
  -e API_UPSTREAM=http://concierge-api:8080 \
  -e OTLP_UPSTREAM=http://otel-collector:4318 \
  fakeco-securities-web      # open http://localhost:8081, the port without the gate
```

nginx listens on 8080 (public) and 8081 (the same without the demo gate, for the browser load generator) as the non-root `nginx` user. On 8080 the pages and `/api/` need a gate sign-in: nginx asks `GATE_UPSTREAM` (`python -m fakeco.gate serve`, a sidecar in the pod; default `http://127.0.0.1:8090`) with `auth_request`, redirects a page to `/gate/login` and answers an `/api` call with `401` and the code `gate_required`, which `src/api.ts` turns into a redirect to the form. `/otlp/` and `/healthz` are never behind the gate (README.md, "The demo gate"). Without the gate sidecar, run `docker run` as below on port 8081. `/api/` goes to `API_UPSTREAM` with the path kept (read timeout 330 s), `/otlp/` goes to `OTLP_UPSTREAM` with the `/otlp` prefix removed, `/healthz` answers 200, and every other path returns `index.html`. The config is `nginx/default.conf.template`.

## Who sees what

Sign-in is the page at `/signin` (the "Switch person" button in the header goes there). The token and the person are kept in `sessionStorage`, so two tabs can be two people. A person who opens a page of another role is sent to their own home page.

| Role | Pages |
| --- | --- |
| Customer | `/` dashboard, `/chat` Concierge, `/trades`, `/transfers`, `/tickets` |
| Support | `/support` customer search, `/support/customers/:id`, `/support/conversations` (filters), `/support/conversations/:id` transcript and reply, `/support/tickets` (notes) |
| Operations | `/ops` services and queues (message lists, dead-letter replay), `/ops/stories`, `/ops/faults`, `/ops/loadgen`, `/ops/runs` (settlement, sweep, pricing, payroll deposits), `/ops/tickets` |
| Compliance | `/compliance` held transfers (approve, reject), `/compliance/restricted` |

Lists on the operations, compliance and support pages refresh every 5 s. The customer ticket list refreshes every 5 s while a ticket is not in a final state (`completed`, `failed`, `dead_lettered`). An open chat polls its conversation every 5 s for support replies. The open chat shows its conversation ID (the `gen_ai.conversation.id` of the agent spans) above the messages, with a Copy button.

## Telemetry

`src/telemetry.ts` starts the OpenTelemetry browser SDK (`@opentelemetry/browser-sdk` with `@opentelemetry/browser-instrumentation`; not the classic `sdk-trace-web` packages) after `GET /api/config` (it waits at most 2.5 s for it). Resource `service.name` comes from the config (`concierge-web`) and `service.namespace` is `fakeco-securities`. The Fetch instrumentation makes the spans, named like `POST /api/chat`, and `/api` calls carry `traceparent`. The Navigation Timing instrumentation sends one `browser.navigation_timing` log record per page load (it replaces the classic document-load spans). Every span and log record gets `session.id` (one per tab), `app.persona.role` and `app.customer.id` (with `app.customer.name`) or `app.staff.id`; chat sends also get `app.chat.chip`. Spans go to `<otlp_path>/v1/traces` and log records to `<otlp_path>/v1/logs` (OTLP/HTTP JSON). The path is on the page's own origin, so nginx forwards it to the collector and no CORS setup is needed; the collector is not exposed itself. If the collector is missing, the export fails without any message and the app is unaffected.

## Layout of the code

- `src/api.ts`, `src/types.ts`: typed client for the public API.
- `src/auth.tsx`, `src/hooks.ts`: sign-in state; `useApi` (load and poll) and `useAction`.
- `src/components/Cards.tsx`: the chat card types. Unknown card types are ignored.
- `src/pages/<role>/`: pages per role. `src/styles/app.css`: all styles (CSS variables, light and dark by system setting).
