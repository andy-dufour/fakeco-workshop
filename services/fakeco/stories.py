"""The fault flags and the 13 stories of the telemetry generator (concierge/beats.py), as things the live system can do.

A story turns on one or more faults for a while. `from` and `to` are shares of the story's length, so a two-part story (the bank outage, then the
ACH returns) plays its parts one after the other.
"""
# name -> (description, default params, the service that reads it)
FAULTS = {
    "model_rate_limit": ("The model provider answers HTTP 429 on this share of calls; the agent retries after a back-off.", {"rate": 0.45}, "agents"),
    "model_slow": ("Model calls take this many times longer.", {"factor": 1.8}, "agents"),
    "trades_service_slow": ("trades-service is slow, and this share of its calls runs past the 8 s tool limit.", {"factor": 40, "timeout_rate": 0.25}, "trades-service"),
    "portfolio_breakdown_timeout": ("portfolio-service is slow on the portfolio breakdown, and this share of the calls runs past the 8 s tool limit.", {"rate": 0.55, "factor": 12}, "portfolio-service"),
    "app_hang_on_send": ("The newest app release hangs on send for this share of messages: the answer never comes and the user gives up.", {"rate": 0.3}, "concierge-orchestrator"),
    "prompt_regression": ("A prompt change: for this share of portfolio questions the Concierge answers from memory, without its tools.", {"rate": 0.4}, "concierge-orchestrator"),
    "totals_bug": ("portfolio-service returns a position without a value for this share of breakdowns; PortfolioSpecialist's totals code does not expect it and throws.", {"rate": 0.1}, "portfolio-service"),
    "load_sell_off": ("The load generator starts more conversations, mostly portfolio, rebalance and trades.", {"volume": 2.4, "flows": {"portfolio": 2.5, "rebalance": 3, "trades": 1.5}}, "loadgen"),
    "market_sell_off": ("Prices fall on every tick of market-data-service.", {"drift_pct": -0.4}, "market-data-service"),
    "load_rebalance_rush": ("The load generator starts more conversations, mostly portfolio and rebalance.", {"volume": 1.8, "flows": {"portfolio": 1.8, "rebalance": 3}}, "loadgen"),
    "asset_valuation_rush": ("Customers, whales among them, ask for a valuation of every holding: the load generator starts this many extra valuation conversations a minute, on top of the usual mix.",
                             {"per_minute": 2.5}, "loadgen"),
    "payday_burst": ("Payday: the load generator posts this many times the usual balance changes, so the one sweep worker falls behind.", {"factor": 60}, "loadgen"),
    "card_network_slow": ("The card network's program-limit check is this many times slower.", {"factor": 10}, "sweep-service"),
    "venue_slow": ("The trading venue answers this many times slower.", {"factor": 15}, "venue-gateway"),
    "venue_timeout": ("The venue sends no execution report for this share of orders; the order is retried.", {"rate": 0.25}, "venue-gateway"),
    "sweep_worker_stuck": ("The sweep worker takes no messages; the sweeps queue backs up.", {}, "sweep-service"),
    "bank_down": ("The partner bank answers HTTP 503 for this share of ACH submissions; they are retried and some dead-letter.", {"rate": 0.8}, "ach-gateway"),
    "ach_returns": ("The bank returns this share of ACH entries (R01, R02, R03) a little later.", {"rate": 0.15}, "ach-gateway"),
    "settle_fail": ("This share of the chunks of a settlement run has a failed delivery and a buy-in.", {"rate": 0.25}, "settlement-service"),
    "settlement_lot_cache_leak": ("A settlement-service release keeps the raw tax lots of Bronze-plan accounts in memory for the settlement cycle and never lets go. From the next "
                                  "cycle on (every every_minutes, on the clock), reconciling Bronze accounts gets slower and slower (most of all the one with thousands of lots), "
                                  "the pod's memory climbs, and after about minutes it is OOM-killed. It does it again every cycle while on; turning it off frees the memory.",
                                  {"every_minutes": 120, "minutes": 20, "scan_s": 6}, "settlement-service"),
}
# Flags that are a release, not a fault: turning one on or off makes a "deploy <build id>" marker in Honeycomb, not the "configuration change" marker
# of the other flags. Neither marker says which flag it was.
RELEASE_FLAGS = set()


def _p(fault, a=0.0, b=1.0, **params):
    return {"fault": fault, "params": {**FAULTS[fault][1], **params}, "from": a, "to": b}


# name -> (title, kind, description, parts); same names and order as `python3 -m concierge --list-stories`
STORIES = {
    "rate-limit-afternoon": ("Provider rate limits", "agent", "The model provider rate-limits: many calls get HTTP 429 and a retry, and model calls are slower.",
                             [_p("model_rate_limit"), _p("model_slow")]),
    "trades-service-slow": ("trades-service slow", "agent", "trades-service is slow and times out (list_pending_trades, propose_trade_change, apply_trade_change).", [_p("trades_service_slow")]),
    "market-sell-off": ("Market sell-off", "agent", "A market sell-off: 2.4x the conversations, mostly portfolio, rebalance and trades, and prices fall.", [_p("load_sell_off"), _p("market_sell_off")]),
    "busy-rebalance-morning": ("Rebalance rush", "agent", "A rate cut: 1.8x the conversations, mostly portfolio and rebalance.", [_p("load_rebalance_rush")]),
    "portfolio-service-timeouts": ("portfolio-service timeouts", "agent", "portfolio-service times out on about half of its get_portfolio_breakdown calls, and is slow.", [_p("portfolio_breakdown_timeout")]),
    "app-hangs-on-send": ("App hangs on send", "agent", "The newest app release hangs on send: its users wait, then give up.", [_p("app_hang_on_send")]),
    "prompt-change-lowers-quality": ("Prompt change lowers quality", "agent", "A prompt change makes the Concierge answer without the tools; the evaluations score it as unsupported.", [_p("prompt_regression")]),
    "portfolio-specialist-totals-bug": ("PortfolioSpecialist totals bug", "agent", "A PortfolioSpecialist release totals positions wrongly: some get_portfolio_breakdown turns hit an unhandled exception in the agent's own code. "
                                        "A partial fix halves that in the second part.", [_p("totals_bug", 0, 0.33), _p("totals_bug", 0.33, 1, rate=0.05)]),
    "payday-sweep-burst": ("Payday sweep burst", "backoffice", "Payday: 60 times the usual balance changes, and the card network's program-limit check is 10 times slower. The one sweep worker cannot keep up, "
                           "so the sweeps queue backs up and drains.", [_p("payday_burst"), _p("card_network_slow")]),
    "venue-slow-afternoon": ("Venue slow", "backoffice", "The trading venue answers 15 times slower and times out on 25% of orders.", [_p("venue_slow"), _p("venue_timeout")]),
    "sweep-worker-stuck": ("Sweep worker stuck", "backoffice", "The sweep worker is stuck: nothing is swept and the sweeps queue backs up, then it drains.", [_p("sweep_worker_stuck")]),
    "bank-outage-ach-returns": ("Bank outage, then ACH returns", "backoffice", "The partner bank is down for the first half (80% of ACH submissions fail, are retried and some go to the dead-letter queue), "
                                "then ACH returns come in.", [_p("bank_down", 0, 0.5), _p("ach_returns", 0.5, 1)]),
    "settlement-fails-spike": ("Settlement fails", "backoffice", "Settlement runs have many failed deliveries (25% of their chunks) and buy-ins.", [_p("settle_fail")]),
}
