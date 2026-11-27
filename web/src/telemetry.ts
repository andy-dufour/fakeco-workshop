// Browser telemetry, section 4 of the specification: OTLP/HTTP JSON to
// <otlp_path>/v1/traces, W3C traceparent on /api calls, and the app.* and
// session.id attributes on every span. Built on the OpenTelemetry browser SDK
// (@opentelemetry/browser-sdk); the classic opentelemetry-js web packages are
// not used. Everything here fails quietly: if the collector is not there, the
// app works the same.
import type { Context } from "@opentelemetry/api";
import { startBrowserSdk } from "@opentelemetry/browser-sdk";
import { browserDetector } from "@opentelemetry/browser-sdk/browser-detector";
import { ErrorsInstrumentation } from "@opentelemetry/browser-instrumentation/experimental/errors";
import { FetchInstrumentation } from "@opentelemetry/browser-instrumentation/experimental/fetch";
import { NavigationInstrumentation } from "@opentelemetry/browser-instrumentation/experimental/navigation";
import { NavigationTimingInstrumentation } from "@opentelemetry/browser-instrumentation/experimental/navigation-timing";
import { ResourceTimingInstrumentation } from "@opentelemetry/browser-instrumentation/experimental/resource-timing";
import { UserActionInstrumentation } from "@opentelemetry/browser-instrumentation/experimental/user-action";
import { WebVitalsInstrumentation } from "@opentelemetry/browser-instrumentation/experimental/web-vitals";
import type { LogRecordProcessor, ReadWriteLogRecord } from "@opentelemetry/sdk-logs";
import type { Span, SpanProcessor } from "@opentelemetry/sdk-trace";
import { session } from "./api";
import type { AppConfig, Person } from "./types";

const SESSION_KEY = "fakeco.session";

/** One id per browser tab, so two tabs are two sessions. */
export const sessionId: string = (() => {
  let id = sessionStorage.getItem(SESSION_KEY);
  if (!id) {
    id = crypto.randomUUID();
    sessionStorage.setItem(SESSION_KEY, id);
  }
  return id;
})();

let persona: Person | null = session.user;

/** Called by the sign-in code whenever the person changes. */
export function setTelemetryPersona(p: Person | null) {
  persona = p;
}

/** The attributes that belong on every browser span and log record. */
function personaAttributes(): Record<string, string> {
  const attrs: Record<string, string> = { "session.id": sessionId };
  if (persona) {
    attrs[persona.role === "customer" ? "app.customer.id" : "app.staff.id"] = persona.id;
    if (persona.role === "customer") attrs["app.customer.name"] = persona.name;
    attrs["app.persona.role"] = persona.role;
  }
  return attrs;
}

class PersonaSpanProcessor implements SpanProcessor {
  onStart(span: Span, _ctx: Context) {
    void _ctx;
    span.setAttributes(personaAttributes());
  }
  onEnd() {}
  forceFlush() {
    return Promise.resolve();
  }
  shutdown() {
    return Promise.resolve();
  }
}

class PersonaLogRecordProcessor implements LogRecordProcessor {
  onEmit(record: ReadWriteLogRecord) {
    record.setAttributes(personaAttributes());
  }
  forceFlush() {
    return Promise.resolve();
  }
  shutdown() {
    return Promise.resolve();
  }
}

let started = false;

export function startTelemetry(config: AppConfig) {
  if (started || !config.otlp_path) return;
  started = true;
  try {
    // The SDK wants an absolute URL for each signal; otlp_path is a path on this origin (nginx forwards it to the collector).
    const base = new URL(config.otlp_path.replace(/\/$/, ""), location.origin).href;
    // Our own exports must not be traced or timed, or each export would create the next one.
    const ignoreUrls = [new RegExp(`${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/v1/`)];
    startBrowserSdk({
      logLevel: "ERROR",
      serviceName: config.service_name || "concierge-web",
      serviceVersion: config.version,
      resourceAttributes: {
        // browser.language, user_agent.original, and (Chromium only) browser.brands, browser.platform, browser.mobile
        ...browserDetector.detect().attributes,
        "service.namespace": "fakeco-securities",
        "deployment.environment.name": config.environment,
      },
      // The persona processors come first, so the attributes are set before the exporter's batch processor sees the span or record.
      traces: { processors: [new PersonaSpanProcessor()], exportConfig: { url: `${base}/v1/traces` } },
      logs: { processors: [new PersonaLogRecordProcessor()], exportConfig: { url: `${base}/v1/logs` } },
      instrumentations: [
        // Page load: a browser.navigation_timing log record in place of the classic document-load spans.
        new NavigationTimingInstrumentation(),
        // Log events: route changes, uncaught errors, clicks, Core Web Vitals, and one record per loaded resource.
        new NavigationInstrumentation(),
        new ErrorsInstrumentation(),
        new UserActionInstrumentation(),
        new WebVitalsInstrumentation(),
        new ResourceTimingInstrumentation({ ignoreUrls }),
        new FetchInstrumentation({
          // Same-origin calls get traceparent anyway; this makes it explicit.
          propagateTraceHeaderCorsUrls: [/\/api\//],
          ignoreUrls,
          requestHook(span, request) {
            // Name the span "POST /api/chat" like the others, and mark chat
            // sends with app.chat.chip.
            try {
              const attrs = (span as unknown as { attributes?: Record<string, unknown> }).attributes ?? {};
              const method = String(attrs["http.request.method"] ?? ("method" in request && request.method) ?? "GET");
              const path = new URL(String(attrs["url.full"] ?? ""), location.href).pathname;
              span.updateName(`${method.toUpperCase()} ${path}`);
              if (path === "/api/chat" && "body" in request && typeof request.body === "string") {
                span.setAttribute("app.chat.chip", JSON.parse(request.body).chip === true);
              }
            } catch {
              /* keep the default name */
            }
          },
        }),
      ],
    });
  } catch {
    /* telemetry must never break the app */
  }
}
