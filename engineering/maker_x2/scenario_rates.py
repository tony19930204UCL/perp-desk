"""Frozen baseline rates with optional explicit 1.5x sensitivity.

Maker queue penetration stays 2bp; costs do not control eligibility.
No global flags, floats, mutable shared state or account-size defaults.
"""
from decimal import Decimal
from maker_exit_lab import X2Error, number

D = Decimal

class ScenarioRates:
    def __init__(self, multiplier="1"):
        factor = number(multiplier, "cost multiplier", True)
        if factor not in (D("1"), D("1.5")):
            raise X2Error("only frozen baseline/1.5x costs allowed")
        self.multiplier = factor
        self.maker_fee = D(".0002") * factor
        self.taker_fee = D(".0005") * factor
        self.slip = D(".0002") * factor
        self.liq_fee = D(".0005") * factor

BASE = ScenarioRates()
SENSITIVITY = ScenarioRates("1.5")

def resolve_rates(rates):
    if rates is None:
        return BASE
    if not isinstance(rates, ScenarioRates):
        raise X2Error("ScenarioRates expected")
    return rates
