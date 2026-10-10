import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
import relay_cycle as cycle
import relay_resource_lock as locks


class Transport:
    def __init__(self):
        self.messages = []
        self.calls = 0
        self.busy = False
        self.fail = False

    def observe(self):
        return dict(route_ok=True, composer_empty=True, busy=self.busy,
                    auth_required=False, inline_approval_present=False)

    def send(self, prompt):
        self.calls += 1
        if self.fail:
            raise RuntimeError("send failed")
        self.messages.append(prompt)

    def user_messages(self):
        return list(self.messages)


class GitHub:
    def __init__(self):
        self.receipts = {}

    def read_receipt(self, unit):
        return self.receipts.get(unit["unit_id"])


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "ledger"
        self.locks = self.root / "locks"
        self.transport = Transport()
        self.github = GitHub()
        self.units = [
            dict(unit_id="a", prompt="do a", depends_on=[],
                 expected_receipt_validator_name="v", max_revisions=2),
            dict(unit_id="b", prompt="do b", depends_on=["a"],
                 expected_receipt_validator_name="v", max_revisions=2)]
        self.decision = "ACCEPTED"
        self.config = dict(resource_id="browser", owner_id="operator",
                           units=self.units, timeout_seconds=60,
                           decide=lambda u, r: dict(status=self.decision,
                                                    reason="review", evidence="evidence"))

    def run(self, now=None):
        return cycle.run_cycle(self.config, self.transport, self.github,
                               self.ledger, self.locks, now=now)

    def receipt(self, unit="a", head="head"):
        rec = cycle.describe_state(self.ledger)["dispatch"]
        self.github.receipts[unit] = dict(
            head_sha=head, blob_sha="blob-" + head,
            payload=dict(schema_version=2, unit_id=unit, nonce=rec["nonce"],
                         instruction_id=rec["job_id"], validator_name="v",
                         timestamp=rec["dispatched_at"] + 1,
                         next_step="echo NEVER_EXECUTE"))

    def test_chain_exactly_two_sends(self):
        self.assertEqual(self.run()["status"], "SENT")
        self.assertEqual(self.run()["status"], "WAITING")
        self.receipt()
        self.assertEqual(self.run()["status"], "SENT")
        self.receipt("b")
        self.assertEqual(self.run()["status"], "ALL_DONE")
        self.assertEqual(self.run()["status"], "ALL_DONE")
        self.assertEqual(self.transport.calls, 2)

    def test_rejected_rework_then_accept(self):
        self.run()
        first = cycle.describe_state(self.ledger)["dispatch"]["job_id"]
        self.receipt(head="headA")
        self.decision = "REJECTED"
        self.assertEqual(self.run()["status"], "SENT")
        second = cycle.describe_state(self.ledger)["dispatch"]["job_id"]
        self.assertNotEqual(first, second)
        self.receipt(head="headB")
        self.decision = "ACCEPTED"
        self.assertEqual(self.run()["status"], "SENT")
        self.assertEqual(self.transport.calls, 3)

    def test_cycle_mutex_busy(self):
        lease = locks.acquire("browser-cycle", self.locks, "other", 120)
        try:
            self.assertEqual(self.run()["status"], "BUSY")
            self.assertEqual(self.transport.calls, 0)
        finally:
            locks.release("browser-cycle", self.locks, "other", lease["token"])

    def test_browser_lock_only_deferred(self):
        lease = locks.acquire("browser", self.locks, "other", 120)
        try:
            self.assertEqual(self.run()["status"], "DEFERRED")
            self.assertEqual(self.transport.calls, 0)
        finally:
            locks.release("browser", self.locks, "other", lease["token"])
        self.assertEqual(self.run()["status"], "SENT")

    def test_same_resource_rejected(self):
        self.config["cycle_resource_id"] = "browser"
        with self.assertRaises(ValueError):
            self.run()

    def test_expiry_terminal(self):
        self.run()
        sent_at = cycle.describe_state(self.ledger)["dispatch"]["dispatched_at"]
        self.assertEqual(self.run(now=sent_at + 20)["status"], "WAITING")
        self.assertEqual(self.run(now=sent_at + 61)["status"], "EXPIRED_NEEDS_OWNER")
        self.assertEqual(self.run()["status"], "EXPIRED_NEEDS_OWNER")
        self.assertEqual(self.transport.calls, 1)

    def test_invalid_receipts(self):
        for mutation in ("nonce", "unit_id", "timestamp", "schema_version"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as tmp:
                    self.ledger = Path(tmp) / "ledger"
                    self.run()
                    self.receipt()
                    payload = self.github.receipts["a"]["payload"]
                    payload[mutation] = (0 if mutation in ("timestamp", "schema_version")
                                         else "wrong")
                    self.assertEqual(self.run()["status"], "STOPPED_NEEDS_OWNER")

    def test_uncertain_never_resends(self):
        self.transport.fail = True
        self.assertEqual(self.run()["status"], "UNCERTAIN_NEEDS_OWNER")
        for _ in range(3):
            self.assertEqual(self.run()["status"], "UNCERTAIN_NEEDS_OWNER")
        self.assertEqual(self.transport.calls, 1)

    def test_presend_deferred_retry(self):
        self.transport.busy = True
        self.assertEqual(self.run()["status"], "DEFERRED")
        self.transport.busy = False
        self.assertEqual(self.run()["status"], "SENT")
        self.assertEqual(self.transport.calls, 1)

    def test_crash_after_intent_no_retry(self):
        original = cycle.entry.dispatch_once
        def crash(*args):
            raise RuntimeError("simulated crash")
        cycle.entry.dispatch_once = crash
        try:
            with self.assertRaises(RuntimeError):
                self.run()
        finally:
            cycle.entry.dispatch_once = original
        self.assertEqual(self.run()["status"], "UNCERTAIN_NEEDS_OWNER")
        self.assertEqual(self.transport.calls, 0)

    def test_revision_limit(self):
        self.units[0]["max_revisions"] = 0
        self.run()
        self.receipt()
        self.decision = "REJECTED"
        self.assertEqual(self.run()["status"], "STOPPED_NEEDS_OWNER")
        self.assertEqual(self.transport.calls, 1)

    def test_decider_exception_releases_mutex(self):
        self.run()
        self.receipt()
        self.config["decide"] = lambda u, r: (_ for _ in ()).throw(RuntimeError("decide"))
        with self.assertRaises(RuntimeError):
            self.run()
        self.assertFalse(locks.inspect("browser-cycle", self.locks)["live"])

    def test_remote_text_inert(self):
        self.run()
        target = self.root / "executed"
        self.receipt()
        self.github.receipts["a"]["payload"]["next_step"] = "touch " + str(target)
        self.run()
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
