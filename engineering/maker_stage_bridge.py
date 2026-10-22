#!/usr/bin/env python3
"""Read-only Maker verifier/normalizer; never runs --run or issues acceptance."""
import argparse
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import subprocess
import sys

try:
    import stage_relay as relay
except ModuleNotFoundError:
    mod = importlib.util.spec_from_file_location("stage_relay", Path(__file__).with_name("stage_relay.py"))
    relay = importlib.util.module_from_spec(mod)
    mod.loader.exec_module(relay)

SCENARIOS = (("baseline", "cells", "cells"), ("delay2", "delay2", "delay2_cells"),
             ("cost1_5x", "cost1_5x", "cost1_5x_cells"))
SAMPLERS = {"ETHUSDT:1", "ETHUSDT:15", "BTCUSDT:1", "BTCUSDT:15"}

def demand(value, reason):
    if not value:
        raise relay.RelayError("maker bridge: " + reason)

def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1048576), b""):
            h.update(part)
    return h.hexdigest()

def read_object(path):
    obj = relay.raw_json(path)
    demand(isinstance(obj, dict), "expected JSON object: " + str(path))
    return obj

def fields(graph):
    spec = graph.get("maker_bridge")
    demand(isinstance(spec, dict), "missing maker_bridge configuration")
    stage = next((s for s in graph["stages"] if s["id"] == spec.get("historical_stage")), None)
    demand(stage is not None and stage.get("mode") == "adopt", "historical stage must be adopted")
    relay.stage_inputs(stage)
    producer = spec.get("producer")
    demand(isinstance(producer, dict) and all(isinstance(producer.get(k), str) and
           producer[k] for k in ("item", "job", "instruction")),
           "missing actual producer item/job/instruction")
    hashes = spec.get("symbol_hashes")
    demand(isinstance(hashes, dict) and set(hashes) == {"BTCUSDT", "ETHUSDT"}, "missing BTC/ETH input hashes")
    for item in hashes.values():
        demand(isinstance(item, dict) and set(item) == {"bars_sha256", "funding_sha256"} and
               all(isinstance(v, str) and relay.HEX64.fullmatch(v) for v in item.values()),
               "invalid bars/funding hashes")
    source = spec.get("source_sha256")
    demand(isinstance(source, dict) and source and all(isinstance(k, str) and k and
           isinstance(v, str) and relay.HEX64.fullmatch(v) for k, v in source.items()),
           "missing/invalid Maker source SHA256 dictionary")
    for key in ("config_sha256", "input_manifest_sha256"):
        demand(isinstance(spec.get(key), str) and relay.HEX64.fullmatch(spec[key]), "missing " + key)
    reports = stage.get("reports")
    demand(isinstance(reports, list) and len(reports) == 3, "three scenario report contracts required")
    for (name, _, _), r in zip(SCENARIOS, reports):
        demand(isinstance(r, dict) and r.get("rows") == 48 and
               r.get("symbol_cells") == {"BTCUSDT": 24, "ETHUSDT": 24} and
               isinstance(r.get("path"), str) and name in r["path"],
               "scenario 48-row BTC/ETH contract mismatch: " + name)
    return stage, spec

def source_gate(graph, gh):
    stage, spec = fields(graph)
    path = relay.confined(graph["evidence_root"], spec.get("capacity_acceptance_path"))
    waiting = {"action": "WAITING_CAPACITY_ACCEPTANCE", "stage": stage["id"],
               "producer": spec["producer"], "source_head": stage["source_head"],
               "input_sha256": stage["input_sha256"], "graph_sha256": gh,
               "acceptance_path": str(path), "reason": "actual corrected Maker source not independently accepted"}
    if not path.is_file() or path.is_symlink():
        return waiting
    try:
        proof = read_object(path)
        expected = {"stage": stage["id"], "source_head": stage["source_head"],
                    "input_sha256": stage["input_sha256"], "graph_sha256": gh,
                    "input_manifest_sha256": spec["input_manifest_sha256"],
                    "config_sha256": spec["config_sha256"],
                    "source_sha256": spec["source_sha256"],
                    "symbol_hashes": spec["symbol_hashes"]}
        demand(all(proof.get(k) == value for k, value in expected.items()),
               "capacity approval source/input/config identity mismatch")
        demand(proof.get("decision") == "ACCEPTED" and isinstance(proof.get("reviewer"), str)
               and bool(proof["reviewer"]), "independent capacity acceptance missing")
        return {"action": "CAPACITY_SOURCE_ACCEPTED", "stage": stage["id"],
                "producer": spec["producer"], "acceptance_sha256": file_hash(path)}
    except (relay.RelayError, ValueError, OSError, TypeError, KeyError) as exc:
        return dict(waiting, reason=str(exc))

