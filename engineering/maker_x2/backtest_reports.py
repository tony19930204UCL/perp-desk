"""Durable JSON reports: atomic write and exact input/source manifests."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from decimal import Decimal
from maker_exit_lab import X2Error


def canon(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, set):
        return sorted(canon(v) for v in obj)
    if isinstance(obj, list):
        return [canon(v) for v in obj]
    if isinstance(obj, dict):
        return {k:canon(v) for k,v in obj.items()}
    return obj


def sha256_file(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()


def source_manifest(directory):
    names=("account_lifecycle.py","account_ledger.py","liquidation.py",
           "entry_lifecycle.py","exit_lifecycle.py","maker_exit_lab.py",
           "accounting.py","signal_features.py","matched_signals.py",
           "data_adapter.py","market_replay.py","matched_replay.py",
           "grid_runner.py","backtest_stats.py","backtest_reports.py",
           "backtest_entry.py","backtest_controls.py")
    root=Path(directory)
    return {name:sha256_file(root/name) for name in names}


def atomic_json(path, value):
    target=Path(path)
    target.parent.mkdir(parents=True,exist_ok=True)
    with NamedTemporaryFile("w",dir=target.parent,delete=False,
                            encoding="utf-8") as temp:
        tmp=temp.name
        json.dump(canon(value),temp,sort_keys=True,indent=2,allow_nan=False)
        temp.write("\n")
    os.replace(tmp,target)
    return sha256_file(target)


def persist_run(out, manifest, result):
    root=Path(out)
    if root.exists() and any(root.iterdir()):
        raise X2Error("output directory must be empty (no overwrite)")
    root.mkdir(parents=True,exist_ok=True)
    cells=result["cells"]
    inventory={}
    for index,cell in enumerate(cells):
        label=f"{index:02d}"
        for name,data in (("strategy",cell["strategy"]),
                          ("matched",cell["matched"])):
            relative=f"cells/{label}_{name}.json"
            inventory[relative]=atomic_json(root/relative,data)
    delayed=result.get("delay2_cells",[])
    for index,cell in enumerate(delayed):
        label=f"{index:02d}"
        for name,data in (("strategy",cell["strategy"]),
                          ("matched",cell["matched"])):
            relative=f"delay2/{label}_{name}.json"
            inventory[relative]=atomic_json(root/relative,data)
    inventory["results.json"]=atomic_json(
        root/"results.json",
        {"cell_count":result["cell_count"],
         "cells":[{"cell_id":c["cell_id"],"metrics":c["metrics"],
                   "matched_status":c["matched_status"]}
                  for c in cells],
         "delay2_count":len(delayed),
         "delay2_cells":[{"cell_id":c["cell_id"],"metrics":c["metrics"]}
                         for c in delayed],
         "fee_slip_1_5x_status":result.get(
             "fee_slip_1_5x_status","NOT_IMPLEMENTED_NEEDS_AUTHORIZED_GLUE"),
         "samplers":result["samplers"],
         "classification":result["classification"]})
    manifest=dict(manifest)
    manifest["artifacts"]=inventory
    digest=atomic_json(root/"manifest.json",manifest)
    return {"manifest_sha256":digest,"artifacts":inventory}


def verify_run(directory):
    root=Path(directory)
    with open(root/"manifest.json","r",encoding="utf-8") as f:
        manifest=json.load(f)
    for name,expected in manifest["artifacts"].items():
        if sha256_file(root/name)!=expected:
            raise X2Error("output artifact checksum mismatch: "+name)
    if not (root/"results.json").exists():
        raise X2Error("missing results")
    return {"status":"CHECKSUMS_VERIFIED","artifact_count":
            len(manifest["artifacts"]),"manifest_sha256":
            sha256_file(root/"manifest.json")}
