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
import threading

from health_watchdog import load_monitor_config, once as health_once, runtime_processes


class StartupBlocked(RuntimeError):
    """Cold recovery is unsafe or ambiguous; operator action is required."""


@dataclass(frozen=True)
class StartupConfig:
    runtime_python: Path
    runtime_root: Path
    runtime_script: str
    runtime_config: Path
    state_dir: Path
    status_path: Path
    storage_policy: Path
    monitor_root: Path
    health_config: Path
    dashboard_html: Path
    dashboard_ledgers: Path | None
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
        runtime_script=data.get('runtime_script','paper_runtime_v3.py'),
        runtime_config=_absolute(data,'runtime_config'),
        state_dir=_absolute(data,'state_dir'),
        status_path=_absolute(data,'status_path'),
        storage_policy=_absolute(data,'storage_policy'),
        monitor_root=_absolute(data,'monitor_root'),
        health_config=_absolute(data,'health_config'),
        dashboard_html=_absolute(data,'dashboard_html'),
        dashboard_ledgers=(_absolute(data,'dashboard_ledgers') if data.get('dashboard_ledgers') is not None else None),
        dashboard_port=data.get('dashboard_port'),
        health_interval_seconds=data.get('health_interval_seconds'),
        startup_health_attempts=data.get('startup_health_attempts'),
        startup_health_interval_seconds=data.get('startup_health_interval_seconds'))
    if cfg.runtime_script not in ('paper_runtime_v3.py','paper_runtime_v4.py'):
        raise ValueError('unsupported explicit runtime_script')
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
    if not cfg.state_dir.is_dir() or cfg.state_dir.is_symlink():
        raise StartupBlocked('missing or unsafe configured state namespace')
    if not cfg.runtime_root.is_dir() or cfg.runtime_root.is_symlink():
        raise StartupBlocked('missing or unsafe runtime root')
    if not cfg.monitor_root.is_dir() or cfg.monitor_root.is_symlink():
        raise StartupBlocked('missing or unsafe monitor root')
    if not cfg.runtime_python.is_file():
        raise StartupBlocked('configured Python interpreter unavailable')
    for path,label in ((cfg.runtime_root/cfg.runtime_script,'runtime script'),
                       (cfg.runtime_config,'runtime config'),
                       (cfg.storage_policy,'storage policy'),
                       (cfg.dashboard_html,'dashboard asset'),
                       *(([(cfg.dashboard_ledgers,'dashboard ledger config')] if cfg.dashboard_ledgers is not None else [])),
                       (cfg.monitor_root/'health_watchdog.py','health watchdog'),
                       (cfg.monitor_root/'dashboard.py','dashboard server'),
                       (cfg.health_config,'health config')):
        if not path.is_file() or path.is_symlink():
            raise StartupBlocked('missing or unsafe '+label)
    if cfg.dashboard_ledgers is not None:
        from dashboard import load_ledger_config
        try:
            load_ledger_config(cfg.dashboard_ledgers,cfg.status_path)
        except (OSError,ValueError,UnicodeError,json.JSONDecodeError) as exc:
            raise StartupBlocked('dashboard ledger configuration invalid') from exc
    health=load_monitor_config(cfg.health_config)
    expected={'runtime_root':cfg.runtime_root,'state_dir':cfg.state_dir,'status_path':cfg.status_path}
    if any(health[k].resolve()!=v for k,v in expected.items()) or health['runtime_script']!=cfg.runtime_script:
        raise StartupBlocked('health config does not match startup runtime/state/status/script identity')

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
    try:
        configured=json.loads(cfg.runtime_config.read_text())
    except (OSError,UnicodeError,json.JSONDecodeError) as exc:
        raise StartupBlocked('runtime config unreadable') from exc
    version=configured.get('version_id')
    expected_script={'H1-PAPER-003':'paper_runtime_v3.py','H1-PAPER-004':'paper_runtime_v4.py'}.get(version)
    if expected_script is None or cfg.runtime_script!=expected_script:
        raise StartupBlocked('runtime config/version/script identity unsupported')
    deployment=state.get('deployment')
    if not isinstance(deployment,dict) or deployment.get('version_id')!=version:
        raise StartupBlocked('durable namespace is not accepted configured PAPER deployment')
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


