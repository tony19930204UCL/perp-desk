"""48 symbol-cell offline orchestration: same account logic for placebo."""
from itertools import product
import hashlib
from matched_signals import select_matched
from signal_features import eligible_decisions
from market_replay import replay_cell
from backtest_stats import summarize, calendar_bootstrap, bh_flags
from account_ledger import utc_day
from decimal import Decimal as D
from maker_exit_lab import X2Error

KS=(".25",".50")
TTLS=(2,5)
EXITS=("X1","X2","X3")
FRAMES=(1,15)
SYMBOLS=("ETHUSDT","BTCUSDT")


def run_grid(dataset, account_config, start, end, sample_count=6000,
             delay_minutes=0, eligible_cache=None, rates=None,
             sample_cache=None):
    cells=[]
    sources={}
    cache={} if eligible_cache is None else eligible_cache
    selected={} if sample_cache is None else sample_cache
    for symbol in SYMBOLS:
        if symbol not in dataset:
            raise X2Error("missing symbol dataset "+symbol)
        bars=dataset[symbol]["bars"]
        funding=dataset[symbol]["funding"]
        for frame in FRAMES:
            key=(symbol,frame)
            if key not in cache:
                cache[key]=eligible_decisions(bars,frame)
            eligible=cache[key]
            if key not in selected:
                selected[key]=select_matched(eligible,frame,start,end,
                                             count=sample_count)
            selection=selected[key]
            tagged=dict(selection)
            tagged["symbol_universe_sha256"]=hashlib.sha256(
                (symbol+":"+selection["source_universe_sha256"])
                .encode("ascii")).hexdigest()
            sources[symbol+":"+str(frame)]=tagged
            for k,ttl,exit_style in product(KS,TTLS,EXITS):
                cell_id=f"{symbol}:{frame}m:{k}:{ttl}:{exit_style}"
                real=replay_cell(symbol,bars,funding,frame,k,ttl,exit_style,
                                 account_config,start,end,
                                 delay_minutes=delay_minutes,
                                 eligible=eligible,rates=rates)
                matched=None
                if selection["status"]=="READY":
                    matched=replay_cell(
                        symbol,bars,funding,frame,k,ttl,exit_style,
                        account_config,start,end,
                        signal_times=selection["selected_timestamps"],
                        delay_minutes=delay_minutes,eligible=eligible,rates=rates)
                metrics=summarize(real,matched)
                cells.append({"cell_id":cell_id,"symbol":symbol,
                              "framework":frame,"k":k,"ttl":ttl,
                              "exit":exit_style,"strategy":real,
                              "matched":matched,"metrics":metrics,
                              "matched_status":selection["status"]})
    if len(cells)!=48:
        raise X2Error("48-cell construction invariant")
    start_day=utc_day(start)
    end_day=utc_day(end)
    for cell in cells:
        test=calendar_bootstrap(cell["strategy"]["episodes"],
                                start_day,end_day,seed=20261020)
        cell["statistics"]=test
        cell["metrics"]["bootstrap_p"]=test["p"]
        cell["metrics"]["bootstrap_ci95"]=test["ci95"]
        cell["metrics"]["p_is_placeholder"]=test["p_is_placeholder"]
        final=cell["strategy"]["ledger"]
        available=D(final["available"])
        needed=D(account_config["per_trade_margin"])
        insufficient=cell["strategy"]["counts"].get(
            "INSUFFICIENT_CASH_OR_MARGIN",0)>0
        not_exhausted=(not insufficient and available>=needed and
                       D(final["cash"])>0)
        cell["metrics"]["not_exhausted"]=not_exhausted
        cell["metrics"]["exhausted"]=not not_exhausted
        cell["metrics"]["admin_tail_open"]=bool(
            cell["strategy"]["counts"].get("admin_4h_triggers",0)
            and D(final["qty"])>0)
    for symbol in SYMBOLS:
        family=[c for c in cells if c["symbol"]==symbol]
        flags=bh_flags([c["statistics"]["p"] for c in family])
        for cell,flag in zip(family,flags):
            cell["metrics"]["bh24_reject"]=flag
    across=bh_flags([c["statistics"]["p"] for c in cells])
    for cell,flag in zip(cells,across):
        cell["metrics"]["bh48_sensitivity_reject"]=flag
        neighbors=[other for other in cells
                   if other["symbol"]==cell["symbol"] and
                      other["framework"]==cell["framework"] and
                      other["exit"]==cell["exit"] and
                      (other["k"],other["ttl"])!=(cell["k"],cell["ttl"])]
        alpha=cell["metrics"]["net_alpha_bp"]
        pf=cell["metrics"]["profit_factor"]
        neighborhood=(len(neighbors)==3 and all(
            n["metrics"]["net_alpha_bp"] is not None and
            D(n["metrics"]["net_alpha_bp"])>0 for n in neighbors))
        candidate=(cell["metrics"]["bh24_reject"] and
                   alpha is not None and D(alpha)>5 and
                   pf is not None and D(pf)>D("1.30") and
                   neighborhood and
                   cell["metrics"]["not_exhausted"] is True and
                   not cell["metrics"]["admin_tail_open"])
        cell["metrics"]["neighbor_three_positive"]=neighborhood
        cell["metrics"]["candidate"]=bool(candidate)
        cell["metrics"]["classification"]=(
            "NOT_ACCEPTED_ENGINEERING_UNVERIFIED" if not candidate else
            "REQUIRES_SEPARATE_ENGINEERING_ACCEPTANCE")
    return {"cells":cells,"samplers":sources,"cell_count":len(cells),
            "sensitivity_delay_minutes":delay_minutes,
            "classification":"EXPLORATORY_NOT_ACCEPTED"}
