"""Exploratory summaries: no candidate approval or holdout inference."""
from decimal import Decimal as D
from collections import defaultdict
from math import sqrt
import random


def summarize(result, matched=None):
    ledger = result["ledger"]
    episodes = [e for e in result["episodes"] if D(e["entry_notional"]) > 0]
    gross = sum((D(e["gross"]) for e in episodes), D(0))
    net = sum((D(e["net"]) for e in episodes), D(0))
    notional = sum((D(e["entry_notional"]) for e in episodes), D(0))
    wins = sum((D(e["net"]) for e in episodes if D(e["net"]) > 0), D(0))
    losses = -sum((D(e["net"]) for e in episodes if D(e["net"]) < 0), D(0))
    pf = None if losses == 0 else str(wins / losses)
    matched_net = None if matched is None else sum(
        (D(e["net"]) for e in matched["episodes"]
         if D(e["entry_notional"]) > 0), D(0))
    matched_notional = None if matched is None else sum(
        (D(e["entry_notional"]) for e in matched["episodes"]
         if D(e["entry_notional"]) > 0), D(0))
    net_bp = net/notional*10000 if notional else None
    placebo_bp = (matched_net/matched_notional*10000
                  if matched_notional else None)
    counts = result["counts"]
    return {"signal_count": result["signal_count"],
            "attempts": counts.get("attempts", 0),
            "filled_episodes": counts.get("filled_episodes", 0),
            "complete_episodes": len(episodes),
            "open": counts.get("open", 0),
            "fill_rate": (None if not counts.get("attempts") else
                          str(D(counts.get("filled_episodes",0)) /
                              D(counts["attempts"]))),
            "gross_bp": None if notional == 0 else str(gross/notional*10000),
            "net_bp": None if net_bp is None else str(net_bp),
            "matched_net_bp": None if placebo_bp is None else str(placebo_bp),
            "net_alpha_bp": (None if net_bp is None or placebo_bp is None
                              else str(net_bp-placebo_bp)),
            "profit_factor": pf, "cash": ledger["cash"],
            "equity": result["account"]["equity"],
            "unrealized": result["account"]["unrealized"],
            "actual_fees": ledger["fees"], "funding_cost": ledger["funding_cost"],
            "liq_writeoff": ledger["writeoff"],
            "exit_counts": {k:v for k,v in counts.items()
                            if k in ("CLOSED", "CLOSED_SL", "LIQ_PROXY",
                                     "SL_GAP", "DEADLINE_INTENT")},
            "true_time_exit": 0, "admin_time_close": "NOT_APPLICABLE",
            "tp_over_tp_plus_sl": None,
            "tp_rate": None, "time_rate": 0,
            "liq_rate": None, "net_positive_rate": (
                None if not episodes else
                str(D(sum(D(e["net"])>0 for e in episodes))/len(episodes))),
            "classification": "DESCRIPTIVE_UNVERIFIED"}


def calendar_bootstrap(episodes, start_day, end_day, iterations=9999,
                       seed=20261020):
    """7-day moving calendar blocks including zero-trade days; no p shortcut."""
    from datetime import date, timedelta
    first, end = date.fromisoformat(start_day), date.fromisoformat(end_day)
    days = []
    cursor = first
    while cursor < end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    pnl = defaultdict(lambda: D(0))
    entry_dates = set()
    for ep in episodes:
        if not ep.get("arrival_ts") or not ep.get("entry_notional"):
            continue
        from account_ledger import utc_day
        day = utc_day(ep["arrival_ts"])
        entry_dates.add(day)
        pnl[day] += D(ep["net"])
    if len(episodes) < 30 or len(entry_dates) < 30 or len(days) < 7:
        return {"status":"INSUFFICIENT", "p":1,
                "p_is_placeholder":True,"ci95":None}
    values = [pnl[day] for day in days]
    observed = sum(values,D(0))/D(len(values))
    centered = [v-observed for v in values]
    rng = random.Random(seed)
    samples = []
    for _ in range(iterations):
        drawn = []
        while len(drawn)<len(days):
            offset=rng.randrange(len(days)-6)
            drawn.extend(centered[offset:offset+7])
        samples.append(sum(drawn[:len(days)],D(0))/D(len(days)))
    samples.sort()
    above=sum(v>=observed for v in samples)
    lo=samples[int(iterations*.025)]
    hi=samples[int(iterations*.975)]
    return {"status":"EXPLORATORY","p":str(D(above+1)/D(iterations+1)),
            "p_is_placeholder":False,"ci95":[str(observed-hi),str(observed-lo)]}


def bh_flags(pvalues, q=D(".05")):
    size=len(pvalues)
    sorted_ids=sorted(range(size),key=lambda i:D(str(pvalues[i])))
    cutoff=-1
    for rank,index in enumerate(sorted_ids,1):
        if D(str(pvalues[index]))<=q*D(rank)/D(size):
            cutoff=rank
    return [index in sorted_ids[:cutoff] if cutoff>=0 else False
            for index in range(size)]
