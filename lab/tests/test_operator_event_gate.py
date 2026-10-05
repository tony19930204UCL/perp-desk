import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path

LAB=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(LAB))

from operator_event_gate import Gate,GateError,load_config,scheduler_control,FALSE_BYTES

def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat().replace('+00:00','Z')

class GateFixture:
    def __init__(self,base):
        self.base=Path(base)
        self.health=self.base/'health.json'
        self.snapshot=self.base/'paper.json'
        self.work=self.base/'work.json'
        self.hold=self.base/'hold.json'
        self.timer=self.base/'timer.json'
        self.ns=self.base/'gate'
        self.config=self.base/'config.json'
        self.now=1_800_000_000_000
        self.write_normal()
        self.write_config()

    def write_normal(self,*,now=None,process_hint=1):
        now=self.now if now is None else now
        self.health.write_text(json.dumps({
            'schema_version':1,'checked_at':iso(now),'available':True,
            'incidents':[],'faults':{},'operational_healthy':True
        }))
        self.snapshot.write_text(json.dumps({
            'mode':'paper','candidate_not_deployed':False,'updated_at':iso(now),
            'engine':{'candidate_implementation':'paper-engine-v3'}
        }))
        self.work.write_text(json.dumps({'schema_version':1,'tasks':[]}))
        self.hold.write_text(json.dumps({'active':False,'operator_confirmed':False}))
        self.timer.write_text(json.dumps({'schema_version':1,'active':False,'operator_accepted':False}))
        self.process_hint=process_hint

    def refresh_observation_sources(self,now):
        self.health.write_text(json.dumps({
            'schema_version':1,'checked_at':iso(now),'available':True,
            'incidents':[],'faults':{},'operational_healthy':True
        }))
        self.snapshot.write_text(json.dumps({
            'mode':'paper','candidate_not_deployed':False,'updated_at':iso(now),
            'engine':{'candidate_implementation':'paper-engine-v3'}
        }))

    def write_config(self,**overrides):
        data={
            'schema_version':1,'enabled':True,
            'namespace_dir':str(self.ns.resolve()),
            'health_path':str(self.health.resolve()),
            'snapshot_path':str(self.snapshot.resolve()),
            'work_path':str(self.work.resolve()),
            'hold_path':str(self.hold.resolve()),
            'health_stale_seconds':180,'snapshot_stale_seconds':180,
            'active_work_overdue_seconds':600,'lease_seconds':300,
            'retry_backoff_seconds':[60,120,240],'max_attempts':3,'max_records':128,
            'runtime_identity':{
                'runtime_root':str((self.base/'runtime').resolve()),
                'state_dir':str((self.base/'trading-state').resolve()),
                'status_path':str(self.snapshot.resolve()),'script':'paper_runtime_v3.py'
            },
            'timer_sources':[]
        }
        data.update(overrides)
        self.config.write_text(json.dumps(data))
        return data

