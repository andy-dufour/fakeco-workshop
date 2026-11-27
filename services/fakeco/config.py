"""Settings and the list of services. Everything comes from the environment; nothing is read at import time except defaults."""
import os

VERSION = "0.1.0"
MODEL = os.environ.get("FAKECO_MODEL_NAME", "claude-haiku-4-5-20251001")   # the model of every agent that FAKECO_AGENT_MODELS does not name
PROVIDER = "anthropic"
CONTEXT_LIMIT_TOKENS = 200_000
TOOL_LIMIT_S = 8.0
MAX_ATTEMPTS = 3
HOLD_AMOUNT_USD = 10_000
LATEST_APP_VERSION = "0.2.0-demo"
HANG_CAP_S = 300.0   # concierge-api gives up on the Concierge after this long
SPECIALIST_LIMIT_S = HANG_CAP_S - 30   # the Concierge gives up on a specialist after this long, and still has time to answer

# name -> kind, module that holds `app`, port when everything runs on one machine (FAKECO_LOCAL=1)
SERVICES = {
    "concierge-api": ("edge", "fakeco.api", 9000),
    "concierge-orchestrator": ("agent", "fakeco.agents.service", 9001),
    "portfolio-specialist": ("agent", "fakeco.agents.service", 9002),
    "trades-specialist": ("agent", "fakeco.agents.service", 9003),
    "transfers-specialist": ("agent", "fakeco.agents.service", 9004),
    "rebalance-specialist": ("agent", "fakeco.agents.service", 9005),
    "ticket-specialist": ("agent", "fakeco.agents.service", 9006),
    "portfolio-service": ("backend", "fakeco.backends.portfolio", 9010),
    "trades-service": ("backend", "fakeco.backends.trades", 9011),
    "transfers-service": ("backend", "fakeco.backends.transfers", 9012),
    "ticket-status-service": ("backend", "fakeco.backends.tickets", 9013),
    "order-router": ("backoffice", "fakeco.backoffice.order_router", 9020),
    "ledger-service": ("backoffice", "fakeco.backoffice.ledger", 9021),
    "compliance-service": ("backoffice", "fakeco.backoffice.compliance", 9022),
    "venue-gateway": ("backoffice", "fakeco.backoffice.venue", 9023),
    "fraud-service": ("backoffice", "fakeco.backoffice.fraud", 9024),
    "ach-gateway": ("backoffice", "fakeco.backoffice.ach", 9025),
    "sweep-service": ("backoffice", "fakeco.backoffice.sweep", 9026),
    "wallet-balance-service": ("backoffice", "fakeco.backoffice.wallet", 9027),
    "settlement-service": ("backoffice", "fakeco.backoffice.settlement", 9028),
    "notification-service": ("backoffice", "fakeco.backoffice.notification", 9029),
    "market-data-service": ("backoffice", "fakeco.backoffice.market_data", 9030),
}
# queue -> the service that consumes it
QUEUES = {"orders": "order-router", "transfers.outbound": "transfers-service", "sweeps": "sweep-service", "ach.returns": "ach-gateway",
          "settlement.instructions": "settlement-service", "notifications": "notification-service"}


def model_for(agent):
    """The model an agent calls: its entry in FAKECO_AGENT_MODELS ("PortfolioSpecialist=claude-opus-4-1-20250805,Concierge=claude-sonnet-4-5-20250929"), else MODEL."""
    pairs = [p.strip() for p in os.environ.get("FAKECO_AGENT_MODELS", "").split(",") if p.strip()]
    if any("=" not in p for p in pairs):
        raise ValueError(f"FAKECO_AGENT_MODELS must be Agent=model pairs separated by commas, not {os.environ['FAKECO_AGENT_MODELS']!r}")
    return dict(tuple(x.strip() for x in p.split("=", 1)) for p in pairs).get(agent, MODEL)


def service_name():
    return os.environ.get("SERVICE_NAME", "unknown-service")


def environment():
    return os.environ.get("FAKECO_ENV", "demo")


def story_markers():
    """False when FAKECO_STORY_MARKERS is "off": starting a story then puts no marker in Honeycomb, whose title would name the faults."""
    return os.environ.get("FAKECO_STORY_MARKERS", "on").strip().lower() != "off"


def story_build_markers():
    """Story names from FAKECO_STORY_BUILD_MARKERS (comma-separated): starting one of them puts a "deploy" marker in Honeycomb that looks like a release."""
    return {s.strip() for s in os.environ.get("FAKECO_STORY_BUILD_MARKERS", "").split(",") if s.strip()}


def fault_attributes():
    """True when FAKECO_FAULT_ATTRIBUTES is "on": changing a fault flag then puts app.fault.name and app.fault.active on its span, which names the fault."""
    return os.environ.get("FAKECO_FAULT_ATTRIBUTES", "off").strip().lower() == "on"


def url(service):
    """Base URL of another service: its Kubernetes Service, or a local port when FAKECO_LOCAL=1."""
    if os.environ.get("FAKECO_LOCAL") == "1":
        return f"http://127.0.0.1:{SERVICES[service][2]}"
    return f"http://{service}:8080"


def database_url():
    return os.environ.get("DATABASE_URL", "postgresql://fakeco:fakeco@127.0.0.1:5432/fakeco")
