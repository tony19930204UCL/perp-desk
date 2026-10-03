"""Standalone operational monitoring; fixtures never enter trading state."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)

def module():
    spec = importlib.util.spec_from_file_location('health_watchdog', ROOT / 'health_watchdog.py')
    if not spec or not spec.loader or not (ROOT / 'health_watchdog.py').exists():
        return None
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def snapshot():
    d = json.loads((ROOT / 'tests/fixtures/health_paper_fixture.json').read_text())
    d['updated_at'] = NOW.isoformat()
    d['feed'].update(connected=True, last_success_at=NOW.isoformat(), errors_count=0)
    d['latest_error'] = None; d['blockers'] = []; d['engine']['status'] = 'running'
    d['engine']['version_id']='H1-PAPER-003'; d['engine']['candidate_implementation']='paper-engine-v3'
    d['engine']['deployment']['version_id']='H1-PAPER-003'
    d['versions']=[dict(created_at=NOW.isoformat(),status='observing',version_id='H1-PAPER-003')]
    d['research']=dict(strategy_start_ms=int(NOW.timestamp()*1000)-3600000,
                       deadline_ms=int(NOW.timestamp()*1000)+3600000,
                       target_complete_round_trips=30,target_not_guarantee=True,status='unproven',
                       complete_round_trips=0,signals_count=0,blocked_signals_count=0,rejection_categories={})
    for m in d['markets']:
        m['last_received_at'] = NOW.isoformat()
        m['source_timestamps_ms'] = dict.fromkeys(('bookTicker','depth5','premiumIndex'), int(NOW.timestamp()*1000))
    return d

class HealthTests(unittest.TestCase):
    def test_ready_monitor_scripts_are_profile_bound_and_gate_is_script_only(self):
        import ast
        scripts=ROOT.parent/'scripts'
        for filename in ('paper_health_monitor.py','paper_health_engineering_gate.py'):
            path=scripts/filename
            self.assertTrue(path.exists(),'ready scheduler script missing: '+filename)
            source=path.read_text();ast.parse(source)
            # Scheduler remains bound to the deployment profile, not the clone.
            self.assertIn("ROOT=Path('/home/chihcheng/.hermes/profiles/perp-desk/lab')",source)
            self.assertNotIn('subprocess',source)
            self.assertNotIn('work_status.update',source)
        gate=(scripts/'paper_health_engineering_gate.py').read_text()
        monitor=(scripts/'paper_health_monitor.py').read_text()
        self.assertIn('issue_handoff',gate);self.assertNotIn('wakeAgent',gate)
        for source in (gate,monitor):
            self.assertIn('health_monitor_config.json',source)
            self.assertIn('load_monitor_config',source)

    def test_split_root_real_subprocess_exact_identity_and_namespace(self):
        import subprocess, sys
        mod=module()
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);monitor=base/'monitor';runtime=base/'runtime';shared=base/'shared-namespace'
            for p in (monitor/'shared',monitor/'data/health',runtime,shared/'data/paper-v2',shared/'shared'):p.mkdir(parents=True,exist_ok=True)
            (runtime/'paper_runtime_v3.py').write_text('import time; time.sleep(30)')
            status=shared/'shared/paper_v2_live.json';state=shared/'data/paper-v2'
            status.write_text(json.dumps(snapshot()))
            trading_db=state/'runtime.sqlite3';trading_db.write_bytes(b'synthetic trading state must remain untouched')
            (monitor/'shared/work_status.json').write_text(json.dumps({'schema_version':1,'tasks':[]}))
            status_before=status.read_bytes();db_before=trading_db.read_bytes()
            proc=subprocess.Popen([sys.executable,'-u','paper_runtime_v3.py','--state-dir',str(state),'--status',str(status)],cwd=runtime)
            try:
                self.assertNotEqual(monitor.resolve(),runtime.resolve(),'fixture must reproduce split monitor/runtime roots')
                self.assertEqual(mod.runtime_processes(runtime,state,status),[proc.pid])
                report=mod.once(monitor,NOW,runtime_root=runtime,state_dir=state,status_path=status)
                self.assertEqual(status.read_bytes(),status_before)
                self.assertEqual(trading_db.read_bytes(),db_before)
                self.assertEqual(report['runtime_pids'],[proc.pid])
                self.assertNotIn('runtime-absent',report['faults'])
                self.assertNotIn('runtime-duplicate',report['faults'])
                self.assertEqual(report['runtime_identity']['runtime_root'],str(runtime.resolve()))
                # Exactness: monitor root, wrong namespace/status and wrong script cannot match.
                self.assertEqual(mod.runtime_processes(monitor,state,status),[])
                self.assertEqual(mod.runtime_processes(runtime,base/'wrong-state',status),[])
                self.assertEqual(mod.runtime_processes(runtime,state,base/'wrong-status.json'),[])
                wrong=runtime/'not_runtime.py';wrong.write_text('import time; time.sleep(30)')
                wrongp=subprocess.Popen([sys.executable,'-u','not_runtime.py','--state-dir',str(state),'--status',str(status)],cwd=runtime)
                try:self.assertEqual(mod.runtime_processes(runtime,state,status),[proc.pid])
                finally:wrongp.terminate();wrongp.wait(timeout=5)
                second=subprocess.Popen([sys.executable,'-B','-u','paper_runtime_v3.py','--state-dir',str(state),'--status',str(status)],cwd=runtime)
                try:
                    pids=sorted(mod.runtime_processes(runtime,state,status))
                    self.assertEqual(pids,sorted([proc.pid,second.pid]))
                    duplicate=mod.once(monitor,NOW,runtime_root=runtime,state_dir=state,status_path=status)
                    self.assertIn('runtime-duplicate',duplicate['faults'])
                finally:second.terminate();second.wait(timeout=5)
                self.assertEqual(mod.runtime_processes(runtime,state,status),[proc.pid])
            finally:proc.terminate();proc.wait(timeout=5)

    def test_operator_monitor_config_requires_complete_absolute_paths(self):
        mod=module()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);cfg=root/'health.json'
            good={'schema_version':1,'runtime_root':str((root/'runtime').resolve()),
                  'state_dir':str((root/'state').resolve()),'status_path':str((root/'status.json').resolve())}
            cfg.write_text(json.dumps(good));loaded=mod.load_monitor_config(cfg)
            self.assertEqual(loaded['runtime_root'],Path(good['runtime_root']))
            for bad in ({},dict(good,runtime_root='relative/runtime'),dict(good,schema_version=2)):
                cfg.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):mod.load_monitor_config(cfg)


    def test_issue_handoff_never_wakes_agent_and_suppresses_active_owner(self):
        import work_status
        mod=module();self.assertTrue(hasattr(mod,'issue_handoff'),'read-only issue handoff missing')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'shared').mkdir();(root/'data/paper-v2').mkdir(parents=True)
            d=snapshot();d['latest_error']='batch error';(root/'shared/paper_v2_live.json').write_text(json.dumps(d))
            work=root/'shared/work_status.json';work_status.update(work,'history','old','completed','done','monitor')
            mod.once(root,NOW,True)
            gate=mod.engineering_gate(root,NOW)
            self.assertFalse(gate['wakeAgent']);self.assertTrue(gate['handoff_required'])
            self.assertEqual(gate['incident_ids'],['paper-v3:runtime-error'])
            work_status.update(work,'batch-time-recurrence','repair','running','actual investigation','test')
            # Changed failure is new evidence, but an existing owner prevents duplicate repair.
            d['latest_error']='new batch error';(root/'shared/paper_v2_live.json').write_text(json.dumps(d));mod.once(root,NOW,True)
            self.assertFalse(mod.engineering_gate(root,datetime.now(timezone.utc))['handoff_required'])
            (root/'data/health/incident_state.json').write_text('{corrupt')
            mod.once(root,datetime.now(timezone.utc),True)
            broken=mod.engineering_gate(root,datetime.now(timezone.utc))
            self.assertFalse(broken['wakeAgent'],'monitor must never dispatch engineering')
            self.assertTrue(broken['handoff_required'])
            self.assertIn('paper-v3:watchdog-state-unavailable',broken['incident_ids'])

    def test_explicit_tested_resolution_and_new_error_delivery(self):
        mod=module();self.assertTrue(hasattr(mod,'resolve_incident'),'explicit tested resolution missing')
        with tempfile.TemporaryDirectory() as td:
            state=Path(td)/'state.json';out=Path(td)/'report.json'
            d=snapshot();d['latest_error']='first';bad=mod.evaluate(d,{'tasks':[]},NOW,True)
            first=mod.record(state,out,bad,NOW); iid=first['incidents'][0]['id']
            with self.assertRaises(ValueError):mod.resolve_incident(state,iid,{'tested_resolution':True,'accepted_by_parent':True,'evidence_paths':['missing']},NOW)
            d['latest_error']='different failure';bad=mod.evaluate(d,{'tasks':[]},NOW,True)
            self.assertTrue(mod.record(state,out,bad,NOW)['changed'])
            good=mod.evaluate(snapshot(),{'tasks':[]},NOW,True)
            for _ in range(3):mod.record(state,out,good,NOW)
            proof=Path(td)/'passed-tests.txt';proof.write_text('actual test evidence fixture')
            evidence=dict(tested_resolution=True,accepted_by_parent=True,evidence_paths=[str(proof)])
            mod.resolve_incident(state,iid,evidence,NOW)
            r=mod.record(state,out,good,NOW);self.assertTrue(r['engineering_resolved'])
            self.assertEqual(r['incidents'][0]['status'],'resolved')
            self.assertFalse(mod.record(state,out,bad,NOW)['engineering_resolved'])

    def test_parallel_watchers_preserve_links_and_concurrent_work_writers(self):
        import subprocess,sys
        from concurrent.futures import ThreadPoolExecutor
        import work_status
        mod=module()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'shared').mkdir();(root/'data/paper-v2').mkdir(parents=True)
            d=snapshot();d['latest_error']='failure';(root/'shared/paper_v2_live.json').write_text(json.dumps(d))
            work=root/'shared/work_status.json';work_status.update(work,'history','historical task','completed','done','observe')
            history=json.loads(work.read_text())['tasks'][0]
            state=root/'data/health/incident_state.json'
            mod.once(root,NOW,True)
            mod.link_incident(state,'paper-v3:runtime-error','repair')
            def run(n):
                if n%2:
                    return subprocess.run([sys.executable,'work_status.py','--path',str(work),'--id','task-'+str(n),'--title','task','--state','queued','--current','planned','--next','test'],capture_output=True,text=True).returncode
                return subprocess.run([sys.executable,'-c',"import health_watchdog as h; from pathlib import Path; from datetime import datetime,timezone; h.once(Path("+repr(str(root))+"),datetime.now(timezone.utc),True)"],capture_output=True,text=True).returncode
            with ThreadPoolExecutor(max_workers=8) as pool:self.assertEqual(list(pool.map(run,range(16))),[0]*16)
            tasks=json.loads(work.read_text())['tasks'];self.assertEqual(len(tasks),9)
            self.assertEqual(next(t for t in tasks if t['id']=='history'),history)
            incident=json.loads(state.read_text())['incidents']['runtime-error']
            self.assertEqual(incident['linked_task_id'],'repair')
            self.assertEqual(incident['first_seen'],NOW.isoformat())

    def test_ui_executes_fault_overview_even_when_all_jobs_completed(self):
        import subprocess
        html=(ROOT/'dashboard.html').read_text()
        self.assertIn('現在健康／未解問題', html)
        self.assertLess(html.index('現在健康／未解問題'),html.index('背景工作 / 下一步'))
        self.assertNotIn('innerHTML', html)
        script=html.split('<script>',1)[1].split('</script>',1)[0]
        harness="""
