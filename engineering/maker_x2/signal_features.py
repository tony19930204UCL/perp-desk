"""Causal S1 (1m C1) / S2 (15m C2) features on completed UTC minutes.

Pure standard-library Decimal50, no source IO, future bars or strategy search.
"""
from decimal import Decimal, localcontext
from math import isqrt

from entry_lifecycle import MINUTE, atr14, vwap60, closed_bar
from maker_exit_lab import X2Error, number

D = Decimal
ZERO = D("0")
HISTORY_RETURNS = 288


def validate_minutes(bars):
    """Validate sequence order without rearranging, padding or filling gaps."""
    if not isinstance(bars, (list, tuple)):
        raise X2Error("closed minutes must be an ordered sequence")
    validated = []
    last = None
    for index, bar in enumerate(bars):
        if type(bar) is not dict or bar.get("closed") is not True:
            raise X2Error("incomplete minute")
        op, hi, lo, cl = closed_bar(bar)
        ts = bar["ts"]
        if ts % MINUTE or (last is not None and ts <= last):
            raise X2Error("unaligned/duplicate/nonascending UTC minute")
        base = number(bar["base_volume"], "base_volume")
        quote = number(bar["quote_volume"], "quote_volume")
        if base < ZERO or quote < ZERO:
            raise X2Error("negative traded volume")
        validated.append({
            "ts": ts, "open": op, "high": hi, "low": lo,
            "close": cl, "base_volume": base, "quote_volume": quote,
            "closed": True, "minute_index": index,
        })
        last = ts
    return validated


def framework_bars(bars, framework_minutes):
    """Return completed 1m/UTC 15m buckets, discarding any incomplete bucket."""
    if type(framework_minutes) is not int or framework_minutes not in (1, 15):
        raise X2Error("framework must be 1 or 15 minutes")
    minutes = validate_minutes(bars)
    if framework_minutes == 1:
        return [dict(m, final_minute_ts=m["ts"],
                     final_minute_index=m["minute_index"],
                     decision_ts=m["ts"] + MINUTE) for m in minutes]
    width = 15 * MINUTE
    groups = {}
    for bar in minutes:
        start = bar["ts"] // width * width
        groups.setdefault(start, []).append(bar)
    complete = []
    for start in sorted(groups):
        group = groups[start]
        if (len(group) != 15 or group[0]["ts"] != start or
                any(b["ts"] != start + i * MINUTE
                    for i, b in enumerate(group))):
            continue
        complete.append({
            "ts": start, "open": group[0]["open"],
            "high": max(b["high"] for b in group),
            "low": min(b["low"] for b in group),
            "close": group[-1]["close"],
            "base_volume": sum((b["base_volume"] for b in group), ZERO),
            "quote_volume": sum((b["quote_volume"] for b in group), ZERO),
            "closed": True, "final_minute_ts": group[-1]["ts"],
            "final_minute_index": group[-1]["minute_index"],
            "decision_ts": group[-1]["ts"] + MINUTE,
        })
    return complete


def sample_std(values):
    """Sample std with ddof=1; no float round-trip."""
    if len(values) < 2:
        raise X2Error("sample std requires two observations")
    with localcontext() as ctx:
        ctx.prec = 50
        mean = sum(values, ZERO) / D(len(values))
        variance = sum(((v - mean) ** 2 for v in values), ZERO) / D(len(values)-1)
        return variance.sqrt()


def eligible_decisions(bars, framework_minutes):
    """Feature-complete causal decisions, regardless of S1/S2 trigger.

    A decision i needs the 288 returns i-288..i-1, hence closes
    i-289..i-1; current i only contributes its current close and high.
    Framework continuity is mandatory through all 290 bars.
    """
    minutes = validate_minutes(bars)
    frame = framework_bars(bars, framework_minutes)
    period = framework_minutes * MINUTE
    minute_by_ts = {b["ts"]: j for j, b in enumerate(minutes)}
    records = []
    for i in range(289, len(frame)):
        window = frame[i-289:i+1]
        if any(b["ts"] + period != nxt["ts"]
               for b, nxt in zip(window, window[1:])):
            continue
        current = frame[i]
        last_ts = current["final_minute_ts"]
        last_minute_index = minute_by_ts[last_ts]
        # ATR includes 14 complete TRs and their previous close.
        # VWAP60 includes the latest 60 completed minutes.
        if last_minute_index < 59:
            continue
        trailing = minutes[last_minute_index-59:last_minute_index+1]
        if any(a["ts"] + MINUTE != b["ts"]
               for a, b in zip(trailing, trailing[1:])):
            continue
        atr_window = minutes[last_minute_index-14:last_minute_index+1]
        if len(atr_window) != 15:
            continue
        if any(a["ts"] + MINUTE != b["ts"]
               for a, b in zip(atr_window, atr_window[1:])):
            continue
        try:
            with localcontext() as ctx:
                ctx.prec = 50
                returns = [
                    window[j]["close"] / window[j-1]["close"] - 1
                    for j in range(1, 289)
                ]
                sigma = sample_std(returns)
                if sigma <= ZERO:
                    continue
                std12 = sample_std(returns[-12:])
                r6 = current["close"] / frame[i-6]["close"] - 1
                z = r6 / (sigma * D(6).sqrt())
                h12 = max(b["high"] for b in frame[i-12:i])
                l12 = min(b["low"] for b in frame[i-12:i])
                atr = atr14(atr_window)
                vwap = vwap60(trailing)
        except (X2Error, ArithmeticError):
            continue
        kind = "S1" if framework_minutes == 1 else "S2"
        fired = ((z > D("2.5")) if kind == "S1" else
                 (std12 <= D("0.5") * sigma and current["close"] < l12))
        stamp = current["decision_ts"]
        records.append({
            "framework_minutes": framework_minutes,
            "signal_type": kind, "signal_id": f"{kind}-{framework_minutes}m-{stamp}",
            "signal_time": stamp, "final_minute_ts": last_ts,
            "final_minute_index": current["final_minute_index"],
            "signal_high": current["high"], "signal_close": current["close"],
            "atr14": atr, "vwap60": vwap,
            "sigma288": sigma, "std12": std12, "r6": r6, "z": z,
            "previous_h12": h12, "previous_l12": l12,
            "triggered": fired,
        })
    return records


def signal_records(bars, framework_minutes):
    """Only S1 1m or S2 15m signals; never require an execution suffix."""
    return [r for r in eligible_decisions(bars, framework_minutes)
            if r["triggered"]]
