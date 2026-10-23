#!/usr/bin/env python3
"""test_relay_stall_adapter.py - Unittests for production adapter and consumer."""

import fcntl
import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import relay_stall_adapter as rsa
import relay_stall_cron as rsc
import relay_stall_watch as rsw

WORKTREE_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(WORKTREE_DIR, "operator-integration", "fixture-temp")
os.makedirs(FIXTURE_DIR, exist_ok=True)
tempfile.tempdir = FIXTURE_DIR


class TestRelayStallAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=FIXTURE_DIR)
        self.state_dir = os.path.join(self.temp_dir.name, "state")
        self.norm_file = os.path.join(self.temp_dir.name, "normalized.json")

        snap_candidates = [
            os.path.join(WORKTREE_DIR, "source-snapshot.json"),
            os.path.join(WORKTREE_DIR, "fixtures", "source-snapshot.json"),
            os.path.join(WORKTREE_DIR, "..", "..", "source-snapshot.json"),
        ]
        self.snapshot_file = next((p for p in snap_candidates if os.path.isfile(p)), snap_candidates[0])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_snapshot_parse(self):
        doc = rsa.build_normalized(self.snapshot_file, self.state_dir, 1792757400.0)
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(len(doc["jobs"]), 2)
        pr_nums = {j["job_id"].split("_")[0] for j in doc["jobs"]}
        self.assertEqual(pr_nums, {"PR75", "PR78"})

    def test_missing_fields_unk(self):
        with open(self.snapshot_file, "r", encoding="utf-8") as f:
            snap = json.load(f)
        snap["jobs"][0]["job"] = {}
        bad_snap = os.path.join(self.temp_dir.name, "bad_snap.json")
        with open(bad_snap, "w", encoding="utf-8") as f:
            json.dump(snap, f)

        doc = rsa.build_normalized(bad_snap, self.state_dir, 1792757400.0)
        rsa.rsw.atomic_write(self.norm_file, doc)
        rc = rsa.rsw.run_scan(self.norm_file, self.state_dir, 1792757400.0)
        self.assertEqual(rc, 0)

        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        states = [st for ev in pending for st in ev.get("states", [])]
        self.assertIn("UNKNOWN", states)

    def test_refusal_preserved(self):
        doc = rsa.build_normalized(self.snapshot_file, self.state_dir, 1792757400.0)
        for j in doc["jobs"]:
            self.assertTrue(j["policy_refusal"])

    def test_inst_not_inherit(self):
        with open(self.snapshot_file, "r", encoding="utf-8") as f:
            snap = json.load(f)
        pr78_raw = next(j for j in snap["jobs"] if j["job"]["pr_number"] == 78)
        pr78_raw["files"]["active_instruction.json"]["instruction_id"] = "brand-new-inst-003"
        c_snap = os.path.join(self.temp_dir.name, "inst_no_inherit_snap.json")
        with open(c_snap, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        doc = rsa.build_normalized(c_snap, self.state_dir, 1792757400.0)
        pr78 = next(j for j in doc["jobs"] if j["job_id"].startswith("PR78"))
        self.assertFalse(pr78["handback_valid"])

    def test_journal_before_ack(self):
        doc = rsa.build_normalized(self.snapshot_file, self.state_dir, 1792757400.0)
        rsa.rsw.atomic_write(self.norm_file, doc)
        rsw.run_scan(self.norm_file, self.state_dir, 1792757400.0)

        rc = rsa.run_consumer_intake(self.state_dir, self.snapshot_file, 1792757405.0)
        self.assertEqual(rc, 0)

        journal_f = os.path.join(self.state_dir, "consumer_journal.jsonl")
        self.assertTrue(os.path.isfile(journal_f))
        with open(journal_f, "r") as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 2)
        decisions = {l["decision"] for l in lines}
        self.assertIn(rsa.DEC_WAIT_PROV, decisions)
        self.assertIn(rsa.DEC_WAIT_HUMAN, decisions)

    def test_unknown_event_ack(self):
        os.makedirs(self.state_dir, exist_ok=True)
        pending_f = os.path.join(self.state_dir, "pending.json")
        with open(pending_f, "w") as f:
            json.dump([], f)
        rc = rsw.run_ack(self.state_dir, "fake_ev_123", "action", 1000.0)
        self.assertNotEqual(rc, 0)

    def test_drill_same_event(self):
        rsa.main(["produce-scan", "--snapshot", self.snapshot_file, "--state-dir", self.state_dir, "--now", "1792757400.0"])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p1 = json.load(f)
        ev_id1 = {p["job_id"]: p["event_id"] for p in [dict(e, job_id=e["identity"]["job_id"]) for e in p1]}

        rsa.main(["produce-scan", "--snapshot", self.snapshot_file, "--state-dir", self.state_dir, "--now", "1792757450.0"])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p2 = json.load(f)
        ev_id2 = {p["job_id"]: p["event_id"] for p in [dict(e, job_id=e["identity"]["job_id"]) for e in p2]}
        self.assertEqual(ev_id1, ev_id2)

    def test_bad_state_failopen(self):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, "state.json"), "w") as f:
            f.write("{broken")
        rc = rsa.main(["produce-scan", "--snapshot", self.snapshot_file, "--state-dir", self.state_dir, "--now", "1792757400.0"])
        self.assertNotEqual(rc, 0)

    def test_receipt_freshness(self):
        c1 = rsa.get_consumer_status(self.state_dir)
        self.assertIsNone(c1["last_receipt_at"])

        os.makedirs(self.state_dir, exist_ok=True)
        rcpt_file = os.path.join(self.state_dir, "receipts.jsonl")
        rsw.append_jsonl(rcpt_file, {"recorded_at": 1792757410.0, "receipt_id": "r1"})

        c2 = rsa.get_consumer_status(self.state_dir)
        self.assertEqual(c2["last_receipt_at"], 1792757410.0)

    def test_two_job_match(self):
        with open(self.snapshot_file, "r") as f:
            snap = json.load(f)
        tr75 = snap["jobs"][0].get("files", {}).get("PARENT_PROVIDER_BLOCK_SCOPE_TRANSFER.json")
        inst75 = snap["jobs"][0]["files"].get("active_instruction.json", {}).get("instruction_id")
        gov75 = rsa.match_governing_item(75, inst75, snap["ledger_items"], tr75)
        self.assertIsNotNone(gov75)
        exp_75 = "C-9001" if any(i.get("id") == "C-9001" for i in snap.get("ledger_items", [])) else "C-1342"
        self.assertEqual(gov75["id"], exp_75)

        tr78 = snap["jobs"][1].get("files", {}).get("PARENT_OWNER_TRANSFER.json")
        inst78 = snap["jobs"][1]["files"].get("active_instruction.json", {}).get("instruction_id")
        gov78 = rsa.match_governing_item(78, inst78, snap["ledger_items"], tr78)
        self.assertIsNotNone(gov78)
        exp_78 = "C-9002" if any(i.get("id") == "C-9002" for i in snap.get("ledger_items", [])) else "C-1340"
        self.assertEqual(gov78["id"], exp_78)

    def test_chat_rc1_unknown(self):
        with open(self.snapshot_file, "r", encoding="utf-8") as f:
            snap = json.load(f)
        for j in snap["jobs"]:
            j["chat_rc"] = 1
            j.pop("chat_snapshot", None)
            j.pop("chat", None)
        c_snap = os.path.join(self.temp_dir.name, "chat_rc1_snap.json")
        with open(c_snap, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        doc = rsa.build_normalized(c_snap, self.state_dir, 1792757400.0)
        for j in doc["jobs"]:
            self.assertNotIn("chat", j)

    def test_full_run_all(self):
        rc_prod = rsa.main(["produce-scan", "--snapshot", self.snapshot_file, "--state-dir", self.state_dir, "--now", "1792757400.0"])
        self.assertEqual(rc_prod, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.state_dir, "latest.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.state_dir, "pending.json")))

        rc_cons = rsa.main(["consume", "--snapshot", self.snapshot_file, "--state-dir", self.state_dir, "--now", "1792757405.0"])
        self.assertEqual(rc_cons, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.state_dir, "consumer_journal.jsonl")))
        self.assertTrue(os.path.isfile(os.path.join(self.state_dir, "consumer_runs.jsonl")))

    def test_fake_pid_dead(self):
        is_alive, id_ok = rsa.check_pid_liveness(999999999, "start_123")
        self.assertFalse(is_alive)
        self.assertFalse(id_ok)

    def test_new_inst_no_refuse(self):
        with open(self.snapshot_file, "r") as f:
            snap = json.load(f)
        gov = rsa.match_governing_item(75, "brand-new-instruction-xyz", snap["ledger_items"])
        self.assertIsNone(gov)

    def test_chat_busy_preserved(self):
        raw_entry = {
            "job": {"pr_number": 75, "repository": "r", "branch": "b"},
            "files": {"active_instruction.json": {"instruction_id": "inst_1"}},
            "chat_rc": 0,
            "chat_observed_at": 1000.0,
            "chat_snapshot": {"busy": True, "pending_card": None},
        }
        res = rsa.adapt_job_entry(raw_entry, [], 1000.0)
        self.assertIn("chat", res)
        self.assertTrue(res["chat"]["busy"])
        self.assertIsNone(res["chat"]["pending_card"])

    def test_mb_rc1_null_hand(self):
        raw_entry = {
            "job": {"pr_number": 75, "repository": "r", "branch": "b"},
            "files": {"active_instruction.json": {"instruction_id": "inst_1"}},
            "mailbox_rc": 1,
            "mailbox": {"schema_version": 2},
        }
        res = rsa.adapt_job_entry(raw_entry, [], 1000.0)
        self.assertIsNone(res["handback_valid"])

    def test_checker_not_inherit(self):
        raw_entry = {
            "job": {"pr_number": 78, "repository": "r", "branch": "b"},
            "files": {
                "active_instruction.json": {"instruction_id": "new-instruction-2"},
                "claim_readback": {"approval_grant": {"instruction_id": "old-instruction-1"}},
                "NORMAL_CHECKER_RESULT.json": {"result": "STOPPED", "error": "token-like"},
            },
        }
        res = rsa.adapt_job_entry(raw_entry, [], 1000.0)
        self.assertIsNone(res["policy_refusal"])

    def test_cron_wrapper(self):
        cfg_path = os.path.join(self.temp_dir.name, "cron_config.json")
        with open(cfg_path, "w") as f:
            json.dump({
                "snapshot_path": self.snapshot_file,
                "state_dir": self.state_dir,
            }, f)
        rc = rsc.main(["--config", cfg_path, "--now", "1792757400.0"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(self.state_dir, "latest.json")))

    def test_cruns_on_empty(self):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, "pending.json"), "w") as f:
            json.dump([], f)
        now_ts = 1792758000.0
        rc = rsa.run_consumer_intake(self.state_dir, self.snapshot_file, now_ts)
        self.assertEqual(rc, 0)
        cruns_file = os.path.join(self.state_dir, "consumer_runs.jsonl")
        self.assertTrue(os.path.isfile(cruns_file))
        status = rsa.get_consumer_status(self.state_dir)
        self.assertEqual(status["last_receipt_at"], now_ts)

    def test_cruns_on_repeat(self):
        os.makedirs(self.state_dir, exist_ok=True)
        pending_item = [{
            "event_id": "ev_test_123",
            "identity": {"job_id": "PR75_test"},
            "states": ["UNKNOWN"],
            "ack_receipt_id": "rcpt_prior_001"
        }]
        with open(os.path.join(self.state_dir, "pending.json"), "w") as f:
            json.dump(pending_item, f)
        now_ts = 1792758100.0
        rc = rsa.run_consumer_intake(self.state_dir, self.snapshot_file, now_ts)
        self.assertEqual(rc, 0)
        status = rsa.get_consumer_status(self.state_dir)
        self.assertEqual(status["last_receipt_at"], now_ts)

    def test_cruns_fail_bad_pend(self):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, "pending.json"), "w") as f:
            f.write("{invalid_json")
        rc = rsa.run_consumer_intake(self.state_dir, self.snapshot_file, 1792758200.0)
        self.assertNotEqual(rc, 0)
        cruns_file = os.path.join(self.state_dir, "consumer_runs.jsonl")
        self.assertFalse(os.path.isfile(cruns_file))

    def test_cron_enabled_flag(self):
        st1 = rsa.get_consumer_status(self.state_dir, snapshot={"cron_enabled": False})
        self.assertFalse(st1["enabled"])
        st2 = rsa.get_consumer_status(self.state_dir, snapshot={"consumer": {"enabled": False}})
        self.assertFalse(st2["enabled"])
        st3 = rsa.get_consumer_status(self.state_dir, snapshot={"consumer": {"enabled": True}})
        self.assertTrue(st3["enabled"])

    def test_live_collect_mock(self):
        synth_job_dir = os.path.join(self.temp_dir.name, "synth_job")
        os.makedirs(synth_job_dir, exist_ok=True)
        job_json = os.path.join(synth_job_dir, "job.json")
        with open(job_json, "w") as f:
            json.dump({
                "pr_number": 75,
                "repository": "mock-org/mock-repo",
                "branch": "main",
                "mailbox": ".relay/mailbox.json",
                "dispatch_release_grant": {"next_instruction": "inst-1"}
            }, f)
        reg_file = os.path.join(self.temp_dir.name, "synthetic_registry.json")
        with open(reg_file, "w") as f:
            json.dump({
                "profile_home": self.temp_dir.name,
                "jobs": [{
                    "job_path": job_json,
                    "transfer_files": [],
                    "policy_files": [],
                }]
            }, f)
        class MockProc:
            returncode = 0
            stdout = '{"sha": "0123456789abcdef"}'
            stderr = ""
        with patch("subprocess.run", return_value=MockProc()):
            snap = rsa.collect_live_snapshot(reg_file, 1792758300.0, explicit_profile_home=self.temp_dir.name)
        self.assertGreater(snap["observed_at"], 0)
        self.assertEqual(len(snap["jobs"]), 1)
        self.assertEqual(snap["jobs"][0]["job"]["pr_number"], 75)

    def test_dyn_successor_match(self):
        ledger = [
            {"id": "C-2001", "text": "PR100 successor work", "state": "OPEN"},
            {"id": "C-2002", "text": "PR100 other work", "state": "OPEN"}
        ]
        tr = {"successor": {"id": "C-2001"}}
        res = rsa.match_governing_item(100, "inst-xyz", ledger, transfer_data=tr)
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], "C-2001")

    def test_terminal_item_skip(self):
        for term_st in ["VERIFIED", "COMPLETED", "CLOSED", "DROPPED"]:
            ledger = [{"id": "C-3001", "text": "PR100 item", "state": term_st}]
            tr = {"successor": {"id": "C-3001"}}
            res = rsa.match_governing_item(100, "inst-xyz", ledger, transfer_data=tr)
            self.assertIsNone(res)

    def test_lock_fail_no_trans(self):
        synth_job_dir = os.path.join(self.temp_dir.name, "lock_job")
        os.makedirs(synth_job_dir, exist_ok=True)
        job_json = os.path.join(synth_job_dir, "job.json")
        with open(job_json, "w") as f:
            json.dump({
                "pr_number": 75,
                "repository": "mock-org/mock-repo",
                "branch": "main",
                "transport_command": ["synthetic-transport"]
            }, f)
        reg_file = os.path.join(self.temp_dir.name, "reg_lock.json")
        with open(reg_file, "w") as f:
            json.dump({
                "profile_home": self.temp_dir.name,
                "jobs": [{"job_path": job_json}]
            }, f)
        lock_dir = os.path.join(self.temp_dir.name, "cache")
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, "auto_allow.lock")
        f_lock = open(lock_path, "w")
        fcntl.flock(f_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls = []
        class MockProc:
            returncode = 0
            stdout = '{"sha": "0123456789abcdef"}'
            stderr = ""
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return MockProc()
        try:
            with patch("subprocess.run", side_effect=fake_run):
                snap = rsa.collect_live_snapshot(reg_file, 1792758300.0, explicit_profile_home=self.temp_dir.name)
            self.assertEqual(snap["jobs"][0]["chat_rc"], 1)
            self.assertIn("LOCK_FAILED", snap["jobs"][0].get("chat_error", ""))
            self.assertFalse(any(c[0] == "synthetic-transport" for c in calls))
        finally:
            fcntl.flock(f_lock, fcntl.LOCK_UN)
            f_lock.close()

    def test_root_escape_reject(self):
        reg_file = os.path.join(self.temp_dir.name, "reg_escape.json")
        with open(reg_file, "w") as f:
            json.dump({
                "profile_home": self.temp_dir.name,
                "jobs": [{
                    "job_path": "../outside.json",
                    "pr_number": 99,
                    "job_id": "PR99_escape",
                }]
            }, f)
        with patch("subprocess.run", side_effect=AssertionError("no IO")):
            snap = rsa.collect_live_snapshot(reg_file, 1792758300.0, explicit_profile_home=self.temp_dir.name)
        self.assertEqual(len(snap["jobs"]), 1)
        self.assertEqual(snap["jobs"][0]["source_error"], "PATH_TRAVERSAL_REJECTED")

    def test_missing_job_kept(self):
        reg_file = os.path.join(self.temp_dir.name, "reg_missing.json")
        with open(reg_file, "w") as f:
            json.dump({
                "profile_home": self.temp_dir.name,
                "jobs": [{
                    "job_path": os.path.join(self.temp_dir.name, "nonexistent_job.json"),
                    "repository": "test-org/test-repo",
                    "branch": "main",
                    "instruction_id": "missing-inst-1",
                    "job_id": "PR99_missing-inst-1",
                    "pr_number": 99
                }]
            }, f)
        snap = rsa.collect_live_snapshot(reg_file, 1792758300.0, explicit_profile_home=self.temp_dir.name)
        self.assertEqual(len(snap["jobs"]), 1)
        j0 = snap["jobs"][0]
        self.assertEqual(j0["job"]["job_id"], "PR99_missing-inst-1")
        self.assertEqual(j0["source_error"], "MISSING_JOB_FILE")

    def test_prod_no_autoconsume(self):
        rsa.main(["produce-scan", "--snapshot", self.snapshot_file, "--state-dir", self.state_dir, "--now", "1792757400.0"])
        self.assertTrue(os.path.isfile(os.path.join(self.state_dir, "latest.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.state_dir, "pending.json")))
        # Producer must NOT auto-consume
        self.assertFalse(os.path.isfile(os.path.join(self.state_dir, "consumer_journal.jsonl")))
        self.assertFalse(os.path.isfile(os.path.join(self.state_dir, "consumer_runs.jsonl")))


if __name__ == "__main__":
    unittest.main()
