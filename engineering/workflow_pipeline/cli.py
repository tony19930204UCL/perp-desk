#!/usr/bin/env python3
"""Read-only operator CLI for an exported ledger plus isolated projection state."""
import argparse, json
from pathlib import Path
from .pipeline import normalize_item

def load(path):
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise SystemExit("invalid ledger: expected object with items")
    return data

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    sub=ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("one-tick")
    for name in ("start","stop","install","rollback"):
        p=sub.add_parser(name); p.add_argument("--plan", action="store_true", required=True)
    a=ap.parse_args(argv)
    if a.cmd in ("start","stop","install","rollback"):
        print(json.dumps({"action":a.cmd,"mode":"PLAN_ONLY","performed":False}, sort_keys=True)); return 0
    data=load(a.ledger)
    projected=[normalize_item(x) for x in data["items"]]
    print(json.dumps({"mode":"READ_ONLY","items":projected}, sort_keys=True))
    return 0
if __name__=="__main__":
    raise SystemExit(main())