STARTUP_ADVISORY_FAULTS=frozenset({'work-overdue','work-unavailable'})


def startup_readiness(report):
    """Separate trading/runtime readiness from external engineering-history advisories."""
    if not isinstance(report,dict) or not isinstance(report.get('faults'),dict):
        return dict(ready=False,blocking_faults={'health-report-invalid':'Health report unavailable or malformed'},
                    advisory_faults={})
    faults=report['faults']
    advisory={k:v for k,v in faults.items() if k in STARTUP_ADVISORY_FAULTS}
    blocking={k:v for k,v in faults.items() if k not in STARTUP_ADVISORY_FAULTS}
    return dict(ready=not blocking,blocking_faults=blocking,advisory_faults=advisory)


def activation_platform_status(*,run=subprocess.run,env=None):
    """Read-only activation capability probe. Never installs/enables/starts anything."""
    env=dict(os.environ if env is None else env)
    if not env.get('WSL_INTEROP') and not env.get('WSL_DISTRO_NAME'):
        platform='non-wsl'
    else:
        platform='wsl'
    try:
        result=run(['systemctl','--user','show-environment'],capture_output=True,text=True,timeout=5,env=env)
    except (OSError,subprocess.SubprocessError) as exc:
        return dict(schema_version=1,platform=platform,user_systemd_bus=False,
                    autostart_supported=False,manual_supervisor_supported=True,
                    reason='user-systemd probe unavailable: '+type(exc).__name__)
    if result.returncode!=0:
        detail=(result.stderr or result.stdout or 'user-systemd bus unavailable').strip().splitlines()[0][:300]
        return dict(schema_version=1,platform=platform,user_systemd_bus=False,
                    autostart_supported=False,manual_supervisor_supported=True,
                    reason=detail)
    return dict(schema_version=1,platform=platform,user_systemd_bus=True,
                autostart_supported=True,manual_supervisor_supported=True,
                reason='user-systemd user bus reachable; service installation still operator-owned')


def runtime_command(cfg):
    return [str(cfg.runtime_python),'-u',str(cfg.runtime_root/cfg.runtime_script),
            '--state-dir',str(cfg.state_dir),'--status',str(cfg.status_path),
            '--config',str(cfg.runtime_config),'--storage-policy',str(cfg.storage_policy)]


def dashboard_command(cfg):
    command=[str(cfg.runtime_python),str(cfg.monitor_root/'dashboard.py'),
             '--port',str(cfg.dashboard_port),'--status',str(cfg.status_path),
             '--html',str(cfg.dashboard_html)]
    if cfg.dashboard_ledgers is not None:
        command += ['--ledgers',str(cfg.dashboard_ledgers)]
    return command


def preflight(cfg):
    durable=inspect_durable_state(cfg)
    pids=runtime_processes(cfg.runtime_root,cfg.state_dir,cfg.status_path,cfg.runtime_script)
    if len(pids)>1:
        raise StartupBlocked('duplicate exact configured PAPER runtimes already present')
    if len(pids)==1:
        return dict(state='already-running',runtime_pids=pids,durable=durable)
    return dict(state='ready-to-start',runtime_pids=[],durable=durable)


STOP_BUDGET_SECONDS=10.0


def _stop_owned(children,*,timeout=STOP_BUDGET_SECONDS,monotonic=time.monotonic):
    """Stop only direct children launched by this supervisor under one shared deadline."""
    owned=[child for child in children if child is not None and child.poll() is None]
    if not owned:
        return
    started=monotonic()
    graceful_deadline=started+min(8.0,timeout)
    final_deadline=started+timeout
    for child in owned:
        child.terminate()
    survivors=[]
    for child in owned:
        remaining=max(0.0,graceful_deadline-monotonic())
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            survivors.append(child)
    for child in survivors:
        if child.poll() is None:
            child.kill()
    for child in survivors:
        remaining=max(0.0,final_deadline-monotonic())
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('owned child exceeded supervisor stop budget') from exc


