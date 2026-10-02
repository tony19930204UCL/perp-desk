"""PAPER-only read-only operational observer; never runs or stops trading."""
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
import fcntl
import json
import os
import tempfile
from dashboard import validate_snapshot

ROOT_VERSION = 'H1-PAPER-002'
MAX_AGE = 60

def age(stamp, now):
    dt = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if dt.utcoffset() is None:
        raise ValueError('timestamp lacks timezone')
    return (now-dt).total_seconds()

def evaluate(snapshot, work, now, process_present=None, storage=None):
    faults = {}; evidence = {}
    try:
        validate_snapshot(snapshot)
        if (snapshot['mode'] != 'paper' or snapshot.get('candidate_not_deployed') is not False
                or snapshot['engine']['version_id'] != ROOT_VERSION
                or snapshot['engine'].get('candidate_implementation') != 'paper-engine-v2'):
            raise ValueError('expected deployed PAPER v2 identity')
        heartbeat = age(snapshot['updated_at'], now)
        success = age(snapshot['feed']['last_success_at'], now)
        evidence = {k: snapshot.get(k) for k in ('updated_at','latest_error','blockers')}
        evidence.update(errors_count=snapshot['feed']['errors_count'], gaps_count=snapshot['feed']['gaps_count'],
                        heartbeat_age_seconds=heartbeat, last_success_age_seconds=success,
                        engine_version=snapshot['engine']['version_id'], markets=snapshot['markets'])
        if not 0 <= heartbeat <= MAX_AGE:
            faults['heartbeat-stale'] = 'Runtime snapshot stale or future'
        if not snapshot['feed']['connected'] or not 0 <= success <= MAX_AGE:
            faults['feed-disconnected'] = 'Feed disconnected, stale or future last-success timestamp'
        if snapshot.get('latest_error') or snapshot['blockers'] or snapshot['engine']['status'] in ('error','halted'):
            faults['runtime-error'] = str(snapshot.get('latest_error') or snapshot['blockers'] or snapshot['engine']['status'])[:2000]
        if not snapshot['markets']:
            faults['source-freshness'] = 'No raw market timestamps'
        ages = []
        for market in snapshot['markets']:
            try:
                receipt = age(market['last_received_at'], now)
                source_ages = {}
                for endpoint in ('bookTicker','depth5','premiumIndex'):
                    stamp = market['source_timestamps_ms'].get(endpoint)
                    if type(stamp) is not int:
                        raise ValueError('missing or malformed raw source timestamp')
                    source_ages[endpoint] = now.timestamp()-stamp/1000
                ages.append(dict(symbol=market['symbol'], receipt_age_seconds=receipt, source_age_seconds=source_ages))
                if any(not 0 <= v <= MAX_AGE for v in [receipt, *source_ages.values()]):
                    faults['source-freshness'] = 'Raw source or receipt timestamp stale/future'
            except (ValueError, TypeError, KeyError, AttributeError):
                faults['source-freshness'] = 'Raw source or receipt timestamp invalid'
        evidence['market_ages'] = ages
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        faults['snapshot-unavailable'] = 'Live PAPER snapshot unavailable, malformed or wrong runtime identity'
        if isinstance(snapshot, dict):
            evidence['latest_error'] = snapshot.get('latest_error')
    if process_present is not True:
        faults['runtime-absent'] = 'Exact live runtime process absent or unverified'
    pending = []
    try:
        if not isinstance(work, dict) or not isinstance(work.get('tasks'), list):
            raise ValueError('invalid work')
        for task in work['tasks']:
            if task['state'] not in ('completed','cancelled'):
                elapsed = age(task['updated_at'], now)
                active = task['state'] in ('running','testing','verifying')
                pending.append(dict(id=task['id'], state=task['state'], updated_at=task['updated_at'],
                                    update_age_seconds=elapsed, activity_unconfirmed=active and elapsed>300))
                if elapsed < 0 or elapsed > (300 if active else 3600):
                    faults['work-overdue'] = 'Pending work update overdue or future; observation is not execution proof'
    except (ValueError, TypeError, KeyError, AttributeError):
        faults['work-unavailable'] = 'Engineering work status unavailable or malformed'
    if storage is not None:
        try:
            if storage['used_bytes'] >= storage['budget_bytes'] or storage['free_bytes'] <= storage['min_free_bytes']:
                faults['storage-capacity'] = 'Storage budget or minimum free-space threshold exceeded'
        except (KeyError, TypeError):
            faults['storage-capacity'] = 'Storage capacity unverified'
    return dict(operational_healthy=not faults, faults=faults, evidence=evidence, pending_work=pending,
                storage=storage, service_restart_limitation='Windows/WSL startup recovery not verified; watcher never restarts trading')

