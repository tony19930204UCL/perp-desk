"""Bounded cold-start/recovery supervisor for PAPER v3, dashboard and health observer.

This module never installs a service, changes trading state, backfills offline trades or
restarts indefinitely. Operator configuration is explicit and local.
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

from health_watchdog import load_monitor_config, once as health_once, runtime_processes


class StartupBlocked(RuntimeError):
    """Cold recovery is unsafe or ambiguous; operator action is required."""


@dataclass(frozen=True)
class StartupConfig:
    runtime_python: Path
    runtime_root: Path
    runtime_config: Path
    state_dir: Path
    status_path: Path
    storage_policy: Path
    monitor_root: Path
    health_config: Path
    dashboard_html: Path
    dashboard_port: int
    health_interval_seconds: int
    startup_health_attempts: int
    startup_health_interval_seconds: int


def _absolute(data,key):
    value=data.get(key)
    if not isinstance(value,str) or not value:
        raise ValueError('startup config requires '+key)
    path=Path(value)
    if not path.is_absolute():
        raise ValueError(key+' must be absolute')
    return path.resolve()


def load_config(path):
    data=json.loads(Path(path).read_text())
    if not isinstance(data,dict) or data.get('schema_version')!=1:
        raise ValueError('invalid startup config schema')
    cfg=StartupConfig(
        runtime_python=_absolute(data,'runtime_python'),
        runtime_root=_absolute(data,'runtime_root'),
        runtime_config=_absolute(data,'runtime_config'),
        state_dir=_absolute(data,'state_dir'),
        status_path=_absolute(data,'status_path'),
        storage_policy=_absolute(data,'storage_policy'),
        monitor_root=_absolute(data,'monitor_root'),
        health_config=_absolute(data,'health_config'),
        dashboard_html=_absolute(data,'dashboard_html'),
        dashboard_port=data.get('dashboard_port'),
        health_interval_seconds=data.get('health_interval_seconds'),
        startup_health_attempts=data.get('startup_health_attempts'),
        startup_health_interval_seconds=data.get('startup_health_interval_seconds'))
    if type(cfg.dashboard_port) is not int or not 1<=cfg.dashboard_port<=65535:
        raise ValueError('dashboard_port must be an integer TCP port')
    if type(cfg.health_interval_seconds) is not int or not 30<=cfg.health_interval_seconds<=300:
        raise ValueError('health_interval_seconds must be 30..300')
    if type(cfg.startup_health_attempts) is not int or not 1<=cfg.startup_health_attempts<=5:
        raise ValueError('startup_health_attempts must be 1..5')
    if type(cfg.startup_health_interval_seconds) is not int or not 1<=cfg.startup_health_interval_seconds<=30:
        raise ValueError('startup_health_interval_seconds must be 1..30')
    return cfg


def _read_sqlite(path,required_tables):
    path=Path(path)
    if not path.is_file() or path.is_symlink():
        raise StartupBlocked('missing durable state: '+path.name)
    try:
        with sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True) as db:
            check=db.execute('PRAGMA quick_check').fetchone()
            if check != ('ok',):
                raise StartupBlocked('corrupt durable state: '+path.name)
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing=set(required_tables)-tables
            if missing:
                raise StartupBlocked('missing durable tables in '+path.name)
            return db
    except sqlite3.DatabaseError as exc:
        raise StartupBlocked('corrupt durable state: '+path.name) from exc


def inspect_durable_state(cfg):
    """Read-only cold-recovery proof. Never initializes a missing namespace."""
    if not cfg.runtime_python.is_file():
        raise StartupBlocked('configured Python interpreter unavailable')
    for path,label in ((cfg.runtime_root/'paper_runtime_v3.py','runtime script'),
                       (cfg.runtime_config,'runtime config'),
                       (cfg.storage_policy,'storage policy'),
                       (cfg.dashboard_html,'dashboard asset'),
                       (cfg.monitor_root/'health_watchdog.py','health watchdog'),
                       (cfg.monitor_root/'dashboard.py','dashboard server'),
                       (cfg.health_config,'health config')):
        if not path.is_file() or path.is_symlink():
            raise StartupBlocked('missing or unsafe '+label)
    health=load_monitor_config(cfg.health_config)
    expected={'runtime_root':cfg.runtime_root,'state_dir':cfg.state_dir,'status_path':cfg.status_path}
    if any(health[k].resolve()!=v for k,v in expected.items()):
        raise StartupBlocked('health config does not match startup runtime/state/status identity')

    runtime_db=cfg.state_dir/'runtime.sqlite3'
    try:
        with sqlite3.connect('file:'+str(runtime_db.resolve())+'?mode=ro',uri=True) as db:
            if not runtime_db.is_file() or runtime_db.is_symlink():
                raise StartupBlocked('missing durable state: runtime.sqlite3')
            if db.execute('PRAGMA quick_check').fetchone()!=('ok',):
                raise StartupBlocked('corrupt durable state: runtime.sqlite3')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'state','audit'}<=tables:
                raise StartupBlocked('missing durable tables in runtime.sqlite3')
            row=db.execute('SELECT payload FROM state WHERE id=1').fetchone()
            if row is None:
                raise StartupBlocked('missing runtime state row')
            state=json.loads(row[0])
            audit_count=db.execute('SELECT count(*) FROM audit').fetchone()[0]
    except (sqlite3.DatabaseError,json.JSONDecodeError,TypeError,KeyError) as exc:
        if isinstance(exc,StartupBlocked): raise
        raise StartupBlocked('corrupt runtime state') from exc

    config_hash=hashlib.sha256(cfg.runtime_config.read_bytes()).hexdigest()
    if state.get('config_hash')!=config_hash:
        raise StartupBlocked('runtime config hash differs from durable namespace')
    deployment=state.get('deployment')
    if not isinstance(deployment,dict) or deployment.get('version_id')!='H1-PAPER-003':
        raise StartupBlocked('durable namespace is not accepted H1-PAPER-003 deployment')
    for key in ('forward_start_ms','strategy_start_ms','research_deadline_ms',
                'strategy_fill_baseline','strategy_ledger_baseline'):
        if type(state.get(key)) is not int:
            raise StartupBlocked('durable recovery field missing: '+key)

    signals=cfg.state_dir/'signals.sqlite3'
    try:
        with sqlite3.connect('file:'+str(signals.resolve())+'?mode=ro',uri=True) as db:
            if not signals.is_file() or signals.is_symlink() or db.execute('PRAGMA quick_check').fetchone()!=('ok',):
                raise StartupBlocked('missing/corrupt signals.sqlite3')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'h1_versions','h1_state','h1_signals'}<=tables:
                raise StartupBlocked('missing durable signal tables')
    except sqlite3.DatabaseError as exc:
        raise StartupBlocked('missing/corrupt signals.sqlite3') from exc

    broker=cfg.state_dir/'broker.sqlite3'
    try:
        with sqlite3.connect('file:'+str(broker.resolve())+'?mode=ro',uri=True) as db:
            if not broker.is_file() or broker.is_symlink() or db.execute('PRAGMA quick_check').fetchone()!=('ok',):
                raise StartupBlocked('missing/corrupt broker.sqlite3')
            row=db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()
            if row is None: raise StartupBlocked('missing broker state row')
            saved=json.loads(row[0])
    except (sqlite3.DatabaseError,json.JSONDecodeError,TypeError,KeyError) as exc:
        if isinstance(exc,StartupBlocked): raise
        raise StartupBlocked('missing/corrupt broker.sqlite3') from exc

    return dict(
        version_id='H1-PAPER-003',
        forward_start_ms=state['forward_start_ms'],
        strategy_start_ms=state['strategy_start_ms'],
        research_deadline_ms=state['research_deadline_ms'],
        strategy_fill_baseline=state['strategy_fill_baseline'],
        strategy_ledger_baseline=state['strategy_ledger_baseline'],
        runtime_audit_count=audit_count,
        fills_count=len(saved.get('fills',[])),
        ledger_count=len(saved.get('ledger',[])),
        broker_audit_count=len(saved.get('audit',[])),
        open_positions=len(saved.get('positions',{})),
        pending_orders=sum(o.get('status') in ('PENDING','RESTING') for o in saved.get('orders',{}).values()))


def runtime_command(cfg):
    return [str(cfg.runtime_python),'-u',str(cfg.runtime_root/'paper_runtime_v3.py'),
            '--state-dir',str(cfg.state_dir),'--status',str(cfg.status_path),
            '--config',str(cfg.runtime_config),'--storage-policy',str(cfg.storage_policy)]


def dashboard_command(cfg):
    return [str(cfg.runtime_python),str(cfg.monitor_root/'dashboard.py'),
            '--port',str(cfg.dashboard_port),'--status',str(cfg.status_path),
            '--html',str(cfg.dashboard_html)]


def preflight(cfg):
    durable=inspect_durable_state(cfg)
    pids=runtime_processes(cfg.runtime_root,cfg.state_dir,cfg.status_path)
    if len(pids)>1:
        raise StartupBlocked('duplicate exact PAPER v3 runtimes already present')
    if len(pids)==1:
        return dict(state='already-running',runtime_pids=pids,durable=durable)
    return dict(state='ready-to-start',runtime_pids=[],durable=durable)


def _terminate(child,timeout=3):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try: child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill();child.wait(timeout=timeout)


def run_supervisor(cfg,*,popen=subprocess.Popen,sleep=time.sleep,health=health_once,
                   stop_requested=None,max_monitor_cycles=None):
    """Own one runtime/dashboard process set. Never restarts a stopped runtime."""
    check=preflight(cfg)
    if check['state']=='already-running':
        raise StartupBlocked('runtime already active; refuse second supervisor ownership')
    dashboard=None;runtime=None
    stop_requested=stop_requested or (lambda:False)
    try:
        dashboard=popen(dashboard_command(cfg),cwd=cfg.monitor_root)
        sleep(0.05)
        if dashboard.poll() is not None:
            raise StartupBlocked('dashboard failed during startup')
        runtime=popen(runtime_command(cfg),cwd=cfg.runtime_root)
        # The runtime lock is authoritative against a launch race; an immediate
        # nonzero exit is never retried here.
        for attempt in range(cfg.startup_health_attempts):
            sleep(cfg.startup_health_interval_seconds)
            rc=runtime.poll()
            if rc is not None:
                if rc==2:
                    return dict(state='operator-hold',reason='intentional-storage-stop',runtime_exit=rc,
                                durable=check['durable'])
                raise StartupBlocked('runtime failed during cold recovery; operator action required')
            report=health(cfg.monitor_root,runtime_root=cfg.runtime_root,
                          state_dir=cfg.state_dir,status_path=cfg.status_path)
            if report.get('operational_healthy') is True:
                break
        else:
            raise StartupBlocked('startup health did not become current within bounded attempts')

        cycles=0
        while not stop_requested():
            rc=runtime.poll()
            if rc is not None:
                if rc==2:
                    # One final read-only observation only; no restart loop.
                    try:health(cfg.monitor_root,runtime_root=cfg.runtime_root,
                               state_dir=cfg.state_dir,status_path=cfg.status_path)
                    except Exception:pass
                    return dict(state='operator-hold',reason='intentional-storage-stop',
                                runtime_exit=rc,durable=check['durable'])
                return dict(state='operator-hold',reason='runtime-exited-no-auto-restart',
                            runtime_exit=rc,durable=check['durable'])
            if dashboard.poll() is not None:
                return dict(state='operator-hold',reason='dashboard-exited-no-auto-restart',
                            dashboard_exit=dashboard.returncode,durable=check['durable'])
            health(cfg.monitor_root,runtime_root=cfg.runtime_root,
                   state_dir=cfg.state_dir,status_path=cfg.status_path)
            cycles+=1
            if max_monitor_cycles is not None and cycles>=max_monitor_cycles:
                return dict(state='test-complete',durable=check['durable'])
            sleep(cfg.health_interval_seconds)
        return dict(state='stopped-by-operator',durable=check['durable'])
    finally:
        _terminate(runtime)
        _terminate(dashboard)


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--check',action='store_true',help='read-only preflight only')
    args=parser.parse_args(argv)
    try:
        cfg=load_config(args.config)
        if args.check:
            print(json.dumps(preflight(cfg),sort_keys=True))
            return 0
        stop={'value':False}
        def request_stop(signum,frame): stop['value']=True
        signal.signal(signal.SIGTERM,request_stop)
        signal.signal(signal.SIGINT,request_stop)
        result=run_supervisor(cfg,stop_requested=lambda:stop['value'])
        print(json.dumps(result,sort_keys=True))
        return 0 if result['state']=='stopped-by-operator' else 2
    except (OSError,ValueError,StartupBlocked) as exc:
        print('StartupBlocked: '+str(exc),file=sys.stderr,flush=True)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