def preflight(graph, gh, state):
    stage, spec = fields(graph)
    current = relay.stage_of(graph, state)
    if not current or current["id"] != stage["id"]:
        return {"action": "OTHER_STAGE"}
    gate = source_gate(graph, gh)
    if gate["action"] == "CAPACITY_SOURCE_ACCEPTED":
        return gate
    receipt = relay.confined(graph["evidence_root"], "inbox/" + stage["id"] + ".receipt.json")
    if receipt.is_file() and not receipt.is_symlink():
        obj = read_object(receipt)
        if obj.get("stage") == stage["id"] and obj.get("outcome") in ("FAILED", "UNKNOWN", "REFUSED"):
            return {"action": "ALLOW_TERMINAL_FAILURE", "producer": spec["producer"]}
    return gate

def reflection_notice(graph, state):
    spec = graph["maker_bridge"]
    current = relay.stage_of(graph, state)
    if not current or current["id"] != spec.get("reflection_stage"):
        return None
    completed = next((c for c in state["completed"] if c["stage"] == spec["historical_stage"]), None)
    if not completed or current.get("mode") != "adopt":
        return {"action": "BLOCKED", "reason": "historical acceptance and external reflection owner required"}
    return {"action": "READY_OPERATOR_REFLECTION", "producer": spec["producer"],
            "historical_source_head": completed["source_head"],
            "historical_input_sha256": completed["input_sha256"],
            "historical_report_hashes": completed["report_hashes"],
            "historical_output_count": completed["output_count"], "graph_sha256": completed["graph_sha256"],
            "reason": "operator/model must inspect actual reports; no automatic trading conclusion"}

def verify_argv(spec):
    run, verify = spec.get("run_argv"), spec.get("verify_argv")
    relay.validate_argv(run)
    relay.validate_argv(verify)
    for name in ("config_path", "input_path", "output_dir", "cwd"):
        demand(isinstance(spec.get(name), str) and Path(spec[name]).is_absolute(),
               "absolute local " + name + " required")
    cwd = Path(spec["cwd"])
    demand(cwd.is_dir() and not cwd.is_symlink(), "trusted absolute Maker cwd missing/symlink")
    def python_child(argv, mode, is_run):
        # Only Python's literal -u is permitted; no wrappers, -m, -c or shell.
        prefix = 3 if len(argv) == 10 and argv[1] == "-u" else 2
        demand(len(argv) == prefix + 7 and argv[prefix:] ==
               [mode, "--config", spec["config_path"], "--input", spec["input_path"],
                "--output", spec["output_dir"]],
               "Maker CLI has unsupported flag or incorrect mode/config/input/output")
        interpreter, script = argv[0], argv[prefix - 1]
        if is_run and interpreter == "python3":
            captured = spec.get("child_executable")
            demand(isinstance(captured, str) and Path(captured).is_absolute() and
                   Path(captured).is_file(),
                   "relative python3 requires captured absolute child_executable")
            interpreter = captured
        demand(Path(interpreter).is_absolute() and Path(interpreter).is_file() and
               Path(script).name == "backtest_entry.py",
               "absolute/captured Maker Python child and backtest_entry.py required")
        if not Path(script).is_absolute():
            demand(is_run and script == "backtest_entry.py",
                   "only exact relative backtest_entry.py from trusted cwd is permitted")
            script = str(cwd / script)
        return Path(interpreter).resolve(), Path(script).resolve()
    child = python_child(run, "--run", True)
    verifier = python_child(verify, "--verify", False)
    demand(child == verifier, "Maker run/verify interpreter or source entrypoint mismatch")
    demand(child[1].is_file(), "Maker source backtest_entry.py missing")
    demand(file_hash(spec["config_path"]) == spec["config_sha256"] and
           file_hash(spec["input_path"]) == spec["input_manifest_sha256"],
           "actual Maker CLI config/input checksum mismatch")
    return verify


def all_cells():
    return {f"{symbol}:{frame}m:{k}:{ttl}:{exit_name}" for symbol, frame, k, ttl, exit_name in
            itertools.product(("BTCUSDT", "ETHUSDT"), (1, 15),
                              (".25", ".50"), (2, 5), ("X1", "X2", "X3"))}

