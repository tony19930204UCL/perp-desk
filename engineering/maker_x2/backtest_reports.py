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
    """Hash actual production source, executable tests and public contract."""
    root=Path(directory)
    required=("account_lifecycle.py","account_ledger.py","liquidation.py",
              "entry_lifecycle.py","exit_lifecycle.py","maker_exit_lab.py",
              "accounting.py","signal_features.py","matched_signals.py",
              "data_adapter.py","market_replay.py","matched_replay.py",
              "grid_runner.py","backtest_stats.py","backtest_reports.py",
              "backtest_entry.py","backtest_controls.py","scenario_rates.py",
              "bounded_history.py","README.md")
    paths=[root/name for name in required]
    paths.extend(sorted((root/"tests").glob("test_*.py")))
    if any(not path.is_file() for path in paths):
        raise X2Error("source/test manifest missing a required file")
    return {path.relative_to(root).as_posix():sha256_file(path)
            for path in paths}


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
    stressed=result.get("fee_slip_1_5x_cells",[])
    for index,cell in enumerate(stressed):
        for name,data in (("strategy",cell["strategy"]),
                          ("matched",cell["matched"])):
            relative=f"cost1_5x/{index:02d}_{name}.json"
            inventory[relative]=atomic_json(root/relative,data)
    inventory["results.json"]=atomic_json(
        root/"results.json",
        {"cell_count":result["cell_count"],
         "cells":[{"cell_id":c["cell_id"],"metrics":c["metrics"],
                   "matched_status":c["matched_status"]}
                  for c in cells],
         "delay2_count":len(delayed),
         "cost1_5x_count":len(stressed),
         "cost1_5x_cells":[{"cell_id":v["cell_id"],"metrics":v["metrics"]}
                             for v in stressed],
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
    with open(root/"results.json", encoding="utf-8") as handle:
        results = json.load(handle)
    if "samplers" in results:
        if (results.get("cell_count") != 48 or
            results.get("delay2_count") != 48 or
            results.get("cost1_5x_count") != 48):
            raise X2Error("incomplete scenario cell counts")
        for field in ("cells", "delay2_cells", "cost1_5x_cells"):
            ids = [row["cell_id"] for row in results[field]]
            if len(ids) != 48 or len(set(ids)) != 48:
                raise X2Error("duplicate or missing cell identities")
        scenarios = ("cells", "delay2", "cost1_5x")
        for name in scenarios:
            for index in range(48):
                for side in ("strategy", "matched"):
                    artifact = f"{name}/{index:02d}_{side}.json"
                    if artifact not in manifest["artifacts"]:
                        raise X2Error("missing raw account " + artifact)
        if set(results["samplers"]) != {
            "ETHUSDT:1", "ETHUSDT:15", "BTCUSDT:1", "BTCUSDT:15"}:
            raise X2Error("missing framework sampler identity")
    return {"status":"CHECKSUMS_VERIFIED","artifact_count":
            len(manifest["artifacts"]),"manifest_sha256":
            sha256_file(root/"manifest.json")}


class BoundedReport:
    """Write each complete strategy/placebo cell before the next one begins."""
    def __init__(self, out):
        self.root = Path(out)
        if self.root.exists():
            raise X2Error("result root exists; never overwrite")
        self.root.mkdir(parents=True)
        self.inventory = {}
        self.rows = {"baseline": [], "delay2": [], "cost1_5x": []}
        self.scenarios = {"baseline": "cells", "delay2": "delay2",
                          "cost1_5x": "cost1_5x"}

    def record(self, scenario, meta, strategy, matched):
        rows = self.rows[scenario]
        index = len(rows)
        if index >= 48:
            raise X2Error("scenario has more than 48 cells")
        for part, content in (("strategy", strategy), ("matched", matched)):
            name = f"{self.scenarios[scenario]}/{index:02d}_{part}.json"
            self.inventory[name] = atomic_json(self.root / name, content)
        row = dict(meta)
        row["strategy"] = {"episodes": strategy["episodes"],
                           "ledger": strategy["ledger"],
                           "counts": strategy["counts"]}
        rows.append(row)

    def finish(self, manifest, samplers, config, start, end):
        from grid_runner import finalize_bounded
        for scenario, cells in self.rows.items():
            if len(cells) != 48:
                raise X2Error("incomplete " + scenario + " 48-cell scenario")
            finalize_bounded(cells, config, start, end)
        compact = lambda cells: [
            {"cell_id": c["cell_id"], "metrics": c["metrics"],
             "matched_status": c["matched_status"]} for c in cells]
        self.inventory["results.json"] = atomic_json(
            self.root / "results.json",
            {"cell_count": 48, "cells": compact(self.rows["baseline"]),
             "delay2_count": 48, "delay2_cells": compact(self.rows["delay2"]),
             "cost1_5x_count": 48,
             "cost1_5x_cells": compact(self.rows["cost1_5x"]),
             "fee_slip_1_5x_status": "SOURCE_DELIVERED_NOT_VERIFIED",
             "samplers": samplers,
             "classification": "EXPLORATORY_NOT_ACCEPTED"})
        document = dict(manifest)
        document["artifacts"] = self.inventory
        digest = atomic_json(self.root / "manifest.json", document)
        verify = verify_run(self.root)
        if verify["manifest_sha256"] != digest:
            raise X2Error("bounded output verification failed")
        return {"manifest_sha256": digest, "artifacts": self.inventory}