@contextmanager
def state_lock(path):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield

def atomic_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)

def read_state(path):
    path = Path(path)
    if not path.exists(): return dict(schema_version=1, incidents={})
    d = json.loads(path.read_text())
    if d.get('schema_version') != 1 or not isinstance(d.get('incidents'),dict):
        raise ValueError('incident state invalid; preserve evidence, do not reset')
    return d

def link_incident(path, incident_id, task_id):
    """Explicit parent linkage only. Never modifies work_status or claims a task."""
    if not isinstance(task_id,str) or not task_id.strip() or len(task_id)>200:
        raise ValueError('invalid task id')
    with state_lock(path):
        state = read_state(path)
        incident = next(x for x in state['incidents'].values() if x['id']==incident_id)
        incident['linked_task_id'] = task_id
        atomic_json(path,state)

def resolve_incident(path, incident_id, proof, now):
    """Parent explicitly accepts tested root-cause resolution; never inferred from recovery."""
    if (not isinstance(proof,dict) or proof.get('tested_resolution') is not True
            or proof.get('accepted_by_parent') is not True
            or not isinstance(proof.get('evidence_paths'),list) or not proof['evidence_paths']
            or any(not isinstance(p,str) or not Path(p).is_file() for p in proof['evidence_paths'])):
        raise ValueError('explicit parent acceptance and existing tested-resolution evidence required')
    with state_lock(path):
        state=read_state(path)
        incident=next(i for i in state['incidents'].values() if i['id']==incident_id)
        if incident['status']!='recovered_monitoring':
            raise ValueError('active or already resolved incident cannot be resolved')
        incident.update(status='resolved', resolved_at=now.isoformat(), resolution_evidence=proof)
        atomic_json(path,state)

def record(state_path, output_path, report, now):
    """Persist deduplicated incidents. Recovery is never engineering completion."""
    with state_lock(state_path):
        state = read_state(state_path); stamp = now.isoformat()
        report = dict(report, faults=dict(report['faults']))
        counter = report['evidence'].get('errors_count')
        baseline = state.get('error_baseline')
        growth = dict(baseline=baseline, observed_at=stamp, current_count=counter,
                      delta=None, elapsed_seconds=None, rate_per_minute=None)
        if type(counter) is int:
            if baseline:
                elapsed = age(baseline['observed_at'], now)
                delta = counter-baseline['count']
                growth.update(delta=delta, elapsed_seconds=elapsed)
                if delta < 0 or elapsed < 0:
                    report['faults']['counter-reset'] = 'Error counter decreased or clock regressed; baseline restarted'
                elif elapsed > 0:
                    growth['rate_per_minute'] = delta*60/elapsed
                    if delta > 0:
                        report['faults']['error-growth'] = 'Cumulative errors increased over explicit observation baseline'
            if not baseline or now.isoformat()!=baseline['observed_at']:
                state['error_baseline'] = dict(count=counter, observed_at=stamp)
        report['error_growth'] = growth
        report['operational_healthy'] = not report['faults']
        samples = state.setdefault('samples', [])
        samples.append(dict(checked_at=stamp, errors_count=counter, faults=sorted(report['faults']),
                            operational_healthy=report['operational_healthy'], storage=report.get('storage')))
        state['samples'] = samples[-240:]
        hourly=state.setdefault('hourly_samples',[])
        hour=now.astimezone(timezone.utc).replace(minute=0,second=0,microsecond=0).isoformat()
        bucket=next((h for h in hourly if h['hour']==hour),None)
        if bucket is None:
            bucket=dict(hour=hour,observations=0,fault_observations=0,first_observed=stamp,last_observed=stamp,
                        first_errors_count=counter,last_errors_count=counter,max_errors_count=counter,storage_max_bytes=0)
            hourly.append(bucket)
        bucket['observations']+=1;bucket['fault_observations']+=int(bool(report['faults']))
        bucket['last_observed']=stamp;bucket['last_errors_count']=counter
        if type(counter) is int:
            bucket['max_errors_count']=max(counter,bucket['max_errors_count'] or 0)
        if isinstance(report.get('storage'),dict):
            bucket['storage_max_bytes']=max(bucket['storage_max_bytes'],report['storage'].get('used_bytes',0))
        state['hourly_samples']=hourly[-720:]
        incidents = state['incidents']
        for key, message in report['faults'].items():
            if key not in incidents:
                incidents[key] = dict(id='paper-v2:'+key, kind=key, first_seen=stamp, status='open',
                                      linked_task_id=None, last_error=None, recovery_observations=0)
            i = incidents[key]
            i.update(status='open', last_observed=stamp, recovery_observations=0, message=message,
                     evidence=report['evidence'])
            if report['evidence'].get('latest_error'):
                i['last_error'] = report['evidence']['latest_error']
                i['last_error_observed_at'] = stamp
        for key, i in incidents.items():
            if key not in report['faults'] and i['status'] == 'open':
                i['recovery_observations'] += 1
                if i['recovery_observations'] >= 3:
                    i.update(status='recovered_monitoring', recovered_at=stamp)
        overview = [{'id':i['id'],'status':i['status'],'linked_task_id':i['linked_task_id'],
                     'last_error':i.get('last_error'), 'message':i.get('message')} for i in sorted(incidents.values(),key=lambda x:x['id'])]
        signature = json.dumps(dict(incidents=overview, faults=sorted(report['faults']),
                                    pending=[(t['id'],t['state'],t['activity_unconfirmed']) for t in report['pending_work']]),sort_keys=True)
        changed = signature != state.get('delivery_signature')
        state['delivery_signature'] = signature
        output = dict(report, schema_version=1, available=True, mode='paper', checked_at=stamp,
                      changed=changed, incidents=list(incidents.values()),
                      engineering_resolved=all(i['status']=='resolved' for i in incidents.values()))
        atomic_json(state_path,state); atomic_json(output_path,output)
        return output

