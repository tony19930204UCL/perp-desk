"""Real subprocess acceptance for bounded supervisor signal shutdown."""
import hashlib
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

LAB=Path(__file__).resolve().parents[1]
REPO=LAB.parent
SUPERVISOR=REPO/'scripts/paper_startup_supervisor.py'


def free_port():
    s=socket.socket();s.bind(('127.0.0.1',0));port=s.getsockname()[1];s.close();return port


def proc_children(pid):
    path=Path('/proc')/str(pid)/'task'/str(pid)/'children'
    if not path.exists(): return []
    raw=path.read_text().strip()
    return [int(x) for x in raw.split()] if raw else []


def wait_until(fn,timeout=10,interval=.02):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=fn()
        if value:return value
        time.sleep(interval)
    return None


def port_open(port):
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.15):
            return True
    except OSError:
        return False


class RealSupervisorSignalTests(unittest.TestCase):
    def fixture(self,td,*,startup_interval=1):
        root=Path(td);runtime=root/'runtime';monitor=root/'monitor';state=root/'state';shared=root/'shared'
        for p in (runtime,monitor/'shared',monitor/'data/health',state,shared):p.mkdir(parents=True,exist_ok=True)
        port=free_port()
        runtime_config=runtime/'paper_config_v3.json';runtime_config.write_text('{"fixture":"signal-stop"}\n')
        storage=runtime/'storage-policy.json'
        storage.write_text(json.dumps(dict(schema_version=1,warning_bytes=10_000_000,
                                           new_risk_limit_bytes=20_000_000,
                                           exit_reserve_bytes=10_000_000,
                                           min_free_bytes=1,max_exit_cycle_bytes=1_000_000)))
        status=shared/'paper_v2_live.json'
        now=datetime.now(timezone.utc);now_ms=int(now.timestamp()*1000)
        snap=json.loads((LAB/'tests/fixtures/health_paper_fixture.json').read_text())
        snap.update(candidate_not_deployed=False,updated_at=now.isoformat(),latest_error=None,blockers=[])
        snap['feed'].update(connected=True,last_success_at=now.isoformat(),errors_count=0)
        snap['engine'].update(status='running',version_id='H1-PAPER-003',
                              candidate_implementation='paper-engine-v3')
        snap['engine']['deployment']['version_id']='H1-PAPER-003'
        snap['versions']=[dict(created_at=now.isoformat(),status='observing',version_id='H1-PAPER-003')]
        snap['research']=dict(strategy_start_ms=now_ms-3600000,deadline_ms=now_ms+3600000,
                              target_complete_round_trips=30,target_not_guarantee=True,
                              status='unproven',complete_round_trips=0,signals_count=0,
                              blocked_signals_count=0,rejection_categories={})
        for market in snap['markets']:
            market['last_received_at']=now.isoformat()
            market['source_timestamps_ms']=dict.fromkeys(('bookTicker','depth5','premiumIndex'),now_ms)
        seed=runtime/'status_seed.json';seed.write_text(json.dumps(snap))

        runtime_script=runtime/'paper_runtime_v3.py'
        runtime_script.write_text(
            "import argparse,json,signal,time\n"
            "from pathlib import Path\n"
            "p=argparse.ArgumentParser();p.add_argument('--state-dir');p.add_argument('--status');"
            "p.add_argument('--config');p.add_argument('--storage-policy');a=p.parse_args()\n"
            "Path(a.status).write_text((Path(__file__).with_name('status_seed.json')).read_text())\n"
            "stop=False\n"
            "def h(*_):\n global stop;stop=True\n"
            "signal.signal(signal.SIGTERM,h);signal.signal(signal.SIGINT,h)\n"
            "while not stop: time.sleep(.05)\n")

        dashboard=monitor/'dashboard.py'
        dashboard.write_text(
            "import argparse,signal,socket,time\n"
            "p=argparse.ArgumentParser();p.add_argument('--port',type=int);p.add_argument('--status');p.add_argument('--html');a=p.parse_args()\n"
            "s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',a.port));s.listen()\n"
            "stop=False\n"
            "def h(*_):\n global stop;stop=True\n"
            "signal.signal(signal.SIGTERM,h);signal.signal(signal.SIGINT,h)\n"
            "while not stop: time.sleep(.05)\n"
            "s.close()\n")
        (monitor/'health_watchdog.py').write_text('# required monitor identity asset\n')
        html=monitor/'dashboard.html';html.write_text('<html>isolated</html>')
        health_cfg=monitor/'shared/health_monitor_config.json'
        health_cfg.write_text(json.dumps(dict(schema_version=1,runtime_root=str(runtime.resolve()),
                                              state_dir=str(state.resolve()),status_path=str(status.resolve()))))

        config_hash=hashlib.sha256(runtime_config.read_bytes()).hexdigest()
        durable=dict(config_hash=config_hash,fixture=True,forward_start_ms=1000,
                     strategy_start_ms=1100,research_deadline_ms=9_999_999,
                     strategy_fill_baseline=3,strategy_ledger_baseline=4,
                     deployment=dict(version_id='H1-PAPER-003'))
        with sqlite3.connect(state/'runtime.sqlite3') as db:
            db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY,payload TEXT NOT NULL,previous_hash TEXT NOT NULL,hash TEXT NOT NULL)')
            db.execute('INSERT INTO state VALUES(1,?)',(json.dumps(durable,sort_keys=True),))
            db.execute('INSERT INTO audit VALUES(1,?,?,?)',('{"type":"prefix"}','0'*64,'a'*64))
        with sqlite3.connect(state/'signals.sqlite3') as db:
            db.execute('CREATE TABLE h1_versions(version_id TEXT,registration TEXT)')
            db.execute('CREATE TABLE h1_state(version_id TEXT,symbol TEXT,bars TEXT)')
            db.execute('CREATE TABLE h1_signals(version_id TEXT,intent TEXT)')
        broker=dict(meta={'synthetic':True},cash='99',
                    fills=[{'fill_id':'prefix-fill'}],
                    ledger=[{'ledger_id':'prefix-ledger','type':'fee','amount':'-1'}],
                    audit=[{'event':'prefix-audit'}],
                    positions={'ETHUSDT':{'qty':'0.008','entry':'100','stop':'95','opened_ts':1000}},
                    orders={'order:pending':{'status':'PENDING','intent':{'reduce_only':False}}})
        with sqlite3.connect(state/'broker.sqlite3') as db:
            db.execute('CREATE TABLE sim_broker_state(singleton INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute('INSERT INTO sim_broker_state VALUES(1,?)',(json.dumps(broker,sort_keys=True),))

        cfg=root/'startup.json'
        cfg.write_text(json.dumps(dict(schema_version=1,runtime_python=str(Path(sys.executable).resolve()),
                                       runtime_root=str(runtime.resolve()),runtime_config=str(runtime_config.resolve()),
                                       state_dir=str(state.resolve()),status_path=str(status.resolve()),
                                       storage_policy=str(storage.resolve()),monitor_root=str(monitor.resolve()),
                                       health_config=str(health_cfg.resolve()),dashboard_html=str(html.resolve()),
                                       dashboard_port=port,health_interval_seconds=120,
                                       startup_health_attempts=3,startup_health_interval_seconds=startup_interval)))
        return dict(root=root,runtime=runtime,monitor=monitor,state=state,status=status,
                    config=cfg,port=port)

    def launch(self,fx):
        return subprocess.Popen([sys.executable,str(SUPERVISOR),'--config',str(fx['config'])],
                                cwd=REPO,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)

    def wait_periodic(self,proc,fx):
        self.assertEqual(json.loads(fx['config'].read_text())['health_interval_seconds'],120)
        children=wait_until(lambda: proc_children(proc.pid) if len(proc_children(proc.pid))==2 else None,8)
        self.assertIsNotNone(children,'real supervisor did not own exactly two children')
        self.assertTrue(wait_until(lambda:port_open(fx['port']),5),'dashboard listener never opened')
        health=fx['monitor']/'shared/health_status.json'
        self.assertTrue(wait_until(health.exists,8),'startup health never completed')
        # Startup readiness is followed immediately by the first monitor-loop health
        # tick. Require the output to become stable for >1s before signaling; this
        # proves that tick finished and the real supervisor is inside the configured
        # 120s periodic wait rather than between startup/loop observations.
        stable=False
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            stamp=health.stat().st_mtime_ns
            time.sleep(1.2)
            if health.exists() and health.stat().st_mtime_ns==stamp:
                stable=True;break
        self.assertTrue(stable,'health output never became stable inside periodic wait')
        self.assertIsNone(proc.poll())
        return children

    def assert_bounded_stop(self,proc,children,port,sig,*,repeat=False):
        start=time.monotonic();proc.send_signal(sig)
        if repeat:
            time.sleep(.05)
            if proc.poll() is None: proc.send_signal(sig)
        out,err=proc.communicate(timeout=10)
        elapsed=time.monotonic()-start
        self.assertLessEqual(elapsed,10.0,'supervisor exceeded documented stop budget')
        self.assertEqual(proc.returncode,0,(out,err))
        self.assertIn('"state": "stopped-by-operator"',out)
        self.assertTrue(all(not (Path('/proc')/str(pid)).exists() for pid in children),
                        'owned child PID survived supervisor stop')
        self.assertFalse(port_open(port),'owned dashboard listener survived supervisor stop')
        return elapsed

    def test_sigterm_and_sigint_interrupt_real_120s_periodic_wait_within_10s(self):
        for sig in (signal.SIGTERM,signal.SIGINT):
            with self.subTest(sig=sig),tempfile.TemporaryDirectory() as td:
                fx=self.fixture(td)
                before={p.name:p.read_bytes() for p in fx['state'].glob('*.sqlite3')}
                proc=self.launch(fx)
                try:
                    children=self.wait_periodic(proc,fx)
                    self.assert_bounded_stop(proc,children,fx['port'],sig)
                    self.assertEqual(before,{p.name:p.read_bytes() for p in fx['state'].glob('*.sqlite3')})
                finally:
                    if proc.poll() is None:proc.kill();proc.wait()

    def test_signal_interrupts_real_startup_wait_and_repeated_stop_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            fx=self.fixture(td,startup_interval=5);proc=self.launch(fx)
            try:
                children=wait_until(lambda: proc_children(proc.pid) if len(proc_children(proc.pid))==2 else None,8)
                self.assertIsNotNone(children)
                # Signal before the configured 5s first health wait can finish.
                self.assert_bounded_stop(proc,children,fx['port'],signal.SIGTERM,repeat=True)
            finally:
                if proc.poll() is None:proc.kill();proc.wait()

    def test_second_real_supervisor_cannot_take_duplicate_namespace_ownership(self):
        with tempfile.TemporaryDirectory() as td:
            fx=self.fixture(td);first=self.launch(fx)
            try:
                children=self.wait_periodic(first,fx)
                second=self.launch(fx)
                out,err=second.communicate(timeout=5)
                self.assertEqual(second.returncode,2,(out,err))
                self.assertIn('runtime already active; refuse second supervisor ownership',err)
                self.assertEqual(sorted(proc_children(first.pid)),sorted(children))
                self.assertEqual(len(children),2)
                self.assert_bounded_stop(first,children,fx['port'],signal.SIGTERM)
            finally:
                if first.poll() is None:first.kill();first.wait()


if __name__=='__main__':unittest.main()
