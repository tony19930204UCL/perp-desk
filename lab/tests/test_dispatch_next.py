"""Bounded source-only dispatch candidate tests. No browser, network, or trading."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / "engineering" / "relay" / "dispatch_next.py"
SPEC = importlib.util.spec_from_file_location("dispatch_next_c0182", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DispatchNextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        state = root / "state"
        state.mkdir()
        jobdir = root / "jobdir"
        jobdir.mkdir()
        paths = {}
        for name in ("preflight", "queue", "commit", "observer"):
            path = root / (name + ".py")
            path.write_text("pass\n", encoding="utf-8")
            paths[name] = str(path)
        job = root / "job.json"
        job.write_text(json.dumps({"state_dir": str(state), "repository": "sample/repo",
                                   "branch": "branch", "task_id": "task"}), encoding="utf-8")
        self.state = state
        self.root = root
        self.config = dict(paths, job=str(job), job_dir=str(jobdir), state_dir=str(state),
                           receipt_path=str(root / "receipt.json"), repository="sample/repo",
                           branch="branch", task_id="task", instruction_id="task-r0",
                           exact_target_approval_owner="owner",
                           observer_argv=[paths["observer"]],
                           observer_send_incapable_verified=True,
                           ledger_text="Observe exact target")

    def test_preflight_nonzero_stops(self):
        with patch.object(MODULE, "invoke", return_value={"returncode": 1, "json": {"ok": False},
                                                           "stdout": "{}", "stderr": "failed"}) as call:
            result = MODULE.run(self.config)
        self.assertEqual(result["status"], "PRECHECK_FAILED")
        self.assertEqual(call.call_count, 1)

    def test_preflight_malformed_json_stops(self):
        with patch.object(MODULE, "invoke", return_value={"returncode": 0, "stdout": "invalid",
                                                           "stderr": "", "json_error": "MALFORMED_JSON"}) as call:
            result = MODULE.run(self.config)
        self.assertEqual(result["status"], "PRECHECK_FAILED")
        self.assertEqual(call.call_count, 1)

    def test_relative_entrypoint_rejected(self):
        self.config["queue"] = "queue.py"
        with self.assertRaises(ValueError):
            MODULE.run(self.config)

    def test_missing_owner_stops_before_send(self):
        self.config.pop("exact_target_approval_owner")
        with patch.object(MODULE, "invoke") as call:
            result = MODULE.run(self.config)
        self.assertEqual(result["status"], "PRECHECK_FAILED")
        call.assert_not_called()

    def test_active_instruction_stops_before_send(self):
        (self.state / "active_instruction.json").write_text("{}")
        with patch.object(MODULE, "invoke") as call:
            result = MODULE.run(self.config)
        self.assertEqual(result["status"], "UNSENT")
        call.assert_not_called()

    def test_unknown_send_does_not_retry(self):
        records = iter([
            {"returncode": 0, "json": {"ok": True}, "stdout": "{}", "stderr": ""},
            {"returncode": 1, "json": {"status": "BLOCKED_OWNER"}, "stdout": "{}",
             "stderr": "uncertain"},
        ])
        with patch.object(MODULE, "invoke", side_effect=lambda argv: next(records)) as call:
            result = MODULE.run(self.config)
        self.assertEqual(result["status"], "SEND_UNKNOWN")
        self.assertEqual(call.call_count, 2)
        self.assertTrue((self.root / "receipt.json.outcome.json").exists())
        with self.assertRaises(FileExistsError):
            MODULE.run(self.config)

    def test_confirmed_delivery_registration_failure(self):
        def fake(argv):
            if argv[1] == self.config["preflight"]:
                return {"returncode": 0, "json": {"ok": True}, "stdout": "{}", "stderr": ""}
            if argv[1] == self.config["queue"]:
                (self.state / "send_task-r0.json").write_text(json.dumps({
                    "status": "CONFIRMED", "evidence_kind": "EXACT_RENDERED_USER_BUBBLE",
                    "text_hash": "hash", "confirmed_at": "timestamp",
                    "head_sha": "head", "blob_sha": "blob"}))
                return {"returncode": 0, "json": {"status": "DISPATCHED",
                        "instruction_id": "task-r0", "item": "item"},
                        "stdout": "{}", "stderr": ""}
            return {"returncode": 1, "stdout": "", "stderr": "ledger unavailable"}
        with patch.object(MODULE, "invoke", side_effect=fake) as call:
            result = MODULE.run(self.config)
        self.assertEqual(result["status"], "REGISTRATION_FAILED")
        self.assertEqual(result["send_receipt"]["head_sha"], "head")
        self.assertEqual(call.call_count, 3)

    def test_registered_stamp_preserved(self):
        def fake(argv):
            if argv[1] == self.config["preflight"]:
                return {"returncode": 0, "json": {"ok": True}, "stdout": "{}", "stderr": ""}
            if argv[1] == self.config["queue"]:
                (self.state / "send_task-r0.json").write_text(json.dumps({
                    "status": "CONFIRMED", "evidence_kind": "EXACT_RENDERED_USER_BUBBLE",
                    "text_hash": "hash", "confirmed_at": "timestamp"}))
                return {"returncode": 0, "json": {"status": "DISPATCHED",
                        "instruction_id": "task-r0"}, "stdout": "{}", "stderr": ""}
            return {"returncode": 0, "stdout": "ledger-stamp\n", "stderr": ""}
        with patch.object(MODULE, "invoke", side_effect=fake):
            result = MODULE.run(self.config)
        self.assertEqual(result["status"], "WATCH_REGISTERED")
        self.assertEqual(result["ledger_stamp_raw"], "ledger-stamp\n")
        self.assertFalse(result["deployed"])


if __name__ == "__main__":
    unittest.main()