def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError, UnicodeError):
        return None

def runtime_processes(root):
    """Only exact cwd, executable script and live namespace count, not substring matches."""
    found = []
    root = Path(root).resolve()
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try:
            args = (proc/'cmdline').read_bytes().decode().strip('\0').split('\0')
            if (proc/'cwd').resolve() != root: continue
            if len(args)<2 or not Path(args[0]).name.startswith('python'): continue
            script_index=1
            while script_index<len(args) and args[script_index] in ('-u','-B','-E','-s','-S','-I','-O','-OO'):
                script_index+=1
            if script_index>=len(args) or (root/args[script_index]).resolve()!=root/'paper_runtime_v2.py': continue
            def option(key):
                n=args.index(key); return (root/args[n+1]).resolve()
            if option('--state-dir')!=root/'data/paper-v2' or option('--status')!=root/'shared/paper_v2_live.json': continue
            found.append(int(proc.name))
        except (OSError, ValueError, IndexError, UnicodeError):
            continue
    return found

def storage_status(root):
    """Only stat files; never opens, truncates or prunes trading databases."""
    import shutil
    root = Path(root)
    sizes = {str(p.relative_to(root)):p.stat().st_size for p in (root/'data/paper-v2').glob('*') if p.is_file()}
    disk = shutil.disk_usage(root)
    return dict(used_bytes=sum(sizes.values()), budget_bytes=512*1024*1024,
                free_bytes=disk.free, min_free_bytes=1024*1024*1024, files=sizes)

def once(root, now=None, process_present=None):
    from work_status import load_status
    root = Path(root).resolve(); now = now or datetime.now(timezone.utc)
    snapshot = read_json(root/'shared/paper_v2_live.json')
    try: work = load_status(root/'shared/work_status.json', now)
    except (OSError, ValueError, UnicodeError, TypeError, KeyError, OverflowError): work = None
    pids = runtime_processes(root) if process_present is None else []
    if process_present is None: process_present = len(pids)==1
    try: storage = storage_status(root)
    except OSError: storage = {}
    report = evaluate(snapshot, work, now, process_present, storage)
    report['runtime_pids'] = pids
    state = root/'data/health/incident_state.json'; output=root/'shared/health_status.json'
    try:
        return record(state,output,report,now)
    except (OSError, ValueError, TypeError, KeyError):
        # Do not erase corrupt incident evidence. A fixed unavailable output is not green.
        unavailable = dict(schema_version=1, available=False, mode='paper', checked_at=now.isoformat(),
                           operational_healthy=False, engineering_resolved=False,
                           faults={'watchdog-state-unavailable':'Incident state unavailable; retained for diagnosis'},
                           incidents=[], pending_work=report['pending_work'], changed=True)
        with state_lock(state):
            previous = read_json(output)
            unavailable['first_seen']=now.isoformat()
            if isinstance(previous,dict) and previous.get('available') is False and previous.get('faults') == unavailable['faults']:
                unavailable['changed'] = False
                unavailable['first_seen']=previous.get('first_seen',previous.get('checked_at',now.isoformat()))
            atomic_json(output,unavailable)
        return unavailable

