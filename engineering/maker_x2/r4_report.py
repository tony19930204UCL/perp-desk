"""R4 research artifacts and frozen exploratory 96-hypothesis audit."""
import json
import hashlib
from pathlib import Path
from decimal import Decimal as D, localcontext
from datetime import datetime, timezone
from itertools import product
from backtest_reports import atomic_json, sha256_file, canon
from backtest_stats import calendar_bootstrap, bh_flags
from account_ledger import replay_account
from scenario_rates import SENSITIVITY
from maker_exit_lab import X2Error

SYMS = ("ETHUSDT", "BTCUSDT")
SCEN = ("baseline", "delay2", "cost1_5x")
REG = ("HIGH_VOL", "UP_TREND", "DOWN_TREND", "RANGE")
STRUCT = tuple(product((".25", ".50"), ("X1", "X2", "X3")))
PREREG = "bc6a0c14bb47fbace2cf2edfbd2cd5377b568d8c43d0ad54bb0f79e3a9c37e8c"


def key(sym, mode, filt, scenario, k, exit_style):
    return f"{sym}:{mode}:{filt}:{scenario}:{k}:2:{exit_style}"


def required():
    return {key(*x) for x in product(SYMS, ("A", "B"), ("F0", "F1"),
                                      SCEN, (".25", ".50"), ("X1","X2","X3"))}


def money(result, matched):
    """Flat-to-flat bp; B does not synthesize an investable cash curve."""
    with localcontext() as ctx:
        ctx.prec=50
        ep = [e for e in result["episodes"] if D(e["entry_notional"]) > 0]
        gross = sum((D(e["gross"]) for e in ep), D(0))
        net = sum((D(e["net"]) for e in ep), D(0))
        notion = sum((D(e["entry_notional"]) for e in ep), D(0))
        wins = sum((D(e["net"]) for e in ep if D(e["net"]) > 0), D(0))
        losses = -sum((D(e["net"]) for e in ep if D(e["net"]) < 0), D(0))
        def bp(other):
            arr = [e for e in other["episodes"] if D(e["entry_notional"]) > 0]
            den = sum((D(e["entry_notional"]) for e in arr), D(0))
            return (sum((D(e["net"]) for e in arr),D(0))/den*10000
                    if den else None)
        matched_bp = bp(matched)
        net_bp = net/notion*10000 if notion else None
        types = {k:sum(e["exit_type"]==k for e in ep)
                 for k in ("TP","SL","SL_GAP","PASSIVE_X3","LIQ","NO_FILL")}
        return {"n":len(ep),"signals":result["signal_count"],
                "gross_bp":str(gross/notion*10000) if notion else None,
                "net_bp":str(net_bp) if net_bp is not None else None,
                "matched_net_bp":str(matched_bp) if matched_bp is not None else None,
                "net_alpha_bp":str(net_bp-matched_bp)
                if net_bp is not None and matched_bp is not None else None,
                "pf":str(wins/losses) if losses else None,
                "exits":types,"liq":types["LIQ"],
                "admin_4h":result["counts"].get("admin_4h_triggers",0),
                "open":result["counts"].get("open",0),
                "funnel":result.get("funnel"),
                "funnel_denoms":result.get("funnel_denoms")}


def audit_result(result, capital, rates):
    """Recheck every actual event, including all reset B accounts."""
    if result.get("research_mode")=="B":
        if result.get("ledger") is not None or not result.get("cash_curve_not_investable"):
            raise X2Error("B investment-curve conflation")
        sections=result["independent_episodes"]
        if not sections or sum(len(x["events"]) for x in sections)!=len(result["events"]):
            raise X2Error("missing B raw episode events")
    else:
        sections=[{"events":result["events"],"ledger":result["ledger"]}]
    from backtest_reports import canon
    for section in sections:
        actual=replay_account(str(capital),section["events"],rates=rates)
        if canon(actual)!=section["ledger"]:
            raise X2Error("independent R4 ledger divergence")
    if result.get("research_mode") or result.get("funnel"):
        f=result["funnel"]
        if (sum(f["terminal_counts"].values())+f["unprocessed"]
                !=result["signal_count"]):
            raise X2Error("R4 signal funnel not conserved")


def hash_source(root):
    """Include U1/U2/U3 and all tests, unlike frozen R3 manifest subset."""
    paths=sorted([p for p in root.glob("*.py") if p.name!="__init__.py"]+
                 list((root/"tests").glob("test_*.py")))
    return {p.relative_to(root).as_posix():sha256_file(p) for p in paths}


def report_family(rows, start, end):
    """Exactly 96 fixed B baseline regime tests, never posthoc family shrink."""
    bykey={}
    for row in rows:
        if row["mode"]!="B" or row["scenario"]!="baseline":
            continue
        for regime in REG:
            subgroup=[e for e in row["regime_episodes"] if e["regime"]==regime]
            test=(calendar_bootstrap(subgroup,
                    datetime.fromtimestamp(start/1000,timezone.utc).date().isoformat(),
                    datetime.fromtimestamp(end/1000,timezone.utc).date().isoformat())
                  if len(subgroup)>=100 else {"p":1,"status":"INSUFFICIENT",
                                              "p_is_placeholder":True,"ci95":None})
            bykey[(row["symbol"],row["filter"],row["k"],row["exit"],regime)]={
                "n":len(subgroup),"p":test["p"],"placeholder":test["p_is_placeholder"],
                "ci95":test["ci95"],"status":test["status"]}
    keys=list(product(SYMS,("F0","F1"),(".25",".50"),
                      ("X1","X2","X3"),REG))
    if len(keys)!=96 or set(bykey)!=set(keys):
        raise X2Error("missing fixed 96-family hypotheses")
    flags=bh_flags([bykey[k]["p"] for k in keys])
    output={}
    for k,passed in zip(keys,flags):
        item=dict(bykey[k])
        item["bh96"]=bool(passed)
        item["bonf96"]=D(str(item["p"]))<=D(".05")/96
        output[":".join(k)]=item
    return output


def write_manifest(root, meta, inventory):
    document=dict(meta,artifacts=inventory,artifact_count=len(inventory))
    atomic_json(Path(root)/"manifest.json",document)
    return sha256_file(Path(root)/"manifest.json")


def audit_matrix(rows, inventory):
    keys=[r["cell_id"] for r in rows]
    if len(keys)!=144 or set(keys)!=required() or len(set(keys))!=144:
        raise X2Error("R4 matrix incomplete/duplicate")
    if len([r for r in rows if r["symbol"]=="ETHUSDT"])!=72:
        raise X2Error("ETH row count invalid")
    for row in rows:
        for side in ("strategy","matched"):
            path=row[side+"_path"]
            if path not in inventory or row[side+"_sha256"]!=inventory[path]:
                raise X2Error("missing R4 original account artifact")
        if row["metrics"]["n"]<0:
            raise X2Error("negative episode count")
    return True
