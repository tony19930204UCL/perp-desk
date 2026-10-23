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
    accepted=[]
    for row in rows:
        if row["mode"]!="B" or row["scenario"]!="baseline":
            continue
        metric=row["metrics"]
        accepted.append({"cell_id":row["cell_id"],"symbol":row["symbol"],
                         "filter":row["filter"],"net_bp":metric["net_bp"],
                         "gross_bp":metric["gross_bp"],
                         "alpha_bp":metric["net_alpha_bp"],
                         "liq":metric["liq"],
                         "open":metric["open"],
                         "n":metric["n"]})
    if len(accepted)!=24:
        raise X2Error("missing B baseline comparison rows")
    decision={"status":"EXPLORATORY_NO_HOLDOUT_NOT_DEPLOYABLE",
              "candidates":[],"prior_b_baseline":accepted,
              "h1":"Assess post-2024-05 gross bp by causal regime; zero count is insufficient",
              "h2":"Compare F1/F0 net, LIQ, retained signal exposure; do not presume LIQ falls",
              "limitations":["Historical exploration contaminated; no new holdout",
                             "Six earlier surviving mutants are unresolved",
                             "OHLC fills do not model true maker queue or mark LIQ",
                             "B independent resets are not an investable curve",
                             "Data gaps, incomplete and open tail must remain visible"],
              "next_data":"Point-in-time independent holdout and queue/mark microstructure"}
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
