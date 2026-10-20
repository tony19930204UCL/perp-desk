"""Offline Maker V3 CLI. No network, exchange, or deployment operations."""
import argparse
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone

from maker_exit_lab import X2Error
from data_adapter import read_bars, read_funding, sha256_file
from grid_runner import run_grid
from scenario_rates import SENSITIVITY
from backtest_reports import persist_run, verify_run, source_manifest
from backtest_controls import discovery_gate


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


def main(argv=None):
    parser=argparse.ArgumentParser(description="Offline Maker V3 backtest")
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test",action="store_true")
    mode.add_argument("--run",action="store_true")
    mode.add_argument("--verify",action="store_true")
    parser.add_argument("--config")
    parser.add_argument("--input",help="JSON manifest of local ETH/BTC bar/funding filenames")
    parser.add_argument("--output",help="new output directory or existing verify directory")
    args=parser.parse_args(argv)
    if args.self_test:
        return 0 if discovery_gate() else 1
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
    data={}
    inputs={}
    for symbol,paths in sources.items():
        if set(paths)!={"bars","funding"}:
            raise X2Error("each symbol needs bars and funding filenames")
        bars=read_bars(paths["bars"])
        funding=read_funding(paths["funding"])
        # Refuse silently shortened research windows, including incomplete
        # 15m signal warmup. Gaps remain inventoried; never backfill them.
        start=int(config["evaluation_start"])
        end=int(config["evaluation_end"])
        warmup_start=int(datetime(2023,10,1,
                                  tzinfo=timezone.utc).timestamp()*1000)
        if (not bars["bars"] or bars["start_open_ms"] > warmup_start
                or bars["end_open_ms"]+60000 < end):
            raise X2Error("insufficient historical warmup/evaluation coverage")
        data[symbol]={"bars":bars["bars"],"funding":funding["events"]}
        inputs[symbol]={"bars_sha256":bars["sha256"],
                        "funding_sha256":funding["sha256"],
                        "bar_count":bars["count"],
                        "funding_count":funding["count"],
                        "gaps":bars["gaps"],
                        "start_open_ms":bars["start_open_ms"],
                        "end_open_ms":bars["end_open_ms"]}
    root=Path(__file__).resolve().parent
    source_before=source_manifest(root)
    manifest={"input":inputs,"input_manifest_sha256":sha256_file(args.input),
              "config_sha256":sha256_file(args.config),
              "source_sha256":source_before,
              "evaluation_start":int(config["evaluation_start"]),
              "evaluation_end":int(config["evaluation_end"]),
              "research_status":"EXPLORATORY_UNACCEPTED"}
    universe_cache={}
    matched_cache={}
    result=run_grid(data,config,manifest["evaluation_start"],
                    manifest["evaluation_end"],
                    eligible_cache=universe_cache,
                    sample_cache=matched_cache)
    delayed=run_grid(data,config,manifest["evaluation_start"],
                     manifest["evaluation_end"],delay_minutes=2,
                     eligible_cache=universe_cache,
                     sample_cache=matched_cache)
    higher_cost=run_grid(data,config,manifest["evaluation_start"],
                         manifest["evaluation_end"],rates=SENSITIVITY,
                         eligible_cache=universe_cache)
    result["delay2_cells"]=delayed["cells"]
    result["fee_slip_1_5x_cells"]=higher_cost["cells"]
    result["fee_slip_1_5x_status"]="SOURCE_DELIVERED_NOT_VERIFIED"
    if source_manifest(root)!=source_before:
        raise X2Error("sources changed during run")
    written=persist_run(args.output,manifest,result)
    if verify_run(args.output)["manifest_sha256"]!=written["manifest_sha256"]:
        raise X2Error("output verify mismatch")
    print(json.dumps({"status":"OUTPUT_WRITTEN_NOT_ACCEPTED",
                      "cell_count":result["cell_count"],**written},sort_keys=True))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
