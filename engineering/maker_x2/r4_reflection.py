"""Read-only, receipt-bound exploratory R4 interpretation."""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal as D
from backtest_reports import atomic_json, sha256_file
from maker_exit_lab import X2Error
from r4_entry import git_head, stage_args, receipt
from r4_report import audit_matrix, REG, PREREG


def reflect(root, config, input_path, stage_config, receipt_path, out,
            acceptance_path=None):
    root=Path(root)
    manifest=json.loads((root/"manifest.json").read_text())
    matrix=json.loads((root/"reports/r4-matrix.json").read_text())
    regime=json.loads((root/"reports/r4-regime.json").read_text())
    audit=json.loads((root/"reports/r4-verify.json").read_text())
    rows=matrix["rows"]
    audit_matrix(rows,manifest["artifacts"])
    head=git_head()
    inp=sha256_file(input_path)
    graph=stage_args(stage_config,"r4-reflection",head,inp)
    if (head!=manifest["source_head"] or inp!=manifest["input_sha256"] or
            sha256_file(config)!=manifest["config_sha256"] or
            audit["stage"]!="r4-verify" or audit["source_head"]!=head or
            audit["input_sha256"]!=inp or audit["rows"][0]["verified"]!=144):
        raise X2Error("reflection previous-stage identity rejected")
    if regime["prereg_sha256"]!=PREREG or len(regime["family_m96"])!=96:
        raise X2Error("reflection family/prereg missing")
    prior=sha256_file(root/"reports/r4-verify.json")
    if not acceptance_path:
        raise X2Error("reflection requires independent acceptance")
    chain=receipt(receipt_path,acceptance_path,"r4-verify",head,inp,
                  graph,root/"reports/r4-verify.json")
    from decimal import localcontext
    family=regime["family_m96"]
    baseline={}
    computed=[]
    for row in rows:
        if row["mode"]!="B":
            continue
        raw={}
        for side in ("strategy","matched"):
            raw[side]=json.loads((root/row[side+"_path"]).read_text())
        for label in REG:
            ep={}
            for side in ("strategy","matched"):
                ep[side]=[
                    e for e in raw[side]["episodes"]
                    if e.get("arrival_ts") is not None
                    and e["arrival_ts"]>=1714521600000
                    and regime["daily"][row["symbol"]].get(
                        str(e["arrival_ts"]//86400000),{}).get("regime")==label
                    and D(e["entry_notional"])>0]
            def facts(series):
                with localcontext() as ctx:
                    ctx.prec=50
                    notional=sum((D(e["entry_notional"]) for e in series),D(0))
                    gross=sum((D(e["gross"]) for e in series),D(0))
                    net=sum((D(e["net"]) for e in series),D(0))
                    pos=sum((D(e["net"]) for e in series if D(e["net"])>0),D(0))
                    neg=-sum((D(e["net"]) for e in series if D(e["net"])<0),D(0))
                    return {"n":len(series),"gross_bp":str(gross/notional*10000)
                            if notional else None,
                            "net_bp":str(net/notional*10000) if notional else None,
                            "pf":str(pos/neg) if neg else None,
                            "liq":sum(e["exit_type"]=="LIQ" for e in series)}
            a,b=facts(ep["strategy"]),facts(ep["matched"])
            with localcontext() as ctx:
                ctx.prec=50
                alpha=(str(D(a["net_bp"])-D(b["net_bp"]))
                       if a["net_bp"] is not None and b["net_bp"] is not None
                       else None)
            key=":".join((row["symbol"],row["filter"],row["k"],row["exit"],label))
            fam=family[key]
            good=(a["n"]>=100 and a["net_bp"] is not None and
                  D(a["net_bp"])>0 and fam["bh96"] and alpha is not None and
                  D(alpha)>5 and a["pf"] is not None and D(a["pf"])>D("1.30"))
            item={"cell_id":row["cell_id"],"regime":label,
                  "strategy":a,"matched":b,"net_alpha_bp":alpha,
                  "bh96":fam["bh96"],"bonf96":fam["bonf96"],
                  "candidate_base":bool(good)}
            computed.append(item)
            if row["scenario"]=="baseline":
                baseline[key]=item
    candidates=[]
    for key,item in baseline.items():
        if not item["candidate_base"]:
            continue
        stress=[r for r in computed if r["regime"]==item["regime"]
                and all(t in r["cell_id"] for t in key.split(":")[:1])
                and r["cell_id"].split(":")[1] == "B"
                and r["cell_id"].split(":")[2] == key.split(":")[1]
                and r["cell_id"].split(":")[4] == key.split(":")[2]
                and r["cell_id"].split(":")[-1] == key.split(":")[3]
                and r["cell_id"].split(":")[3] in ("delay2","cost1_5x")]
        if len(stress)==2 and all(r["strategy"]["net_bp"] is not None
                and D(r["strategy"]["net_bp"])>0
                and r["net_alpha_bp"] is not None and D(r["net_alpha_bp"])>5
                and r["strategy"]["pf"] is not None
                and D(r["strategy"]["pf"])>D("1.30") for r in stress):
            candidates.append(key)
    decision={"status":"EXPLORATORY_NO_HOLDOUT_NOT_DEPLOYABLE",
              "candidates":candidates,"regime_measures":computed,
              "h1":"B post-2024-05 by regime: gross_bp and n in regime_measures",
              "h2":"F0/F1 net, LIQ and exposed signal counts; no guaranteed reduction",
              "limitations":["Historical exploration contaminated; no new holdout",
                             "Six earlier surviving mutants are unresolved",
                             "OHLC queue and mark-price liquidation unverified",
                             "B reset research accounts are not an investment curve",
                             "UNKNOWN/gap/open-tail coverage not a protective PASS"],
              "next_data":"Point-in-time holdout and queue/mark microstructure"}
    report={"stage":"r4-reflection","source_head":head,
            "input_sha256":inp,"graph_sha256":graph,
            "verify_report_sha256":prior,**chain,
            "rows":[{"symbol":"ALL","cell_id":"REFLECTION","decision":decision}]}
    target=Path(out)
    if target.exists():
        raise X2Error("reflection result path already exists")
    atomic_json(target,report)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser()
    for name in ("config","input","output","stage-config","receipt","acceptance","report"):
        parser.add_argument("--"+name,required=True)
    args=parser.parse_args(argv)
    try:
        result=reflect(args.output,args.config,args.input,args.stage_config,
                       args.receipt,args.report,args.acceptance)
        print(json.dumps(result,sort_keys=True),flush=True)
        return 0
    except (OSError,ValueError,KeyError,X2Error) as exc:
        print("R4 REFLECTION FAILED: "+str(exc),file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
