import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LAB=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(LAB))
sys.path.insert(0,str(LAB/'tests'))

from operator_event_gate import Gate,GateError,load_config,scheduler_control,FALSE_BYTES
from operator_owner_handoff import deliver_once
from test_operator_event_gate import GateFixture,iso


class OwnerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory()
        self.f=GateFixture(self.td.name)
        cfg=json.loads(self.f.config.read_text())
        cfg['owner_handoff']={
            'enabled':True,
            'target':'fixture-owner-target',
            'delivery_lease_seconds':30,
            'owner_execution_lease_seconds':120,
            'retry_backoff_seconds':[60,120],
            'max_attempts':3,
        }
        self.f.config.write_text(json.dumps(cfg))

    def tearDown(self):
        self.td.cleanup()

    def _ready_work(self,task_id='analysis'):
        self.f.work.write_text(json.dumps({'schema_version':1,'tasks':[{
            'id':task_id,'state':'queued','updated_at':iso(self.f.now),'started_at':iso(self.f.now),
            'title':'portable synthetic analysis','current_step':'queued',
            'next_step':'read-only analysis','evidence':[]
        }]}))

    def _policy_block(self,task_id='analysis'):
        self._ready_work(task_id)
        wake=json.loads(scheduler_control(self.f.config,now_ms=self.f.now,process_count=1))
        self.assertTrue(wake['wakeAgent'])
        g=Gate(load_config(self.f.config),now_ms=self.f.now,process_count=1)
        token=wake['context']['claim_token'];handle='cron-worker:fixture'
        g.worker_adopt(token,handle)
        g.worker_finish(token,outcome='blocked',worker_handle=handle,
                        evidence_ref='evidence:approval-denial',
                        reason='approval_denied_unattended',
                        block_class='policy_capability')
        return wake['context']['event_id']

    def _deliver(self,event_id,now=None):
        now=self.f.now if now is None else now
        result=deliver_once(self.f.config,now_ms=now,
            transport=lambda _target,_message:(True,'fixture-delivery:accepted',None))
        self.assertTrue(result['attempted']);self.assertTrue(result['success'])
        self.assertEqual(result['event_id'],event_id)

    def _record(self,event_id):
        return json.loads((self.f.ns/'gate_state.json').read_text())['records'][event_id]

    def test_policy_denial_becomes_awaiting_owner_not_completed(self):
        event_id=self._policy_block()
        rec=self._record(event_id)
        self.assertEqual(rec['status'],'awaiting_owner')
        self.assertEqual(rec['blocked']['block_class'],'policy_capability')
        self.assertEqual(rec['handoff']['state'],'pending')
        self.assertIsNone(rec['completion'])
        states=[x['state'] for x in rec['lifecycle']]
        self.assertIn('blocked',states);self.assertIn('awaiting_owner',states)

    def test_external_prerequisite_block_stays_terminal_and_has_no_owner_escalation(self):
        self._ready_work('external')
        wake=json.loads(scheduler_control(self.f.config,now_ms=self.f.now,process_count=1))
        g=Gate(load_config(self.f.config),now_ms=self.f.now,process_count=1)
        token=wake['context']['claim_token'];g.worker_adopt(token,'cron-worker:fixture')
        event_id=g.worker_finish(token,outcome='blocked',worker_handle='cron-worker:fixture',
                                 evidence_ref='evidence:external-prereq',
                                 reason='awaiting external research prerequisite',
                                 block_class='external_prerequisite')
        rec=self._record(event_id)
        self.assertEqual(rec['status'],'blocked')
        self.assertEqual(rec['blocked']['block_class'],'external_prerequisite')
        self.assertIsNone(g.claim_owner_escalation())

    def test_delivery_is_not_owner_receipt_execution_or_completion(self):
        event_id=self._policy_block()
        self._deliver(event_id)
        rec=self._record(event_id)
        self.assertEqual(rec['status'],'awaiting_owner')
        self.assertEqual(rec['handoff']['state'],'delivered')
        self.assertIsNone(rec['handoff']['owner_receipt'])
        self.assertIsNone(rec['handoff']['execution'])
        self.assertIsNone(rec['completion'])
        # Stable delivered lifecycle is deduplicated; no second send claim.
        g=Gate(load_config(self.f.config),now_ms=self.f.now+60_000,process_count=1)
        self.assertIsNone(g.claim_owner_escalation())

    def test_owner_receipt_without_execution_remains_non_active(self):
        event_id=self._policy_block();self._deliver(event_id)
        g=Gate(load_config(self.f.config),now_ms=self.f.now+1_000,process_count=1)
        g.owner_receive(event_id,'owner-receipt:fixture')
        # A touched/running task source is not proof of owner execution.
        work=json.loads(self.f.work.read_text());work['tasks'][0]['state']='running'
        work['tasks'][0]['updated_at']=iso(self.f.now+2_000);self.f.work.write_text(json.dumps(work))
        scheduler_control(self.f.config,now_ms=self.f.now+2_000,process_count=1)
        rec=self._record(event_id)
        self.assertEqual(rec['status'],'owner_received')
        self.assertIsNone(rec['handoff']['execution'])
        self.assertIsNone(rec['completion'])

    def test_executing_requires_owner_handle_and_evidence_and_interruption_reverts(self):
        event_id=self._policy_block();self._deliver(event_id)
        g=Gate(load_config(self.f.config),now_ms=self.f.now+1_000,process_count=1)
        g.owner_receive(event_id,'owner-receipt:fixture')
        with self.assertRaises(GateError):
            g.owner_start(event_id,'owner:interactive','/private/path')
        g.owner_start(event_id,'owner:interactive','evidence:owner-analysis-start')
        rec=self._record(event_id)
        self.assertEqual(rec['status'],'executing')
        self.assertEqual(rec['handoff']['execution']['owner_handle'],'owner:interactive')
        # Execution lease expiry means no verified executor remains.
        self.f.refresh_observation_sources(self.f.now+122_000)
        scheduler_control(self.f.config,now_ms=self.f.now+122_000,process_count=1)
        rec=self._record(event_id)
        self.assertEqual(rec['status'],'owner_received')
        self.assertIsNone(rec['handoff']['execution'])
        self.assertIn('owner_execution_interrupted',[x['state'] for x in rec['lifecycle']])

    def test_owner_terminal_result_requires_authoritative_work_completion(self):
        event_id=self._policy_block();self._deliver(event_id)
        g=Gate(load_config(self.f.config),now_ms=self.f.now+1_000,process_count=1)
        g.owner_receive(event_id,'owner-receipt:fixture')
        g.owner_start(event_id,'owner:interactive','evidence:owner-analysis-start')
        with self.assertRaisesRegex(GateError,'still unfinished'):
            g.owner_complete(event_id,'owner:interactive','evidence:analysis-result')
        work=json.loads(self.f.work.read_text());work['tasks'][0]['state']='completed'
        work['tasks'][0]['updated_at']=iso(self.f.now+2_000);self.f.work.write_text(json.dumps(work))
        g=Gate(load_config(self.f.config),now_ms=self.f.now+2_000,process_count=1)
        with self.assertRaises(GateError):
            g.owner_complete(event_id,'owner:wrong','evidence:analysis-result')
        g.owner_complete(event_id,'owner:interactive','evidence:analysis-result')
        rec=self._record(event_id)
        self.assertEqual(rec['status'],'completed')
        self.assertEqual(rec['completion']['worker_handle'],'owner:interactive')
        self.assertEqual(rec['completion']['evidence_ref'],'evidence:analysis-result')

    def test_delivery_claim_interruption_and_failure_retry_are_bounded(self):
        event_id=self._policy_block()
        g=Gate(load_config(self.f.config),now_ms=self.f.now,process_count=1)
        first=g.claim_owner_escalation();self.assertIsNotNone(first)
        self.assertIsNone(g.claim_owner_escalation(),'single delivery claim must dedupe')
        # Claim expires, then bounded backoff applies.
        g=Gate(load_config(self.f.config),now_ms=self.f.now+31_000,process_count=1)
        self.assertIsNone(g.claim_owner_escalation())
        g=Gate(load_config(self.f.config),now_ms=self.f.now+91_000,process_count=1)
        second=g.claim_owner_escalation();self.assertIsNotNone(second)
        g.finish_owner_escalation(second['escalation_token'],success=False,failure_kind='notification')
        g=Gate(load_config(self.f.config),now_ms=self.f.now+151_000,process_count=1)
        third=g.claim_owner_escalation();self.assertIsNotNone(third)
        g.finish_owner_escalation(third['escalation_token'],success=False,failure_kind='notification')
        rec=self._record(event_id)
        self.assertEqual(rec['status'],'awaiting_owner')
        self.assertEqual(rec['handoff']['state'],'delivery_blocked')
        self.assertEqual(rec['handoff']['attempts'],3)
        self.assertIsNone(Gate(load_config(self.f.config),now_ms=self.f.now+999_000,process_count=1).claim_owner_escalation())

    def test_subprocess_escalation_adapter_is_public_safe_and_not_completion(self):
        event_id=self._policy_block()
        cmd=[sys.executable,str(LAB/'operator_owner_handoff.py'),'--config',str(self.f.config),
             '--engineering-fixture','--fixture-result','success','--now-ms',str(self.f.now)]
        p=subprocess.run(cmd,cwd=LAB,capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stderr)
        out=json.loads(p.stdout);self.assertTrue(out['attempted']);self.assertTrue(out['success'])
        self.assertEqual(out['event_id'],event_id)
        self.assertNotIn(str(self.f.base),p.stdout)
        rec=self._record(event_id)
        self.assertEqual(rec['status'],'awaiting_owner')
        self.assertEqual(rec['handoff']['state'],'delivered')
        self.assertIsNone(rec['completion'])

    def test_normal_ticks_still_zero_model_with_owner_handoff_enabled(self):
        first=scheduler_control(self.f.config,now_ms=self.f.now,process_count=1)
        self.assertEqual(first,FALSE_BYTES)
        state1=(self.f.ns/'gate_state.json').read_bytes()
        for n in range(1,4):
            now=self.f.now+n*60_000;self.f.write_normal(now=now)
            self.assertEqual(scheduler_control(self.f.config,now_ms=now,process_count=1),FALSE_BYTES)
            self.assertEqual((self.f.ns/'gate_state.json').read_bytes(),state1)


if __name__=='__main__':
    unittest.main()
