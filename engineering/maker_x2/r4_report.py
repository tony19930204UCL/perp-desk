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
from functools import wraps


def decimal50(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with localcontext() as ctx:
            ctx.prec=50
            return fn(*args, **kwargs)
    return wrapped

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


@decimal50
def regime_measures(root, rows, daily, since=0):
    """Both sides use identical causal day labels and time boundaries."""
    output=[]
    for row in rows:
        if row["mode"]!="B":
            continue
        raw={s:json.loads((Path(root)/row[s+"_path"]).read_text())
             for s in ("strategy","matched")}
        for label in (*REG,"UNKNOWN","UNCLASSIFIED"):
            pair=[]
            facts={}
            for side,result in raw.items():
                eps=[e for e in result["episodes"]
                     if e.get("arrival_ts") is not None and e["arrival_ts"]>=since
                     and daily[row["symbol"]].get(str(e["arrival_ts"]//86400000),
                         {}).get("regime","UNCLASSIFIED")==label]
                view=dict(result,episodes=eps)
                pair.append(view)
                facts[side]=money(view,view)
                facts[side]["entry_notional"]=str(sum(
                    (D(e["entry_notional"]) for e in eps),D(0)))
                facts[side]["gaps"]=sum(e.get("type")=="DATA_GAP"
                    and e.get("ts",0)>=since for e in result.get("other_events",[]))
            alpha=money(*pair)["net_alpha_bp"]
            a,b=facts["strategy"],facts["matched"]
            output.append({"cell_id":row["cell_id"],"regime":label,
                "strategy":a,"matched":b,"net_alpha_bp":alpha,
                "judgement":("INSUFFICIENT" if min(a["n"],b["n"])<100 else
                    "DESCRIPTIVE_POSITIVE_ALPHA" if D(alpha)>0 else "NO_POSITIVE_ALPHA")})
    return output


@decimal50
def filter_effects(measures):
    output=[]
    index={(x["cell_id"],x["regime"]):x for x in measures}
    for (cid,label),f0 in index.items():
        if cid.split(":")[2]!="F0":
            continue
        f1=index[(cid.replace(":F0:",":F1:"),label)]
        effect={"cell_id":cid,"regime":label}
        for side in ("strategy","matched"):
            a,b=f0[side],f1[side]
            effect[side]={"f0_net_bp":a["net_bp"],"f1_net_bp":b["net_bp"],
                "f0_liq":a["liq"],"f1_liq":b["liq"],"trade_decrease":a["n"]-b["n"],
                "retained_exposure":str(D(b["entry_notional"])/D(a["entry_notional"]))
                    if D(a["entry_notional"]) else None,
                "f0_gaps":a["gaps"],"f1_gaps":b["gaps"],
                "status":"INSUFFICIENT" if min(a["n"],b["n"])<100 else "DESCRIPTIVE"}
        output.append(effect)
    return output


@decimal50
def entry_notionals(result):
    """Derive new report denominators without changing replay/ledger context."""
    sections=(result["independent_episodes"] if result.get("research_mode")=="B"
              else [{"events":result["events"]}])
    notionals={}
    for section in sections:
        current=None
        for event in section["events"]:
            if event["type"]=="reserve":
                current=event["episode"]
                notionals[current]=D(0)
            elif event["type"]=="fill" and event["side"]=="sell":
                notionals[current]+=D(event["qty"])*D(event["price"])
    for ep in result["episodes"]:
        ep["entry_notional"]=str(notionals[ep["id"]])
    return result


@decimal50
def audit_result(result, capital, rates):
    """Recheck every actual event, including all reset B accounts."""
    if result.get("research_mode")=="B":
        if result.get("ledger") is not None or not result.get("cash_curve_not_investable"):
            raise X2Error("B investment-curve conflation")
        sections=result["independent_episodes"]
        if not sections or [e for x in sections for e in x["events"]]!=result["events"]:
            raise X2Error("missing B raw episode events")
    else:
        sections=[{"events":result["events"],"ledger":result["ledger"]}]
    from backtest_reports import canon
    episodes=[]
    for section in sections:
        actual=replay_account(str(capital),section["events"],rates=rates)
        if canon(actual)!=section["ledger"]:
            raise X2Error("independent R4 ledger divergence")
        for ep in canon(actual)["episodes"]:
            reserves=[i for i,e in enumerate(section["events"])
                      if e["type"]=="reserve" and e.get("episode")==ep["id"]]
            if len(reserves)!=1:
                raise X2Error("episode reserve mismatch")
            offset=reserves[0]+1
            stop=next((i for i in range(offset,len(section["events"]))
                       if section["events"][i]["type"]=="reserve"),
                      len(section["events"]))
            events=section["events"][offset:stop]
            notion=sum((D(e["qty"])*D(e["price"]) for e in events
                        if e["type"]=="fill" and e["side"]=="sell"),D(0))
            buys=[e for e in events if e["type"]=="fill" and e["side"]=="buy"]
            last=buys[-1] if buys else {}
            src=last.get("source_id",last.get("id",""))
            kind=("LIQ" if any(e["type"]=="liquidation" for e in events) else
                  "SL_GAP" if "gap-risk" in src else "SL" if "risk-" in src else
                  "TP" if "profit-" in src else "PASSIVE_X3" if "withdraw-" in src
                  else "NO_FILL")
            episodes.append((ep,notion,kind))
    if len(episodes)!=len(result["episodes"]):
        raise X2Error("financial episode count mismatch")
    for (ep,notion,kind),reported in zip(episodes,result["episodes"]):
        if (any(reported.get(k)!=v for k,v in ep.items()) or
                D(reported["entry_notional"])!=notion or
                reported["exit_type"]!=kind):
            raise X2Error("financial episode differs from ledger events")
    if result.get("research_mode") or result.get("funnel"):
        f=result["funnel"]
        if (any(type(v) is not int or v<0 for v in
                [*f["terminal_counts"].values(),f["unprocessed"]]) or
                sum(f["terminal_counts"].values())+f["unprocessed"]
                !=result["signal_count"]):
            raise X2Error("R4 signal funnel not conserved")


def hash_source(root):
    """Include U1/U2/U3 and all tests, unlike frozen R3 manifest subset."""
    paths=sorted([p for p in root.glob("*.py") if p.name!="__init__.py"]+
                 list((root/"tests").glob("test_*.py")))
    return {p.relative_to(root).as_posix():sha256_file(p) for p in paths}


@decimal50
def regime_tests(episodes, start, end):
    result={}
    for label in REG:
        group=[e for e in episodes if e["regime"]==label
               and D(e["entry_notional"])>0]
        test=(calendar_bootstrap(group,
              datetime.fromtimestamp(start/1000,timezone.utc).date().isoformat(),
              datetime.fromtimestamp(end/1000,timezone.utc).date().isoformat())
              if len(group)>=100 else {"p":1,"status":"INSUFFICIENT",
                                       "p_is_placeholder":True,"ci95":None})
        result[label]={"n":len(group),"p":test["p"],
                       "placeholder":test["p_is_placeholder"],
                       "ci95":test["ci95"],"status":test["status"]}
    return result


@decimal50
def report_family(rows, start, end):
    """Exactly 96 fixed B baseline regime tests, never posthoc family shrink."""
    bykey={}
    for row in rows:
        if row["mode"]!="B" or row["scenario"]!="baseline":
            continue
        tests=(row["regime_tests"] if "regime_tests" in row else
               regime_tests(row["regime_episodes"],start,end))
        for regime in REG:
            bykey[(row["symbol"],row["filter"],row["k"],row["exit"],regime)]=tests[regime]
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
        parts=row["cell_id"].split(":")
        for name,value in zip(("symbol","mode","filter","scenario","k","ttl","exit"),parts):
            if name in row and str(row[name])!=value:
                raise X2Error("matrix metadata differs from cell identity")
        for side in ("strategy","matched"):
            path=row[side+"_path"]
            if path not in inventory or row[side+"_sha256"]!=inventory[path]:
                raise X2Error("missing R4 original account artifact")
        if row["metrics"]["n"]<0:
            raise X2Error("negative episode count")
    return True
