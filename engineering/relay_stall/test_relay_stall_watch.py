#!/usr/bin/env python3
"""test_relay_stall_watch.py - Isolated unittests for observe-only stall detector."""

import io
import json
import os
import sys
import tempfile
import unittest

import relay_stall_watch as rsw

# Ensure all temp files stay strictly within the authorized worktree
WORKTREE_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(WORKTREE_DIR, "operator-review", "fixture-temp")
os.makedirs(FIXTURE_DIR, exist_ok=True)
tempfile.tempdir = FIXTURE_DIR


def make_valid_input(
    job_overrides: dict | None = None,
    consumer_overrides: dict | None = None,
    now: float = 1700000000.0,
) -> dict:
    consumer = {"enabled": True, "last_receipt_at": now - 100.0}
    if consumer_overrides:
        consumer.update(consumer_overrides)

    job = {
        "repository": "repo-test",
        "branch": "main",
        "instruction_id": "inst-1",
        "job_id": "job-1",
        "expires_at": now + 3600.0,
        "owner": {
            "kind": "pid",
            "pid": 12345,
            "start_id": "proc-uuid",
            "alive": True,
            "identity_ok": True,
        },
        "head": "sha-head-001",
        "chat": {
            "observed_at": now - 30.0,
            "pending_card": False,
            "busy": True,
        },
        "send_status": "CONFIRMED",
        "handback_valid": True,
        "policy_refusal": False,
    }
    if job_overrides:
        job.update(job_overrides)

    return {"schema_version": 1, "consumer": consumer, "jobs": [job]}


