"""Tests for fail-closed relay operator dispatch."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
import relay_operator_entry as entry
import relay_resource_lock as lock


class FakeTransport:
    def __init__(self):
        self.state = dict(route_ok=True, composer_empty=True, busy=False,
                          auth_required=False, inline_approval_present=False)
        self.messages = []
        self.calls = 0
        self.fail = False

    def observe(self):
        return dict(self.state)

    def send(self, prompt):
        self.calls += 1
        if self.fail:
            raise RuntimeError("network uncertainty")
        self.messages.append(prompt)

    def user_messages(self):
        return list(self.messages)


class RelayEntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger = root / "ledger"
        self.cfg = dict(resource_id="browser", lock_dir=root / "locks",
                        owner_id="operator", lease_seconds=120)
        self.transport = FakeTransport()

    def dispatch(self, job="job1", prompt="exact prompt"):
        return entry.dispatch_once(job, prompt, self.cfg, self.transport, self.ledger)

    def record(self, job="job1"):
        return json.loads((self.ledger / (job + ".json")).read_text())

    def test_single_dispatch_success(self):
        self.assertEqual(self.dispatch(), "SENT")
        self.assertEqual(self.transport.calls, 1)
        self.assertTrue(self.record()["intent_recorded"])

    def test_duplicate_sent_never_resends(self):
        self.dispatch()
        self.assertEqual(self.dispatch(), "REFUSED_DUPLICATE")
        self.assertEqual(self.transport.calls, 1)

    def test_uncertain_not_resent(self):
        self.transport.fail = True
        self.assertEqual(self.dispatch(), "UNCERTAIN")
        self.assertEqual(self.dispatch(), "REFUSED_DUPLICATE")
        self.assertEqual(self.transport.calls, 1)

    def test_reconcile_exact_match(self):
        self.transport.fail = True
        self.dispatch()
        self.transport.messages.append("exact prompt")
        self.assertEqual(entry.reconcile("job1", self.transport, self.ledger), "SENT_CONFIRMED")
        self.assertEqual(self.transport.calls, 1)

    def test_reconcile_mismatch(self):
        self.transport.fail = True
        self.dispatch()
        self.transport.messages.append("different prompt")
        self.assertEqual(entry.reconcile("job1", self.transport, self.ledger), "UNCERTAIN")

    def _refuse(self, key, value, reason):
        self.transport.state[key] = value
        self.assertEqual(self.dispatch(), "REFUSED_PRE_SEND")
        self.assertEqual(self.record()["refusal_reason"], reason)
        self.assertFalse(self.record()["intent_recorded"])
        self.assertEqual(self.transport.calls, 0)

    def test_busy_refusal(self):
        self._refuse("busy", True, "busy")

    def test_draft_refusal(self):
        self._refuse("composer_empty", False, "draft_present")

    def test_auth_refusal(self):
        self._refuse("auth_required", True, "auth_required")

    def test_approval_refusal(self):
        self._refuse("inline_approval_present", True, "inline_approval_present")

    def test_route_refusal(self):
        self._refuse("route_ok", False, "wrong_route")

    def test_refusal_does_not_block_new_job(self):
        self.transport.state["busy"] = True
        self.dispatch()
        self.transport.state["busy"] = False
        self.assertEqual(self.dispatch("job2"), "SENT")
        self.assertEqual(self.record()["status"], "REFUSED_PRE_SEND")

    def test_live_resource_owner_refusal(self):
        lease = lock.acquire("browser", self.cfg["lock_dir"], "other", 120)
        try:
            self.assertEqual(self.dispatch(), "REFUSED_RESOURCE_LOCK")
            self.assertEqual(self.transport.calls, 0)
        finally:
            lock.release("browser", self.cfg["lock_dir"], "other", lease["token"])

    def test_stale_reply_rejected(self):
        self.assertFalse(entry.accept_reply(dict(job_id="j", nonce="n", timestamp=99), "j", "n", 100))

    def test_wrong_nonce_rejected(self):
        self.assertFalse(entry.accept_reply(dict(job_id="j", nonce="wrong", timestamp=101), "j", "n", 100))

    def test_wrong_job_rejected(self):
        self.assertFalse(entry.accept_reply(dict(job_id="other", nonce="n", timestamp=101), "j", "n", 100))

    def test_matching_reply_accepted(self):
        self.assertTrue(entry.accept_reply(dict(job_id="j", nonce="n", timestamp=101), "j", "n", 100))

    def test_observation_resource_owner(self):
        self.assertIsNone(entry.observe_prerequisites(self.cfg, self.transport)["resource_owner"])


if __name__ == "__main__":
    unittest.main()