const vm=require('vm'); const assert=require('assert');
class E {constructor(){this.textContent='';this.children=[];this.style={};this.classList={remove(){},add(){}};} appendChild(e){this.children.push(e);} replaceChildren(){this.children=[];} setAttribute(){} }
const nodes={}; const document={getElementById(id){return nodes[id]??=(new E());},createElement(){return new E();},createElementNS(){return new E();}};
const ctx={document,console,AbortController,setTimeout(){return 1;},clearTimeout(){},setInterval(){},fetch:async()=>({ok:false,status:503})};
vm.createContext(ctx);vm.runInContext(SCRIPT,ctx);
vm.runInContext(`renderWork({available:true,tasks:[{title:'old repair',state:'completed',activity_unconfirmed:false,current_step:'done',next_step:'monitor',updated_at:'old',update_age_seconds:5,evidence:[]}]});renderHealth({available:true,health_stale:false,operational_healthy:false,engineering_resolved:false,checked_at:'now',faults:{'runtime-error':'ValueError: batch source age exceeded'},incidents:[{id:'paper-v3:runtime-error',status:'open',first_seen:'first',last_observed:'last',last_error:'<img src=x onerror=alert(1)>',linked_task_id:null,message:'ValueError: batch source age exceeded'}],pending_work:[],service_restart_limitation:'startup not verified'});`,ctx);
assert(nodes.opsHealth.textContent.includes('故障'));assert(nodes.opsIssues.children[0].textContent.includes('<img'));
assert(nodes.opsIssues.children[0].textContent.includes('尚未連結'));
vm.runInContext(`renderHealth({available:true,health_stale:false,operational_healthy:true,engineering_resolved:false,checked_at:'now',faults:{},incidents:[{id:'i',status:'recovered_monitoring',first_seen:'first',last_observed:'last',last_error:'batch error',linked_task_id:null}],pending_work:[]});`,ctx);
assert(nodes.opsHealth.textContent.includes('未結案'));assert(nodes.opsIssues.children[0].textContent.includes('恢復觀察'));
vm.runInContext(`renderHealth({available:true,health_stale:true,operational_healthy:true,engineering_resolved:true,faults:{},incidents:[],pending_work:[]});`,ctx);
assert(nodes.opsHealth.textContent.includes('未確認'));console.log('UI fault, recovery, stale, escaped text and completed-history separation OK');
"""
        harness=harness.replace('SCRIPT',json.dumps(script))
        result=subprocess.run(['node','-e',harness],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_health_api_independent_freshness_security_and_invalid_data(self):
        import threading
        from http.client import HTTPConnection
        path=ROOT/'dashboard.py'
        self.assertTrue(path.exists(), 'staged health API missing')
        spec=importlib.util.spec_from_file_location('staged_health_dashboard',path)
        dash=importlib.util.module_from_spec(spec);spec.loader.exec_module(dash)
        mod=module()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); now=datetime.now(timezone.utc)
            d=snapshot(); d['updated_at']=now.isoformat(); d['feed']['last_success_at']=now.isoformat()
            for m in d['markets']:
                m['last_received_at']=now.isoformat(); m['source_timestamps_ms']=dict.fromkeys(('bookTicker','depth5','premiumIndex'),int(now.timestamp()*1000))
            report=mod.record(root/'state.json',root/'health_status.json',mod.evaluate(d,{'tasks':[]},now,True),now)
            (root/'work_status.json').write_text(json.dumps({'schema_version':1,'tasks':[]}))
            server=dash.make_server(0,root/'missing-market.json',ROOT/'dashboard.html')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def get(route='/api/health',headers=None,method='GET'):
                c=HTTPConnection('127.0.0.1',server.server_port,timeout=3);c.request(method,route,headers=headers or {});r=c.getresponse();body=r.read();c.close();return r.status,body
            try:
                code,body=get();self.assertEqual(code,200);self.assertFalse(json.loads(body)['health_stale'])
                self.assertEqual(get('/api/status')[0],503);self.assertEqual(get('/api/work')[0],200)
                for headers in ({'Host':'evil.test'},{'Origin':'https://evil.test'},{'Sec-Fetch-Site':'cross-site'}): self.assertEqual(get(headers=headers)[0],403)
                for method in ('POST','PUT','DELETE','PATCH'):self.assertEqual(get(method=method)[0],405)
                for route in ('/../health_status.json','/api/health?path=../../data/paper-v2/runtime.sqlite3','/%2e%2e/health_status.json'):self.assertEqual(get(route)[0],404)
                report['checked_at']='2026-01-01T00:00:00Z'; (root/'health_status.json').write_text(json.dumps(report))
                stale=json.loads(get()[1]);self.assertTrue(stale['health_stale']);self.assertFalse(stale['operational_healthy'])
                for payload in ('{}','[]','{bad',json.dumps(dict(report,checked_at='2099-01-01T00:00:00Z'))):
                    (root/'health_status.json').write_text(payload);self.assertEqual(get()[0],503)
                (root/'health_status.json').unlink();self.assertEqual(get()[0],503)
            finally:server.shutdown();server.server_close();thread.join()

    def test_once_cli_reads_only_live_inputs_and_deduplicates_stdout(self):
        import subprocess, sys
        mod = module(); self.assertTrue(hasattr(mod,'once'),'read-only tick missing')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/'shared').mkdir(); (root/'data/paper-v2').mkdir(parents=True)
            d=snapshot(); d['latest_error']='same failure'
            (root/'shared/paper_v2_live.json').write_text(json.dumps(d))
            (root/'shared/work_status.json').write_text(json.dumps({'schema_version':1,'tasks':[]}))
            (root/'data/paper-v2/runtime.sqlite3').write_bytes(b'preserve trading evidence')
            before={p.name:p.read_bytes() for p in [root/'shared/paper_v2_live.json',root/'shared/work_status.json',root/'data/paper-v2/runtime.sqlite3']}
            r=mod.once(root,NOW,process_present=True)
            self.assertIn('runtime-error',r['faults'])
            self.assertFalse(mod.once(root,NOW,process_present=True)['changed'])
            for path in (root/'shared/paper_v2_live.json',root/'shared/work_status.json',root/'data/paper-v2/runtime.sqlite3'):
                self.assertEqual(path.read_bytes(),before[path.name])
            self.assertTrue((root/'shared/health_status.json').exists())
            self.assertEqual(mod.delivery(r),mod.delivery(dict(r,checked_at='changed timestamp')))
            r['changed']=False; self.assertEqual(mod.delivery(r),'')
            (root/'shared/paper_v2_live.json').write_text('{bad json')
            self.assertIn('snapshot-unavailable',mod.once(root,NOW,process_present=True)['faults'])
            (root/'data/health/incident_state.json').write_text('{corrupt')
            r=mod.once(root,NOW,process_present=True)
            self.assertFalse(r['available']); self.assertFalse(r['operational_healthy'])
            self.assertEqual((root/'data/health/incident_state.json').read_text(),'{corrupt')
            self.assertFalse(mod.once(root,NOW,process_present=True)['changed'])
            mod.__file__=str(root/'health_watchdog.py')
            self.assertEqual(mod.main(['--once']),0, 'handled unhealthy status is data, not a broken collector')

    def test_error_rate_baseline_bounded_samples_and_counter_reset(self):
        from datetime import timedelta
        mod = module()
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)/'state.json'; out=Path(td)/'out.json'
            report = mod.evaluate(snapshot(), {'tasks': []}, NOW, True)
            first=mod.record(state,out,report,NOW)
            self.assertIn('error_growth', first, 'explicit error baseline missing')
            self.assertIsNone(first['error_growth']['rate_per_minute'])
            report['evidence']['errors_count'] = 4
            r=mod.record(state,out,report,NOW+timedelta(seconds=120))
            self.assertEqual(r['error_growth']['delta'],4)
            self.assertEqual(r['error_growth']['elapsed_seconds'],120)
            self.assertEqual(r['error_growth']['rate_per_minute'],2)
            self.assertIn('error-growth',r['faults'])
            report['evidence']['errors_count']=1
            r=mod.record(state,out,report,NOW+timedelta(seconds=180))
            self.assertIn('counter-reset',r['faults']); self.assertIsNone(r['error_growth']['rate_per_minute'])
            for n in range(250): mod.record(state,out,report,NOW+timedelta(seconds=200+n))
            saved=json.loads(state.read_text())
            self.assertLessEqual(len(saved['samples']),240)
            self.assertLess(state.stat().st_size,1048576)
            self.assertIn('hourly_samples',saved,'bounded long-term health summaries missing')
            self.assertLessEqual(len(saved['hourly_samples']),720)
            self.assertEqual(sum(h['observations'] for h in saved['hourly_samples']),253)

    def test_persistent_dedup_recovery_hysteresis_and_explicit_resolution(self):
        mod = module(); self.assertTrue(hasattr(mod, 'record'), 'persistent incident lifecycle missing')
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)/'incidents.json'; out = Path(td)/'health.json'
            d = snapshot(); d['latest_error'] = 'ValueError: batch source age exceeded'
            bad = mod.evaluate(d, {'tasks': []}, NOW, True)
            first = mod.record(state, out, bad, NOW)
            second = mod.record(state, out, bad, NOW)
            self.assertTrue(first['changed']); self.assertFalse(second['changed'])
            self.assertEqual(len(second['incidents']), 1)
            self.assertEqual(second['incidents'][0]['status'], 'open')
            self.assertEqual(second['incidents'][0]['first_seen'], NOW.isoformat())
            self.assertEqual(second['incidents'][0]['last_error'], d['latest_error'])
            healthy = mod.evaluate(snapshot(), {'tasks': []}, NOW, True)
            for _ in range(2):
                r = mod.record(state, out, healthy, NOW)
                self.assertEqual(r['incidents'][0]['status'], 'open')
            r = mod.record(state, out, healthy, NOW)
            self.assertEqual(r['incidents'][0]['status'], 'recovered_monitoring')
            self.assertFalse(r['engineering_resolved'])
            self.assertEqual(mod.record(state,out,healthy,NOW)['incidents'][0]['status'],'recovered_monitoring')
            mod.link_incident(state, r['incidents'][0]['id'], 'repair')
            self.assertEqual(mod.record(state,out,healthy,NOW)['incidents'][0]['linked_task_id'],'repair')
            r = mod.record(state,out,bad,NOW)
            self.assertEqual(len(r['incidents']),1); self.assertEqual(r['incidents'][0]['status'],'open')

    def test_work_and_storage_are_independent_and_do_not_claim_execution(self):
        mod = module(); d = snapshot()
        work = {'tasks': [dict(id='old', state='completed', updated_at='2026-10-01T06:00:00Z', current_step='done'),
                          dict(id='repair', state='running', updated_at='2026-10-02T05:50:00Z', current_step='watching'),
                          dict(id='later', state='queued', updated_at='2026-10-02T04:00:00Z', current_step='planned')]}
        before = json.dumps(work, sort_keys=True)
        r = mod.evaluate(d, work, NOW, True, dict(used_bytes=600000000, budget_bytes=536870912, free_bytes=100000000, min_free_bytes=1073741824))
        self.assertIn('work-overdue', r['faults']); self.assertIn('storage-capacity', r['faults'])
        self.assertEqual(r['pending_work'][0]['id'], 'repair')
        self.assertTrue(r['pending_work'][0]['activity_unconfirmed'])
        self.assertNotIn('executing', r['pending_work'][0])
        self.assertEqual(json.dumps(work, sort_keys=True), before)
        self.assertIn('work-unavailable', mod.evaluate(d, None, NOW, True)['faults'])
        self.assertIn('work-overdue', mod.evaluate(None, work, NOW, True)['faults'])
        self.assertIn('not verified', r['service_restart_limitation'])

    def test_schema_identity_and_raw_timestamp_gates(self):
        mod = module(); good = snapshot()
        self.assertTrue(mod.evaluate(good, {'tasks': []}, NOW, True)['operational_healthy'])
        cases = [({}, 'snapshot-unavailable'),
                 ({'candidate_not_deployed': True}, 'snapshot-unavailable'),
                 ({'updated_at': 'invalid'}, 'snapshot-unavailable'),
                 ({'updated_at': '2026-10-02T05:58:00Z'}, 'heartbeat-stale'),
                 ({'updated_at': '2026-10-02T06:00:01Z'}, 'heartbeat-stale')]
        for changes, key in cases:
            d = snapshot() if changes else {}; d.update(changes)
            self.assertIn(key, mod.evaluate(d, {'tasks': []}, NOW, True)['faults'])
        for stamp in (int(NOW.timestamp()*1000)-61000, int(NOW.timestamp()*1000)+1, None, True):
            d = snapshot(); d['markets'][0]['source_timestamps_ms']['bookTicker'] = stamp
            self.assertIn('source-freshness', mod.evaluate(d, {'tasks': []}, NOW, True)['faults'])
        d = snapshot(); d['engine']['version_id'] = 'wrong'
        self.assertIn('snapshot-unavailable', mod.evaluate(d, {'tasks': []}, NOW, True)['faults'])
        d = snapshot(); d['markets'][0]['last_received_at'] = '2026-10-02T05:58:00Z'
        self.assertIn('source-freshness', mod.evaluate(d, {'tasks': []}, NOW, True)['faults'])
        d = snapshot(); d['feed']['connected'] = False; d['latest_error'] = 'ValueError: batch source age exceeded'
        r = mod.evaluate(d, {'tasks': []}, NOW, False)
        for key in ('runtime-absent','feed-disconnected','runtime-error'):
            self.assertIn(key, r['faults'])
        self.assertEqual(r['evidence']['latest_error'], d['latest_error'])

    def test_missing_snapshot_cannot_be_healthy(self):
        mod = module()
        self.assertIsNotNone(mod, 'operational watchdog missing')
        report = mod.evaluate(None, None, NOW, process_present=False)
        self.assertFalse(report['operational_healthy'])
        self.assertIn('snapshot-unavailable', report['faults'])

if __name__ == '__main__':
    unittest.main()
