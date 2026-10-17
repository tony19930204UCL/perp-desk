import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engineering.workflow_pipeline.pipeline import acquire_lease, reconcile, normalize_item

class WorkflowPipelineTests(unittest.TestCase):
    def test_dead_worker(self): self.assertEqual(reconcile({"worker":"dead"}),"REPAIR")
    def test_supervisor_rc0_no_progress(self): self.assertEqual(reconcile({"worker":"alive"}),"OBSERVE")
    def test_ready_unowned(self): self.assertEqual(reconcile({}),"BLOCKED")
    def test_human_unblock_original_attempt(self):
        p=normalize_item({"id":"C0001","text":"x","state":"OPEN","created":1,"updated":2,"blocked_by":None})
        self.assertEqual(p["attempt"],1)
    def test_handback_before_observer_exits(self): self.assertEqual(reconcile({"worker":"alive","handback":True}),"ACCEPT")
    def test_wrong_instruction_preserved(self):
        p=normalize_item({"id":"C0002","text":"x","state":"OPEN","created":1,"updated":2},{"instruction":"wrong"})
        self.assertEqual(p["instruction"],"wrong")
    def test_ci_pending(self): self.assertEqual(reconcile({"ci":"pending"}),"WAIT_CI")
    def test_exact_head_reuse(self):
        p=normalize_item({"id":"C0003","text":"x","state":"DISPATCHED","created":1,"updated":2},{"source_head":"abc"})
        self.assertEqual(p["source_head"],"abc")
    def test_same_live_worker_soft_budget(self): self.assertEqual(reconcile({"worker":"alive","soft_budget":True}),"OBSERVE")
    def test_quota_loss(self): self.assertEqual(reconcile({"quota_lost":True}),"WAIT_AVAILABILITY")
    def test_unknown_send_no_retry(self): self.assertEqual(reconcile({"send":"unknown","worker":"dead"}),"OBSERVE_ONLY")
    def test_two_writers_lease_race(self):
        lease,_=acquire_lease("C1",1,"a",10,30,{})
        got,why=acquire_lease("C1",1,"b",11,30,{"C1:1":lease})
        self.assertIsNone(got); self.assertEqual(why,"LEASE_HELD")
