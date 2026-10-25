"""Categorize original offline entry cancellation/admission reason strings."""
CATS = ("TTL_EXPIRED", "POST_ONLY_CROSS", "RISK_GUARD",
        "X1_ADMISSION", "H2_SKIP", "OTHER")
RAW_MAP = {
    "EXPIRED": "TTL_EXPIRED",
    "POST_ONLY_REJECT": "POST_ONLY_CROSS",
    "ADMISSION_REJECTED": "RISK_GUARD",
    "INSUFFICIENT_CASH_OR_MARGIN": "RISK_GUARD",
    "single slot occupied": "RISK_GUARD",
    "gap/halt blocks entry": "RISK_GUARD",
    "pending/position occupies single slot": "RISK_GUARD",
    "X1_ADMISSION_REJECTED": "X1_ADMISSION",
    "H2_SKIP": "H2_SKIP",
}


def cancel_table(reasons, total_signals, intents=None, unprocessed=0):
    if type(total_signals) is not int or total_signals < 0:
        raise ValueError("invalid total_signals")
    if type(unprocessed) is not int or unprocessed < 0:
        raise ValueError("invalid unprocessed")
    counts = {key: 0 for key in CATS}
    other_raw = {}
    cancelled = 0
    for raw in reasons:
        cancelled += 1
        if cancelled > total_signals:
            raise ValueError("cancelled exceeds total_signals")
        cat = RAW_MAP.get(raw, "OTHER")
        counts[cat] += 1
        if cat == "OTHER":
            other_raw[raw] = other_raw.get(raw, 0) + 1
    if intents is None:
        intents = total_signals - unprocessed
    if type(intents) is not int or not 0 <= intents <= total_signals:
        raise ValueError("invalid intents")
    if cancelled > intents or intents + unprocessed > total_signals:
        raise ValueError("funnel exceeds signals")
    filled = total_signals - cancelled - unprocessed
    return {"total": total_signals, "signals": total_signals,
            "intents": intents, "unprocessed": unprocessed,
            "cancelled": cancelled, "counts": counts,
            "other_raw": other_raw, "filled_or_open": filled,
            "stage_denoms": {"signals": total_signals, "intents": intents,
                             "cancelled": cancelled},
            "signal_shares": {cat: counts[cat] / total_signals
                              if total_signals else 0.0 for cat in CATS}}


def share(table):
    n = table["cancelled"]
    return {cat: table["counts"][cat] / n if n else 0.0 for cat in CATS}
