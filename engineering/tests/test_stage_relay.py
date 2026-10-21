"""Finite offline stage-relay regressions; stdlib, isolated temporary paths."""
import concurrent.futures
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

MODULE = Path(__file__).resolve().parents[1] / "stage_relay.py"
spec = importlib.util.spec_from_file_location("stage_relay", MODULE)
relay = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = relay
spec.loader.exec_module(relay)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.state = self.root / "state.json"
        self.config = self.root / "graph.json"
        self.head = "a" * 40
        self.inp = "b" * 64
        prior = self.evidence / "upstream.json"
        relay.atomic_json(prior, {"synthetic": True})
        relay.atomic_json(self.evidence / "upstream-acceptance.json",
                          {"stage_id": "upstream", "source_head": self.head,
                           "input_sha256": self.inp, "decision": "ACCEPTED",
                           "reviewer": "test-fixture", "evidence_path": "upstream.json",
                           "evidence_sha256": relay.digest(prior.read_bytes())})

    def setup_graph(self, stages, *, kind="synthetic_stage_relay"):
        graph = {"schema_version": 1, "kind": kind,
                 "evidence_root": str(self.evidence),
                 "bootstrap": {"acceptance_path": "upstream-acceptance.json",
                               "stage_id": "upstream", "source_head": self.head,
                               "input_sha256": self.inp}, "stages": stages}
        relay.atomic_json(self.config, graph)
        relay.init(self.config, self.state)
        return graph

    def stage(self, name, mode="exec", rows=1):
        stage = {"id": name, "mode": mode, "source_head": self.head,
                 "input_sha256": self.inp, "reports": [{"path": name + ".json", "rows": rows}],
                 "lease_seconds": 60}
        if mode == "exec":
            stage["command"] = [sys.executable, str(MODULE), "_demo_task",
                                "--config", str(self.config), "--stage", name, "--rows", str(rows)]
        return stage

    def await_completion(self):
        limit = time.monotonic() + 8
        while time.monotonic() < limit:
            state = relay.raw_json(self.state)
            if state["status"] != "RUNNING":
                return state
            time.sleep(0.02)
        self.fail("subprocess did not finish")

    def receipt(self, state, valid=True, outcome="SUCCEEDED", exit_code=0):
        graph, gh = relay.configuration(self.config)
        stage = graph["stages"][state["stage_index"]]
        report_path = self.evidence / stage["reports"][0]["path"]
        owner = state["owner"]
        report_digest = relay.digest(report_path.read_bytes()) if valid else "0" * 64
        value = {"stage": stage["id"], "source_head": self.head,
                 "input_sha256": self.inp, "graph_sha256": gh,
                 "owner": {k: owner[k] for k in ("pid", "start_ticks", "boot_id", "token")},
                 "exit_code": exit_code, "outcome": outcome, "reason": "test",
                 "output_count": stage["reports"][0]["rows"],
                 "reports": [{"path": stage["reports"][0]["path"],
                              "sha256": report_digest, "rows": stage["reports"][0]["rows"]}]}
        path = self.evidence / ("receipt-" + stage["id"] + ".json")
        relay.atomic_json(path, value)
        acceptance = self.evidence / ("acceptance-" + stage["id"] + ".json")
        relay.atomic_json(acceptance, {"stage": stage["id"], "source_head": self.head,
                                      "input_sha256": self.inp, "graph_sha256": gh,
                                      "receipt_sha256": relay.digest(path.read_bytes()),
                                      "decision": "ACCEPTED", "reviewer": "test-only"})
        return path.name, acceptance.name

    def test_exit_zero_missing_report_no_advance(self):
        self.setup_graph([self.stage("first"), self.stage("second")])
        relay.tick(self.config, self.state)
        state = self.await_completion()
        (self.evidence / "first.json").unlink()
        receipt, acceptance = self.receipt_without_file(state)
        with self.assertRaises(relay.RelayError):
            relay.complete(self.config, self.state, receipt, acceptance)
        self.assertEqual(relay.raw_json(self.state)["stage_index"], 0)

    def receipt_without_file(self, state):
        relay.atomic_json(self.evidence / "first.json", {"temporary": True})
        pair = self.receipt(state)
        (self.evidence / "first.json").unlink()
        return pair

    def test_acceptance_starts_next_without_message_and_deduplicates(self):
        self.setup_graph([self.stage("first"), self.stage("second")])
        relay.tick(self.config, self.state)
        state = self.await_completion()
        receipt, acceptance = self.receipt(state)
        answer = relay.complete(self.config, self.state, receipt, acceptance)
        self.assertEqual(answer["next"]["action"], "WORKER_STARTED")
        duplicate = relay.complete(self.config, self.state, receipt, acceptance)
        self.assertEqual(duplicate["action"], "DUPLICATE_IGNORED")
        self.assertEqual(relay.raw_json(self.state)["attempts"]["second"], 1)
        self.await_completion()

    def test_failure_cannot_fake_pass(self):
        bad = self.stage("bad")
        bad["command"] = [sys.executable, "-c", "raise SystemExit(3)"]
        self.setup_graph([bad, self.stage("next")])
        relay.tick(self.config, self.state)
        state = self.await_completion()
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["stage_index"], 0)
        self.assertEqual(relay.tick(self.config, self.state)["action"], "FAILED")
        self.assertEqual(state["attempts"]["bad"], 1)

    def test_restart_persists_source_and_stage(self):
        self.setup_graph([self.stage("first", mode="adopt")])
        before = relay.tick(self.config, self.state)
        self.assertEqual(before["action"], "READY_MISSING_OWNER")
        after = relay.status(self.config, self.state)
        self.assertEqual(after["status"], "WAITING_ADOPTION")
        self.assertEqual(after["source_head"], self.head)

    def test_concurrent_parent_cron_exactly_one_owner(self):
        self.setup_graph([self.stage("first")])
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: relay.tick(self.config, self.state), range(2)))
        self.assertEqual(sum(r["action"] == "WORKER_STARTED" for r in responses), 1)
        self.assertEqual(relay.raw_json(self.state)["attempts"]["first"], 1)
        self.await_completion()

    def test_unknown_and_refusal_do_not_resend(self):
        self.setup_graph([self.stage("first"), self.stage("second")])
        relay.tick(self.config, self.state)
        state = self.await_completion()
        pair = self.receipt(state, outcome="UNKNOWN")
        result = relay.complete(self.config, self.state, *pair)
        self.assertEqual(result["action"], "BLOCKED")
        self.assertEqual(relay.tick(self.config, self.state)["action"], "BLOCKED")
        self.assertNotIn("second", relay.raw_json(self.state)["attempts"])

    def test_final_report_source_bound_and_truthful(self):
        self.setup_graph([self.stage("first", rows=2)])
        relay.tick(self.config, self.state)
        state = self.await_completion()
        pair = self.receipt(state)
        report_path = self.evidence / "first.json"
        report = relay.raw_json(report_path)
        report["source_head"] = "f" * 40
        relay.atomic_json(report_path, report)
        with self.assertRaises(relay.RelayError):
            relay.complete(self.config, self.state, *pair)
        self.assertEqual(relay.raw_json(self.state)["stage_index"], 0)
        report["source_head"] = self.head
        relay.atomic_json(report_path, report)
        self.assertEqual(relay.complete(self.config, self.state, *pair)["action"], "ACCEPTED")
        self.assertEqual(relay.raw_json(self.state)["completed"][0]["output_count"], 2)

    def test_real_subprocess_multistage_demo(self):
        proc = subprocess.run([sys.executable, str(MODULE), "demo", "--dir", str(self.root / "drill")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        output = json.loads(proc.stdout)
        self.assertTrue(output["not_maker_product_evidence"])
        self.assertEqual([x["output_count"] for x in output["completed"]], [1, 2, 48, 1])
        self.assertEqual(len({x["pid"] for x in output["completed"]}) >= 1, True)
        persisted = relay.raw_json(output["state"])
        self.assertEqual(persisted["status"], "DONE")
        self.assertTrue(any(x["action"] == "PRODUCT_STARTED" for x in persisted["history"]))

    def test_no_executor_blocks_precisely(self):
        missing = self.stage("first")
        missing.pop("command")
        self.setup_graph([missing])
        response = relay.tick(self.config, self.state)
        self.assertEqual(response["action"], "BLOCKED")
        self.assertIn("missing configured", response["reason"])


    def installed(self, configured=True, profile_name="my-active-profile"):
        """Copy actual source into a temporary installed-profile scripts path."""
        import shutil
        profile = self.root / profile_name
        scripts = profile / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        target = scripts / "stage_relay.py"
        shutil.copyfile(MODULE, target)
        namespace = profile / "stage-relay" / "C0041"
        namespace.mkdir(parents=True, exist_ok=True)
        self.evidence = namespace / "evidence"
        self.evidence.mkdir(exist_ok=True)
        self.config = namespace / "graph.json"
        self.state = namespace / "state.json"
        prior = self.evidence / "upstream.json"
        relay.atomic_json(prior, {"synthetic": True})
        relay.atomic_json(self.evidence / "upstream-acceptance.json",
                          {"stage_id": "upstream", "source_head": self.head,
                           "input_sha256": self.inp, "decision": "ACCEPTED",
                           "reviewer": "test-fixture", "evidence_path": "upstream.json",
                           "evidence_sha256": relay.digest(prior.read_bytes())})
        if configured:
            self.setup_graph([self.stage("first"), self.stage("second")],
                             kind="stage_relay")
        return target

    def noarg(self, installed):
        run = subprocess.run([sys.executable, str(installed)], capture_output=True,
                             text=True, timeout=15)
        return run.returncode, json.loads(run.stdout)

    def inbox(self, state, outcome="SUCCEEDED"):
        stage = relay.raw_json(self.config)["stages"][state["stage_index"]]
        report_file = self.evidence / stage["reports"][0]["path"]
        owner = state["owner"]
        gh = relay.configuration(self.config)[1]
        receipt = {"stage": stage["id"], "source_head": self.head,
                   "input_sha256": self.inp, "graph_sha256": gh,
                   "owner": {key: owner[key] for key in ("pid", "start_ticks", "boot_id", "token")},
                   "exit_code": 0, "outcome": outcome, "reason": "synthetic-test",
                   "output_count": stage["reports"][0]["rows"],
                   "reports": [{"path": stage["reports"][0]["path"],
                                "rows": stage["reports"][0]["rows"],
                                "sha256": relay.digest(report_file.read_bytes())}]}
        path = self.evidence / "inbox" / (stage["id"] + ".receipt.json")
        relay.atomic_json(path, receipt)
        relay.atomic_json(self.evidence / "inbox" / (stage["id"] + ".acceptance.json"),
                          {"stage": stage["id"], "source_head": self.head,
                           "input_sha256": self.inp, "graph_sha256": gh,
                           "receipt_sha256": relay.digest(path.read_bytes()),
                           "reviewer": "test-fixture", "decision": "ACCEPTED"})
        return path

    def test_installed_noarg_missing_config_repeat_wake(self):
        target = self.installed(configured=False)
        for _ in range(2):
            code, out = self.noarg(target)
            self.assertEqual(code, 2)
            self.assertEqual(out["action"], "NOT_CONFIGURED")
            self.assertTrue(out["needs_model_decision"])

    def test_installed_noarg_acceptance_spawns_next_once(self):
        target = self.installed()
        code, first = self.noarg(target)
        self.assertEqual(code, 0)
        self.assertEqual(first["tick"]["action"], "WORKER_STARTED")
        state = self.await_completion()
        self.inbox(state)
        code, accepted = self.noarg(target)
        self.assertEqual(code, 0)
        self.assertEqual(accepted["inbox"]["action"], "ACCEPTED")
        self.assertIn(accepted["tick"]["action"], ("RUNNING", "WAITING_ACCEPTANCE"))
        self.assertEqual(relay.raw_json(self.state)["attempts"]["second"], 1)
        self.noarg(target)
        self.assertEqual(relay.raw_json(self.state)["attempts"]["second"], 1)
        self.await_completion()

    def test_installed_noarg_missing_report_and_bad_identity_no_advance(self):
        target = self.installed()
        self.noarg(target)
        state = self.await_completion()
        path = self.inbox(state)
        (self.evidence / "first.json").unlink()
        code, out = self.noarg(target)
        self.assertEqual(code, 2)
        self.assertEqual(out["inbox"]["action"], "INBOX_BLOCKED")
        self.assertEqual(relay.raw_json(self.state)["stage_index"], 0)
        self.assertNotIn("second", relay.raw_json(self.state)["attempts"])
        self.assertTrue(path.is_file())

    def test_installed_noarg_unknown_refusal_are_terminal(self):
        for outcome in ("UNKNOWN", "REFUSED"):
            with self.subTest(outcome=outcome):
                # Each terminal outcome owns a complete independent profile,
                # including script, graph, state, inbox, evidence and xb logs.
                target = self.installed(profile_name="my-active-profile-" + outcome.lower())
                self.noarg(target)
                state = self.await_completion()
                self.inbox(state, outcome=outcome)
                code, out = self.noarg(target)
                self.assertEqual(code, 2)
                self.assertEqual(out["status"], "BLOCKED")
                self.noarg(target)
                self.assertNotIn("second", relay.raw_json(self.state)["attempts"])

    def test_installed_noarg_failure_no_retry(self):
        target = self.installed(configured=False)
        bad = self.stage("failed")
        bad["command"] = [sys.executable, "-c", "raise SystemExit(3)"]
        self.setup_graph([bad, self.stage("next")], kind="stage_relay")
        self.noarg(target)
        self.await_completion()
        code, out = self.noarg(target)
        self.assertEqual(code, 2)
        self.assertEqual(out["status"], "FAILED")
        self.assertNotIn("next", relay.raw_json(self.state)["attempts"])

    def test_installed_noarg_concurrent_process_claim_restart(self):
        target = self.installed()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            res = list(pool.map(lambda _: self.noarg(target), range(3)))
        self.assertEqual(sum(o["tick"]["action"] == "WORKER_STARTED" for _, o in res), 1)
        self.assertEqual(relay.raw_json(self.state)["attempts"]["first"], 1)
        self.await_completion()
        code, out = self.noarg(target)
        self.assertEqual(out["source_head"], self.head)
        self.assertEqual(out["stage"], "first")
        self.assertEqual(relay.raw_json(self.state)["attempts"]["first"], 1)



    def test_maker_source_waits_with_named_producer(self):
        import importlib.util
        bridge_path = MODULE.with_name("maker_stage_bridge.py")
        spec = importlib.util.spec_from_file_location("maker_stage_bridge", bridge_path)
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        historical = self.stage("historical", mode="adopt", rows=48)
        historical["reports"] = [
            {"path": "reports/" + scenario + ".json", "rows": 48,
             "symbol_cells": {"BTCUSDT": 24, "ETHUSDT": 24}}
            for scenario in ("baseline", "delay2", "cost1_5x")]
        graph = self.setup_graph([historical, self.stage("reflection", mode="adopt")])
        graph["maker_bridge"] = {
            "historical_stage": "historical", "reflection_stage": "reflection",
            "producer": {"item": "C-1037", "job": "sa-0-cc6aa1f2",
                         "instruction": "MAKER-V3-GH-CAPACITY-r13"},
            "capacity_acceptance_path": "acceptance/source.json",
            "input_manifest_sha256": "c" * 64, "config_sha256": "d" * 64,
            "source_sha256": {"backtest_entry.py": "e" * 64},
            "symbol_hashes": {s: {"bars_sha256": "f" * 64,
                                  "funding_sha256": "1" * 64}
                              for s in ("BTCUSDT", "ETHUSDT")}}
        result = bridge.source_gate(graph, "2" * 64)
        self.assertEqual(result["action"], "WAITING_CAPACITY_ACCEPTANCE")
        self.assertEqual(result["producer"]["job"], "sa-0-cc6aa1f2")
        self.assertEqual(result["producer"]["instruction"], "MAKER-V3-GH-CAPACITY-r13")
        self.assertFalse((self.evidence / "acceptance/source.json").exists())
        relay.atomic_json(self.evidence / "acceptance/source.json", {"decision": "ACCEPTED"})
        self.assertEqual(bridge.source_gate(graph, "2" * 64)["action"],
                         "WAITING_CAPACITY_ACCEPTANCE")

    def test_maker_expected_cell_universe(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "maker_stage_bridge", MODULE.with_name("maker_stage_bridge.py"))
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        universe = bridge.all_cells()
        self.assertEqual(len(universe), 48)
        self.assertEqual(sum(v.startswith("BTCUSDT:") for v in universe), 24)
        self.assertEqual(sum(v.startswith("ETHUSDT:") for v in universe), 24)

    def test_maker_reflection_needs_actual_historical_acceptance(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "maker_stage_bridge", MODULE.with_name("maker_stage_bridge.py"))
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        graph = {"maker_bridge": {"historical_stage": "historical",
                                  "reflection_stage": "reflection",
                                  "producer": {"item": "C-1037", "job": "sa-0-cc6aa1f2",
                                               "instruction": "MAKER-V3-GH-CAPACITY-r13"}},
                 "stages": [{"id": "historical", "mode": "adopt"},
                            {"id": "reflection", "mode": "adopt"}]}
        state = {"stage_index": 1, "completed": []}
        self.assertEqual(bridge.reflection_notice(graph, state)["action"], "BLOCKED")
        state["completed"] = [{"stage": "historical", "source_head": self.head,
                               "input_sha256": self.inp, "graph_sha256": "d" * 64,
                               "report_hashes": ["a" * 64] * 3, "output_count": 144}]
        ready = bridge.reflection_notice(graph, state)
        self.assertEqual(ready["action"], "READY_OPERATOR_REFLECTION")
        self.assertEqual(ready["historical_output_count"], 144)
        self.assertNotIn("trading_conclusion", ready)



    def maker_installed_fixture(self):
        """Fully isolated synthetic r13-shaped evidence, never market results."""
        import shutil
        profile = self.root / ("maker-profile-" + str(time.monotonic_ns()))
        scripts = profile / "scripts"
        scripts.mkdir(parents=True)
        installed = scripts / "stage_relay.py"
        shutil.copyfile(MODULE, installed)
        shutil.copyfile(MODULE.with_name("maker_stage_bridge.py"),
                        scripts / "maker_stage_bridge.py")
        base = profile / "stage-relay" / "C0041"
        self.evidence = base / "evidence"
        self.evidence.mkdir(parents=True)
        self.config, self.state = base / "graph.json", base / "state.json"
        upstream = self.evidence / "upstream.json"
        relay.atomic_json(upstream, {"synthetic": True, "market_proof": False})
        relay.atomic_json(self.evidence / "upstream-acceptance.json",
                          {"stage_id": "upstream", "source_head": self.head,
                           "input_sha256": self.inp, "decision": "ACCEPTED",
                           "reviewer": "synthetic-fixture",
                           "evidence_path": "upstream.json",
                           "evidence_sha256": relay.digest(upstream.read_bytes())})
        maker_dir = profile / "maker"
        maker_dir.mkdir()
        config = maker_dir / "account.json"
        inp = maker_dir / "inputs.json"
        relay.atomic_json(config, {"synthetic": True})
        relay.atomic_json(inp, {"synthetic": True})
        output = maker_dir / "output"
        source_hashes = {"backtest_entry.py": "e" * 64}
        symbol_hashes = {s: {"bars_sha256": "a" * 64,
                             "funding_sha256": "b" * 64}
                         for s in ("BTCUSDT", "ETHUSDT")}
        runner = maker_dir / "backtest_entry.py"
        runner.write_text(
            "import json,sys,hashlib,pathlib\n"
            "assert sys.argv[1]=='--verify', 'never run historical research in fixture'\n"
            "args=sys.argv[2:]\n"
            "assert args[0]=='--config' and args[2]=='--input' and args[4]=='--output'\n"
            "root=pathlib.Path(args[5]); manifest=root/'manifest.json'\n"
            "m=json.loads(manifest.read_text())\n"
            "assert len(m['artifacts'])==289\n"
            "print(json.dumps({'status':'CHECKSUMS_VERIFIED','artifact_count':289,"
            "'manifest_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest()}))\n")
        historical = self.stage("historical", mode="adopt")
        historical["reports"] = [
            {"path": "reports/" + key + ".json", "rows": 48,
             "symbol_cells": {"BTCUSDT": 24, "ETHUSDT": 24}}
            for key in ("baseline", "delay2", "cost1_5x")]
        reflection = self.stage("reflection", mode="adopt")
        graph = {"schema_version": 1, "kind": "stage_relay",
                 "evidence_root": str(self.evidence),
                 "bootstrap": {"acceptance_path": "upstream-acceptance.json",
                               "stage_id": "upstream", "source_head": self.head,
                               "input_sha256": self.inp}, "stages": [historical, reflection]}
        argv = [sys.executable, str(runner), "--run", "--config", str(config),
                "--input", str(inp), "--output", str(output)]
        graph["maker_bridge"] = {
            "historical_stage": "historical", "reflection_stage": "reflection",
            "producer": {"item": "C-1037", "job": "sa-0-cc6aa1f2",
                         "instruction": "MAKER-V3-GH-CAPACITY-r13"},
            "capacity_acceptance_path": "acceptance/capacity.json",
            "source_sha256": source_hashes,
            "input_manifest_sha256": relay.digest(inp.read_bytes()),
            "config_sha256": relay.digest(config.read_bytes()),
            "symbol_hashes": symbol_hashes, "config_path": str(config),
            "input_path": str(inp), "output_dir": str(output), "cwd": str(maker_dir),
            "run_argv": argv, "verify_argv": argv[:2] + ["--verify"] + argv[3:]}
        relay.atomic_json(self.config, graph)
        relay.init(self.config, self.state)
        return installed, graph

    def maker_capacity_accept(self, graph):
        bridge = graph["maker_bridge"]
        gh = relay.configuration(self.config)[1]
        relay.atomic_json(self.evidence / "acceptance/capacity.json",
                          {"decision": "ACCEPTED", "reviewer": "synthetic independent fixture",
                           "stage": "historical", "source_head": self.head,
                           "input_sha256": self.inp, "graph_sha256": gh,
                           "input_manifest_sha256": bridge["input_manifest_sha256"],
                           "config_sha256": bridge["config_sha256"],
                           "source_sha256": bridge["source_sha256"],
                           "symbol_hashes": bridge["symbol_hashes"]})

    def maker_outputs(self, graph):
        """Real BoundedReport inventory and compact-row shape, synthetic values."""
        import itertools
        out = Path(graph["maker_bridge"]["output_dir"])
        out.mkdir()
        inventory = {}
        cells = []
        for sym, frame, k, ttl, exit_name in itertools.product(
                ("ETHUSDT", "BTCUSDT"), (1, 15), (".25", ".50"),
                (2, 5), ("X1", "X2", "X3")):
            cells.append(f"{sym}:{frame}m:{k}:{ttl}:{exit_name}")
        for directory in ("cells", "delay2", "cost1_5x"):
            for index, cell_id in enumerate(cells):
                symbol, frame, k, ttl, exit_name = cell_id.split(":")
                for side in ("strategy", "matched"):
                    name = f"{directory}/{index:02d}_{side}.json"
                    path = out / name
                    relay.atomic_json(path, {"symbol": symbol, "framework": int(frame[:-1]),
                                             "k": k, "ttl": int(ttl), "exit": exit_name,
                                             "matched": side == "matched",
                                             "synthetic_only": True, "events": []})
                    inventory[name] = relay.digest(path.read_bytes())
        compact = [{"cell_id": name, "metrics": {"synthetic_only": True},
                    "matched_status": "READY"} for name in cells]
        relay.atomic_json(out / "results.json",
                          {"cell_count": 48, "cells": compact, "delay2_count": 48,
                           "delay2_cells": compact, "cost1_5x_count": 48,
                           "cost1_5x_cells": compact,
                           "samplers": {k: {"synthetic_only": True} for k in
                                        ("ETHUSDT:1", "ETHUSDT:15",
                                         "BTCUSDT:1", "BTCUSDT:15")},
                           "classification": "EXPLORATORY_NOT_ACCEPTED"})
        inventory["results.json"] = relay.digest((out / "results.json").read_bytes())
        relay.atomic_json(out / "manifest.json",
                          {"research_status": "EXPLORATORY_UNACCEPTED",
                           "input_manifest_sha256": graph["maker_bridge"]["input_manifest_sha256"],
                           "config_sha256": graph["maker_bridge"]["config_sha256"],
                           "source_sha256": graph["maker_bridge"]["source_sha256"],
                           "input": {k: dict(v) for k, v in
                                     graph["maker_bridge"]["symbol_hashes"].items()},
                           "artifacts": inventory})
        self.assertEqual(len(inventory), 289)
        return out

    def test_maker_installed_noarg_waits_for_real_source(self):
        installed, graph = self.maker_installed_fixture()
        for _ in range(2):
            code, result = self.noarg(installed)
            self.assertEqual(code, 0)
            self.assertEqual(result["action"], "WAITING_CAPACITY_ACCEPTANCE")
            self.assertEqual(result["capacity"]["producer"]["job"], "sa-0-cc6aa1f2")
            self.assertIsNone(relay.raw_json(self.state)["owner"])
        self.assertEqual(relay.raw_json(self.state)["attempts"], {})

    def test_maker_verify_all_three_scenarios_and_guards(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "maker_stage_bridge", MODULE.with_name("maker_stage_bridge.py"))
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        _, graph = self.maker_installed_fixture()
        maker = graph["maker_bridge"]
        self.maker_outputs(graph)
        rows, manifest, result = bridge.verified_rows(maker)
        self.assertEqual([len(s) for s in rows], [48, 48, 48])
        self.assertEqual(len({r["cell_id"] for r in rows[0]}), 48)
        self.assertEqual(sum(r["symbol"] == "BTCUSDT" for r in rows[2]), 24)
        self.assertEqual(len(manifest["artifacts"]), 289)
        self.assertEqual(result["classification"], "EXPLORATORY_NOT_ACCEPTED")
        output = Path(maker["output_dir"])
        original = (output / "results.json").read_bytes()
        manifest_path = output / "manifest.json"
        original_manifest = manifest_path.read_bytes()
        for mutation in ("duplicate", "missing", "wrong_hash"):
            report = json.loads(original)
            if mutation == "duplicate":
                report["delay2_cells"][1]["cell_id"] = report["delay2_cells"][0]["cell_id"]
                relay.atomic_json(output / "results.json", report)
                updated_manifest = relay.raw_json(manifest_path)
                updated_manifest["artifacts"]["results.json"] = relay.digest((output / "results.json").read_bytes())
                relay.atomic_json(manifest_path, updated_manifest)
            elif mutation == "missing":
                (output / "delay2/00_matched.json").unlink()
            else:
                (output / "cost1_5x/00_strategy.json").write_text("{}")
            with self.subTest(mutation=mutation):
                with self.assertRaises(relay.RelayError) as caught:
                    bridge.verified_rows(maker)
                if mutation == "duplicate":
                    self.assertIn("duplicate/missing/wrong scenario cell", str(caught.exception))
                else:
                    self.assertIn("artifact mismatch", str(caught.exception))
            if mutation == "duplicate":
                (output / "results.json").write_bytes(original)
                manifest_path.write_bytes(original_manifest)
            elif mutation == "missing":
                self.maker_outputs_reset_single(graph, "delay2/00_matched.json")
            else:
                self.maker_outputs_reset_single(graph, "cost1_5x/00_strategy.json")
        self.assertEqual([len(s) for s in bridge.verified_rows(maker)[0]], [48] * 3)

    def maker_outputs_reset_single(self, graph, relative):
        """Restore exactly one synthetic raw cell without changing manifest bytes."""
        directory, filename = relative.split("/")
        index = int(filename[:2])
        result = relay.raw_json(Path(graph["maker_bridge"]["output_dir"]) / "results.json")
        key = {"cells": "cells", "delay2": "delay2_cells",
               "cost1_5x": "cost1_5x_cells"}[directory]
        symbol, frame, k, ttl, exit_name = result[key][index]["cell_id"].split(":")
        relay.atomic_json(Path(graph["maker_bridge"]["output_dir"]) / relative,
                          {"symbol": symbol, "framework": int(frame[:-1]),
                           "k": k, "ttl": int(ttl), "exit": exit_name,
                           "matched": filename.endswith("_matched.json"),
                           "synthetic_only": True, "events": []})



    def maker_adopt_synthetic_process(self, graph, exit_code=0):
        """Own a genuine local subprocess with the exact CLI-shaped argv."""
        maker = graph["maker_bridge"]
        runner = Path(maker["run_argv"][1])
        script = runner.read_text()
        runner.write_text(
            "import json,sys,time\n"
            "args=sys.argv[1:]\n"
            "if args[0]=='--run':\n"
            "    time.sleep(0.6)\n"
            + ("    raise MemoryError('synthetic failure, no history')\n"
               if exit_code else "    raise SystemExit(0)\n") +
            "assert args[0]=='--verify'\n"
            "import pathlib,hashlib\n"
            "root=pathlib.Path(args[-1]); m=root/'manifest.json'\n"
            "obj=json.loads(m.read_text())\n"
            "print(json.dumps({'status':'CHECKSUMS_VERIFIED',"
            "'artifact_count':len(obj['artifacts']),"
            "'manifest_sha256':hashlib.sha256(m.read_bytes()).hexdigest()}))\n")
        proc = subprocess.Popen(maker["run_argv"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, cwd=maker["cwd"])
        ident, condition = relay.process_identity(proc.pid)
        self.assertEqual(condition, "LIVE")
        identity_path = self.evidence / "external-owner.json"
        relay.atomic_json(identity_path, dict(ident, argv=maker["run_argv"],
                                               operator="synthetic fixture",
                                               started_at=time.time()))
        adopted = relay.adopt(self.config, self.state, identity_path, 60)
        self.assertEqual(adopted["action"], "ADOPTED")
        proc.wait(timeout=10)
        return adopted["owner"], proc.returncode

    def test_maker_installed_adopt_requires_capacity_and_exact_argv(self):
        installed, graph = self.maker_installed_fixture()
        self.assertEqual(self.noarg(installed)[1]["action"], "WAITING_CAPACITY_ACCEPTANCE")
        with self.assertRaises(FileNotFoundError):
            relay.adopt(self.config, self.state, self.evidence / "nonexistent-owner.json", 60)
        import subprocess
        running = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])
        self.addCleanup(lambda: (running.poll() is None and running.kill(), running.wait()))
        ident, condition = relay.process_identity(running.pid)
        self.assertEqual(condition, "LIVE")
        identity_path = self.evidence / "adoption-candidate.json"
        relay.atomic_json(identity_path, dict(ident, argv=graph["maker_bridge"]["run_argv"], operator="synthetic fixture"))
        with self.assertRaisesRegex(relay.RelayError, "WAITING_CAPACITY_ACCEPTANCE"):
            relay.adopt(self.config, self.state, identity_path, 60)
        self.maker_capacity_accept(graph)
        bad = relay.raw_json(identity_path)
        bad["argv"] = [sys.executable, "-c", "pass"]
        relay.atomic_json(identity_path, bad)
        with self.assertRaisesRegex(relay.RelayError, "adopted argv differs"):
            relay.adopt(self.config, self.state, identity_path, 60)
        self.assertIsNone(relay.raw_json(self.state)["owner"])
        code, result = self.noarg(installed)
        self.assertEqual(result["tick"]["action"], "READY_MISSING_OWNER")
        self.assertEqual(relay.raw_json(self.state)["status"], "WAITING_ADOPTION")
        self.assertEqual(relay.raw_json(self.state)["attempts"], {})

    def test_maker_external_failure_memoryerror_does_not_retry(self):
        installed, graph = self.maker_installed_fixture()
        self.maker_capacity_accept(graph)
        self.noarg(installed)
        owner, rc = self.maker_adopt_synthetic_process(graph, exit_code=1)
        self.assertNotEqual(rc, 0)
        # Trusted parent supplies failure; dead PID alone never implies success.
        gh = relay.configuration(self.config)[1]
        stage = "historical"
        receipt = {"stage": stage, "source_head": self.head,
                   "input_sha256": self.inp, "graph_sha256": gh,
                   "owner": {k: owner[k] for k in ("pid", "boot_id", "start_ticks", "token")},
                   "outcome": "FAILED", "exit_code": rc,
                   "reason": "synthetic MemoryError; product rc1"}
        relay.atomic_json(self.evidence / "inbox/historical.receipt.json", receipt)
        code, result = self.noarg(installed)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["stage"], "historical")
        self.assertEqual(relay.raw_json(self.state)["stage_index"], 0)
        self.noarg(installed)
        self.assertEqual(relay.raw_json(self.state)["attempts"]["historical"], 1)

    def test_maker_installed_normalize_requires_independent_acceptance(self):
        import importlib.util
        bridge_spec = importlib.util.spec_from_file_location(
            "maker_stage_bridge", MODULE.with_name("maker_stage_bridge.py"))
        bridge = importlib.util.module_from_spec(bridge_spec)
        bridge_spec.loader.exec_module(bridge)
        installed, graph = self.maker_installed_fixture()
        self.maker_capacity_accept(graph)
        self.maker_outputs(graph)
        self.noarg(installed)
        owner, rc = self.maker_adopt_synthetic_process(graph)
        self.assertEqual(rc, 0)
        gh = relay.configuration(self.config)[1]
        relay.atomic_json(self.evidence / "terminal/trusted.json",
                          {"stage": "historical", "source_head": self.head,
                           "input_sha256": self.inp, "graph_sha256": gh,
                           "owner": {k: owner[k] for k in
                                     ("pid", "boot_id", "start_ticks", "token")},
                           "trusted_parent": True, "outcome": "SUCCEEDED", "exit_code": 0})
        normalized = bridge.normalize(self.config, self.state, "terminal/trusted.json")
        self.assertEqual(normalized["action"], "WAITING_INDEPENDENT_RESULT_ACCEPTANCE")
        self.assertFalse((self.evidence / "inbox/historical.acceptance.json").exists())
        self.assertEqual(len(normalized["reports"]), 3)
        self.assertEqual([r["rows"] for r in normalized["reports"]], [48, 48, 48])
        code, before = self.noarg(installed)
        self.assertEqual(before["inbox"]["action"], "MISSING_INDEPENDENT_ACCEPTANCE")
        self.assertEqual(relay.raw_json(self.state)["stage_index"], 0)
        receipt = self.evidence / "inbox/historical.receipt.json"
        relay.atomic_json(self.evidence / "inbox/historical.acceptance.json",
                          {"stage": "historical", "source_head": self.head,
                           "input_sha256": self.inp, "graph_sha256": gh,
                           "receipt_sha256": relay.digest(receipt.read_bytes()),
                           "decision": "ACCEPTED", "reviewer": "independent synthetic fixture"})
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            seen = list(pool.map(lambda _: self.noarg(installed), range(2)))
        self.assertEqual(sum(v["inbox"].get("action") == "ACCEPTED" for _, v in seen), 1)
        self.assertEqual(relay.raw_json(self.state)["stage_index"], 1)
        self.assertEqual(len(relay.raw_json(self.state)["completed"]), 1)
        self.assertTrue(any(v["action"] == "READY_OPERATOR_REFLECTION" for _, v in seen))
        again = self.noarg(installed)[1]
        self.assertEqual(again["action"], "READY_OPERATOR_REFLECTION")
        self.assertEqual(again["reflection"]["historical_output_count"], 144)
        self.assertNotIn("trading_conclusion", again["reflection"])


    def test_maker_python_unbuffered_exact_argv(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("maker_stage_bridge", MODULE.with_name("maker_stage_bridge.py"))
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        _, graph = self.maker_installed_fixture()
        maker = graph["maker_bridge"]
        run = maker["run_argv"]
        maker["run_argv"] = [run[0], "-u"] + run[1:]
        maker["verify_argv"] = [run[0], run[1], "--verify"] + run[3:]
        self.assertEqual(bridge.verify_argv(maker), maker["verify_argv"])
        maker["verify_argv"] = [run[0], "-u", run[1], "--verify"] + run[3:]
        self.assertEqual(bridge.verify_argv(maker), maker["verify_argv"])
        maker["run_argv"][1] = "-X"
        with self.assertRaisesRegex(relay.RelayError, "unsupported flag"):
            bridge.verify_argv(maker)
        maker["run_argv"] = [run[0], "-u"] + run[1:]
        maker["verify_argv"] = [run[0], "-u", "/other/backtest_entry.py", "--verify"] + run[3:]
        with self.assertRaisesRegex(relay.RelayError, "interpreter or source entrypoint mismatch"):
            bridge.verify_argv(maker)



    def test_maker_relative_child_actual_subprocess_and_cli(self):
        """SYNTHETIC ONLY. Adopt actual relative python3 -u child, never run history."""
        import shutil
        installed, graph = self.maker_installed_fixture()
        maker = graph["maker_bridge"]
        runner = Path(maker["cwd"]) / "backtest_entry.py"
        runner.write_text(
            "import sys,time,json,pathlib,hashlib\n"
            "if sys.argv[1]=='--run':\n"
            "    time.sleep(1)\n"
            "    raise SystemExit(0)\n"
            "assert sys.argv[1]=='--verify'\n"
            "m=pathlib.Path(sys.argv[-1])/'manifest.json'\n"
            "obj=json.loads(m.read_text())\n"
            "print(json.dumps({'status':'CHECKSUMS_VERIFIED',"
            "'artifact_count':len(obj['artifacts']),"
            "'manifest_sha256':hashlib.sha256(m.read_bytes()).hexdigest()}))\n")
        actual_exe = str(Path(shutil.which("python3")).resolve())
        maker["child_executable"] = actual_exe
        old_run = maker["run_argv"]
        maker["run_argv"] = ["python3", "-u", "backtest_entry.py"] + old_run[2:]
        maker["verify_argv"] = [actual_exe, str(runner), "--verify"] + old_run[3:]
        relay.atomic_json(self.config, graph)
        self.state.unlink()
        relay.init(self.config, self.state)
        self.maker_capacity_accept(graph)
        self.maker_outputs(graph)
        self.assertEqual(self.noarg(installed)[1]["tick"]["action"], "READY_MISSING_OWNER")
        proc = subprocess.Popen(maker["run_argv"], cwd=maker["cwd"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        identity, status = relay.process_identity(proc.pid)
        self.assertEqual(status, "LIVE")
        self.assertEqual(Path("/proc/%d/exe" % proc.pid).resolve(), Path(actual_exe).resolve())
        owner_path = self.evidence / "owner.json"
        relay.atomic_json(owner_path, dict(identity, argv=maker["run_argv"], operator="SYNTHETIC_ONLY"))
        owner = relay.adopt(self.config, self.state, owner_path, 60)["owner"]
        self.assertEqual(owner["argv"], maker["run_argv"])
        self.assertEqual(proc.wait(timeout=10), 0)
        gh = relay.configuration(self.config)[1]
        relay.atomic_json(self.evidence / "terminal/trusted.json",
                          {"stage": "historical", "source_head": self.head,
                           "input_sha256": self.inp, "graph_sha256": gh,
                           "owner": {k: owner[k] for k in
                                     ("pid", "start_ticks", "boot_id", "token")},
                           "trusted_parent": True, "outcome": "SUCCEEDED", "exit_code": 0})
        result = subprocess.run([sys.executable, str(installed.with_name("maker_stage_bridge.py")),
                                 "normalize", "--config", str(self.config), "--state",
                                 str(self.state), "--terminal", "terminal/trusted.json"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["action"], "WAITING_INDEPENDENT_RESULT_ACCEPTANCE")
        self.assertFalse((self.evidence / "inbox/historical.acceptance.json").exists())
        self.assertEqual(self.noarg(installed)[1]["inbox"]["action"], "MISSING_INDEPENDENT_ACCEPTANCE")
        receipt = self.evidence / "inbox/historical.receipt.json"
        relay.atomic_json(self.evidence / "inbox/historical.acceptance.json",
                          {"stage": "historical", "source_head": self.head,
                           "input_sha256": self.inp, "graph_sha256": gh,
                           "receipt_sha256": relay.digest(receipt.read_bytes()),
                           "decision": "ACCEPTED", "reviewer": "SYNTHETIC_ONLY"})
        self.assertEqual(self.noarg(installed)[1]["action"], "READY_OPERATOR_REFLECTION")

    def test_maker_relative_python_rejects_bad_bindings(self):
        import importlib.util
        loader = importlib.util.spec_from_file_location("maker_stage_bridge",
                                                         MODULE.with_name("maker_stage_bridge.py"))
        bridge = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(bridge)
        _, graph = self.maker_installed_fixture()
        maker = graph["maker_bridge"]
        original = list(maker["run_argv"])
        maker["child_executable"] = str(Path(sys.executable).resolve())
        maker["run_argv"] = ["python3", "-u", "backtest_entry.py"] + original[2:]
        self.assertEqual(bridge.verify_argv(maker), maker["verify_argv"])
        bad = ({"child_executable": "/nonexistent/python"},
               {"cwd": "/nonexistent/source"},
               {"run_argv": ["python3", "-X", "backtest_entry.py"] + original[2:]},
               {"run_argv": ["python3", "-u", "../backtest_entry.py"] + original[2:]},
               {"run_argv": ["python3", "-u", "other.py"] + original[2:]},
               {"run_argv": ["python3", "-u", "backtest_entry.py", "--run", "--config",
                             maker["config_path"], "--input", maker["input_path"],
                             "--output", "/wrong/output"]},
               {"verify_argv": [sys.executable, "/other/backtest_entry.py"] + maker["verify_argv"][2:]})
        for change in bad:
            with self.subTest(change=change):
                with self.assertRaises(relay.RelayError):
                    bridge.verify_argv(dict(maker, **change))


if __name__ == "__main__":
    unittest.main()
