"""48 symbol-cell offline orchestration: same account logic for placebo."""
from itertools import product
from matched_signals import sample_completed
from market_replay import replay_cell
from backtest_stats import summarize, calendar_bootstrap, bh_flags
from maker_exit_lab import X2Error

KS=(".25",".50")
TTLS=(2,5)
EXITS=("X1","X2","X3")
FRAMES=(1,15)
SYMBOLS=("ETHUSDT","BTCUSDT")


def run_grid(dataset, account_config, start, end, sample_count=6000,
             delay_minutes=0):
    cells=[]
    sources={}
    for symbol in SYMBOLS:
        if symbol not in dataset:
            raise X2Error("missing symbol dataset "+symbol)
        bars=dataset[symbol]["bars"]
        funding=dataset[symbol]["funding"]
        for frame in FRAMES:
            selection=sample_completed(bars,frame,start,end,
                                        count=sample_count)
            sources[symbol+":"+str(frame)]=selection
            for k,ttl,exit_style in product(KS,TTLS,EXITS):
                cell_id=f"{symbol}:{frame}m:{k}:{ttl}:{exit_style}"
                real=replay_cell(symbol,bars,funding,frame,k,ttl,exit_style,
                                 account_config,start,end,
                                 delay_minutes=delay_minutes)
                matched=None
                if selection["status"]=="READY":
                    matched=replay_cell(
                        symbol,bars,funding,frame,k,ttl,exit_style,
                        account_config,start,end,
                        signal_times=selection["selected_timestamps"],
                        delay_minutes=delay_minutes)
                metrics=summarize(real,matched)
                cells.append({"cell_id":cell_id,"symbol":symbol,
                              "framework":frame,"k":k,"ttl":ttl,
                              "exit":exit_style,"strategy":real,
                              "matched":matched,"metrics":metrics,
                              "matched_status":selection["status"]})
    if len(cells)!=48:
        raise X2Error("48-cell construction invariant")
    return {"cells":cells,"samplers":sources,"cell_count":len(cells),
            "sensitivity_delay_minutes":delay_minutes,
            "classification":"EXPLORATORY_NOT_ACCEPTED"}
