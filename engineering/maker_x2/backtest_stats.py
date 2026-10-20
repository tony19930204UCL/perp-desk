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
    exits = [ep.get("exit_type","UNKNOWN") for ep in episodes]
    ex_types = ("TP","SL","SL_GAP","PASSIVE_X3","LIQ","NO_FILL")
    exit_counts = {key:exits.count(key) for key in ex_types}
    risk_denominator = (exit_counts["TP"]+exit_counts["SL"]+
                        exit_counts["SL_GAP"])
    completed_count=len(episodes)
    def share(count):
        return None if not completed_count else str(D(count)/completed_count)
    from datetime import datetime, timezone
    us_hours = [ep for ep in episodes if ep.get("arrival_ts") is not None
                and datetime.fromtimestamp(ep["arrival_ts"]/1000,
                    timezone.utc).hour in (13,14,15,16)]
    other_hours=[ep for ep in episodes if ep not in us_hours]
    def subgroup(subset):
        quantity=sum((D(ep["entry_notional"]) for ep in subset),D(0))
        profit=sum((D(ep["net"]) for ep in subset),D(0))
        return {"count":len(subset), "net_bp":(
            None if not quantity else str(profit/quantity*10000))}
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
            "exit_counts": exit_counts,
            "tp_sl_sl_gap_denominator":risk_denominator,
            "tp_over_tp_plus_sl":(
                None if not risk_denominator else
                str(D(exit_counts["TP"])/risk_denominator)),
            "tp_rate":share(exit_counts["TP"]),
            "sl_rate":share(exit_counts["SL"]+exit_counts["SL_GAP"]),
            "liq_rate":share(exit_counts["LIQ"]),
            "time_rate": "0",
            "true_time_exit": 0,
            "admin_4h_trigger_count":counts.get("admin_4h_triggers",0),
            "admin_4h_trigger_rate": (
                None if not counts.get("filled_episodes") else
                str(D(counts.get("admin_4h_triggers",0))/
                    counts["filled_episodes"])),
            "actual_time_close_rate": "NOT_APPLICABLE",
            "samebar_conflicts":counts.get("samebar_risk_profit_conflict",0),
            "samebar_favorable_upper_bound": "NOT_COMPUTED",
            "utc_13_16":subgroup(us_hours),
            "utc_other":subgroup(other_hours),
            "net_positive_rate": (
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
    qualified = [ep for ep in episodes
                 if ep.get("arrival_ts") is not None and
                    D(ep.get("entry_notional","0")) > 0]
    for ep in qualified:
        from account_ledger import utc_day
        day = utc_day(ep["arrival_ts"])
        entry_dates.add(day)
        # One flat-to-flat episode is ONE observation. Normalize its
        # fee-adjusted net by initial entry notional, not by exit legs.
        pnl[day] += D(ep["net"]) / D(ep["entry_notional"]) * D(10000)
    if len(qualified) < 30 or len(entry_dates) < 30 or len(days) < 7:
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