def delivery(report):
    if not report.get('changed'): return ''
    return json.dumps(dict(mode='PAPER ONLY', operational_healthy=report['operational_healthy'],
                           engineering_resolved=report['engineering_resolved'], faults=sorted(report['faults']),
                           incidents=[dict(id=i['id'],status=i['status'],linked_task_id=i['linked_task_id'],
                                           last_error=i.get('last_error')) for i in report['incidents']]),
                      sort_keys=True, ensure_ascii=False)

def engineering_gate(root, now=None):
    """At most one wake per unowned critical incident/evidence generation, never a task claim."""
    from work_status import load_status
    import hashlib
    root=Path(root);now=now or datetime.now(timezone.utc)
    report=read_json(root/'shared/health_status.json') or {}
    try: tasks=load_status(root/'shared/work_status.json',now)['tasks']
    except (OSError, ValueError, TypeError, KeyError): tasks=[]
    active={t['id'] for t in tasks if t['state'] in ('running','testing','verifying') and not t['activity_unconfirmed']}
    owners={'runtime-error':'batch-time-recurrence','error-growth':'batch-time-recurrence',
            'feed-disconnected':'batch-time-recurrence','source-freshness':'batch-time-recurrence',
            'runtime-absent':'restart-autostart','storage-capacity':'storage-capacity',
            'snapshot-unavailable':'reliability-watchdog','counter-reset':'reliability-watchdog',
            'watchdog-state-unavailable':'reliability-watchdog'}
    candidates=list(report.get('incidents',[]))
    if report.get('available') is False and 'watchdog-state-unavailable' in report.get('faults',{}):
        candidates.append(dict(id='paper-v2:watchdog-state-unavailable',kind='watchdog-state-unavailable',
                               status='open',first_seen=report.get('first_seen'),linked_task_id=None,
                               message=report['faults']['watchdog-state-unavailable']))
    eligible=[]
    for i in candidates:
        if i['status']!='open' or i.get('kind') not in owners: continue
        if (i.get('linked_task_id') in active or owners[i['kind']] in active):continue
        eligible.append(i)
    state_path=root/'data/health/engineering_gate.json'
    with state_lock(state_path):
        state=read_json(state_path)
        if state is None and state_path.exists():raise ValueError('engineering gate state invalid; do not erase dispatch evidence')
        state=state or {'seen':[]}
        fresh=[]
        for i in eligible:
            token=hashlib.sha256(json.dumps([i['id'],i['first_seen'],i.get('recovered_at'),i.get('last_error'),i.get('message')],sort_keys=True).encode()).hexdigest()
            if token not in state['seen']:
                state['seen'].append(token);fresh.append(i['id'])
        # Fixed incident kinds, bounded retained generation tokens. No timestamps in delivery signature.
        state['seen']=state['seen'][-512:]
        state['checked_at']=now.isoformat();atomic_json(state_path,state)
    return dict(wakeAgent=bool(fresh),incident_ids=fresh,
                context=dict(incident_ids=fresh,health_path=str(root/'shared/health_status.json'),
                             work_path=str(root/'shared/work_status.json'),note='Wake is not execution proof; recheck active owners and actual handles before staged investigation'))

def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--once',action='store_true')
    group.add_argument('--link',metavar='INCIDENT_ID')
    parser.add_argument('--task',metavar='WORK_TASK_ID')
    args=parser.parse_args(argv); root=Path(__file__).resolve().parent
    if args.link:
        from work_status import load_status
        work=load_status(root/'shared/work_status.json')
        if not any(t['id']==args.task and t['state'] not in ('completed','cancelled') for t in work['tasks']):
            parser.error('link requires existing noncompleted task; watcher never creates or claims work')
        link_incident(root/'data/health/incident_state.json',args.link,args.task)
    report=once(root)
    message=delivery(report)
    if message: print(message)
    return 0  # Successful collection can report operational faults; unexpected infrastructure errors still raise.

if __name__=='__main__':
    raise SystemExit(main())

