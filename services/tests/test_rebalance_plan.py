"""The orders of a rebalance plan must not spend more cash than the account has."""
from fakeco.backends.portfolio import plan


def position(symbol, asset_class, quantity, price):
    return {"asset_id": None, "symbol": symbol, "name": symbol, "asset_class": asset_class, "quantity": quantity, "price": price, "market_value": round(quantity * price, 2)}


def cash_after(positions, legs):
    """The cash balance once every leg has filled at the planned price, with whole shares as portfolio-service orders them."""
    cash = next(p["market_value"] for p in positions if p["symbol"] == "CASH")
    for side, symbol, amount, price in legs:
        price = price or 100.0   # a fund the account does not hold yet: the service reads its price from `instruments`
        cash += (-1 if side == "buy" else 1) * int(amount // price) * price
    return cash


def test_a_plan_that_cuts_cash_sells_money_market_holdings_instead_of_overdrawing():
    # Cash is 24% of the account, but most of it is a money-market fund: the cash balance alone cannot pay for the buys.
    positions = [position("KTH-EQ", "Stocks", 400, 120.0), position("TFEN-BD", "Bonds", 170, 95.0), position("EBA-MM", "Cash", 19000, 1.0),
                 position("KVHQ-RE", "Real Estate", 80, 75.0), position("GLD-CM", "Commodities", 20, 200.0), position("CASH", "Cash", 4000.0, 1.0)]
    moves, legs = plan(positions, "moderate", "5-10 years", None)
    assert {"asset_class": "Cash", "from": 24, "to": 5} in moves
    assert ("sell", "EBA-MM") in [(side, symbol) for side, symbol, _, _ in legs]
    assert cash_after(positions, legs) >= 0


def test_a_plan_the_cash_balance_can_pay_for_leaves_the_money_market_holdings_alone():
    positions = [position("KTH-EQ", "Stocks", 400, 120.0), position("TFEN-BD", "Bonds", 170, 95.0), position("EBA-MM", "Cash", 4000, 1.0),
                 position("KVHQ-RE", "Real Estate", 80, 75.0), position("GLD-CM", "Commodities", 20, 200.0), position("CASH", "Cash", 19000.0, 1.0)]
    _, legs = plan(positions, "moderate", "5-10 years", None)
    assert "EBA-MM" not in [symbol for _, symbol, _, _ in legs]
    assert cash_after(positions, legs) >= 0
