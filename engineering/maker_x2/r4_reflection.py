"""Read-only, receipt-bound exploratory R4 interpretation.

--config/--input/--output/--stage-config identify the same run and graph.
--receipt and --acceptance are the actual r4-verify controller receipt and
independent acceptance; --report is output/reports/r4-reflection.json (one row).
No approval or controller receipt is synthesized by this module.
"""
import argparse
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal as D
from backtest_reports import atomic_json, sha256_file
from maker_exit_lab import X2Error
from r4_entry import git_head, stage_args, receipt, verify, cap_memory
from types import SimpleNamespace
from r4_report import (audit_matrix, REG, PREREG, regime_measures,
                       filter_effects, decimal50)


@decimal50
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
    # Restrict Decimal50 to report arithmetic, including independent audits.
    checked=verify(SimpleNamespace(output=str(root),config=config,input=input_path,
                    stage_config=stage_config),write=False)
    if (audit["graph_sha256"]!=graph or manifest["graph_sha256"]!=graph or
            audit["rows"]!=checked["rows"]):
        raise X2Error("reflection stale verification report")
    family=regime["family_m96"]
    computed=regime_measures(root,rows,regime["daily"])
    h1=regime_measures(root,rows,regime["daily"],1714521600000)
    baseline={}
    for item in computed:
        bits=item["cell_id"].split(":")
        label=item["regime"]
        if label not in REG:
            item["candidate_base"]=False
            continue
        key=":".join((bits[0],bits[2],bits[4],bits[6],label))
        fam=family[key]
        a=item["strategy"]
        alpha=item["net_alpha_bp"]
        item.update(bh96=fam["bh96"],bonf96=fam["bonf96"])
        item["candidate_base"]=bool(a["n"]>=100 and
            a["net_bp"] is not None and D(a["net_bp"])>0 and
            fam["bh96"] and alpha is not None and D(alpha)>5 and
            a["pf"] is not None and D(a["pf"])>D("1.30"))
        if bits[3]=="baseline":
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
        if len(stress)==2 and all(r["strategy"]["n"]>=100
                and r["strategy"]["net_bp"] is not None
                and D(r["strategy"]["net_bp"])>0
                and r["net_alpha_bp"] is not None and D(r["net_alpha_bp"])>5
                and r["strategy"]["pf"] is not None
                and D(r["strategy"]["pf"])>D("1.30") for r in stress):
            candidates.append(key)
    decision={"status":"EXPLORATORY_NO_HOLDOUT_NOT_DEPLOYABLE",
              "candidates":candidates,"regime_measures":computed,
              "h1":{"since":1714521600000,"measures":h1,
                    "status":"DESCRIPTIVE_OR_INSUFFICIENT"},
              "h2":filter_effects(computed),
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
        cap_memory()
        result=reflect(args.output,args.config,args.input,args.stage_config,
                       args.receipt,args.report,args.acceptance)
        print(json.dumps(result,sort_keys=True),flush=True)
        return 0
    except (OSError,ValueError,KeyError,X2Error) as exc:
        print("R4 REFLECTION FAILED: "+str(exc),file=sys.stderr)
        return 1


if __name__=="__main__":
    raise SystemExit(main())
