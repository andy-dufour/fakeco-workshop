"""OpenTelemetry setup: traces and logs over OTLP/HTTP to the collector. See exporting() for when it exports.
No metrics: the traces have every request, and the collector sends the pod and container metrics (k8s/collector.yaml)."""
import logging, os

from opentelemetry import trace
from opentelemetry._logs import get_logger_provider, set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from . import config

_done = False
_version = config.VERSION   # the service.version of everything that leaves this process from now on; a release flag change sets a new one (api.change_marker)


def version():
    return _version


def set_version(v):
    global _version
    _version = v


class VersionStamp(SpanExporter):
    """Wraps the span exporter: a span leaves with the current service.version, not the one the process started with. A resource is fixed when the
    process starts, so a flag change can only reach it here."""
    def __init__(self, inner):
        self.inner = inner

    def export(self, spans):
        stamped = {}
        for s in spans:
            r = s.resource
            if r.attributes.get("service.version") != _version:
                s._resource = stamped.setdefault(id(r), r.merge(Resource({"service.version": _version})))
        return self.inner.export(spans)

    def shutdown(self):
        self.inner.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.inner.force_flush(timeout_millis)


def exporting():
    """Export only when an endpoint is set, and on one machine (FAKECO_LOCAL=1) only when FAKECO_EXPORT=1 too: a developer's shell may carry an
    OTEL_EXPORTER_OTLP_ENDPOINT that is not meant for this system."""
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    return os.environ.get("FAKECO_LOCAL") != "1" or os.environ.get("FAKECO_EXPORT") == "1"


def resource(service):
    return Resource.create({"service.name": service, "service.version": config.VERSION, "service.namespace": "fakeco-securities",
                            "deployment.environment.name": config.environment()})


def logger_provider(service):
    lp = LoggerProvider(resource=resource(service))
    if exporting():
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    return lp


def init(service):
    """Called once per process, from main(), never at import time."""
    global _done
    if _done:
        return
    _done = True
    tp = TracerProvider(resource=resource(service))
    if exporting():
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        tp.add_span_processor(BatchSpanProcessor(VersionStamp(OTLPSpanExporter()), schedule_delay_millis=2000))
    trace.set_tracer_provider(tp)
    lp = logger_provider(service)
    set_logger_provider(lp)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(logging.StreamHandler())
    root.addHandler(LoggingHandler(level=logging.INFO, logger_provider=lp))
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    HTTPXClientInstrumentor().instrument()


def tracer():
    return trace.get_tracer("fakeco")


def logger(name):
    """An OpenTelemetry logger of this process, for log records that are written directly (the evaluation records). Same service.name as its spans."""
    return get_logger_provider().get_logger(name)


def fail(span, exc_type, message):
    """Mark a span as failed without raising."""
    span.set_attribute("error.type", exc_type)
    span.set_status(trace.Status(trace.StatusCode.ERROR, message))
    span.add_event("exception", {"exception.type": exc_type, "exception.message": message})
