"""Honest read-only engineering activity, separate from PAPER results."""
import argparse
import fcntl
import json
import os
from pathlib import Path
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parent
STATES={'queued','running','testing','verifying','completed','failed','blocked','cancelled'}
ACTIVE={'running','testing','verifying'}

def load_status(path, now=None):
    data=json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data,dict) or data.get('schema_version')!=1 or not isinstance(data.get('tasks'),list) or len(data['tasks'])>100:
        raise ValueError('invalid work schema')
    now=now or datetime.now(timezone.utc)
    seen=set()
    for task in data['tasks']:
        if not isinstance(task,dict) or task.get('state') not in STATES:
            raise ValueError('invalid work state')
        for key in ('id','title','current_step','next_step','updated_at','started_at'):
            if not isinstance(task.get(key),str) or not task[key] or len(task[key])>2000:
                raise ValueError('invalid work field '+key)
        if task['id'] in seen: raise ValueError('duplicate work id')
        seen.add(task['id'])
        if not isinstance(task.get('evidence'),list) or any(not isinstance(x,str) or len(x)>2000 for x in task['evidence']):
            raise ValueError('invalid evidence')
        dt=datetime.fromisoformat(task['updated_at'].replace('Z','+00:00'))
        start=datetime.fromisoformat(task['started_at'].replace('Z','+00:00'))
        if dt.utcoffset() is None or start.utcoffset() is None or dt<start:
            raise ValueError('invalid work timestamps')
        age=(now-dt).total_seconds()
        if age < -5: raise ValueError('future work update')
        task['update_age_seconds']=max(0,age)
        task['activity_unconfirmed']=task['state'] in ACTIVE and age>300
    data['available']=True
    return data

def update(path, task_id, title, state, current, next_step, evidence=None):
    if state not in STATES: raise ValueError('invalid state')
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data=load_status(path) if path.exists() else dict(schema_version=1,tasks=[])
        now=datetime.now(timezone.utc).isoformat()
        task=next((x for x in data['tasks'] if x['id']==task_id),None)
        if task is None:
            task=dict(id=task_id,started_at=now); data['tasks'].append(task)
        task.update(title=title,state=state,current_step=current,next_step=next_step,updated_at=now,evidence=evidence or task.get('evidence',[]))
        for t in data['tasks']:
            t.pop('update_age_seconds',None); t.pop('activity_unconfirmed',None)
        data.pop('available',None)
        temp=path.with_suffix('.json.tmp')
        with temp.open('w',encoding='utf-8') as stream:
            json.dump(data,stream,ensure_ascii=False,indent=2); stream.flush(); os.fsync(stream.fileno())
        load_status(temp)
        os.replace(temp,path)
    return task

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--path',type=Path,default=ROOT/'shared/work_status.json')
    p.add_argument('--id',required=True); p.add_argument('--title',required=True)
    p.add_argument('--state',required=True,choices=sorted(STATES)); p.add_argument('--current',required=True)
    p.add_argument('--next',required=True,dest='next_step'); p.add_argument('--evidence',action='append')
    a=p.parse_args(); print(json.dumps(update(a.path,a.id,a.title,a.state,a.current,a.next_step,a.evidence),ensure_ascii=False))
if __name__=='__main__': main()
