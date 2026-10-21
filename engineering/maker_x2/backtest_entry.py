"""Offline Maker V3 CLI. No network, exchange, or deployment operations."""
import argparse
import json
import hashlib
import time
import os
from tempfile import NamedTemporaryFile
from pathlib import Path
from datetime import datetime, timezone

from maker_exit_lab import X2Error
from data_adapter import read_bars, read_funding, sha256_file
from grid_runner import run_grid, bounded_symbol
from scenario_rates import SENSITIVITY
from backtest_reports import persist_run, verify_run, source_manifest, BoundedReport
from bounded_history import prepare_bars, prepare_decisions
from tempfile import TemporaryDirectory
from backtest_controls import discovery_gate
import market_replay


def load_config(path):
    with open(path,encoding="utf-8") as handle:
        value=json.load(handle)
    if not isinstance(value,dict):
        raise X2Error("config must be a JSON object")
    required=("capital","per_trade_margin","leverage","daily_loss_limit",
              "loss_streak_limit","halt_hours","liq_pct","liq_fee_pct",
              "evaluation_start","evaluation_end")
    if any(name not in value for name in required):
        raise X2Error("required account or time boundary not in config")
    if int(value["evaluation_end"])<=int(value["evaluation_start"]):
        raise X2Error("empty evaluation window")
    frozen_start=int(datetime(2023,11,1,tzinfo=timezone.utc).timestamp()*1000)
    frozen_end=int(datetime(2026,10,19,tzinfo=timezone.utc).timestamp()*1000)
    if (int(value["evaluation_start"])!=frozen_start or
            int(value["evaluation_end"])!=frozen_end):
        raise X2Error("historical CLI requires exact frozen UTC exploration window")
    return value


def execute_prepared(sources, config, input_path, config_path, output,
                     sample_count=6000, frozen=True):
    """Identical real entry pipeline; fixture-only calls may shorten windows."""
    root=Path(__file__).resolve().parent
    source_before=source_manifest(root)
    inputs={}
    start=int(config["evaluation_start"])
    end=int(config["evaluation_end"])
    warmup=(int(datetime(2023,10,1,tzinfo=timezone.utc).timestamp()*1000)
            if frozen else None)
    manifest={"input":inputs,"input_manifest_sha256":sha256_file(input_path),
              "config_sha256":sha256_file(config_path),
              "source_sha256":source_before,
              "evaluation_start":start,"evaluation_end":end,
              "research_status":"EXPLORATORY_UNACCEPTED"}
    report=BoundedReport(output)
    started=time.monotonic()
    total=144
    def heartbeat(stage, symbol="", cell_id="", bar_i=0, bar_n=0):
        now=datetime.now(timezone.utc).isoformat()
        done=sum(len(rows) for rows in report.rows.values())
        payload={"stage":stage,"symbol":symbol,"cell_id":cell_id,
                 "cell_done":done,"cell_total":total,"bar_i":bar_i,
                 "bar_n":bar_n,"elapsed":time.monotonic()-started,
                 "updated_utc":now}
        target=Path(output)/"progress.json"
        with NamedTemporaryFile("w",dir=target.parent,delete=False,
                                 encoding="utf-8") as tmp:
            json.dump(payload,tmp,sort_keys=True)
            name=tmp.name
        os.replace(name,target)
        print(now,stage,symbol,cell_id,bar_i,bar_n,
              round(payload["elapsed"],3),flush=True)
    heartbeat("START")
    samplers={}
    with TemporaryDirectory(prefix="maker-v3-bounded-") as scratch:
        for symbol in ("ETHUSDT","BTCUSDT"):
            paths=sources[symbol]
            if set(paths)!={"bars","funding"}:
                raise X2Error("each symbol needs bars and funding filenames")
            heartbeat("PREPARING",symbol)
            bars=prepare_bars(paths["bars"],Path(scratch)/(symbol+"-bars.db"))
            funding=read_funding(paths["funding"])
            if (not bars["count"] or (frozen and
                    bars["start_open_ms"]>warmup) or
                    bars["end_open_ms"]+60000<end):
                raise X2Error("insufficient historical warmup/evaluation coverage")
            inputs[symbol]={"bars_sha256":bars["sha256"],
                            "funding_sha256":funding["sha256"],
                            "bar_count":bars["count"],
                            "funding_count":funding["count"],
                            "gaps":bars["gaps"],
                            "start_open_ms":bars["start_open_ms"],
                            "end_open_ms":bars["end_open_ms"]}
            prepared={}
            for tf in (1,15):
                heartbeat("FEATURES_START",symbol,str(tf)+"m")
                prepared[tf]=prepare_decisions(
                    bars["bars"],tf,Path(scratch)/(symbol+"-"+str(tf)+".db"))
                heartbeat("FEATURES_DONE",symbol,str(tf)+"m")
            def write_cell(scenario,meta,strategy,matched):
                report.record(scenario,meta,strategy,matched)
                heartbeat("CELL_DONE",symbol,scenario+":"+meta["cell_id"])
            market_replay.BAR_PROGRESS = heartbeat
            _, selections = bounded_symbol(symbol,bars["bars"],
                funding["events"],prepared,config,start,end,write_cell,count=sample_count)
            market_replay.BAR_PROGRESS = None
            samplers.update(selections)
            del prepared, bars, funding
    if source_manifest(root)!=source_before:
        raise X2Error("source/test changed during run")
    heartbeat("FINALIZING")
    written=report.finish(manifest,samplers,config,start,end)
    heartbeat("COMPLETE")
    return written



