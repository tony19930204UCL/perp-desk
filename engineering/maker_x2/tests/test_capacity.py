"""r13 validated capacity, differential and real-path resource controls.

No market files, network, credentials, account configuration or deployment.
--differential requires an independent immutable pre-r13 checkout.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal as D

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data_adapter import read_bars, sha256_file
from signal_features import eligible_decisions
from matched_signals import select_matched
from bounded_history import prepare_bars, prepare_decisions, iter_eligible
from backtest_entry import execute_prepared
from backtest_reports import verify_run
from maker_exit_lab import X2Error


def fictional_files(folder, n):
    folder = Path(folder)
    sources = {}
    for symbol in ("ETHUSDT", "BTCUSDT"):
        path = folder / (symbol + ".csv")
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("open_time", "open", "high", "low", "close",
                             "volume", "quote_volume"))
            for i in range(n):
                close = D("100") + D(i % 7) / D("100")
                writer.writerow((i * 60000, str(close), str(close+D(".30")),
                                 str(close-D(".30")), str(close), "2", "200"))
        funding = folder / (symbol + "-funding.csv")
        with funding.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("funding_time_ms", "rate"))
            writer.writerow((300*60000, ".01"))
        sources[symbol] = {"bars": str(path), "funding": str(funding)}
    return sources


def fictional_config(n):
    return {"capital": "1000", "per_trade_margin": "50", "leverage": "5",
            "daily_loss_limit": "5", "loss_streak_limit": 2,
            "halt_hours": "24", "liq_pct": ".0145", "liq_fee_pct": ".0005",
            "evaluation_start": 289*15*60000,
            "evaluation_end": n*60000}


def config_files(folder, n):
    sources = fictional_files(folder, n)
    config = fictional_config(n)
    inp, cfg = Path(folder)/"sources.json", Path(folder)/"config.json"
    inp.write_text(json.dumps(sources), encoding="utf-8")
    cfg.write_text(json.dumps(config), encoding="utf-8")
    return sources, config, inp, cfg


class Control(unittest.TestCase):
    def test_1m_exact_features_and_sample_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            sources = fictional_files(folder, 320)
            path = sources["ETHUSDT"]["bars"]
            before = read_bars(path)
            prepared = prepare_bars(path, Path(folder)/"bars.db")
            self.assertEqual(prepared["sha256"], before["sha256"])
            self.assertEqual(list(prepared["bars"]), before["bars"])
            old = eligible_decisions(before["bars"], 1)
            new = list(iter_eligible(prepared["bars"], 1))
            self.assertEqual(new, old)
            disk = prepare_decisions(prepared["bars"],1,
                                     Path(folder)/"decisions.db")
            self.assertEqual(list(disk), old)
            a = select_matched(old,1,0,320*60000,count=4)
            b = select_matched(disk,1,0,320*60000,count=4)
            self.assertEqual(a, b)
            self.assertEqual(new[-1]["signal_time"],320*60000)

    def test_complete_15m_prefix_gap_and_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            sources = fictional_files(folder,15*291)
            path = sources["ETHUSDT"]["bars"]
            before=read_bars(path)
            prepared=prepare_bars(path,Path(folder)/"full.db")
            old=eligible_decisions(before["bars"],15)
            disk=prepare_decisions(prepared["bars"],15,
                                   Path(folder)/"features.db")
            self.assertEqual(list(disk),old)
            self.assertEqual(old[-1]["signal_time"],291*15*60000)
            self.assertEqual(select_matched(old,15,0,10**10,count=2),
                             select_matched(disk,15,0,10**10,count=2))
            broken=Path(folder)/"gap.csv"
            with open(path, encoding="utf-8") as handle:
                lines=handle.readlines()
            broken.write_text("".join(lines[:240]+lines[241:]),
                              encoding="utf-8")
            gap=prepare_bars(broken,Path(folder)/"gap.db")
            self.assertEqual(len(gap["gaps"]),1)
            self.assertEqual(list(iter_eligible(gap["bars"],15)),[])
            duplicate=Path(folder)/"invalid.csv"
            duplicate.write_text("".join(lines[:8]+[lines[7]]+lines[8:]),
                                 encoding="utf-8")
            with self.assertRaises(X2Error):
                prepare_bars(duplicate,Path(folder)/"invalid.db")

    def test_incremental_json_array_and_tail_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"tiny.json"
            rows=[[i*60000,"100","101","99","100","2",
                   i*60000+59999,"200",0,0,0,"0"] for i in range(6)]
            path.write_text(json.dumps(rows),encoding="utf-8")
            prepared=prepare_bars(path,Path(folder)/"raw.db")
            self.assertEqual(len(prepared["bars"]),6)
            self.assertEqual(len(list(prepared["bars"])),6)
            mutated=Path(folder)/"invalid.json"
            mutated.write_text(json.dumps(rows)+" 23",encoding="utf-8")
            with self.assertRaises(X2Error):
                prepare_bars(mutated,Path(folder)/"bad.db")

    def test_prepared_instances_must_be_proven_validated(self):
        from bounded_history import PreparedBars, PreparedDecisions
        with self.assertRaises(X2Error):
            PreparedBars("untrusted.db",1,None)
        with self.assertRaises(X2Error):
            PreparedDecisions("untrusted.db",1,None)


def independent_reference(base_dir, inp, cfg, out):
    """Run immutable pre-r13 sources in their own interpreter/process."""
    code = """
