"""Offline terminal REVISE claim release tests; no network or browser."""
import datetime
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "engineering/relay/product_dispatch.py"
spec = importlib.util.spec_from_file_location("product_dispatch", SOURCE)
pd = importlib.util.module_from_spec(spec)
sys.modules["product_dispatch"] = pd
spec.loader.exec_module(pd)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.first = self.root / "job-a"
        self.second = self.root / "job-b"
        self.first.mkdir()
        self.second.mkdir()
        self.job = {
            "repository": "synthetic/example", "pr_number": 76,
            "branch": "offline", "dispatch_lock_root": str(self.root / "shared"),
            "dispatch_chat_url": "https://chatgpt.com/c/" + "a" * 12,
            "write_allowlist": ["test.py"],
            "valid_until": (datetime.datetime.now(datetime.timezone.utc) +
                            datetime.timedelta(hours=2)).isoformat()}
        self.instr = "r2"
        self.next_id = "r3"
        self._sync()

    def _sync(self, directory=None):
        (directory or self.first).joinpath("job.json").write_text(
            json.dumps(self.job), encoding="utf-8")

    def _claim(self, phase="SENT_CONFIRMED"):
        original = pd.claim(self.job, self.first, self.instr)
        if phase == "PREPARED":
            return original
        pd.phase(self.job, self.first, self.instr, "SEND_INTENT")
        if phase == "SEND_INTENT":
            return pd.inspect(self.job, self.first, self.instr)
        pd.phase(self.job, self.first, self.instr, "SEND_UNKNOWN")
        if phase == "SEND_UNKNOWN":
            return pd.inspect(self.job, self.first, self.instr)
        if phase == "BLOCKED":
            # A genuine BLOCKED state is reachable only before SEND_INTENT.
            raise AssertionError("use _blocked for BLOCKED")
        receipt = self.first / ("send_" + self.instr + ".json")
        receipt.write_text(json.dumps({
            "status": "CONFIRMED", "instruction_id": self.instr,
            "chat_url": self.job["dispatch_chat_url"],
            "evidence_kind": "EXACT_RENDERED_USER_BUBBLE",
            "text_hash": hashlib.sha256(b"synthetic").hexdigest()}))
        pd.phase(self.job, self.first, self.instr, "SENT_CONFIRMED")
        return pd.inspect(self.job, self.first, self.instr)

    def _verdict(self, original, **changes):
        verdict = {
            "schema_version": 1, "status": "REVISE",
            "reviewer": "synthetic-independent-reviewer",
            "repository": self.job["repository"], "pr_number": self.job["pr_number"],
            "branch": self.job["branch"], "released_instruction": self.instr,
            "next_instruction": self.next_id,
            "released_generation": original["generation"],
            "evidence": ["synthetic finite test failed"]}
        verdict.update(changes)
        path = self.root / "verdict.json"
        path.write_text(json.dumps(verdict), encoding="utf-8")
        return path

    def _grant(self, original, verdict):
        job = dict(self.job, dispatch_release_grant={
            "verdict_sha256": hashlib.sha256(verdict.read_bytes()).hexdigest(),
            "released_generation": original["generation"],
            "next_instruction": self.next_id})
        self.job = job
        self._sync()
        return job

    def _release(self, original, **changes):
        verdict = self._verdict(original, **changes)
        self._grant(original, verdict)
        return pd.release_revise(self.job, self.first, self.next_id, verdict)

    def _blocked(self, status):
        original = pd.claim(self.job, self.first, self.instr)
        pd.phase(self.job, self.first, self.instr, "BLOCKED")
        return pd.inspect(self.job, self.first, self.instr)

    def test_release_revise(self):
        original = self._claim()
        archived = self._release(original)
        self.assertEqual(archived["generation"], original["generation"])
        self.assertEqual(archived["scope"], original["scope"])
        self.assertEqual(archived["verdict"]["status"], "REVISE")
        self.assertFalse((Path(self.job["dispatch_lock_root"]) /
                          (pd._sha([self.job["repository"], 76]) + ".json")).exists())
        copies = list((Path(self.job["dispatch_lock_root"]) / "released").glob("*.json"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(json.loads(copies[0].read_text()), archived)
        winner = pd.claim(self.job, self.first, self.next_id)
        self.assertEqual(winner["scope"]["instruction"], self.next_id)

    def test_no_verdict(self):
        original = self._claim()
        missing = self.root / "missing.json"
        with self.assertRaisesRegex(pd.DispatchBlocked, "RELEASE_VERDICT_INVALID"):
            pd.release_revise(self.job, self.first, self.next_id, missing)
        self.assertEqual(pd.inspect(self.job, self.first, self.instr), original)

    def test_bad_status(self):
        for status in ("PASS", "FAIL", "BLOCKED"):
            with self.subTest(status=status):
                original = self._claim()
                with self.assertRaisesRegex(pd.DispatchBlocked, "RELEASE_NOT_REVISE"):
                    self._release(original, status=status)
                self.assertEqual(pd.inspect(self.job, self.first, self.instr), original)
                # Separate immutable storage for the next synthetic subcase.
                self.job["dispatch_lock_root"] = str(self.root / ("root-" + status))
                self._sync()

    def test_inflight_kept(self):
        for phase in ("PREPARED", "SEND_INTENT", "SEND_UNKNOWN", "BLOCKED"):
            with self.subTest(phase=phase):
                record = self._blocked(phase) if phase == "BLOCKED" else self._claim(phase)
                with self.assertRaisesRegex(pd.DispatchBlocked, "RELEASE_CLAIM_ACTIVE"):
                    self._release(record)
                self.assertEqual(pd.inspect(self.job, self.first, self.instr), record)
                self.job["dispatch_lock_root"] = str(self.root / ("root-" + phase))
                self._sync()
        pd.adopt_existing(self.job, self.first, self.instr, pd._identity())
        old = pd.inspect(self.job, self.first, self.instr)
        with self.assertRaisesRegex(pd.DispatchBlocked, "RELEASE_CLAIM_ACTIVE"):
            self._release(old)

    def test_card_pending(self):
        original = self._claim()
        card = dict(original)
        card["cards"] = {hashlib.sha256(b"card").hexdigest(): {
            "phase": "APPROVE_INTENT", "owner": pd._identity()}}
        root, key, _, _ = pd._scope(self.job, self.first, self.instr)
        pd._write(root / (key + ".json"), card)
        with self.assertRaisesRegex(pd.DispatchBlocked, "RELEASE_CLAIM_ACTIVE"):
            self._release(card)

    def test_wrong_instr(self):
        fields = [
            ("released_instruction", "other"), ("released_generation", "wrong"),
            ("next_instruction", "wrong"), ("repository", "other/repo"),
            ("pr_number", 1), ("branch", "other")]
        for field, value in fields:
            with self.subTest(field=field):
                original = self._claim()
                with self.assertRaisesRegex(pd.DispatchBlocked, "RELEASE_INSTR_MISMATCH"):
                    self._release(original, **{field: value})
                self.job["dispatch_lock_root"] = str(self.root / ("root-" + field))
                self._sync()

    def test_grant_mismatch(self):
        original = self._claim()
        verdict = self._verdict(original)
        with self.assertRaisesRegex(pd.DispatchBlocked, "RELEASE_GRANT_MISMATCH"):
            pd.release_revise(self.job, self.first, self.next_id, verdict)

    def test_claim_unchanged(self):
        self._claim()
        with self.assertRaisesRegex(pd.DispatchBlocked, "PRODUCT_ALREADY_CLAIMED"):
            pd.claim(self.job, self.second, self.next_id)
        with self.assertRaisesRegex(pd.DispatchBlocked, "SEND_ALREADY_INTENDED_NO_RETRY"):
            pd.claim(self.job, self.first, self.instr)
