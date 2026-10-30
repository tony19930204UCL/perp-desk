"""Full offline R4 run/verify/15m diagnostics; no trading or network.

--config: frozen account/window JSON; --input: both symbols' bars/funding paths;
--output: new run directory (existing run for verify/diag15);
--stage-config: the ONE installed stage_relay graph, unchanged between stages;
--receipt / --acceptance: controller's successful prior-stage receipt and
independent ACCEPTED JSON, respectively. They are read only, never generated.
Reports are reports/r4-matrix.json (144 rows), r4-verify.json (one row),
and r4-diag15.json (12 diagnostic rows, outside the family).
CLI execution requires POSIX RLIMIT_AS and enforces a hard 5 GiB ceiling.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
import resource
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone

from backtest_entry import load_config
from backtest_reports import atomic_json, sha256_file
from bounded_history import prepare_bars, prepare_decisions
from data_adapter import read_funding
from market_replay import replay_cell
from matched_signals import select_matched
from path_speed import SpeedFilter
from regime_split import classify, regime_of, DAY_MS
from scenario_rates import SENSITIVITY
from maker_exit_lab import X2Error
from r4_report import (SYMS, SCEN, STRUCT, PREREG, money, audit_result,
                       audit_matrix, hash_source, report_family, write_manifest,
                       regime_tests, entry_notionals)

STAGES = ("r4-run", "r4-verify", "r4-reflection")


def phase_log(name, symbol, started=None):
    """Process-wide high-water RSS; Linux ru_maxrss is KiB."""
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    elapsed = "" if started is None else f" sec={time.monotonic()-started:.3f}"
    print(f"PHASE {symbol} {name} peak_rss_mib={rss:.1f}{elapsed}",
          flush=True)
    return time.monotonic()



def cap_memory():
    try:
        import resource
    except ImportError as exc:
        raise X2Error("POSIX RLIMIT_AS required for hard 5GiB cap") from exc
    cap=5*1024**3
    soft,hard=resource.getrlimit(resource.RLIMIT_AS)
    limit=min(cap,hard) if hard!=resource.RLIM_INFINITY else cap
    resource.setrlimit(resource.RLIMIT_AS,
        (min(soft,limit) if soft!=resource.RLIM_INFINITY else limit,limit))


class DiagFilter:
    def skip(self, ts):
        return False,"DIAG_PASS"


def git_head():
    result=subprocess.run(["git","rev-parse","HEAD"],text=True,
                          capture_output=True,check=True)
    head=result.stdout.strip()
    if len(head)!=40 or any(x not in "0123456789abcdef" for x in head):
        raise X2Error("invalid source HEAD")
    return head


def stage_args(path, expected, head, input_sha):
    """Validate one stage from the SAME installed stage_relay graph bytes."""
    if not path:
        return None
    raw=Path(path).read_bytes()
    graph=json.loads(raw)
    if (graph.get("schema_version")!=1 or graph.get("kind")!="stage_relay"
            or not Path(graph["evidence_root"]).is_absolute()
            or not isinstance(graph.get("stages"),list)):
        raise X2Error("invalid stage_relay graph")
    ids=[x["id"] for x in graph["stages"]]
    if len(ids)!=len(set(ids)) or not set(STAGES).issubset(ids):
        raise X2Error("missing/duplicate R4 stage")
    entry=next(x for x in graph["stages"] if x["id"]==expected)
    command=entry.get("command",entry.get("argv"))
    if (entry.get("mode")!="exec" or entry.get("source_head")!=head
            or entry.get("input_sha256")!=input_sha
            or not isinstance(command,list)
            or ("cwd" in entry and (not isinstance(entry["cwd"],str)
                or not Path(entry["cwd"]).is_absolute()))
            or not entry.get("reports")):
        raise X2Error("stage graph/source/input identity mismatch")
    return hashlib.sha256(raw).hexdigest()


def receipt(path, acceptance, stage, head, inp, graph, report):
    """Verify actual completed controller receipt PLUS separate acceptance."""
    value=json.loads(Path(path).read_text(encoding="utf-8"))
    expected={"stage":stage,"source_head":head,"input_sha256":inp,
              "graph_sha256":graph}
    if any(value.get(k)!=v for k,v in expected.items()):
        raise X2Error("previous receipt identity mismatch")
    owner=value.get("owner")
    if (not isinstance(owner,dict) or not all(
            owner.get(k) is not None for k in
            ("pid","start_ticks","boot_id","token"))
            or value.get("exit_code")!=0 or value.get("outcome")!="SUCCEEDED"):
        raise X2Error("previous receipt not genuine successful executor")
    rows=144 if stage=="r4-run" else 1
    reports=value.get("reports",[])
    if not any(r.get("path") in (report.name,"reports/"+report.name) and
               r.get("sha256")==sha256_file(report) and
               r.get("rows")==rows for r in reports):
        raise X2Error("previous receipt report identity mismatch")
    accepted=json.loads(Path(acceptance).read_text(encoding="utf-8"))
    expected["receipt_sha256"]=sha256_file(path)
    if (any(accepted.get(k)!=v for k,v in expected.items()) or
        accepted.get("decision")!="ACCEPTED" or not accepted.get("reviewer")):
        raise X2Error("missing independent prior acceptance")
    return {"receipt_sha256":sha256_file(path),
            "acceptance_sha256":sha256_file(acceptance)}


def read_sources(path):
    raw=json.loads(Path(path).read_text(encoding="utf-8"))
    if set(raw)!=set(SYMS) or any(set(raw[s])!={"bars","funding"} for s in SYMS):
        raise X2Error("both original independent symbol input paths required")
    return raw


def collect(result, regimes):
    enriched=[]
    for ep in result["episodes"]:
        date=ep.get("arrival_ts")
        label=regime_of(regimes,date) if date is not None else "UNCLASSIFIED"
        enriched.append(dict(ep,regime=label))
    return enriched


def replay(symbol, bars, funding, eligible, selection, config, start, end,
           k, exit_style, mode, filtered, scenario, speed):
    delay = 2 if scenario=="delay2" else 0
    rates = SENSITIVITY if scenario=="cost1_5x" else None
    kwargs={"delay_minutes":delay,"eligible":eligible,"rates":rates,
            "mode":mode,"entry_filter":speed if filtered else None}
    strategy=replay_cell(symbol,bars,funding,1,k,2,exit_style,config,
                         start,end,**kwargs)
    if selection["status"]!="READY":
        raise X2Error("R4 matched universe cannot supply frozen 6000")
    matched=replay_cell(symbol,bars,funding,1,k,2,exit_style,config,
        start,end,signal_times=selection["selected_timestamps"],**kwargs)
    for result in (strategy,matched):
        entry_notionals(result)
        audit_result(result,config["capital"],rates)
    return strategy,matched


def generate_diag15(symbol, bars, funding_events, config, start, end, workdir,
                    output=None):
    """Original 15m A/F0 baseline; never an additional family member."""
    diag=[]
    eligible15=prepare_decisions(bars,15,Path(workdir)/"15m.db")
    placebo15=select_matched(eligible15,15,start,end)
    if placebo15.get("status")!="READY":
        raise X2Error("15m matched universe cannot supply frozen 6000")
    for k,exit_style in STRUCT:
        a=replay_cell(symbol,bars,funding_events,15,k,2,exit_style,
             config,start,end,eligible=eligible15,entry_filter=DiagFilter())
        b=replay_cell(symbol,bars,funding_events,15,k,2,exit_style,
             config,start,end,eligible=eligible15,
             signal_times=placebo15["selected_timestamps"],entry_filter=DiagFilter())
        audit_result(entry_notionals(a),config["capital"],None)
        audit_result(entry_notionals(b),config["capital"],None)
        row={"symbol":symbol,"cell_id":
            f"{symbol}:15m:A:F0:baseline:{k}:2:{exit_style}",
            "metrics":money(a,b),"diagnostic_only":True}
        if output is not None:
            for side,raw in (("strategy",a),("matched",b)):
                name="cells/"+row["cell_id"].replace(":","_")+"_"+side+".json"
                row[side+"_path"]=name
                row[side+"_sha256"]=atomic_json(Path(output)/name,raw)
        diag.append(row)
    return diag


def diag15(args):
    config=load_config(args.config)
    if str(config["capital"])!="150":
        raise X2Error("R4 A capital must remain 150")
    head=git_head()
    inp=sha256_file(args.input)
    sources=read_sources(args.input)
    graph=stage_args(args.stage_config,"r4-run",head,inp)
    target=Path(args.output)/"reports/r4-diag15.json"
    if target.exists():
        raise X2Error("15m diagnostic report already exists; no overwrite")
    start,end=int(config["evaluation_start"]),int(config["evaluation_end"])
    rows=[]
    hashes={path:sha256_file(path) for src in sources.values()
            for path in src.values()}
    with TemporaryDirectory(prefix="maker-r4-diag15-") as tmp:
        for symbol in SYMS:
            sub=Path(tmp)/symbol
            sub.mkdir()
            src=sources[symbol]
            prepared=prepare_bars(src["bars"],sub/"bars.db")
            if (prepared["count"]!=1604160 or
                prepared["start_open_ms"] > 1696118400000 or
                prepared["end_open_ms"]+60000 < end):
                raise X2Error("full historical coverage/bar count incorrect")
            fund=read_funding(src["funding"])
            rows.extend(generate_diag15(symbol,prepared["bars"],
                        fund["events"],config,start,end,sub,Path(args.output)))
    if (sha256_file(args.input)!=inp or any(
            sha256_file(path)!=digest for path,digest in hashes.items())):
        raise X2Error("15m diagnostic input changed during run")
    report={"stage":"r4-run","source_head":head,"input_sha256":inp,
            "graph_sha256":graph,"rows":rows,"family_member":False}
    if target.exists():
        raise X2Error("15m diagnostic report already exists; no overwrite")
    atomic_json(target,report)
    return report


def run(args):
    config=load_config(args.config)  # Enforces original frozen dates.
    if str(config["capital"])!="150":
        raise X2Error("R4 A capital must remain 150")
    sources=read_sources(args.input)
    start,end=int(config["evaluation_start"]),int(config["evaluation_end"])
    head=git_head()
    input_sha=sha256_file(args.input)
    graph=stage_args(args.stage_config,"r4-run",head,input_sha)
    before={p:sha256_file(p) for src in sources.values() for p in src.values()}
    root=Path(args.output)
    if root.exists():
        raise X2Error("R4 output root exists: never overwrite")
    root.mkdir(parents=True)
    source=hash_source(Path(__file__).resolve().parent)
    inputs={}
    items={}
    rows=[]
    diag=[]
    regimes={}
    with TemporaryDirectory(prefix="maker-r4-") as tmp:
        for symbol in SYMS:
            print("PREPARE",symbol,flush=True)
            tick=phase_log("begin",symbol)
            sub=Path(tmp)/symbol
            sub.mkdir()
            src=sources[symbol]
            prepared=prepare_bars(src["bars"],sub/"bars.db")
            if (prepared["count"]!=1604160 or
                prepared["start_open_ms"] > 1696118400000 or
                prepared["end_open_ms"]+60000 < end):
                raise X2Error("full historical coverage/bar count incorrect")
            tick=phase_log("prepare_bars",symbol,tick)
            fund=read_funding(src["funding"])
            inputs[symbol]={"bars_sha256":prepared["sha256"],
                            "funding_sha256":fund["sha256"],
                            "bars":prepared["count"],
                            "funding":fund["count"],
                            "gaps":prepared["gaps"]}
            bars=prepared["bars"]
            tick=phase_log("read_funding",symbol,tick)
            table=classify(bars,start,end)
            tick=phase_log("classify",symbol,tick)
            regimes[symbol]=table
            eligible=prepare_decisions(bars,1,sub/"1m.db")
            tick=phase_log("prepare_decisions",symbol,tick)
            matched=select_matched(eligible,1,start,end)
            tick=phase_log("select_matched",symbol,tick)
            speed=SpeedFilter(bars)
            tick=phase_log("SpeedFilter",symbol,tick)
            print("FEATURES",symbol,"1m",len(eligible),flush=True)
            for mode in ("A","B"):
                for filt in ("F0","F1"):
                    for scenario in SCEN:
                        for k,exit_style in STRUCT:
                            cid=f"{symbol}:{mode}:{filt}:{scenario}:{k}:2:{exit_style}"
                            print("REPLAY",cid,flush=True)
                            cell_tick=time.monotonic()
                            a,b=replay(symbol,bars,fund["events"],eligible,
                                matched,config,start,end,k,exit_style,mode,
                                filt=="F1",scenario,speed)
                            entry={"symbol":symbol,"cell_id":cid,"mode":mode,
                                   "filter":filt,"scenario":scenario,
                                   "k":k,"ttl":2,"exit":exit_style,
                                   "metrics":money(a,b)}
                            for side,result in (("strategy",a),("matched",b)):
                                name=f"cells/{cid.replace(':','_')}_{side}.json"
                                items[name]=atomic_json(root/name,result)
                                entry[side+"_path"]=name
                                entry[side+"_sha256"]=items[name]
                            if mode=="B" and scenario=="baseline":
                                entry["regime_tests"]=regime_tests(collect(a,table),start,end)
                            rows.append(entry)
                            phase_log("replay "+cid,symbol,cell_tick)
                            # No matrix raw is held after each cell write.
                            del a,b
            tick=time.monotonic()
            diag.extend(generate_diag15(symbol,bars,fund["events"],
                                        config,start,end,sub,root))
            phase_log("diag15",symbol,tick)
            del speed, eligible, matched, table, bars, prepared, fund
            phase_log("symbol_done",symbol)
    for row in diag:
        for side in ("strategy","matched"):
            items[row[side+"_path"]]=row[side+"_sha256"]
    audit_matrix(rows,items)
    if sha256_file(args.input)!=input_sha or any(
            sha256_file(p)!=sha for p,sha in before.items()):
        raise X2Error("R4 input changed during run")
    family=report_family(rows,start,end)
    matrix=rows
    docs={"reports/r4-matrix.json":{
              "stage":"r4-run","source_head":head,"input_sha256":input_sha,
              "graph_sha256":graph,"rows":matrix},
          "reports/r4-regime.json":{"family_m96":family,
               "daily":regimes,"prereg_sha256":PREREG,
               "status":"EXPLORATORY_NO_HOLDOUT"},
          "reports/r4-diag15.json":{"stage":"r4-run","source_head":head,
              "input_sha256":input_sha,"graph_sha256":graph,"rows":diag,
                                   "family_member":False}}
    for name,doc in docs.items():
        items[name]=atomic_json(root/name,doc)
    if hash_source(Path(__file__).resolve().parent)!=source:
        raise X2Error("R4 source changed during run")
    meta={"stage":"r4-run","source_head":head,"source_sha256":source,
          "input_sha256":input_sha,"config_sha256":sha256_file(args.config),
          "graph_sha256":graph,"inputs":inputs,"rows":len(rows),
          "research_status":"EXPLORATORY_NOT_ACCEPTED"}
    manifest=write_manifest(root,meta,items)
    return {"stage":"r4-run","source_head":head,"input_sha256":input_sha,
            "graph_sha256":graph,"rows":matrix,"manifest_sha256":manifest}


def verify(args, write=True):
    root=Path(args.output)
    doc=json.loads((root/"manifest.json").read_text(encoding="utf-8"))
    head=git_head()
    inp=sha256_file(args.input)
    graph=stage_args(args.stage_config,"r4-verify",head,inp)
    chain={}
    if args.stage_config and write:
        if not args.receipt or not args.acceptance:
            raise X2Error("r4-verify requires receipt and acceptance")
        chain=receipt(args.receipt,args.acceptance,"r4-run",head,inp,graph,
                root/"reports/r4-matrix.json")
    if (doc["source_head"]!=head or doc["input_sha256"]!=inp or
        doc["graph_sha256"]!=graph or
        doc["config_sha256"]!=sha256_file(args.config) or
        doc["source_sha256"]!=hash_source(Path(__file__).resolve().parent)):
        raise X2Error("R4 manifest source/input/config/graph changed")
    sources=read_sources(args.input)
    for symbol in SYMS:
        actual=doc["inputs"][symbol]
        for kind,field in (("bars","bars_sha256"),
                           ("funding","funding_sha256")):
            if sha256_file(sources[symbol][kind])!=actual[field]:
                raise X2Error("original underlying market data hash mismatch")
    needed={"reports/r4-matrix.json","reports/r4-regime.json","reports/r4-diag15.json"}
    if not needed.issubset(doc["artifacts"]) or doc["rows"]!=144:
        raise X2Error("missing required R4 reports")
    for name,sha in doc["artifacts"].items():
        if not (name.startswith("cells/") or name.startswith("reports/")):
            raise X2Error("unexpected R4 artifact path")
        target=(root/name).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            raise X2Error("R4 artifact escapes output")
        if sha256_file(target)!=sha:
            raise X2Error("R4 artifact hash mismatch")
    matrix=json.loads((root/"reports/r4-matrix.json").read_text())
    rows=matrix["rows"]
    for name,value in {"stage":"r4-run","source_head":head,
                       "input_sha256":inp,"graph_sha256":graph}.items():
        if matrix.get(name)!=value:
            raise X2Error("matrix identity mismatch")
    audit_matrix(rows,doc["artifacts"])
    cfg=load_config(args.config)
    if str(cfg["capital"])!="150":
        raise X2Error("R4 A capital must remain 150")
    regime=json.loads((root/"reports/r4-regime.json").read_text())
    tests=[]
    diag=json.loads((root/"reports/r4-diag15.json").read_text())
    expected={f"{s}:15m:A:F0:baseline:{k}:2:{x}" for s in SYMS for k,x in STRUCT}
    if (diag.get("family_member") is not False or
            len(diag["rows"])!=12 or {r["cell_id"] for r in diag["rows"]}!=expected):
        raise X2Error("diagnostic coverage mismatch")
    for row in diag["rows"]:
        pair=[]
        for side in ("strategy","matched"):
            name=row[side+"_path"]
            if doc["artifacts"].get(name)!=row[side+"_sha256"]:
                raise X2Error("diagnostic raw artifact missing")
            raw=json.loads((root/name).read_text())
            audit_result(raw,cfg["capital"],None)
            pair.append(raw)
        if money(*pair)!=row["metrics"]:
            raise X2Error("diagnostic metrics mismatch")
    for row in rows:
        rates=SENSITIVITY if row["scenario"]=="cost1_5x" else None
        pair=[]
        for side in ("strategy","matched"):
            raw=json.loads((root/row[side+"_path"]).read_text())
            fields={"symbol":row["symbol"],"framework":1,"k":row["k"],
                    "ttl":2,"exit":row["exit"],"matched":side=="matched",
                    "delay_minutes":2 if row["scenario"]=="delay2" else 0}
            if (any(raw.get(k)!=v for k,v in fields.items()) or
                    raw.get("research_mode","A")!=row["mode"] or
                    doc["inputs"][row["symbol"]]["bars"]!=1604160):
                raise X2Error("raw cell identity/coverage mismatch")
            audit_result(raw,cfg["capital"],rates)
            pair.append(raw)
        if money(*pair)!=row["metrics"]:
            raise X2Error("R4 metrics do not match original episodes")
        if row["mode"]=="B" and row["scenario"]=="baseline":
            table={int(k):v for k,v in regime["daily"][row["symbol"]].items()}
            computed=regime_tests(collect(pair[0],table),
                                 int(cfg["evaluation_start"]),int(cfg["evaluation_end"]))
            if row.get("regime_tests")!=computed:
                raise X2Error("regime statistics mismatch")
            tests.append(dict(row,regime_tests=computed))
    family=report_family(tests,int(cfg["evaluation_start"]),int(cfg["evaluation_end"]))
    if regime["family_m96"]!=family or regime["prereg_sha256"]!=PREREG:
        raise X2Error("missing original prereg family")
    report={"stage":"r4-verify","source_head":head,"input_sha256":inp,
            "graph_sha256":graph,"run_graph_sha256":doc["graph_sha256"],**chain,
            "rows":[{"symbol":"ALL","cell_id":"AUDIT",
            "verified":len(rows),"manifest_sha256":sha256_file(root/"manifest.json")}]}
    if not write:
        return report
    if (root/"reports/r4-verify.json").exists():
        raise X2Error("verify report already exists; no overwrite")
    atomic_json(root/"reports/r4-verify.json",report)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser()
    modes=parser.add_mutually_exclusive_group(required=True)
    for name in ("run","verify","diag15"):
        modes.add_argument("--"+name,action="store_true")
    for name in ("config","input","output","stage-config","receipt",
                 "acceptance"):
        parser.add_argument("--"+name,required=name in ("config","input","output"))
    args=parser.parse_args(argv)
    try:
        cap_memory()
        if args.diag15:
            result=diag15(args)
        else:
            result=run(args) if args.run else verify(args)
        print(json.dumps(result,sort_keys=True,default=str),flush=True)
        return 0
    except (OSError,ValueError,KeyError,X2Error) as exc:
        print("R4 FAILED:",str(exc),file=sys.stderr,flush=True)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