class TestRelayStallWatch(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=FIXTURE_DIR)
        self.state_dir = os.path.join(self.temp_dir.name, "state")
        self.input_file = os.path.join(self.temp_dir.name, "normalized.json")

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        old_out, old_err = sys.stdout, sys.stderr
        s_out, s_err = io.StringIO(), io.StringIO()
        sys.stdout, sys.stderr = s_out, s_err
        try:
            code = rsw.main(argv)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return code, s_out.getvalue(), s_err.getvalue()

    def write_input(self, data: dict):
        with open(self.input_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_pending_boundary(self):
        t0 = 1000.0
        data1 = make_valid_input({"chat": {"observed_at": t0, "pending_card": True, "busy": False}}, now=t0)
        self.write_input(data1)
        code, out, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(pending, [])

        t_sub = t0 + 299.0
        data2 = make_valid_input({"chat": {"observed_at": t_sub, "pending_card": True, "busy": False}}, now=t_sub)
        self.write_input(data2)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t_sub)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(pending, [])

        t_exact = t0 + 300.0
        data3 = make_valid_input({"chat": {"observed_at": t_exact, "pending_card": True, "busy": False}}, now=t_exact)
        self.write_input(data3)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t_exact)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertIn(rsw.ST_WAIT_APPR, pending[0]["states"])

    def test_expiry_unchanged(self):
        t0 = 2000.0
        orig_exp = 1500.0
        data = make_valid_input({"expires_at": orig_exp}, now=t0)
        self.write_input(data)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertIn(rsw.ST_EXPIRED, pending[0]["states"])
        self.assertEqual(pending[0]["original_expires_at"], orig_exp)

        with open(self.input_file, "r") as f:
            disk_data = json.load(f)
        self.assertEqual(disk_data["jobs"][0]["expires_at"], orig_exp)

    def test_dead_pid_owner(self):
        t0 = 1000.0
        data = make_valid_input({"owner": {"kind": "pid", "pid": 999, "start_id": "init", "alive": False, "identity_ok": True}}, now=t0)
        self.write_input(data)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertIn(rsw.ST_OWNER_DEAD, pending[0]["states"])

    def test_reused_start_id(self):
        t0 = 1000.0
        data = make_valid_input({"owner": {"kind": "pid", "pid": 999, "start_id": "new_start", "alive": True, "identity_ok": False}}, now=t0)
        self.write_input(data)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertIn(rsw.ST_OWNER_DEAD, pending[0]["states"])

    def test_send_unknown_kept(self):
        t0 = 1000.0
        data = make_valid_input({"send_status": "SEND_UNKNOWN"}, now=t0)
        self.write_input(data)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertIn(rsw.ST_UNKNOWN, pending[0]["states"])
        self.assertIn("send_unknown", pending[0]["reasons"])

    def test_no_resend_surfs(self):
        forbidden = ["send", "resend", "retry", "dispatch", "click", "execute", "approve"]
        for name in dir(rsw):
            for f in forbidden:
                self.assertNotIn(f, name.lower())

        t0 = 1000.0
        data = make_valid_input({"send_status": "SEND_UNKNOWN"}, now=t0)
        self.write_input(data)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(pending[0]["disposition"], rsw.DISP_REVIEW)

    def test_dedup_repeats(self):
        t0 = 1000.0
        data = make_valid_input({"send_status": "SEND_UNKNOWN"}, now=t0)
        self.write_input(data)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p1 = json.load(f)
        ev_id_1 = p1[0]["event_id"]

        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 50)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p2 = json.load(f)
        self.assertEqual(len(p2), 1)
        self.assertEqual(p2[0]["event_id"], ev_id_1)
        self.assertEqual(p2[0]["first_seen"], t0)
        self.assertEqual(p2[0]["last_seen"], t0 + 50)

        with open(os.path.join(self.state_dir, "events.jsonl")) as f:
            lines = [l.strip() for l in f if l.strip()]
        self.assertEqual(len(lines), 2)

    def test_new_fault_event(self):
        t0 = 1000.0
        d1 = make_valid_input({"send_status": "SEND_UNKNOWN"}, now=t0)
        self.write_input(d1)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p1 = json.load(f)
        ev1 = p1[0]["event_id"]

        d2 = make_valid_input({"send_status": "SEND_UNKNOWN", "expires_at": t0 - 10}, now=t0 + 10)
        self.write_input(d2)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 10)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p2 = json.load(f)
        ev_ids = [p["event_id"] for p in p2]
        self.assertNotIn(ev1, ev_ids)

    def test_head_only_clock(self):
        t0 = 1000.0
        d1 = make_valid_input({"head": "commit-AAA"}, now=t0)
        self.write_input(d1)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "state.json")) as f:
            st1 = json.load(f)
        job_st = list(st1["jobs"].values())[0]
        self.assertEqual(job_st["head_seen_at"], t0)

        d2 = make_valid_input({"head": "commit-AAA"}, now=t0 + 500)
        self.write_input(d2)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 500)])
        with open(os.path.join(self.state_dir, "state.json")) as f:
            st2 = json.load(f)
        job_st = list(st2["jobs"].values())[0]
        self.assertEqual(job_st["head_seen_at"], t0)

        d3 = make_valid_input({"head": "commit-BBB"}, now=t0 + 600)
        self.write_input(d3)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 600)])
        with open(os.path.join(self.state_dir, "state.json")) as f:
            st3 = json.load(f)
        job_st = list(st3["jobs"].values())[0]
        self.assertEqual(job_st["head_seen_at"], t0 + 600)

    def test_missing_input_unk(self):
        t0 = 1000.0
        missing_path = os.path.join(self.temp_dir.name, "nonexistent.json")
        code, out, _ = self.run_cli(["--input", missing_path, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertIn(rsw.ST_UNKNOWN, pending[0]["states"])

    def test_stale_input_unk(self):
        t0 = 1000.0
        d = make_valid_input({"chat": {"observed_at": t0 - 601.0, "pending_card": False, "busy": False}}, now=t0)
        self.write_input(d)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertIn(rsw.ST_UNKNOWN, pending[0]["states"])
        self.assertIn("stale_chat", pending[0]["reasons"])

    def test_genuine_stalled(self):
        t0 = 1000.0
        d1 = make_valid_input(
            {"head": "H1", "handback_valid": False, "chat": {"observed_at": t0, "pending_card": False, "busy": False}},
            now=t0,
        )
        self.write_input(d1)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(pending, [])

        t_stall = t0 + 1200.0
        d2 = make_valid_input(
            {"head": "H1", "handback_valid": False, "chat": {"observed_at": t_stall, "pending_card": False, "busy": False}},
            now=t_stall,
        )
        self.write_input(d2)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t_stall)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertIn(rsw.ST_STALLED, pending[0]["states"])

    def test_active_not_stall(self):
        t0 = 1000.0
        t_stall = t0 + 1300.0

        d1 = make_valid_input(
            {"head": "H1", "handback_valid": False, "chat": {"observed_at": t_stall, "pending_card": False, "busy": True}},
            now=t_stall,
        )
        self.write_input(d1)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t_stall)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p1 = json.load(f)
        self.assertEqual(p1, [])

        d2 = make_valid_input(
            {"head": "H1", "handback_valid": True, "chat": {"observed_at": t_stall, "pending_card": False, "busy": False}},
            now=t_stall,
        )
        self.write_input(d2)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t_stall)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p2 = json.load(f)
        self.assertEqual(p2, [])

    def test_consumer_notify(self):
        t0 = 2000.0
        data = make_valid_input(consumer_overrides={"last_receipt_at": 700.0}, now=t0)
        self.write_input(data)
        code, out, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        self.assertIn("human_notify", out)
        self.assertIn("consumer_stale", out)

    def test_consumer_dedup(self):
        t0 = 2000.0
        data = make_valid_input(consumer_overrides={"last_receipt_at": 700.0}, now=t0)
        self.write_input(data)
        _, out1, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertIn("human_notify", out1)

        _, out2, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 10)])
        self.assertNotIn("human_notify", out2)

    def test_ack_roundtrip(self):
        t0 = 1000.0
        data = make_valid_input({"send_status": "SEND_UNKNOWN", "receipt_ref": "immutable:orig"}, now=t0)
        self.write_input(data)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p = json.load(f)
        ev_id = p[0]["event_id"]

        code, out, _ = self.run_cli(["ack", "--state-dir", self.state_dir, "--event-id", ev_id, "--action", "read_and_logged", "--now", str(t0 + 5)])
        self.assertEqual(code, 0)
        self.assertIn("ACK_RECORDED", out)

        with open(os.path.join(self.state_dir, "receipts.jsonl")) as f:
            rcpts = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(rcpts), 1)
        self.assertEqual(rcpts[0]["event_id"], ev_id)
        self.assertEqual(rcpts[0]["action"], "read_and_logged")

        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p_after = json.load(f)
        self.assertEqual(len(p_after), 1)
        self.assertEqual(p_after[0]["event_id"], ev_id)
        self.assertEqual(p_after[0]["receipt_ref"], "immutable:orig")
        self.assertEqual(p_after[0]["ack_receipt_id"], rcpts[0]["receipt_id"])
        self.assertEqual(p_after[0]["consumer_receipt_ref"], rcpts[0]["receipt_id"])

    def test_resolve_reappear(self):
        t0 = 1000.0
        d1 = make_valid_input({"send_status": "SEND_UNKNOWN"}, now=t0)
        self.write_input(d1)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p1 = json.load(f)
        self.assertEqual(len(p1), 1)

        d2 = make_valid_input(now=t0 + 10)
        self.write_input(d2)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 10)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p2 = json.load(f)
        self.assertEqual(p2, [])

        d3 = make_valid_input({"send_status": "SEND_UNKNOWN"}, now=t0 + 20)
        self.write_input(d3)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 20)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p3 = json.load(f)
        self.assertEqual(len(p3), 1)
        self.assertEqual(p3[0]["first_seen"], t0 + 20)

    def test_policy_refusal(self):
        t0 = 1000.0
        data = make_valid_input({"policy_refusal": True}, now=t0)
        self.write_input(data)
        code, out, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        self.assertIn("human_notify", out)
        self.assertIn("policy_refusal", out)

        with open(os.path.join(self.state_dir, "pending.json")) as f:
            pending = json.load(f)
        self.assertEqual(len(pending), 1)
        self.assertIn(rsw.ST_REFUSAL, pending[0]["states"])
        self.assertEqual(pending[0]["internal_action"], rsw.ACT_PRESERVE)

    def test_persist_error(self):
        bad_dir = os.path.join(self.temp_dir.name, "bad_dir_file")
        with open(bad_dir, "w") as f:
            f.write("not a directory")
        t0 = 1000.0
        data = make_valid_input(now=t0)
        self.write_input(data)
        code, _, err = self.run_cli(["--input", self.input_file, "--state-dir", bad_dir, "--now", str(t0)])
        self.assertNotEqual(code, 0)
        self.assertIn("Relay stall watch error", err)

    # ----------------- REVISION REGRESSION TESTS -----------------

    def test_missing_handback(self):
        t0 = 1000.0
        d = make_valid_input(now=t0)
        del d["jobs"][0]["handback_valid"]
        self.write_input(d)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p = json.load(f)
        self.assertEqual(len(p), 1)
        self.assertIn(rsw.ST_UNKNOWN, p[0]["states"])
        self.assertIn("invalid_handback", p[0]["reasons"])

    def test_missing_refusal(self):
        t0 = 1000.0
        d = make_valid_input(now=t0)
        del d["jobs"][0]["policy_refusal"]
        self.write_input(d)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p = json.load(f)
        self.assertEqual(len(p), 1)
        self.assertIn(rsw.ST_UNKNOWN, p[0]["states"])
        self.assertIn("invalid_refusal", p[0]["reasons"])

    def test_original_receipt(self):
        t0 = 1000.0
        d = make_valid_input({"send_status": "SEND_UNKNOWN", "receipt_ref": "immutable:original"}, now=t0)
        self.write_input(d)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p = json.load(f)
        self.assertEqual(len(p), 1)
        self.assertEqual(p[0]["receipt_ref"], "immutable:original")

    def test_absent_job_kept(self):
        t0 = 1000.0
        d1 = make_valid_input({"send_status": "SEND_UNKNOWN", "receipt_ref": "immutable:original"}, now=t0)
        self.write_input(d1)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p1 = json.load(f)
        self.assertEqual(len(p1), 1)
        ev_id = p1[0]["event_id"]

        # Empty jobs: absent job must NOT be cleared, but preserved as UNKNOWN with same event_id
        d2 = {"schema_version": 1, "consumer": {"enabled": True, "last_receipt_at": t0 + 10}, "jobs": []}
        self.write_input(d2)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 10)])
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p2 = json.load(f)
        self.assertEqual(len(p2), 1)
        self.assertEqual(p2[0]["event_id"], ev_id)
        self.assertIn(rsw.ST_UNKNOWN, p2[0]["states"])
        self.assertIn("absent_from_input", p2[0]["reasons"])
        self.assertEqual(p2[0]["receipt_ref"], "immutable:original")

    def test_corrupt_state(self):
        t0 = 1000.0
        d = make_valid_input(now=t0)
        self.write_input(d)
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, "state.json"), "w") as f:
            f.write("{broken")
        code, _, err = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertNotEqual(code, 0)
        self.assertIn("Relay stall watch error", err)

    def test_bool_epoch_pid(self):
        t0 = 1000.0
        d = make_valid_input({"expires_at": True, "owner": {"kind": "pid", "pid": True, "start_id": "s", "alive": True, "identity_ok": True}}, now=t0)
        self.write_input(d)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p = json.load(f)
        self.assertEqual(len(p), 1)
        self.assertIn(rsw.ST_UNKNOWN, p[0]["states"])

    def test_future_consumer(self):
        t0 = 1000.0
        d = make_valid_input(consumer_overrides={"last_receipt_at": t0 + 100.0}, now=t0)
        self.write_input(d)
        code, out, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        self.assertIn("human_notify", out)
        self.assertIn("consumer_future_receipt", out)

    def test_empty_head_unk(self):
        t0 = 1000.0
        d = make_valid_input({"head": "   "}, now=t0)
        self.write_input(d)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        with open(os.path.join(self.state_dir, "pending.json")) as f:
            p = json.load(f)
        self.assertEqual(len(p), 1)
        self.assertIn(rsw.ST_UNKNOWN, p[0]["states"])
        self.assertIn("invalid_head", p[0]["reasons"])

    def test_nonfinite_now(self):
        d = make_valid_input()
        self.write_input(d)
        code, _, err = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", "nan"])
        self.assertNotEqual(code, 0)
        self.assertIn("now must be finite epoch", err)

    def test_healthy_round(self):
        t0 = 1000.0
        d = make_valid_input(now=t0)
        self.write_input(d)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        self.assertEqual(code, 0)
        events_f = os.path.join(self.state_dir, "events.jsonl")
        self.assertTrue(os.path.isfile(events_f))
        with open(events_f) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].get("event_type"), "round_healthy")

    def test_unknown_event_ack(self):
        os.makedirs(self.state_dir, exist_ok=True)
        pending_f = os.path.join(self.state_dir, "pending.json")
        with open(pending_f, "w") as f:
            json.dump([], f)
        code, _, err = self.run_cli(["ack", "--state-dir", self.state_dir, "--event-id", "non_existent_id", "--action", "logged", "--now", "1000"])
        self.assertNotEqual(code, 0)
        self.assertIn("Event non_existent_id not found in pending", err)

    def test_empty_state_err(self):
        t0 = 1000.0
        d = make_valid_input(now=t0)
        self.write_input(d)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        # Emptied state.json in initialized dir must fail-open
        with open(os.path.join(self.state_dir, "state.json"), "w") as f:
            f.write("{}")
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 10)])
        self.assertNotEqual(code, 0)

    def test_missing_state_err(self):
        t0 = 1000.0
        d = make_valid_input(now=t0)
        self.write_input(d)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        # Missing state.json in initialized dir must fail-open
        os.unlink(os.path.join(self.state_dir, "state.json"))
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 10)])
        self.assertNotEqual(code, 0)

    def test_missing_pend_err(self):
        t0 = 1000.0
        d = make_valid_input(now=t0)
        self.write_input(d)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        # Missing pending.json in initialized dir must fail-open
        os.unlink(os.path.join(self.state_dir, "pending.json"))
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 10)])
        self.assertNotEqual(code, 0)

    def test_invalid_pend_entry(self):
        t0 = 1000.0
        d = make_valid_input(now=t0)
        self.write_input(d)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        # Invalid entry in pending.json must fail-open
        with open(os.path.join(self.state_dir, "pending.json"), "w") as f:
            json.dump([{}], f)
        code, _, _ = self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 10)])
        self.assertNotEqual(code, 0)

    def test_chat_clock_kept(self):
        t0 = 1000.0
        # Round 1 at t=1000: pending card true
        d1 = make_valid_input({"chat": {"observed_at": t0, "pending_card": True, "busy": False}}, now=t0)
        self.write_input(d1)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "state.json")) as f:
            st1 = json.load(f)
        key = list(st1["jobs"].keys())[0]
        self.assertEqual(st1["jobs"][key]["pending_since"], t0)

        # Round 2: chat is missing from input, pending_since MUST NOT be cleared
        d2 = make_valid_input(now=t0 + 50)
        del d2["jobs"][0]["chat"]
        self.write_input(d2)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 50)])
        with open(os.path.join(self.state_dir, "state.json")) as f:
            st2 = json.load(f)
        self.assertEqual(st2["jobs"][key]["pending_since"], t0)

    def test_head_clk_kept(self):
        t0 = 1000.0
        # Round 1: seen with head "h" at 1000
        d1 = make_valid_input({"head": "h"}, now=t0)
        self.write_input(d1)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0)])
        with open(os.path.join(self.state_dir, "state.json")) as f:
            st1 = json.load(f)
        key = list(st1["jobs"].keys())[0]
        self.assertEqual(st1["jobs"][key]["head_seen_at"], t0)

        # Round 2 at 1300: head missing from input -> classified UNKNOWN, preserves head/clock
        d2 = make_valid_input(now=t0 + 300)
        del d2["jobs"][0]["head"]
        self.write_input(d2)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 300)])
        with open(os.path.join(self.state_dir, "state.json")) as f:
            st2 = json.load(f)
        self.assertEqual(st2["jobs"][key]["head_seen_at"], t0)

        # Round 3 at 1400: head "h" present again -> head_seen_at must STILL be 1000
        d3 = make_valid_input({"head": "h"}, now=t0 + 400)
        self.write_input(d3)
        self.run_cli(["--input", self.input_file, "--state-dir", self.state_dir, "--now", str(t0 + 400)])
        with open(os.path.join(self.state_dir, "state.json")) as f:
            st3 = json.load(f)
        self.assertEqual(st3["jobs"][key]["head_seen_at"], t0)


if __name__ == "__main__":
    unittest.main()