import json
from data_adapter import read_bars,read_funding
from grid_runner import run_grid
from backtest_reports import canon
from scenario_rates import SENSITIVITY
sources=json.load(open(__import__('sys').argv[1],encoding='utf-8'))
config=json.load(open(__import__('sys').argv[2],encoding='utf-8'))
data={symbol:{'bars':read_bars(v['bars'])['bars'],
              'funding':read_funding(v['funding'])['events']}
      for symbol,v in sources.items()}
start,end=config['evaluation_start'],config['evaluation_end']
cache={}; samples={}
result={}
for name,delay,rates in [('baseline',0,None),('delay2',2,None),
                         ('cost1_5x',0,SENSITIVITY)]:
    run=run_grid(data,config,start,end,sample_count=1,
                 delay_minutes=delay,rates=rates,
                 eligible_cache=cache,sample_cache=samples)
    result[name]=run
from market_replay import replay_cell
from decimal import Decimal as D
focused=[dict(bar) for bar in data["ETHUSDT"]["bars"]]
focused[300].update(open="100",high="100.27005",low="99",close="100")
focused[301].update(open="100",high="100.1",low="98.9",close="99")
known={"framework_minutes":1,"signal_time":300*60000,
       "triggered":True,"signal_high":D("100"),"signal_close":D("100"),
       "atr14":D("1"),"vwap60":D("99"),"signal_id":"focused",
       "signal_type":"S1"}
fund=[{"ts":301*60000,"rate":".01","mark":"100",
       "settlement_id":"focused-settlement"}]
result["focused"]={}
for scenario,rates in (("baseline",None),("cost1_5x",SENSITIVITY)):
    for exit_style in ("X1","X2","X3"):
        key=scenario+":"+exit_style
        result["focused"][key]=replay_cell(
            "ETHUSDT",focused,fund,1,".25",2,exit_style,config,
            300*60000,303*60000,eligible=[known],rates=rates)
json.dump(canon(result),open(__import__('sys').argv[3],'w'),
          sort_keys=True,allow_nan=False)