def verified_rows(spec):
    verify = verify_argv(spec)
    out = Path(spec["output_dir"])
    demand(out.is_dir() and not out.is_symlink(), "missing Maker result directory")
    manifest, results = read_object(out / "manifest.json"), read_object(out / "results.json")
    demand(manifest.get("research_status") == "EXPLORATORY_UNACCEPTED" and
           results.get("classification") == "EXPLORATORY_NOT_ACCEPTED",
           "Maker research status not exploratory")
    for key in ("input_manifest_sha256", "config_sha256"):
        demand(manifest.get(key) == spec[key], "Maker " + key + " mismatch")
    demand(manifest.get("source_sha256") == spec["source_sha256"], "Maker business source mismatch")
    inputs = manifest.get("input")
    demand(isinstance(inputs, dict) and set(inputs) == {"BTCUSDT", "ETHUSDT"}, "per-symbol inputs missing")
    for symbol, item in inputs.items():
        demand(isinstance(item, dict) and
               {k: item.get(k) for k in ("bars_sha256", "funding_sha256")} == spec["symbol_hashes"][symbol],
               "bar/funding SHA mismatch for " + symbol)
    inv = manifest.get("artifacts")
    required = {"results.json"} | {f"{directory}/{i:02d}_{side}.json"
                for _, directory, _ in SCENARIOS for i in range(48)
                for side in ("strategy", "matched")}
    demand(isinstance(inv, dict) and set(inv) == required and len(inv) == 289,
           "incomplete 288 raw account + results manifest")
    for relative, expected in inv.items():
        path = (out / relative).resolve()
        demand(isinstance(expected, str) and relay.HEX64.fullmatch(expected) and
               out.resolve() in path.parents and path.is_file() and
               file_hash(path) == expected, "Maker artifact mismatch " + relative)
    demand(all(results.get(key) == 48 for key in
               ("cell_count", "delay2_count", "cost1_5x_count")) and
           isinstance(results.get("samplers"), dict) and set(results["samplers"]) ==
           {"ETHUSDT:1", "ETHUSDT:15", "BTCUSDT:1", "BTCUSDT:15"}, "incomplete scenario/sampler")
    rows = []
    for scenario, directory, field in SCENARIOS:
        cells = results.get(field)
        demand(isinstance(cells, list) and len(cells) == 48, "missing scenario " + scenario)
        ids = [c.get("cell_id") for c in cells if isinstance(c, dict)]
        demand(len(ids) == 48 and set(ids) == all_cells(), "duplicate/missing/wrong scenario cell ID " + scenario)
        normalized = []
        for index, cell in enumerate(cells):
            symbol, frame, k, ttl, exit_name = cell["cell_id"].split(":")
            hashes = []
            for side in ("strategy", "matched"):
                relative = f"{directory}/{index:02d}_{side}.json"
                raw = relay.raw_json(out / relative)
                if side == "matched" and raw is None:
                    demand(cell.get("matched_status") != "READY", "READY matched account is null")
                else:
                    demand(isinstance(raw, dict) and all(raw.get(a) == b for a, b in
                           (("symbol", symbol), ("framework", int(frame[:-1])), ("k", k),
                            ("ttl", int(ttl)), ("exit", exit_name), ("matched", side == "matched"))),
                           "raw account identity mismatch " + relative)
                hashes.append(inv[relative])
            demand(isinstance(cell.get("metrics"), dict) and
                   isinstance(cell.get("matched_status"), str), "missing metrics/matched status")
            normalized.append({"symbol": symbol, "cell_id": cell["cell_id"],
                               "metrics": cell["metrics"], "matched_status": cell["matched_status"],
                               "strategy_sha256": hashes[0], "matched_sha256": hashes[1]})
        rows.append(normalized)
    proc = subprocess.run(verify, cwd=spec["cwd"], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=300, check=False)
    demand(proc.returncode == 0, "actual Maker --verify rc " + str(proc.returncode))
    try:
        response = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise relay.RelayError("Maker --verify stdout invalid") from exc
    demand(response.get("status") == "CHECKSUMS_VERIFIED" and
           response.get("artifact_count") == 289 and
           response.get("manifest_sha256") == file_hash(out / "manifest.json"),
           "Maker --verify exact manifest/count mismatch")
    return rows, manifest, results