def run_supervisor(cfg,*,popen=subprocess.Popen,sleep=time.sleep,health=health_once,
                   stop_requested=None,stop_event=None,max_monitor_cycles=None):
    """Own one runtime/dashboard process set. Never restarts a stopped runtime."""
    check=preflight(cfg)
    if check['state']=='already-running':
        raise StartupBlocked('runtime already active; refuse second supervisor ownership')
    dashboard=None;runtime=None
    stop_requested=stop_requested or (lambda:False)
    def stopped():
        return bool(stop_requested()) or bool(stop_event is not None and stop_event.is_set())
    def wait_or_stop(seconds):
        if stopped():
            return True
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            sleep(seconds)
        return stopped()
    try:
        dashboard=popen(dashboard_command(cfg),cwd=cfg.monitor_root)
        if wait_or_stop(0.05):
            return dict(state='stopped-by-operator',durable=check['durable'])
        if dashboard.poll() is not None:
            raise StartupBlocked('dashboard failed during startup')
        runtime=popen(runtime_command(cfg),cwd=cfg.runtime_root)
        # The runtime lock is authoritative against a launch race; an immediate
        # nonzero exit is never retried here.
        for attempt in range(cfg.startup_health_attempts):
            if wait_or_stop(cfg.startup_health_interval_seconds):
                return dict(state='stopped-by-operator',durable=check['durable'])
            rc=runtime.poll()
            if rc is not None:
                if rc==2:
                    try:health(cfg.monitor_root,runtime_root=cfg.runtime_root,
                               state_dir=cfg.state_dir,status_path=cfg.status_path,
                               runtime_script=cfg.runtime_script)
                    except Exception:pass
                    return dict(state='operator-hold',reason='intentional-storage-stop',runtime_exit=rc,
                                durable=check['durable'])
                raise StartupBlocked('runtime failed during cold recovery; operator action required')
            report=health(cfg.monitor_root,runtime_root=cfg.runtime_root,
                          state_dir=cfg.state_dir,status_path=cfg.status_path,
                          runtime_script=cfg.runtime_script)
            readiness=startup_readiness(report)
            if readiness['ready']:
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
                               state_dir=cfg.state_dir,status_path=cfg.status_path,
                               runtime_script=cfg.runtime_script)
                    except Exception:pass
                    return dict(state='operator-hold',reason='intentional-storage-stop',
                                runtime_exit=rc,durable=check['durable'])
                return dict(state='operator-hold',reason='runtime-exited-no-auto-restart',
                            runtime_exit=rc,durable=check['durable'])
            if dashboard.poll() is not None:
                return dict(state='operator-hold',reason='dashboard-exited-no-auto-restart',
                            dashboard_exit=dashboard.returncode,durable=check['durable'])
            health(cfg.monitor_root,runtime_root=cfg.runtime_root,
                   state_dir=cfg.state_dir,status_path=cfg.status_path,
                   runtime_script=cfg.runtime_script)
            cycles+=1
            if max_monitor_cycles is not None and cycles>=max_monitor_cycles:
                return dict(state='test-complete',durable=check['durable'])
            if wait_or_stop(cfg.health_interval_seconds):
                break
        return dict(state='stopped-by-operator',durable=check['durable'])
    finally:
        _stop_owned((runtime,dashboard))


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--check',action='store_true',help='read-only durable/process preflight only')
    parser.add_argument('--check-activation',action='store_true',
                        help='read-only user-systemd/autostart platform capability probe')
    args=parser.parse_args(argv)
    try:
        cfg=load_config(args.config)
        if args.check_activation:
            result=activation_platform_status()
            print(json.dumps(result,sort_keys=True))
            return 0 if result['autostart_supported'] else 2
        if args.check:
            print(json.dumps(preflight(cfg),sort_keys=True))
            return 0
        stop_event=threading.Event()
        def request_stop(signum,frame):
            # Signal handlers only wake the main loop. Child signaling/cleanup stays
            # in normal control flow and is restricted to exactly owned processes.
            stop_event.set()
        signal.signal(signal.SIGTERM,request_stop)
        signal.signal(signal.SIGINT,request_stop)
        result=run_supervisor(cfg,stop_event=stop_event)
        print(json.dumps(result,sort_keys=True))
        return 0 if result['state']=='stopped-by-operator' else 2
    except (OSError,ValueError,StartupBlocked) as exc:
        print('StartupBlocked: '+str(exc),file=sys.stderr,flush=True)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