class OperatorEventGateTests(unittest.TestCase):
    def setUp(self):
        self.td=tempfile.TemporaryDirectory();self.f=GateFixture(self.td.name)
    def tearDown(self):self.td.cleanup()

    def tick(self,now=None,process=1):
        return scheduler_control(self.f.config,now_ms=self.f.now if now is None else now,process_count=process)

    def parse(self,text):return json.loads(text)

    def finish(self,payload,now=None,outcome='completed'):
        now=self.f.now if now is None else now
        gate=Gate(load_config(self.f.config),now_ms=now,process_count=1)
        token=payload['context']['claim_token'];handle='worker:test-1'
        gate.worker_adopt(token,handle)
        gate.worker_finish(token,outcome=outcome,worker_handle=handle,evidence_ref='evidence:test-1',
                           reason='synthetic blocker' if outcome=='blocked' else None)

    def test_normal_ticks_are_byte_stable_and_zero_model_calls(self):
        model_calls=0
        first=self.tick()
        self.assertEqual(first,FALSE_BYTES)
        state1=(self.f.ns/'gate_state.json').read_bytes();status1=(self.f.ns/'gate_status.json').read_bytes()
        for n in range(1,6):
            later=self.f.now+n*60_000
            self.f.write_normal(now=later)
            output=self.tick(later)
            if self.parse(output).get('wakeAgent'):
                model_calls+=1
            self.assertEqual(output,FALSE_BYTES)
            self.assertEqual((self.f.ns/'gate_state.json').read_bytes(),state1)
            self.assertEqual((self.f.ns/'gate_status.json').read_bytes(),status1)
        self.assertEqual(model_calls,0,'unchanged 60s ticks must invoke zero models')

    def test_fault_once_completion_no_repeat_then_recovery_once(self):
        fault=self.parse(self.tick(process=0));self.assertTrue(fault['wakeAgent'])
        self.assertEqual(fault['context']['kind'],'runtime')
        self.finish(fault)
        self.assertEqual(self.tick(self.f.now+60_000,process=0),FALSE_BYTES)
        recovery=self.parse(self.tick(self.f.now+120_000,process=1))
        self.assertTrue(recovery['wakeAgent']);self.assertEqual(recovery['context']['transition'],'normal')
        self.finish(recovery,self.f.now+120_000)
        self.assertEqual(self.tick(self.f.now+180_000,process=1),FALSE_BYTES)

    def test_operator_confirmed_storage_hold_is_not_normal_and_never_autorestart_wake(self):
        old=self.f.now-600_000
        self.f.health.write_text(json.dumps({'schema_version':1,'checked_at':iso(old),'incidents':[]}))
        self.f.snapshot.write_text(json.dumps({'mode':'paper','candidate_not_deployed':False,'updated_at':iso(old),
                                               'engine':{'candidate_implementation':'paper-engine-v3'}}))
        self.f.hold.write_text(json.dumps({'active':True,'operator_confirmed':True,'reason':'storage_protection'}))
        self.assertEqual(self.tick(process=0),FALSE_BYTES)
        status=json.loads((self.f.ns/'gate_status.json').read_text())
        self.assertEqual(status['classification'],'operator_hold');self.assertTrue(status['operator_hold'])
        self.assertEqual(self.tick(self.f.now+60_000,process=0),FALSE_BYTES)

    def test_hold_release_reexposes_persistent_fault_once_and_sources_remain_readonly(self):
        old=self.f.now-600_000
        self.f.health.write_text('{broken')
        self.f.snapshot.write_text(json.dumps({'mode':'paper','candidate_not_deployed':False,'updated_at':iso(old),
                                               'engine':{'candidate_implementation':'paper-engine-v3'}}))
        self.f.hold.write_text(json.dumps({'active':True,'operator_confirmed':True,'reason':'storage_protection'}))
        before_health=self.f.health.read_bytes();before_snapshot=self.f.snapshot.read_bytes();before_work=self.f.work.read_bytes()
        self.assertEqual(self.tick(process=0),FALSE_BYTES)
        self.assertEqual(self.f.health.read_bytes(),before_health)
        self.assertEqual(self.f.snapshot.read_bytes(),before_snapshot)
        self.assertEqual(self.f.work.read_bytes(),before_work)
        self.f.hold.write_text(json.dumps({'active':False,'operator_confirmed':True,'reason':'storage_protection'}))
        wake=self.parse(self.tick(self.f.now+60_000,process=0))
        self.assertTrue(wake['wakeAgent']);self.assertEqual(wake['context']['kind'],'health')
        self.finish(wake,self.f.now+60_000)
        # Snapshot/runtime faults were also intentionally present under the hold.
        # Once the hold is removed they are each actionable once, not duplicates
        # of the already-completed health event.
        seen={wake['context']['event_id']}
        for offset in (120_000,180_000):
            nxt=self.parse(self.tick(self.f.now+offset,process=0))
            self.assertTrue(nxt['wakeAgent']);self.assertNotIn(nxt['context']['event_id'],seen)
            seen.add(nxt['context']['event_id']);self.finish(nxt,self.f.now+offset)
        self.assertEqual(self.tick(self.f.now+240_000,process=0),FALSE_BYTES)

    def test_ready_backlog_survives_claim_interruption_lease_and_backoff(self):
        self.f.work.write_text(json.dumps({'schema_version':1,'tasks':[{
            'id':'task-ready','state':'queued','updated_at':iso(self.f.now),'started_at':iso(self.f.now),
            'title':'synthetic','current_step':'queued','next_step':'execute','evidence':[]
        }]}))
        first=self.parse(self.tick());self.assertTrue(first['wakeAgent'])
        token=first['context']['claim_token']
        self.assertEqual(self.tick(self.f.now+60_000),FALSE_BYTES,'active lease must prevent duplicate claim')
        self.f.refresh_observation_sources(self.f.now+301_000)
        self.assertEqual(self.tick(self.f.now+301_000),FALSE_BYTES,'expired lease enters bounded backoff')
        self.f.refresh_observation_sources(self.f.now+362_000)
        retry=self.parse(self.tick(self.f.now+362_000));self.assertTrue(retry['wakeAgent'])
        self.assertNotEqual(retry['context']['claim_token'],token)
        self.assertEqual(retry['context']['event_id'],first['context']['event_id'])

    def test_provider_and_notification_failures_requeue_without_completion(self):
        self.f.work.write_text(json.dumps({'schema_version':1,'tasks':[{
            'id':'retry','state':'queued','updated_at':iso(self.f.now),'started_at':iso(self.f.now),
            'title':'synthetic','current_step':'queued','next_step':'execute','evidence':[]
        }]}))
        first=self.parse(self.tick());g=Gate(load_config(self.f.config),now_ms=self.f.now,process_count=1)
        g.release_failure(first['context']['claim_token'],'provider')
        self.assertEqual(self.tick(self.f.now+30_000),FALSE_BYTES)
        second=self.parse(self.tick(self.f.now+61_000));self.assertTrue(second['wakeAgent'])
        g=Gate(load_config(self.f.config),now_ms=self.f.now+61_000,process_count=1)
        g.release_failure(second['context']['claim_token'],'notification')
        self.assertEqual(self.tick(self.f.now+120_000),FALSE_BYTES)
        state=json.loads((self.f.ns/'gate_state.json').read_text())
        rec=state['records'][first['context']['event_id']]
        self.assertNotEqual(rec['status'],'completed');self.assertEqual(rec['last_failure'],'notification')

    def test_multiple_ready_events_allow_only_one_active_claim(self):
        self.f.work.write_text(json.dumps({'schema_version':1,'tasks':[
            {'id':'one','state':'queued','updated_at':iso(self.f.now),'started_at':iso(self.f.now),
             'title':'one','current_step':'queued','next_step':'execute','evidence':[]},
            {'id':'two','state':'queued','updated_at':iso(self.f.now),'started_at':iso(self.f.now),
             'title':'two','current_step':'queued','next_step':'execute','evidence':[]}
        ]}))
        first=self.parse(self.tick());self.assertTrue(first['wakeAgent'])
        self.assertEqual(self.tick(self.f.now+1_000),FALSE_BYTES,'second ready event must wait while one claim is active')
        work=json.loads(self.f.work.read_text())
        for task in work['tasks']:
            if task['id']==first['context']['evidence_ref'].split(':',1)[1]:
                task['state']='completed'
        self.f.work.write_text(json.dumps(work))
        self.finish(first,self.f.now+2_000)
        second=self.parse(self.tick(self.f.now+3_000));self.assertTrue(second['wakeAgent'])
        self.assertNotEqual(second['context']['event_id'],first['context']['event_id'])

    def test_retry_budget_exhaustion_blocks_without_endless_wake(self):
        cfg=json.loads(self.f.config.read_text());cfg['max_attempts']=2;cfg['retry_backoff_seconds']=[60]
        self.f.config.write_text(json.dumps(cfg))
        self.f.work.write_text(json.dumps({'schema_version':1,'tasks':[{
            'id':'bounded','state':'queued','updated_at':iso(self.f.now),'started_at':iso(self.f.now),
            'title':'bounded','current_step':'queued','next_step':'execute','evidence':[]
        }]}))
        first=self.parse(self.tick());g=Gate(load_config(self.f.config),now_ms=self.f.now,process_count=1)
        g.release_failure(first['context']['claim_token'],'provider')
        second=self.parse(self.tick(self.f.now+61_000));self.assertTrue(second['wakeAgent'])
        g=Gate(load_config(self.f.config),now_ms=self.f.now+61_000,process_count=1)
        g.release_failure(second['context']['claim_token'],'notification')
        state=json.loads((self.f.ns/'gate_state.json').read_text())
        rec=state['records'][first['context']['event_id']]
        self.assertEqual(rec['status'],'blocked')
        self.assertEqual(rec['blocked']['reason'],'notification_retry_budget_exhausted')
        self.f.refresh_observation_sources(self.f.now+300_000)
        self.assertEqual(self.tick(self.f.now+300_000),FALSE_BYTES)

    def test_unsupported_snapshot_identity_faults_once(self):
        self.f.snapshot.write_text(json.dumps({'mode':'paper','candidate_not_deployed':False,'updated_at':iso(self.f.now),
                                               'engine':{'candidate_implementation':'paper-engine-v99'}}))
        first=self.parse(self.tick());self.assertTrue(first['wakeAgent']);self.assertEqual(first['context']['kind'],'snapshot')
        self.finish(first)
        self.assertEqual(self.tick(self.f.now+60_000),FALSE_BYTES)

    def test_worker_handle_and_evidence_are_required_for_terminal_completion(self):
        self.f.work.write_text(json.dumps({'schema_version':1,'tasks':[{
            'id':'claim','state':'queued','updated_at':iso(self.f.now),'started_at':iso(self.f.now),
            'title':'synthetic','current_step':'queued','next_step':'execute','evidence':[]
        }]}))
        p=self.parse(self.tick());token=p['context']['claim_token']
        g=Gate(load_config(self.f.config),now_ms=self.f.now,process_count=1)
        with self.assertRaises(GateError):g.worker_finish(token,outcome='completed',worker_handle='worker:x',evidence_ref='/private/path')
        g.worker_adopt(token,'worker:x')
        with self.assertRaises(GateError):g.worker_finish(token,outcome='completed',worker_handle='worker:y',evidence_ref='evidence:x')
        g.worker_finish(token,outcome='completed',worker_handle='worker:x',evidence_ref='evidence:x')
        state=json.loads((self.f.ns/'gate_state.json').read_text())
        self.assertEqual(state['records'][p['context']['event_id']]['status'],'completed')

    def test_work_completion_requires_authoritative_terminal_evidence_and_records_lifecycle(self):
        self.f.work.write_text(json.dumps({'schema_version':1,'tasks':[{
            'id':'durable-work','state':'queued','updated_at':iso(self.f.now),'started_at':iso(self.f.now),
            'title':'synthetic','current_step':'queued','next_step':'execute','evidence':[]
        }]}))
        p=self.parse(self.tick());token=p['context']['claim_token'];handle='worker:lifecycle'
        g=Gate(load_config(self.f.config),now_ms=self.f.now,process_count=1)
        g.worker_adopt(token,handle)
        with self.assertRaisesRegex(GateError,'still unfinished'):
            g.worker_finish(token,outcome='completed',worker_handle=handle,evidence_ref='evidence:durable-work')
        state=json.loads((self.f.ns/'gate_state.json').read_text())
        rec=state['records'][p['context']['event_id']]
        self.assertEqual(rec['status'],'claimed')
        self.assertEqual([x['state'] for x in rec['lifecycle'][:3]],['observed','queued','claimed'])
        work=json.loads(self.f.work.read_text());work['tasks'][0]['state']='completed'
        self.f.work.write_text(json.dumps(work))
        g.worker_finish(token,outcome='completed',worker_handle=handle,evidence_ref='evidence:durable-work')
        rec=json.loads((self.f.ns/'gate_state.json').read_text())['records'][p['context']['event_id']]
        self.assertEqual(rec['status'],'completed')
        self.assertEqual(rec['lifecycle'][-1]['state'],'completed')
        self.assertEqual(self.tick(self.f.now+60_000),FALSE_BYTES)

    def test_configured_timer_source_missing_or_corrupt_faults_once_then_recovers_once(self):
        missing=self.f.base/'missing-timer.json'
        cfg=json.loads(self.f.config.read_text())
        cfg['timer_sources']=[{'id':'authoritative-window','kind':'registration','path':str(missing.resolve())}]
        self.f.config.write_text(json.dumps(cfg))
        first=self.parse(self.tick());self.assertTrue(first['wakeAgent'])
        self.assertEqual(first['context']['kind'],'timer')
        self.assertEqual(first['context']['transition'],'fault')
        self.finish(first)
        self.assertEqual(self.tick(self.f.now+60_000),FALSE_BYTES)
        missing.write_text('{broken')
        self.assertEqual(self.tick(self.f.now+120_000),FALSE_BYTES,'same corrupt-source state must dedupe')
        missing.write_text(json.dumps({'schema_version':1,'active':True,'operator_accepted':True,
            'registration_id':'accepted-window','start_ms':self.f.now,
            'checkpoint_ms':self.f.now+600_000,'deadline_ms':self.f.now+1_200_000}))
        recovered=self.parse(self.tick(self.f.now+180_000));self.assertTrue(recovered['wakeAgent'])
        self.assertEqual(recovered['context']['transition'],'normal')
        self.finish(recovered,self.f.now+180_000)
        self.assertEqual(self.tick(self.f.now+240_000),FALSE_BYTES)

    def test_deadline_checkpoint_derive_only_from_configured_active_authority(self):
        # Unconfigured staged files cannot start timers.
        self.f.timer.write_text(json.dumps({'schema_version':1,'active':True,'operator_accepted':True,
            'registration_id':'staged-pr24','start_ms':self.f.now,'checkpoint_ms':self.f.now+60_000,'deadline_ms':self.f.now+120_000}))
        self.assertEqual(self.tick(self.f.now+60_000),FALSE_BYTES)
        cfg=json.loads(self.f.config.read_text());cfg['timer_sources']=[{'id':'registered-window','kind':'registration','path':str(self.f.timer.resolve())}]
        self.f.config.write_text(json.dumps(cfg))
        cp=self.parse(self.tick(self.f.now+60_000));self.assertTrue(cp['wakeAgent']);self.assertEqual(cp['context']['evidence_ref'],'timer:registered-window:checkpoint')
        self.finish(cp,self.f.now+60_000)
        self.assertEqual(self.tick(self.f.now+90_000),FALSE_BYTES)
        dl=self.parse(self.tick(self.f.now+120_000));self.assertTrue(dl['wakeAgent']);self.assertEqual(dl['context']['evidence_ref'],'timer:registered-window:deadline')

    def test_advancing_health_timestamps_and_counters_do_not_create_wakes(self):
        self.assertEqual(self.tick(),FALSE_BYTES)
        for n in range(1,4):
            now=self.f.now+n*60_000
            self.f.health.write_text(json.dumps({'schema_version':1,'checked_at':iso(now),'available':True,
                                                 'incidents':[],'faults':{},'poll_count':n,'errors_count':n+100}))
            self.f.snapshot.write_text(json.dumps({'mode':'paper','candidate_not_deployed':False,'updated_at':iso(now),
                'feed':{'errors_count':100+n},'engine':{'candidate_implementation':'paper-engine-v3'}}))
            self.assertEqual(self.tick(now),FALSE_BYTES)

    def test_incident_recovery_lifecycle_deduplicates(self):
        self.f.health.write_text(json.dumps({'schema_version':1,'checked_at':iso(self.f.now),'incidents':[
            {'id':'paper-v3:storage-new-risk-inhibited','status':'open'}]}))
        p=self.parse(self.tick());self.assertTrue(p['wakeAgent']);self.finish(p)
        self.assertEqual(self.tick(self.f.now+60_000),FALSE_BYTES)
        self.f.health.write_text(json.dumps({'schema_version':1,'checked_at':iso(self.f.now+120_000),'incidents':[
            {'id':'paper-v3:storage-new-risk-inhibited','status':'recovered_monitoring'}]}))
        r=self.parse(self.tick(self.f.now+120_000));self.assertTrue(r['wakeAgent']);self.assertEqual(r['context']['transition'],'recovered_monitoring')

    def test_corrupt_source_is_fault_but_corrupt_gate_state_is_never_reset(self):
        self.f.health.write_text('{broken')
        p=self.parse(self.tick());self.assertTrue(p['wakeAgent']);self.assertEqual(p['context']['kind'],'health')
        state=self.f.ns/'gate_state.json';state.write_text('{broken')
        before=state.read_bytes()
        with self.assertRaises(GateError):self.tick(self.f.now+60_000)
        self.assertEqual(state.read_bytes(),before)

    def test_subprocess_scheduler_adapter_isolated_and_public_safe(self):
        cmd=[sys.executable,str(LAB/'operator_event_gate.py'),'--config',str(self.f.config),
             '--engineering-fixture','--now-ms',str(self.f.now),'--process-count','1','scheduler']
        p=subprocess.run(cmd,cwd=LAB,capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(p.stdout.strip(),FALSE_BYTES)
        self.assertNotIn(str(self.f.base),p.stdout)
        self.f.work.write_text(json.dumps({'schema_version':1,'tasks':[{
            'id':'subprocess-ready','state':'queued','updated_at':iso(self.f.now),'started_at':iso(self.f.now),
            'title':'synthetic','current_step':'queued','next_step':'execute','evidence':[]
        }]}))
        p=subprocess.run(cmd,cwd=LAB,capture_output=True,text=True,timeout=20)
        self.assertEqual(p.returncode,0,p.stderr);out=json.loads(p.stdout)
        self.assertTrue(out['wakeAgent']);self.assertEqual(out['context']['kind'],'work')
        self.assertNotIn(str(self.f.base),p.stdout)

if __name__=='__main__':unittest.main()
