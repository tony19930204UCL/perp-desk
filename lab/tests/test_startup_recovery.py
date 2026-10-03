"""Public-safe isolated startup/cold-recovery acceptance for PAPER v3."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

LAB=Path(__file__).resolve().parents[1]


class StartupRecoveryTests(unittest.TestCase):
    def make_fixture(self,td):
        root=Path(td); runtime=root/'runtime'; monitor=root/'monitor'; state=root/'shared-state'; shared=root/'shared'
        runtime.mkdir();monitor.mkdir();state.mkdir();shared.mkdir()
        (runtime/'paper_runtime_v3.py').write_text('import time; time.sleep(30)\n')
        runtime_config=runtime/'paper_config_v3.json';runtime_config.write_text('{"fixture":"startup"}\n')
        storage=runtime/'storage-policy.json'
        storage.write_text(json.dumps(dict(schema_version=1,warning_bytes=100,new_risk_limit_bytes=200,
                                           exit_reserve_bytes=100,min_free_bytes=50,max_exit_cycle_bytes=20)))
        (monitor/'health_watchdog.py').write_text('# synthetic monitor asset\n')
        (monitor/'dashboard.py').write_text('# synthetic dashboard asset\n')
        dashboard_html=monitor/'dashboard.html';dashboard_html.write_text('<html>synthetic</html>')
        status=shared/'paper_v2_live.json'
        health_cfg=monitor/'health-monitor.json'
        health_cfg.write_text(json.dumps(dict(schema_version=1,runtime_root=str(runtime.resolve()),
                                              state_dir=str(state.resolve()),status_path=str(status.resolve()))))
        config_hash=hashlib.sha256(runtime_config.read_bytes()).hexdigest()
        runtime_state=dict(config_hash=config_hash,fixture=True,forward_start_ms=1000,
                           strategy_start_ms=1100,research_deadline_ms=999999,
                           strategy_fill_baseline=3,strategy_ledger_baseline=4,
                           deployment=dict(version_id='H1-PAPER-003'))
        with sqlite3.connect(state/'runtime.sqlite3') as db:
            db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,previous_hash TEXT NOT NULL,hash TEXT NOT NULL)')
            db.execute('INSERT INTO state VALUES(1,?)',(json.dumps(runtime_state,sort_keys=True),))
            db.execute('INSERT INTO audit VALUES(1,?,?,?)',('{"type":"prefix"}','0'*64,'a'*64))
        with sqlite3.connect(state/'signals.sqlite3') as db:
            db.execute('CREATE TABLE h1_versions(version_id TEXT,registration TEXT)')
            db.execute('CREATE TABLE h1_state(version_id TEXT,symbol TEXT,bars TEXT)')
            db.execute('CREATE TABLE h1_signals(version_id TEXT,intent TEXT)')
        broker=dict(meta={'synthetic':True},cash='99',
                    fills=[{'fill_id':'f1'}],ledger=[{'ledger_id':'l1'}],audit=[{'order_id':'o1'}],
                    positions={'ETHUSDT':{'qty':'0.008'}},
                    orders={'order:pending':{'status':'PENDING','intent':{'reduce_only':False}}})
        with sqlite3.connect(state/'broker.sqlite3') as db:
            db.execute('CREATE TABLE sim_broker_state(singleton INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT INTO sim_broker_state VALUES(1,?)',(json.dumps(broker,sort_keys=True),))
        startup=root/'startup.json'
        startup.write_text(json.dumps(dict(
            schema_version=1,runtime_python=str(Path(sys.executable).resolve()),
            runtime_root=str(runtime.resolve()),runtime_config=str(runtime_config.resolve()),
            state_dir=str(state.resolve()),status_path=str(status.resolve()),
            storage_policy=str(storage.resolve()),monitor_root=str(monitor.resolve()),
            health_config=str(health_cfg.resolve()),dashboard_html=str(dashboard_html.resolve()),
            dashboard_port=18767,health_interval_seconds=60,startup_health_attempts=2,
            startup_health_interval_seconds=1)))
        return dict(root=root,runtime=runtime,monitor=monitor,state=state,status=status,
                    startup=startup,health_cfg=health_cfg)

    def module(self):
        import startup_recovery
        return startup_recovery

    def test_read_only_cold_recovery_preserves_open_position_pending_order_and_baselines(self):
        m=self.module()
        with tempfile.TemporaryDirectory() as td:
            fx=self.make_fixture(td);cfg=m.load_config(fx['startup'])
            before={p.name:p.read_bytes() for p in fx['state'].iterdir() if p.is_file()}
            d=m.inspect_durable_state(cfg)
            after={p.name:p.read_bytes() for p in fx['state'].iterdir() if p.is_file()}
            self.assertEqual(before,after)
            self.assertEqual(d['open_positions'],1);self.assertEqual(d['pending_orders'],1)
            self.assertEqual(d['fills_count'],1);self.assertEqual(d['ledger_count'],1)
            self.assertEqual(d['broker_audit_count'],1);self.assertEqual(d['runtime_audit_count'],1)
            self.assertEqual(d['forward_start_ms'],1000)
            self.assertEqual(d['strategy_start_ms'],1100)
            self.assertEqual(d['research_deadline_ms'],999999)
            self.assertEqual(d['strategy_fill_baseline'],3)
            self.assertEqual(d['strategy_ledger_baseline'],4)

    def test_missing_and_corrupt_durable_state_fail_before_launch(self):
        m=self.module()
        for mode in ('missing-runtime','corrupt-runtime','missing-broker','corrupt-signals'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as td:
                fx=self.make_fixture(td);cfg=m.load_config(fx['startup'])
                target={'missing-runtime':'runtime.sqlite3','corrupt-runtime':'runtime.sqlite3',
                        'missing-broker':'broker.sqlite3','corrupt-signals':'signals.sqlite3'}[mode]
                path=fx['state']/target
                if mode.startswith('missing'):path.unlink()
                else:path.write_bytes(b'not sqlite')
                with self.assertRaises(m.StartupBlocked):
                    m.preflight(cfg)

    def test_real_subprocess_duplicate_namespace_is_detected_and_never_owned(self):
        m=self.module()
        with tempfile.TemporaryDirectory() as td:
            fx=self.make_fixture(td);cfg=m.load_config(fx['startup'])
            proc=subprocess.Popen([sys.executable,'-u',str(fx['runtime']/'paper_runtime_v3.py'),
                                   '--state-dir',str(fx['state']),'--status',str(fx['status'])],
                                  cwd=fx['runtime'])
            try:
                deadline=time.time()+3
                while time.time()<deadline and proc.pid not in m.runtime_processes(cfg.runtime_root,cfg.state_dir,cfg.status_path):
                    time.sleep(.02)
                check=m.preflight(cfg)
                self.assertEqual(check['state'],'already-running')
                self.assertEqual(check['runtime_pids'],[proc.pid])
                with self.assertRaisesRegex(m.StartupBlocked,'second supervisor'):
                    m.run_supervisor(cfg,popen=lambda *a,**k:self.fail('must not spawn duplicate'),
                                     sleep=lambda _:None,health=lambda *a,**k:{})
            finally:
                proc.terminate();proc.wait(timeout=5)

    class Child:
        def __init__(self,sequence=None):
            self.sequence=list(sequence or [None]);self.returncode=None;self.terminated=False
        def poll(self):
            value=self.sequence.pop(0) if len(self.sequence)>1 else self.sequence[0]
            if value is not None:self.returncode=value
            return value
        def terminate(self):self.terminated=True;self.returncode=0
        def wait(self,timeout=None):return 0
        def kill(self):self.terminated=True;self.returncode=-9

    def test_work_overdue_only_is_startup_advisory_with_current_runtime_market_evidence(self):
        m=self.module()
        report=dict(
            operational_healthy=False,
            faults={'work-overdue':'synthetic queued external engineering task is stale'},
            warnings={},
            evidence=dict(heartbeat_age_seconds=1,last_success_age_seconds=1,
                          market_ages=[dict(symbol='ETHUSDT',receipt_age_seconds=1,
                                            source_age_seconds={'bookTicker':1,'depth5':1,'premiumIndex':1})]),
            pending_work=[dict(id='external-task',state='queued',update_age_seconds=7200,
                               activity_unconfirmed=False)])
        readiness=m.startup_readiness(report)
        self.assertTrue(readiness['ready'])
        self.assertEqual(readiness['blocking_faults'],{})
        self.assertEqual(set(readiness['advisory_faults']),{'work-overdue'})
        with tempfile.TemporaryDirectory() as td:
            fx=self.make_fixture(td);cfg=m.load_config(fx['startup'])
            children=[]
            def popen(cmd,**kwargs):
                child=self.Child([None]);children.append(child);return child
            result=m.run_supervisor(cfg,popen=popen,sleep=lambda _:None,
                                    health=lambda *a,**k:report,max_monitor_cycles=1)
            self.assertEqual(result['state'],'test-complete')
            self.assertEqual(len(children),2)
            self.assertTrue(all(c.terminated for c in children))

    def test_startup_readiness_keeps_runtime_source_account_and_storage_faults_fail_closed(self):
        m=self.module()
        for fault in ('runtime-absent','runtime-duplicate','source-freshness',
                      'feed-disconnected','snapshot-unavailable','runtime-error',
                      'storage-capacity-halt','storage-new-risk-inhibited','storage-capacity'):
            with self.subTest(fault=fault):
                readiness=m.startup_readiness(dict(
                    operational_healthy=False,faults={fault:'synthetic'},warnings={},evidence={}))
                self.assertFalse(readiness['ready'])
                self.assertIn(fault,readiness['blocking_faults'])
                self.assertEqual(readiness['advisory_faults'],{})

    def test_activation_probe_reports_user_bus_prerequisite_without_installing_or_starting(self):
        m=self.module()
        class Result:
            def __init__(self,code,stdout='',stderr=''):
                self.returncode=code;self.stdout=stdout;self.stderr=stderr
        calls=[]
        def unavailable(cmd,**kwargs):
            calls.append(cmd);return Result(1,stderr='Failed to connect to bus: No medium found')
        status=m.activation_platform_status(run=unavailable,env={'WSL_DISTRO_NAME':'Synthetic'})
        self.assertFalse(status['user_systemd_bus'])
        self.assertFalse(status['autostart_supported'])
        self.assertTrue(status['manual_supervisor_supported'])
        self.assertIn('Failed to connect to bus',status['reason'])
        self.assertEqual(calls,[['systemctl','--user','show-environment']])
        calls.clear()
        def available(cmd,**kwargs):
            calls.append(cmd);return Result(0,stdout='XDG_RUNTIME_DIR=/run/user/1000\n')
        status=m.activation_platform_status(run=available,env={'WSL_DISTRO_NAME':'Synthetic'})
        self.assertTrue(status['user_systemd_bus'])
        self.assertTrue(status['autostart_supported'])
        self.assertEqual(calls,[['systemctl','--user','show-environment']])

    def test_unavailable_or_stale_source_is_bounded_and_waits_for_operator(self):
        m=self.module()
        for fault in ('source-unavailable','source-stale'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as td:
                fx=self.make_fixture(td);cfg=m.load_config(fx['startup'])
                children=[]
                def popen(*args,**kwargs):
                    child=self.Child([None]);children.append(child);return child
                calls=[]
                def health(*args,**kwargs):
                    calls.append(1)
                    return dict(operational_healthy=False,faults={fault:'synthetic'})
                with self.assertRaisesRegex(m.StartupBlocked,'bounded attempts'):
                    m.run_supervisor(cfg,popen=popen,sleep=lambda _:None,health=health)
                self.assertEqual(len(children),2,'dashboard + runtime only; no restart loop')
                self.assertEqual(len(calls),cfg.startup_health_attempts)
                self.assertTrue(all(c.terminated for c in children))

    def test_nonstorage_runtime_recovery_failure_never_restarts(self):
        m=self.module()
        with tempfile.TemporaryDirectory() as td:
            fx=self.make_fixture(td);cfg=m.load_config(fx['startup'])
            children=[]
            def popen(cmd,**kwargs):
                child=self.Child([None] if not children else [1])
                children.append(child);return child
            health_calls=[]
            with self.assertRaisesRegex(m.StartupBlocked,'runtime failed during cold recovery'):
                m.run_supervisor(cfg,popen=popen,sleep=lambda _:None,
                                 health=lambda *a,**k:(health_calls.append(1) or {'operational_healthy':False}))
            self.assertEqual(len(children),2,'dashboard + exactly one runtime attempt')
            self.assertEqual(health_calls,[],'failed runtime must not be disguised by a health tick')
            self.assertTrue(children[0].terminated)
            self.assertEqual(children[1].returncode,1)

    def test_intentional_storage_stop_is_operator_hold_with_one_final_health_tick_and_no_restart(self):
        m=self.module()
        with tempfile.TemporaryDirectory() as td:
            fx=self.make_fixture(td);cfg=m.load_config(fx['startup'])
            children=[]
            def popen(cmd,**kwargs):
                child=self.Child([None] if not children else [2])
                children.append(child);return child
            calls=[]
            result=m.run_supervisor(cfg,popen=popen,sleep=lambda _:None,
                                    health=lambda *a,**k:(calls.append(1) or {'operational_healthy':False}))
            self.assertEqual(result['state'],'operator-hold')
            self.assertEqual(result['reason'],'intentional-storage-stop')
            self.assertEqual(result['runtime_exit'],2)
            self.assertEqual(len(children),2,'intentional stop must never spawn replacement runtime')
            self.assertEqual(len(calls),1,'exactly one final health observation, not a write loop')

    def test_operator_stop_terminates_owned_children_without_reset_or_recovery_trade(self):
        m=self.module()
        with tempfile.TemporaryDirectory() as td:
            fx=self.make_fixture(td);cfg=m.load_config(fx['startup'])
            before={p.name:p.read_bytes() for p in fx['state'].iterdir() if p.is_file()}
            children=[]
            def popen(cmd,**kwargs):
                child=self.Child([None]);children.append(child);return child
            result=m.run_supervisor(cfg,popen=popen,sleep=lambda _:None,
                                    health=lambda *a,**k:{'operational_healthy':True},
                                    stop_requested=lambda:True)
            self.assertEqual(result['state'],'stopped-by-operator')
            self.assertEqual(before,{p.name:p.read_bytes() for p in fx['state'].iterdir() if p.is_file()})
            self.assertTrue(all(c.terminated for c in children))

    def test_config_and_health_identity_mismatch_fail_closed(self):
        m=self.module()
        with tempfile.TemporaryDirectory() as td:
            fx=self.make_fixture(td);cfg=m.load_config(fx['startup'])
            data=json.loads(fx['health_cfg'].read_text());data['state_dir']=str((fx['root']/'other').resolve())
            fx['health_cfg'].write_text(json.dumps(data))
            with self.assertRaisesRegex(m.StartupBlocked,'health config'):
                m.preflight(cfg)
        with tempfile.TemporaryDirectory() as td:
            fx=self.make_fixture(td);data=json.loads(fx['startup'].read_text())
            data['runtime_root']='relative/path';fx['startup'].write_text(json.dumps(data))
            with self.assertRaises(ValueError):m.load_config(fx['startup'])


if __name__=='__main__':unittest.main()
