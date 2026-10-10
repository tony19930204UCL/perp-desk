import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import relay_decision_ledger as ledger


class DecisionLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def record(self, head="head1", blob="blob1", status="REJECTED", evidence="failure"):
        return ledger.record_decision(self.root, "unit", head, blob, status, "reason", evidence)

    def test_immutable_idempotent(self):
        first = self.record()
        self.assertEqual(first, self.record())
        self.assertEqual(len(list(Path(self.root).rglob("*.json"))), 1)

    def test_conflicting_decision_refused(self):
        self.record()
        with self.assertRaises(ValueError):
            self.record(status="ACCEPTED")

    def test_accepted_advances(self):
        self.record(status="ACCEPTED")
        self.assertEqual(ledger.next_action(self.root, "unit", 2)["action"], "ADVANCE")

    def test_rejected_reworks_with_evidence(self):
        self.record()
        action = ledger.next_action(self.root, "unit", 2)
        self.assertEqual(action["action"], "REWORK")
        self.assertEqual(action["rejecting_evidence"], "failure")
        self.assertEqual(action["instruction_id"], ledger.rework_instruction_id("unit", 1))
        self.assertNotEqual(action["instruction_id"], ledger.rework_instruction_id("unit", 2))

    def test_revision_limit_stops(self):
        self.record()
        self.assertEqual(ledger.next_action(self.root, "unit", 0)["action"], "STOP_BLOCKED")

    def test_stale_head_refused(self):
        self.record()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.record(head="older", blob="older")

    def test_remote_reminder_inert(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "should-not-exist"
            reminder = "__import__('pathlib').Path(%r).touch()" % str(target)
            self.assertEqual(ledger.remote_reminder_data(reminder)["remote_reminder_text"], reminder)
            self.assertFalse(target.exists())

    def test_blocked_stops(self):
        self.record(status="BLOCKED")
        self.assertEqual(ledger.next_action(self.root, "unit", 3)["action"], "STOP_BLOCKED")

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            self.record(status="INVALID")
        with self.assertRaises(ValueError):
            ledger.rework_instruction_id("unit", 0)


if __name__ == "__main__":
    unittest.main()