def profile_cell(sources, config, n, symbol="ETHUSDT"):
    """Finite actual-account N-bar prefix, not whole-matrix research."""
    from itertools import islice
    from time import monotonic
    from market_replay import replay_cell
    if n <= 0:
        raise X2Error("positive profile bar count required")
    with TemporaryDirectory(prefix="maker-profile-") as scratch:
        prepared=prepare_bars(sources[symbol]["bars"],
                              Path(scratch)/"bars.db")
        features=prepare_decisions(prepared["bars"],1,
                                   Path(scratch)/"features.db")
        start=int(config["evaluation_start"])
        bars=(bar for bar in prepared["bars"]
              if bar["ts"]+60000 >= start)
        began=monotonic()
        result=replay_cell(symbol,islice(bars,n),
                           read_funding(sources[symbol]["funding"])["events"],
                           1,".25",2,"X1",config,start,
                           int(config["evaluation_end"]),eligible=features)
        return {"status":"PROFILE_ONLY_NOT_MARKET_ACCEPTANCE",
                "symbol":symbol,"requested_bars":n,
                "elapsed_seconds":monotonic()-began,
                "ledger_events":len(result["events"]),
                "account_episodes":len(result["episodes"])}



def main(argv=None):
    parser=argparse.ArgumentParser(description="Offline Maker V3 backtest")
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test",action="store_true")
    mode.add_argument("--run",action="store_true")
    mode.add_argument("--verify",action="store_true")
    mode.add_argument("--profile-cell",action="store_true")
    parser.add_argument("--bars",type=int,default=20000)
    parser.add_argument("--config")
    parser.add_argument("--input",help="JSON manifest of local ETH/BTC bar/funding filenames")
    parser.add_argument("--output",help="new output directory or existing verify directory")
    args=parser.parse_args(argv)
    if args.self_test:
        return 0 if discovery_gate() else 1
    if args.profile_cell:
        if not (args.config and args.input):
            parser.error("--profile-cell requires --config --input")
        config=load_config(args.config)
        with open(args.input,encoding="utf-8") as handle:
            sources=json.load(handle)
        if set(sources)!={"ETHUSDT","BTCUSDT"}:
            raise X2Error("original two-symbol input required")
        print(json.dumps(profile_cell(sources,config,args.bars),sort_keys=True))
        return 0
    if args.verify:
        if not (args.output and args.input and args.config):
            parser.error("--verify requires --config --input --output")
        with open(Path(args.output)/"manifest.json",
                  encoding="utf-8") as handle:
            manifest=json.load(handle)
        if sha256_file(args.input)!=manifest["input_manifest_sha256"]:
            raise X2Error("input manifest hash changed since run")
        if sha256_file(args.config)!=manifest["config_sha256"]:
            raise X2Error("config hash changed since run")
        if source_manifest(Path(__file__).resolve().parent)!=manifest["source_sha256"]:
            raise X2Error("source files changed since run")
        with open(args.input,encoding="utf-8") as handle:
            paths=json.load(handle)
        for symbol,files in paths.items():
            if symbol not in manifest["input"]:
                raise X2Error("unknown verify symbol")
            for kind in ("bars","funding"):
                if sha256_file(files[kind])!=manifest["input"][symbol][
                      kind+"_sha256"]:
                    raise X2Error("historical "+kind+" input changed")
        print(json.dumps(verify_run(args.output),sort_keys=True))
        return 0
    if not (args.config and args.input and args.output):
        parser.error("--run requires --config --input --output")
    if not discovery_gate():
        raise X2Error("offline CONTROL failed: historical runner not admitted")
    config=load_config(args.config)
    with open(args.input,encoding="utf-8") as handle:
        sources=json.load(handle)
    if set(sources)!={"ETHUSDT","BTCUSDT"}:
        raise X2Error("input manifest needs separate ETHUSDT and BTCUSDT")
    written=execute_prepared(sources,config,args.input,args.config,
                             args.output)
    print(json.dumps({"status":"OUTPUT_WRITTEN_NOT_ACCEPTED",
                      "cell_count":48,**written},sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
