"""Causal UTC regime classification; no day d features enter day d labels."""
from collections import defaultdict
from decimal import Decimal
from math import floor
from statistics import pstdev

P75_Q = 0.75
RET7_TH = 0.05
MIN_BARS = 1000
DAY_MS = 86400000
LOOK_DAYS = 30


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
    """Label UTC day d using only completed days d-30 through d-1."""
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
        if ts < end_ms:
            days[ts // DAY_MS].append(close)
        if start_ms <= ts < end_ms:
            included.add(ts // DAY_MS)

    vols = {}
    last = {}
    for day, prices in days.items():
        last[day] = prices[-1]
        if len(prices) >= MIN_BARS:
            returns = [float(prices[i] / prices[i - 1] - 1)
                       for i in range(1, len(prices))]
            vols[day] = pstdev(returns)
    limit = Decimal(str(RET7_TH))
    result = {}
    for day in sorted(included):
        past = [vols.get(d) for d in range(day - LOOK_DAYS, day)]
        prev_vol = vols.get(day - 1)
        missing = (any(value is None for value in past)
                   or day - 1 not in last or day - 8 not in last)
        if missing:
            label = "UNKNOWN"
        elif prev_vol >= pct7(past, P75_Q):
            label = "HIGH_VOL"
        else:
            ret7 = last[day - 1] / last[day - 8] - 1
            label = ("UP_TREND" if ret7 > limit else
                     "DOWN_TREND" if ret7 < -limit else "RANGE")
        result[day] = {"regime": label, "vol": prev_vol,
                       "insufficient_history": missing}
    return result


def regime_of(table, ts):
    return table.get(ts // DAY_MS, {}).get("regime", "UNCLASSIFIED")
