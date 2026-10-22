"""R4-1 closed-minute UTC daily regime labels; frozen research definitions."""
from collections import defaultdict
from decimal import Decimal
from math import floor
from statistics import pstdev

P75_Q = 0.75
RET7_TH = 0.05
MIN_BARS = 1000
DAY_MS = 86400000


def pct7(values, q):
    """Type-7 linear interpolation, h=(n-1)*q."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("empty percentile input")
    if not 0 <= q <= 1:
        raise ValueError("percentile outside [0,1]")
    h = (len(ordered) - 1) * q
    lo = floor(h)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (h - lo)


def classify(bars, start_ms, end_ms):
    if type(start_ms) is not int or type(end_ms) is not int or end_ms <= start_ms:
        raise ValueError("invalid UTC interval")
    days = defaultdict(list)
    included = set()
    prev = None
    for bar in bars:
        ts = bar["ts"]
        if type(ts) is not int or (prev is not None and ts <= prev):
            raise ValueError("nonascending UTC bar timestamp")
        prev = ts
        close = Decimal(str(bar["close"]))
        if not close.is_finite() or close <= 0:
            raise ValueError("invalid close")
        day = ts // DAY_MS
        if ts < end_ms:
            days[day].append(close)
        if start_ms <= ts < end_ms:
            included.add(day)

    vols = {}
    for day in included:
        prices = days[day]
        if len(prices) < MIN_BARS:
            vols[day] = None
        else:
            returns = [float(prices[i] / prices[i - 1] - 1)
                       for i in range(1, len(prices))]
            vols[day] = pstdev(returns)
    valid = [value for value in vols.values() if value is not None]
    threshold = pct7(valid, P75_Q) if valid else None
    last = {day: prices[-1] for day, prices in days.items() if prices}
    limit = Decimal(str(RET7_TH))
    result = {}
    for day in sorted(included):
        vol = vols[day]
        missing = day - 1 not in last or day - 8 not in last
        insufficient = missing or vol is None
        if vol is None:
            label = "UNCLASSIFIED"
        elif vol >= threshold:
            label = "HIGH_VOL"
        elif missing:
            label = "RANGE"
        else:
            ret7 = last[day - 1] / last[day - 8] - 1
            label = ("UP_TREND" if ret7 > limit else
                     "DOWN_TREND" if ret7 < -limit else "RANGE")
        result[day] = {"regime": label, "vol": vol,
                       "insufficient_history": insufficient}
    return result


def regime_of(table, ts):
    return table.get(ts // DAY_MS, {}).get("regime", "UNCLASSIFIED")