"""
    done=subprocess.run([sys.executable,"-c",code,str(inp),str(cfg),str(out)],
                        cwd=base_dir,check=False,capture_output=True,text=True)
    if done.returncode:
        raise AssertionError("immutable baseline failed rc="+str(done.returncode)
                             +" "+done.stderr[-1200:])


def differential(base_dir):
    if not (Path(base_dir)/"grid_runner.py").is_file():
        raise ValueError("--base-dir must be a pre-r13 maker_x2 checkout")
    with tempfile.TemporaryDirectory() as folder:
        sources, config, inp, cfg = config_files(folder,15*291)
        reference=Path(folder)/"reference.json"
        independent_reference(base_dir,inp,cfg,reference)
        destination=Path(folder)/"bounded"
        execute_prepared(sources,config,inp,cfg,destination,
                         sample_count=1,frozen=False)
        verify_run(destination)
        old=json.loads(reference.read_text(encoding="utf-8"))
        from market_replay import replay_cell
        from scenario_rates import SENSITIVITY
        focused=list(read_bars(sources["ETHUSDT"]["bars"])["bars"])
        focused[300].update(open="100",high="100.27005",low="99",close="100")
        focused[301].update(open="100",high="100.1",low="98.9",close="99")
        known={"framework_minutes":1,"signal_time":300*60000,
               "triggered":True,"signal_high":D("100"),
               "signal_close":D("100"),"atr14":D("1"),"vwap60":D("99"),
               "signal_id":"focused","signal_type":"S1"}
        fund=[{"ts":301*60000,"rate":".01","mark":"100",
               "settlement_id":"focused-settlement"}]
        for scenario,rates in (("baseline",None),("cost1_5x",SENSITIVITY)):
            for style in ("X1","X2","X3"):
                key=scenario+":"+style
                actual=replay_cell("ETHUSDT",focused,fund,1,".25",2,
                    style,config,300*60000,303*60000,
                    eligible=[known],rates=rates)
                from backtest_reports import canon
                assert canon(actual)==old["focused"][key],key
                assert any(event["type"]=="fill" and event["side"]=="sell"
                           for event in actual["events"]),key
                assert any(event["type"]=="funding"
                           for event in actual["events"]),key
        with (destination/"results.json").open(encoding="utf-8") as handle:
            new=json.load(handle)
        for scenario, prefix, summary in (
                ("baseline","cells","cells"),
                ("delay2","delay2","delay2_cells"),
                ("cost1_5x","cost1_5x","cost1_5x_cells")):
            old_rows=old[scenario]["cells"]
            new_rows=new[summary]
            assert len(old_rows)==len(new_rows)==48
            for index,(before,after) in enumerate(zip(old_rows,new_rows)):
                assert before["cell_id"]==after["cell_id"]
                assert before["metrics"]==after["metrics"],before["cell_id"]
                for side in ("strategy","matched"):
                    materialized=json.loads(
                        (destination/prefix/f"{index:02d}_{side}.json")
                        .read_text(encoding="utf-8"))
                    assert before[side]==materialized,(scenario,index,side)
        print("DIFFERENTIAL_PASS 48 baseline +48 delay2 +48 cost1_5x;"
              " complete strategy/matched raw events/ledger/episodes/MTM")


def resource_probe(size):
    import resource
    if size < 15*291:
        raise ValueError("long fixture requires 291 complete 15m buckets")
    limit=5*1024**3
    soft, hard=resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS,(limit, min(hard,limit)
                            if hard != resource.RLIM_INFINITY else limit))
    with tempfile.TemporaryDirectory() as folder:
        sources,config,inp,cfg=config_files(folder,size)
        target=Path(folder)/"bounded"
        start=time.perf_counter()
        execute_prepared(sources,config,inp,cfg,target,
                         sample_count=3,frozen=False)
        verified=verify_run(target)
        elapsed=time.perf_counter()-start
        peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        assert verified["artifact_count"]==289
        print(json.dumps({"status":"RESOURCE_CONTROL_PASS",
            "fixture_minutes_per_symbol":size,"limit_bytes":limit,
            "elapsed_seconds":elapsed,"peak_rss_kib":peak_kib,
            "verified_artifacts":verified["artifact_count"]},sort_keys=True))


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--differential",action="store_true")
    parser.add_argument("--base-dir")
    parser.add_argument("--resource",action="store_true")
    parser.add_argument("--minutes",type=int,default=30000)
    args=parser.parse_args()
    if args.differential:
        if not args.base_dir:
            parser.error("--differential requires --base-dir")
        differential(args.base_dir)
    elif args.resource:
        resource_probe(args.minutes)
    else:
        unittest.main(argv=[sys.argv[0]],verbosity=2)
