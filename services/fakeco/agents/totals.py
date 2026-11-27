"""PortfolioSpecialist's own code: it totals the positions that get_portfolio_breakdown returned, by asset class.

This runs in the agent after the tool has come back and before the next model call. It expects every position to carry a market value; when the
fault `totals_bug` makes portfolio-service send a position without one, the sum below throws, and that is the "agent bug" of the demo.
"""
ORDER = ["Stocks", "Bonds", "Cash", "Real Estate", "Commodities"]


def position_value(position):
    return 0.0 + position["market_value"]


def total_positions(positions):
    totals = {}
    for p in positions:
        totals[p["asset_class"]] = totals.get(p["asset_class"], 0.0) + position_value(p)
    return totals


def percents(totals):
    """Whole percentages that add up to 100 (largest remainder)."""
    whole = sum(totals.values())
    if whole <= 0:
        return {k: 0 for k in totals}
    exact = {k: v * 100 / whole for k, v in totals.items()}
    out = {k: int(x) for k, x in exact.items()}
    for k in sorted(exact, key=lambda k: exact[k] - out[k], reverse=True)[:100 - sum(out.values())]:
        out[k] += 1
    return out


def summarize_breakdown(breakdown):
    """{"account_id", "positions": [...]} -> {"account_id", "total_value", "allocation": [{"asset_class", "percent", "value"}]}"""
    totals = total_positions(breakdown["positions"])
    pct = percents(totals)
    classes = [c for c in ORDER if c in totals] + sorted(c for c in totals if c not in ORDER)
    return {"account_id": breakdown["account_id"], "total_value": round(sum(totals.values()), 2),
            "allocation": [{"asset_class": c, "percent": pct[c], "value": round(totals[c], 2)} for c in classes]}
