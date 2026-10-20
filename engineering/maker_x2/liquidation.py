"""Conservative research LIQ proxy, NOT exchange liquidation or margin rules.

Price is an ordering threshold only. Loss is remaining allocated margin
writeoff plus remaining entry notional times the configured LIQ fee.
"""
from decimal import Decimal, localcontext

from maker_exit_lab import X2Error, number, SL_PCT

D = Decimal
ZERO = D("0")


def liquidation_proxy(entry_price, high, remaining_qty, original_qty,
                      original_margin, liq_pct, liq_fee_pct):
    """Return independent LIQ liability only when closed-bar high crosses.

    A prior partial exit decreases the remaining margin allocation. No
    synthetic trade fill and no percent-price-PnL is fabricated.
    """
    e = number(entry_price, "entry_price", True)
    hi = number(high, "bar_high", True)
    qty = number(remaining_qty, "remaining_qty")
    original = number(original_qty, "original_qty", True)
    margin = number(original_margin, "original_margin", True)
    pct = number(liq_pct, "liq_pct", True)
    fee_pct = number(liq_fee_pct, "liq_fee_pct")
    if pct != D(".0145") or fee_pct != D(".0005"):
        raise X2Error("frozen LIQ proxy requires .0145 / .0005")
    if pct <= SL_PCT or qty < ZERO or qty > original:
        raise X2Error("invalid LIQ/SL ordering or remaining quantity")
    with localcontext() as ctx:
        ctx.prec = 50
        threshold = e * (1 + pct)
        if qty == ZERO or hi < threshold:
            return None
        allocated = margin * qty / original
        notional = qty * e
        fee = notional * fee_pct
    return {"threshold": threshold, "qty": qty,
            "margin_writeoff": allocated, "notional": notional,
            "fee": fee, "reason": "LIQ_PROXY"}
