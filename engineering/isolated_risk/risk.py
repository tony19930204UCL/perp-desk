"""PAPER_ONLY USD-M isolated margin. No exchange orders or private endpoints."""
from decimal import Decimal, InvalidOperation

def dec(x):
    if isinstance(x, bool):
        raise ValueError("bool amount")
    d = Decimal(str(x))
    if not d.is_finite():
        raise ValueError("nonfinite")
    return d

def verified(meta, now, kind):
    if not isinstance(meta, dict):
        return False
    try:
        valid = 0 < int(meta["observed_ms"]) <= now < int(meta["expires_ms"])
        return (valid and meta["type"] == "OFFICIAL_PUBLIC" and
                meta["url"].startswith("https://www.binance.com/") and
                bool(meta.get("symbol")) and meta.get("kind") == kind)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False

def brackets(doc, symbol, now, fixture=False):
    """Fail closed unless each symbol's dated official public table is supplied."""
    if doc.get("symbol") != symbol or not fixture and not verified(
            doc.get("source"), now, "MARGIN_TIERS"):
        raise ValueError("MISSING_OFFICIAL_TIERS")
    rows = doc.get("tiers")
    if not isinstance(rows, list) or not rows:
        raise ValueError("EMPTY_TIERS")
    out = []
    lower = Decimal("0")
    for row in rows:
        lo, hi = dec(row["floor"]), dec(row["cap"])
        rate, deduct = dec(row["mmr"]), dec(row["cum"])
        lev = dec(row["max_leverage"])
        if lo != lower or hi <= lo or not 0 < rate < 1 or deduct < 0 or lev < 1:
            raise ValueError("INVALID_TIERS")
        out.append((lo, hi, rate, deduct, lev))
        lower = hi
    return out

def maintenance(qty, mark, rows):
    n = abs(dec(qty)) * dec(mark)
    for lo, hi, rate, deduction, _ in rows:
        if lo < n <= hi:
            return n * rate - deduction
    raise ValueError("TIER_OUT_OF_RANGE")

def liquidation(side, qty, entry, collateral, cashflow, rows):
    """Piecewise isolated equity == bracket maintenance, mark-price only."""
    q, e, c, f = map(dec, (qty, entry, collateral, cashflow))
    if side not in ("long", "short") or min(q, e, c) <= 0:
        raise ValueError("INVALID_POSITION")
    sign = Decimal(1) if side == "long" else Decimal(-1)
    roots = []
    for lo, hi, rate, deduct, _ in rows:
        den = q * (sign - rate)
        if not den:
            continue
        px = (q * sign * e - c - f - deduct) / den
        if px > 0 and lo < q * px <= hi:
            equity = c + f + q * sign * (px - e)
            mm = q * px * rate - deduct
            if abs(equity - mm) <= Decimal("0.00000001"):
                roots.append(px)
    if len(roots) != 1:
        raise ValueError("LIQ_ROOT_UNKNOWN")
    return roots[0]

def gate(symbol, now, mark, feed, tier_doc, fee_doc, filters, leverage,
         qty, collateral, entry, flow=0):
    """Entry requires complete current official rules, price and fees."""
    try:
        rows = brackets(tier_doc or {}, symbol, now)
        if not verified(fee_doc, now, "FEES"):
            raise ValueError("MISSING_OFFICIAL_FEES")
        if not filters or filters.get("symbol") != symbol or not filters.get("triggerProtect"):
            raise ValueError("MISSING_EXCHANGE_FILTERS")
        if feed.get("mark_ms", 0) > now or now - feed.get("mark_ms", 0) > 2500:
            raise ValueError("MARK_STALE")
        if not feed.get("depth_ready") or not feed.get("book_ready"):
            raise ValueError("BOOK_UNHEALTHY")
        n = dec(qty) * dec(mark)
        if n <= 0 or n / dec(leverage) > dec(collateral):
            raise ValueError("COLLATERAL")
        tier = next(row for row in rows if row[0] < n <= row[1])
        if dec(leverage) > tier[4]:
            raise ValueError("LEVERAGE_CAP")
        if dec(fee_doc["taker"]) < 0 or dec(fee_doc["taker"]) > 1:
            raise ValueError("INVALID_FEE")
        if dec(filters["liquidationFee"]) < 0:
            raise ValueError("INVALID_LIQ_FEE")
        liquidation("long", qty, entry, collateral, flow, rows)
        return {"admissible": True, "tier": str(tier[1])}
    except (ValueError, KeyError, TypeError, StopIteration, InvalidOperation) as exc:
        return {"admissible": False, "reason": str(exc)}
