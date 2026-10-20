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



if __name__ == "__main__":
    unittest.main()