def normalize(config_path, state_path, terminal_rel):
    graph, gh = relay.configuration(config_path)
    stage, spec = fields(graph)
    admission = source_gate(graph, gh)
    demand(admission["action"] == "CAPACITY_SOURCE_ACCEPTED",
           admission.get("reason", "capacity source not accepted"))
    root = graph["evidence_root"]
    terminal_file = relay.confined(root, terminal_rel)
    demand(not terminal_file.is_symlink(), "trusted terminal symlink")
    terminal = read_object(terminal_file)
    with relay.state_lock(state_path):
        state = relay.load_state(state_path, gh)
        current, owner = relay.stage_of(graph, state), state.get("owner")
        demand(current is not None and current["id"] == stage["id"] and
               state["status"] in ("RUNNING", "WAITING_RECEIPT", "WAITING_ACCEPTANCE") and
               isinstance(owner, dict) and owner.get("role") == "external" and
               relay.identity_status(owner) == "DEAD",
               "missing exact stopped adopted historical process")
        for key, value in (("stage", stage["id"]), ("source_head", stage["source_head"]),
                           ("input_sha256", stage["input_sha256"]), ("graph_sha256", gh)):
            demand(terminal.get(key) == value, "trusted terminal " + key + " mismatch")
        demand(isinstance(terminal.get("owner"), dict) and
               all(terminal["owner"].get(k) == owner.get(k) for k in
                   ("pid", "start_ticks", "boot_id", "token")) and
               terminal.get("outcome") == "SUCCEEDED" and terminal.get("exit_code") == 0 and
               terminal.get("trusted_parent") is True,
               "independently recorded trusted exit0/owner required")
        receipt_rel = "inbox/" + stage["id"] + ".receipt.json"
        receipt_path = relay.confined(root, receipt_rel)
        reports = [relay.confined(root, item["path"]) for item in stage["reports"]]
        demand(not receipt_path.exists() and not any(p.exists() for p in reports),
               "already normalized; never overwrite")
        rows, manifest, results = verified_rows(spec)
        hashes = []
        for (scenario, _, _), contract, cells, path in zip(SCENARIOS, stage["reports"], rows, reports):
            relay.atomic_json(path, {"stage": stage["id"], "source_head": stage["source_head"],
                          "input_sha256": stage["input_sha256"], "graph_sha256": gh,
                          "scenario": scenario, "rows": cells,
                          "source_sha256": spec["source_sha256"],
                          "input_manifest_sha256": spec["input_manifest_sha256"],
                          "config_sha256": spec["config_sha256"],
                          "symbol_hashes": spec["symbol_hashes"],
                          "maker_manifest_sha256": file_hash(Path(spec["output_dir"]) / "manifest.json"),
                          "research_status": "EXPLORATORY_UNACCEPTED",
                          "classification": results["classification"]})
            hashes.append({"path": contract["path"], "rows": 48, "sha256": file_hash(path)})
        relay.atomic_json(receipt_path, {"stage": stage["id"], "source_head": stage["source_head"],
                          "input_sha256": stage["input_sha256"], "graph_sha256": gh,
                          "owner": {k: owner[k] for k in ("pid", "start_ticks", "boot_id", "token")},
                          "outcome": "SUCCEEDED", "exit_code": 0, "output_count": 144,
                          "reason": "trusted parent exit0 + Maker --verify; NOT accepted",
                          "reports": hashes, "terminal_sha256": file_hash(terminal_file)})
        return {"action": "WAITING_INDEPENDENT_RESULT_ACCEPTANCE", "reports": hashes,
                "receipt_path": str(receipt_path), "receipt_sha256": file_hash(receipt_path),
                "independent_acceptance_created": False}

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capacity", "normalize"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--state")
    parser.add_argument("--terminal", help="trusted parent terminal file relative to evidence root")
    args = parser.parse_args(argv)
    try:
        graph, gh = relay.configuration(args.config)
        if args.action == "capacity":
            answer = source_gate(graph, gh)
        else:
            demand(bool(args.state and args.terminal), "normalize needs --state and --terminal")
            answer = normalize(args.config, args.state, args.terminal)
        print(json.dumps(answer, sort_keys=True))
        return 2 if answer["action"] in ("WAITING_CAPACITY_ACCEPTANCE", "BLOCKED") else 0
    except (relay.RelayError, OSError, ValueError, KeyError, TypeError,
            subprocess.TimeoutExpired) as exc:
        print(json.dumps({"action": "BLOCKED", "reason": str(exc)}))
        return 2

if __name__ == "__main__":
    sys.exit(main())
