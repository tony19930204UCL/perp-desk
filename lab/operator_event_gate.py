#!/usr/bin/env python3
"""Deterministic read-only operator event gate for Issue #22.

Reads existing health/snapshot/work/timer evidence, writes only an isolated gate
namespace, and emits Hermes pre-check control JSON. It never starts/stops trading,
writes health, edits ledgers, changes research windows, or sends exchange orders.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

SCHEMA=1
TERMINAL={'completed','blocked'}
ACTIVE={'claimed'}
SUPPORTED_IMPL={'paper-engine-v3','paper-engine-v4'}
FALSE_BYTES='{"wakeAgent":false}'

class GateError(RuntimeError):
    pass

def canonical(v):
    return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False)

def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as s:
            json.dump(data,s,sort_keys=True,separators=(',',':'),ensure_ascii=False)
            s.flush();os.fsync(s.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError,UnicodeError):
        return None

def epoch_ms(value):
    if isinstance(value,int): return value
    if not isinstance(value,str): raise ValueError('timestamp required')
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.utcoffset() is None: raise ValueError('timestamp lacks timezone')
    return int(dt.timestamp()*1000)

def safe_ref(value):
    return isinstance(value,str) and value and len(value)<=240 and not value.startswith('/') and ':\\' not in value

def load_config(path):
    raw=json.loads(Path(path).read_text())
    if not isinstance(raw,dict) or raw.get('schema_version')!=1 or raw.get('enabled') is not True:
        raise GateError('enabled schema_version=1 gate config required')
    required=('namespace_dir','health_path','snapshot_path','work_path')
    if any(not isinstance(raw.get(k),str) or not raw[k] for k in required):
        raise GateError('missing gate path config')
    paths={k:Path(raw[k]) for k in required}
    if not all(p.is_absolute() for p in paths.values()):
        raise GateError('gate source/namespace paths must be absolute')
    raw['_paths']=paths
    raw.setdefault('hold_path',None)
    if raw['hold_path'] is not None:
        hp=Path(raw['hold_path'])
        if not hp.is_absolute(): raise GateError('hold_path must be absolute')
        raw['_hold_path']=hp
    else: raw['_hold_path']=None
    raw.setdefault('timer_sources',[])
    if not isinstance(raw['timer_sources'],list) or len(raw['timer_sources'])>16:
        raise GateError('timer_sources must be bounded list')
    for item in raw['timer_sources']:
        if not isinstance(item,dict) or not safe_ref(item.get('id')) or item.get('kind') not in ('paper_status','discovery_state','registration'):
            raise GateError('invalid timer source')
        p=Path(item.get('path',''))
        if not p.is_absolute(): raise GateError('timer source path must be absolute')
        item['_path']=p
    raw.setdefault('health_stale_seconds',180)
    raw.setdefault('snapshot_stale_seconds',180)
    raw.setdefault('active_work_overdue_seconds',600)
    raw.setdefault('lease_seconds',300)
    raw.setdefault('retry_backoff_seconds',[300,900,1800])
    raw.setdefault('max_attempts',4)
    raw.setdefault('max_records',512)
    ints=('health_stale_seconds','snapshot_stale_seconds','active_work_overdue_seconds','lease_seconds','max_attempts','max_records')
    if any(type(raw[x]) is not int or raw[x]<=0 for x in ints):
        raise GateError('positive integer gate limits required')
    if (not isinstance(raw['retry_backoff_seconds'],list) or not raw['retry_backoff_seconds']
            or any(type(x) is not int or x<=0 for x in raw['retry_backoff_seconds'])):
        raise GateError('positive bounded retry backoff required')
    return raw

class Gate:
    def __init__(self,config,*,now_ms=None,process_count=None):
        self.cfg=config
        self.root=config['_paths']['namespace_dir']
        self.root.mkdir(parents=True,exist_ok=True)
        self.state_path=self.root/'gate_state.json'
        self.status_path=self.root/'gate_status.json'
        self.lock_path=self.root/'gate_state.lock'
        self.now=int(time.time()*1000) if now_ms is None else now_ms
        self.process_count_override=process_count

    def _initial(self):
        return dict(schema_version=SCHEMA,subjects={},records={},sequence=0,
                    scheduler_ticks=0,model_wakes=0,capacity_blocked=False)

    def _load(self):
        if not self.state_path.exists(): return self._initial()
        state=json.loads(self.state_path.read_text())
        if (not isinstance(state,dict) or state.get('schema_version')!=SCHEMA
                or not isinstance(state.get('subjects'),dict) or not isinstance(state.get('records'),dict)):
            raise GateError('gate state corrupt; preserve file and investigate')
        return state

    def _lock(self):
        self.lock_path.parent.mkdir(parents=True,exist_ok=True)
        stream=self.lock_path.open('a+')
        fcntl.flock(stream,fcntl.LOCK_EX)
        return stream

    def _event_id(self,subject,transition,generation):
        raw=f'{subject}|{transition}|{generation}'.encode()
        return 'gate-'+hashlib.sha256(raw).hexdigest()[:20]

    def _transition(self,state,subject,transition,*,actionable,kind,evidence_ref,permitted_next_action,details=None):
        prior=state['subjects'].get(subject)
        if prior and prior.get('transition')==transition:
            return None
        generation=(prior or {}).get('generation',0)+1
        state['subjects'][subject]=dict(transition=transition,generation=generation)
        if not actionable:
            return None
        if len(state['records'])>=self.cfg['max_records']:
            state['capacity_blocked']=True
            return None
        event_id=self._event_id(subject,transition,generation)
        record=dict(event_id=event_id,subject=subject,kind=kind,transition=transition,
                    generation=generation,status='queued',attempts=0,not_before_ms=self.now,
                    evidence_ref=evidence_ref,permitted_next_action=permitted_next_action,
                    details=details or {},claim=None,completion=None,blocked=None)
        state['records'][event_id]=record
        state['sequence']+=1
        return record

    def _runtime_count(self):
        if self.process_count_override is not None:
            return self.process_count_override
        runtime=self.cfg.get('runtime_identity')
        if not runtime: return None
        required=('runtime_root','state_dir','status_path','script')
        if any(not isinstance(runtime.get(k),str) or not runtime[k] for k in required):
            raise GateError('invalid runtime_identity config')
        rr=Path(runtime['runtime_root']).resolve();sd=Path(runtime['state_dir']).resolve();sp=Path(runtime['status_path']).resolve()
        if not rr.is_absolute() or not sd.is_absolute() or not sp.is_absolute():
            raise GateError('runtime identity paths must be absolute')
        found=0
        proc=Path('/proc')
        if not proc.is_dir(): return None
        for item in proc.iterdir():
            if not item.name.isdigit(): continue
            try:
                args=(item/'cmdline').read_bytes().decode().strip('\0').split('\0')
                if (item/'cwd').resolve()!=rr: continue
                if len(args)<2 or not Path(args[0]).name.startswith('python'): continue
                idx=1
                while idx<len(args) and args[idx] in ('-u','-B','-E','-s','-S','-I','-O','-OO'): idx+=1
                if idx>=len(args): continue
                script=Path(args[idx]);script=(rr/script).resolve() if not script.is_absolute() else script.resolve()
                if script!=rr/runtime['script']: continue
                def opt(name):
                    n=args.index(name);p=Path(args[n+1]);return (rr/p).resolve() if not p.is_absolute() else p.resolve()
                if opt('--state-dir')!=sd or opt('--status')!=sp: continue
                found+=1
            except (OSError,ValueError,IndexError,UnicodeError):
                continue
        return found

    def _hold(self):
        path=self.cfg['_hold_path']
        if path is None:return None
        data=read_json(path)
        if not isinstance(data,dict): return None
        if data.get('active') is True and data.get('operator_confirmed') is True:
            reason=data.get('reason')
            if reason not in ('storage_protection','operator_hold','manual_hold'):
                return None
            return dict(reason=reason,evidence_ref='hold:operator-confirmed')
        return None

    def _observe_health(self,state,hold):
        data=read_json(self.cfg['_paths']['health_path'])
        if not isinstance(data,dict) or data.get('schema_version')!=1:
            self._transition(state,'health-source','hold-fault' if hold is not None else 'fault',
                             actionable=hold is None,kind='health',
                             evidence_ref='health:status',permitted_next_action='inspect-local-health-evidence',
                             details={'reason':'missing_or_corrupt'})
            return
        try: stale=self.now-epoch_ms(data.get('checked_at'))>self.cfg['health_stale_seconds']*1000
        except Exception: stale=True
        prior=state['subjects'].get('health-source',{}).get('transition')
        health_transition=('hold-stale' if hold is not None and stale else 'stale' if stale else 'normal')
        health_actionable=(hold is None and (stale or (health_transition=='normal' and prior in ('fault','stale'))))
        self._transition(state,'health-source',health_transition,
                         actionable=health_actionable,kind='health',
                         evidence_ref='health:status',permitted_next_action='inspect-local-health-evidence',
                         details={'reason':'stale'} if stale else {})
        for inc in data.get('incidents',[]) if isinstance(data.get('incidents'),list) else []:
            if not isinstance(inc,dict) or not safe_ref(inc.get('id')): continue
            status=inc.get('status')
            if status not in ('open','recovered_monitoring','resolved'): continue
            subject='incident:'+inc['id']
            prior=state['subjects'].get(subject,{}).get('transition')
            transition=('hold:'+status) if hold is not None else status
            actionable=(hold is None and (status=='open' or
                        (status in ('recovered_monitoring','resolved') and prior=='open')))
            self._transition(state,subject,transition,actionable=actionable,kind='incident',
                             evidence_ref='health:incident:'+inc['id'],
                             permitted_next_action='inspect-local-health-and-work',
                             details={'incident_id':inc['id']})

    def _observe_snapshot(self,state,hold):
        snap=read_json(self.cfg['_paths']['snapshot_path'])
        fault=None
        if not isinstance(snap,dict): fault='missing_or_corrupt'
        else:
            try:
                impl=snap.get('engine',{}).get('candidate_implementation')
                if snap.get('mode')!='paper' or impl not in SUPPORTED_IMPL: fault='unsupported_identity'
                elif self.now-epoch_ms(snap.get('updated_at'))>self.cfg['snapshot_stale_seconds']*1000: fault='stale'
            except Exception: fault='invalid_timestamp'
        prior=state['subjects'].get('paper-snapshot',{}).get('transition')
        snapshot_transition=('hold-fault' if hold is not None and fault else 'fault' if fault else 'normal')
        snapshot_actionable=(hold is None and (fault is not None or (snapshot_transition=='normal' and prior=='fault')))
        self._transition(state,'paper-snapshot',snapshot_transition,
                         actionable=snapshot_actionable,kind='snapshot',
                         evidence_ref='paper:snapshot',permitted_next_action='inspect-local-paper-status',
                         details={'reason':fault} if fault else {})

    def _observe_process(self,state,hold):
        count=self._runtime_count()
        if hold is not None:
            self._transition(state,'runtime-process','operator_hold',actionable=False,kind='runtime',
                             evidence_ref=hold['evidence_ref'],permitted_next_action='do-not-restart')
            return
        if count is None: transition='unverified'
        elif count==1: transition='normal'
        elif count==0: transition='absent'
        else: transition='duplicate'
        prior=state['subjects'].get('runtime-process',{}).get('transition')
        self._transition(state,'runtime-process',transition,
                         actionable=(transition!='normal' or prior in ('absent','duplicate','unverified')),kind='runtime',
                         evidence_ref='runtime:identity',permitted_next_action='inspect-runtime-ownership-no-auto-restart',
                         details={'classification':transition})

    def _observe_work(self,state):
        work=read_json(self.cfg['_paths']['work_path'])
        if not isinstance(work,dict) or work.get('schema_version')!=1 or not isinstance(work.get('tasks'),list):
            self._transition(state,'work-source','fault',actionable=True,kind='work',
                             evidence_ref='work:status',permitted_next_action='inspect-local-work-source',
                             details={'reason':'missing_or_corrupt'})
            return
        self._transition(state,'work-source','normal',actionable=False,kind='work',
                         evidence_ref='work:status',permitted_next_action='inspect-local-work-source')
        seen=set()
        for task in work['tasks'][:100]:
            if not isinstance(task,dict) or not safe_ref(task.get('id')): continue
            tid=task['id'];seen.add(tid);status=task.get('state')
            subject='work:'+tid
            if status in ('completed','cancelled'):
                self._transition(state,subject,'terminal',actionable=False,kind='work',
                                 evidence_ref='work:'+tid,permitted_next_action='none')
                for rec in state['records'].values():
                    if rec['subject']==subject and rec['status'] not in TERMINAL:
                        rec['status']='completed';rec['completion']={'worker_handle':'external-work-status','evidence_ref':'work:'+tid}
                continue
            if status in ('queued','failed'):
                self._transition(state,subject,'ready',actionable=True,kind='work',
                                 evidence_ref='work:'+tid,permitted_next_action='continue-local-unfinished-work',
                                 details={'task_id':tid,'source_state':status})
            elif status in ('running','testing','verifying'):
                try: overdue=self.now-epoch_ms(task.get('updated_at'))>self.cfg['active_work_overdue_seconds']*1000
                except Exception: overdue=True
                self._transition(state,subject,'overdue' if overdue else 'active',
                                 actionable=overdue,kind='work',evidence_ref='work:'+tid,
                                 permitted_next_action='verify-worker-handle-and-continue-or-block',
                                 details={'task_id':tid,'source_state':status})
            elif status=='blocked':
                self._transition(state,subject,'blocked-source',actionable=False,kind='work',
                                 evidence_ref='work:'+tid,permitted_next_action='await-blocker-change')

    def _timer_registration(self,item):
        data=read_json(item['_path'])
        if not isinstance(data,dict): return None
        kind=item['kind']
        if kind=='paper_status':
            if data.get('mode')!='paper' or data.get('candidate_not_deployed') is not False:return None
            r=data.get('research')
            if not isinstance(r,dict):return None
            start=r.get('strategy_start_ms');deadline=r.get('deadline_ms');checkpoint=r.get('checkpoint_ms')
            reg=str(r.get('registration_id') or start)
        elif kind=='discovery_state':
            if data.get('activated') is not True:return None
            start=data.get('start_ms');deadline=data.get('deadline_ms');checkpoint=data.get('checkpoint_ms');reg=str(data.get('version_id') or start)
        else:
            if data.get('active') is not True or data.get('operator_accepted') is not True:return None
            start=data.get('start_ms');deadline=data.get('deadline_ms');checkpoint=data.get('checkpoint_ms');reg=str(data.get('registration_id') or start)
        if type(start) is not int or type(deadline) is not int or deadline<=start:return None
        if checkpoint is not None and (type(checkpoint) is not int or not start<checkpoint<=deadline):return None
        return dict(registration_id=reg,start_ms=start,checkpoint_ms=checkpoint,deadline_ms=deadline)

    def _observe_timers(self,state):
        for item in self.cfg['timer_sources']:
            reg=self._timer_registration(item)
            if reg is None: continue
            base='timer:'+item['id']+':'+reg['registration_id']
            if reg['checkpoint_ms'] is not None:
                self._transition(state,base+':checkpoint','due' if self.now>=reg['checkpoint_ms'] else 'pending',
                                 actionable=self.now>=reg['checkpoint_ms'],kind='timer',
                                 evidence_ref='timer:'+item['id']+':checkpoint',
                                 permitted_next_action='review-authoritative-checkpoint',
                                 details={'timer':'checkpoint'})
            self._transition(state,base+':deadline','due' if self.now>=reg['deadline_ms'] else 'pending',
                             actionable=self.now>=reg['deadline_ms'],kind='timer',
                             evidence_ref='timer:'+item['id']+':deadline',
                             permitted_next_action='review-authoritative-deadline',
                             details={'timer':'deadline'})

    def _recover_expired(self,state):
        changed=False
        backoff=self.cfg['retry_backoff_seconds']
        for rec in state['records'].values():
            claim=rec.get('claim')
            if rec['status']!='claimed' or not isinstance(claim,dict): continue
            if self.now<claim.get('lease_until_ms',0): continue
            if rec['attempts']>=self.cfg['max_attempts']:
                rec['status']='blocked';rec['blocked']={'reason':'lease_expired_retry_budget_exhausted','at_ms':self.now}
                rec['claim']=None;changed=True;continue
            delay=backoff[min(max(rec['attempts']-1,0),len(backoff)-1)]*1000
            rec['status']='queued';rec['not_before_ms']=self.now+delay;rec['claim']=None
            rec['last_failure']='lease_expired';changed=True
        return changed

    def _claim_next(self,state):
        if any(r['status']=='claimed' for r in state['records'].values()):
            return None
        due=[r for r in state['records'].values() if r['status']=='queued' and r.get('not_before_ms',0)<=self.now]
        due.sort(key=lambda r:(r['generation'],r['event_id']))
        if not due:return None
        rec=due[0];rec['attempts']+=1
        token=secrets.token_hex(16)
        rec['status']='claimed'
        rec['claim']=dict(token=token,claimed_at_ms=self.now,lease_until_ms=self.now+self.cfg['lease_seconds']*1000,
                          worker_handle=None)
        state['model_wakes']+=1
        return rec

    def observe_and_claim(self):
        lock=self._lock()
        try:
            state=self._load()
            before=canonical(state)
            state['scheduler_ticks']+=1
            # scheduler_ticks is diagnostic only; do not persist it on a quiet tick.
            hold=self._hold()
            self._observe_health(state,hold);self._observe_snapshot(state,hold);self._observe_process(state,hold)
            self._observe_work(state);self._observe_timers(state)
            self._recover_expired(state)
            rec=self._claim_next(state)
            persisted=dict(state)
            if rec is None:
                persisted['scheduler_ticks']-=1
            status=dict(schema_version=1,classification='operator_hold' if hold else 'observing',
                        operator_hold=bool(hold),queued=sum(r['status']=='queued' for r in state['records'].values()),
                        claimed=sum(r['status']=='claimed' for r in state['records'].values()),
                        completed=sum(r['status']=='completed' for r in state['records'].values()),
                        blocked=sum(r['status']=='blocked' for r in state['records'].values()),
                        capacity_blocked=bool(state.get('capacity_blocked')))
            if canonical(persisted)!=before or not self.state_path.exists():
                atomic_json(self.state_path,persisted)
            # Status is local observability only and deliberately contains no source paths/private payloads.
            old=read_json(self.status_path)
            if old!=status: atomic_json(self.status_path,status)
            if rec is None:return None
            return dict(event_id=rec['event_id'],kind=rec['kind'],transition=rec['transition'],
                        evidence_ref=rec['evidence_ref'],permitted_next_action=rec['permitted_next_action'],
                        claim_token=rec['claim']['token'],lease_seconds=self.cfg['lease_seconds'])
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN);lock.close()

    def worker_adopt(self,token,handle):
        if not safe_ref(handle):raise GateError('public-safe worker handle required')
        lock=self._lock()
        try:
            state=self._load()
            rec=next((r for r in state['records'].values() if r['status']=='claimed' and r.get('claim',{}).get('token')==token),None)
            if rec is None:raise GateError('claim missing, expired or already terminal')
            if self.now>=rec['claim']['lease_until_ms']:raise GateError('claim lease expired')
            current=rec['claim'].get('worker_handle')
            if current not in (None,handle):raise GateError('claim already owned by different worker')
            rec['claim']['worker_handle']=handle;atomic_json(self.state_path,state)
            return rec['event_id']
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN);lock.close()

    def worker_finish(self,token,*,outcome,worker_handle,evidence_ref,reason=None):
        if outcome not in ('completed','blocked'):raise GateError('terminal outcome required')
        if not safe_ref(worker_handle) or not safe_ref(evidence_ref):raise GateError('public-safe handle/evidence ref required')
        lock=self._lock()
        try:
            state=self._load()
            rec=next((r for r in state['records'].values() if r['status']=='claimed' and r.get('claim',{}).get('token')==token),None)
            if rec is None:raise GateError('claim missing, expired or already terminal')
            if rec['claim'].get('worker_handle')!=worker_handle:raise GateError('worker handle does not own claim')
            if self.now>=rec['claim']['lease_until_ms']:raise GateError('claim lease expired')
            rec['status']=outcome
            terminal=dict(worker_handle=worker_handle,evidence_ref=evidence_ref,at_ms=self.now)
            if outcome=='completed':rec['completion']=terminal
            else:
                if not isinstance(reason,str) or not reason:raise GateError('blocked reason required')
                rec['blocked']=dict(terminal,reason=reason[:500])
            rec['claim']=None;atomic_json(self.state_path,state)
            return rec['event_id']
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN);lock.close()

    def release_failure(self,token,kind):
        if kind not in ('provider','notification','worker_interrupted'):raise GateError('unsupported failure kind')
        lock=self._lock()
        try:
            state=self._load()
            rec=next((r for r in state['records'].values() if r['status']=='claimed' and r.get('claim',{}).get('token')==token),None)
            if rec is None:raise GateError('claim not active')
            if rec['attempts']>=self.cfg['max_attempts']:
                rec['status']='blocked';rec['blocked']={'reason':kind+'_retry_budget_exhausted','at_ms':self.now}
            else:
                backoff=self.cfg['retry_backoff_seconds'];delay=backoff[min(max(rec['attempts']-1,0),len(backoff)-1)]*1000
                rec['status']='queued';rec['not_before_ms']=self.now+delay;rec['last_failure']=kind
            rec['claim']=None;atomic_json(self.state_path,state)
            return rec['event_id']
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN);lock.close()

    def pending(self):
        state=self._load()
        rows=[]
        for r in sorted(state['records'].values(),key=lambda x:x['event_id']):
            if r['status'] in ('queued','claimed','blocked'):
                rows.append({k:r.get(k) for k in ('event_id','kind','transition','status','attempts','evidence_ref','permitted_next_action','claim','blocked')})
        return rows

def scheduler_control(config_path,*,now_ms=None,process_count=None):
    gate=Gate(load_config(config_path),now_ms=now_ms,process_count=process_count)
    event=gate.observe_and_claim()
    if event is None:return FALSE_BYTES
    return canonical({'wakeAgent':True,'context':event})

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--engineering-fixture',action='store_true')
    p.add_argument('--now-ms',type=int)
    p.add_argument('--process-count',type=int)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('scheduler')
    sub.add_parser('pending')
    adopt=sub.add_parser('adopt');adopt.add_argument('--claim-token',required=True);adopt.add_argument('--worker-handle',required=True)
    finish=sub.add_parser('finish');finish.add_argument('--claim-token',required=True);finish.add_argument('--worker-handle',required=True);finish.add_argument('--evidence-ref',required=True);finish.add_argument('--outcome',choices=('completed','blocked'),required=True);finish.add_argument('--reason')
    rel=sub.add_parser('release-failure');rel.add_argument('--claim-token',required=True);rel.add_argument('--kind',choices=('provider','notification','worker_interrupted'),required=True)
    a=p.parse_args(argv)
    if (a.now_ms is not None or a.process_count is not None) and not a.engineering_fixture:
        p.error('--now-ms/--process-count are engineering-fixture only')
    gate=Gate(load_config(a.config),now_ms=a.now_ms,process_count=a.process_count)
    if a.command=='scheduler':
        print(scheduler_control(a.config,now_ms=a.now_ms,process_count=a.process_count));return 0
    if a.command=='pending':
        print(canonical(gate.pending()));return 0
    if a.command=='adopt':
        print(gate.worker_adopt(a.claim_token,a.worker_handle));return 0
    if a.command=='finish':
        print(gate.worker_finish(a.claim_token,outcome=a.outcome,worker_handle=a.worker_handle,evidence_ref=a.evidence_ref,reason=a.reason));return 0
    print(gate.release_failure(a.claim_token,a.kind));return 0

if __name__=='__main__':
    raise SystemExit(main())
