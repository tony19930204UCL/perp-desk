#!/usr/bin/env python3
"""Profile-only process recovery. No orders, credentials or gateway lifecycle changes."""
import pathlib,os,json,subprocess,sys,time,fcntl,argparse
from datetime import datetime,timezone
ROOT=pathlib.Path(__file__).resolve().parent.parent
DATA=ROOT/'lab/data/desk-watchdog'

def match(argv, expected):
    # Python executable versions may differ, but script and all options must match exactly.
    return len(argv)==len(expected) and argv[1:]==expected[1:] and pathlib.Path(argv[0]).name.startswith(pathlib.Path(expected[0]).name.split('.')[0])

def decide(spec,pids,now,previous):
    if pids:return 'RUNNING' if len(pids)==1 else 'DUPLICATE'
    if spec.get('until',float('inf'))<=now:return 'ENDED'
    if spec.get('safety_halt'):return 'SAFETY_HALT'
    if now-previous.get('last_start',0)<180:return 'COOLDOWN'
    return 'START'

def launchable(spec):
    scripts=[x for x in spec['argv'][1:] if str(x).startswith(str(ROOT)+'/') and str(x).endswith(('.py','.sh'))]
    return bool(scripts) and pathlib.Path(spec['argv'][0]).is_file() and all(pathlib.Path(x).is_file() for x in scripts)

def services():
    py=sys.executable
    items=[]
    for name,script,args in [('schedule','x1_paper_sched.py',[]),('tactical','tactical_sniper.py',['--root',str(ROOT/'lab/data/tactical-sniper'),'--news-config',str(ROOT/'lab/data/news-shock-sentinel/admission-config.json')]),('scanner','sniper_scanner.py',['--interval','900']),('tradfi','tradfi_book_collector.py',[]),('weekend','weekend_tradfi_signal.py',[]),('stock_history','hist_stock_context.py',[])]:
        items.append({'name':name,'argv':[py,str(ROOT/'scripts'/script)]+args})
    items.append({'name':'mtm','argv':['/bin/bash',str(ROOT/'scripts/x1_mtm_loop.sh')]})
    # Reuse only this profile's already-authorized dashboard entry, not paper-arm supervisors.
    old=json.loads((ROOT/'lab/shared/service_watchdog.json').read_text())
    dash=next(x for x in old['services'] if x['name']=='dash18769')
    if dash.get('port')!=18769 or dash['argv'][-2:]!=['--port','18769']:
        raise ValueError('Crypto dashboard restricted to port 18769; no fallback')
    items.append({'name':'dashboard','argv':dash['argv']})
    next(x for x in items if x['name']=='tradfi')['until']=datetime(2026,11,4,22,30,tzinfo=timezone.utc).timestamp()
    return items

def processes():
    out={}
    for d in pathlib.Path('/proc').iterdir():
        if not d.name.isdigit():continue
        try:
            if d.stat().st_uid!=os.getuid():continue
            a=[x.decode() for x in (d/'cmdline').read_bytes().split(b'\0') if x]
            out[int(d.name)]=a
        except (OSError,UnicodeError):continue
    return out

def atomic(p,v):
    tmp=p.with_suffix('.tmp')
    with tmp.open('w') as f:json.dump(v,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,p)

def run_once():
    DATA.mkdir(parents=True,exist_ok=True)
    with (DATA/'guard.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'result':'ALREADY_RUNNING'}
        p=DATA/'state.json'
        state=json.loads(p.read_text()) if p.exists() else {'services':{}}
        now=time.time();boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip();oldboot=state.get('boot')
        if oldboot!=boot:state={'services':{},'boot':boot,'prior_boot':oldboot}
        specs=services();procs=processes();observed=[]
        for spec in specs:
            name=spec['name'];previous=state['services'].get(name,{})
            if name=='tactical':
                try:spec['safety_halt']=json.loads((ROOT/'lab/data/tactical-sniper/status.json').read_text()).get('status')=='HALTED'
                except (OSError,ValueError):pass
            ids=[pid for pid,a in procs.items() if match(a,spec['argv'])]
            action=decide(spec,ids,now,previous);r={'t':now,'boot':boot,'service':name,'action':action,'pids':ids}
            if action=='START':
                if not any(str(x).startswith(str(ROOT)+'/') for x in spec['argv'][1:]):raise ValueError('out of profile launch')
                if not launchable(spec):
                    r.update(action='MISSING_EXECUTABLE');observed.append(r);continue
                with (DATA/(name+'.out')).open('ab') as log:
                    env=dict(os.environ,HERMES_HOME=str(ROOT),TMPDIR=str(ROOT/'cache/scratch'))
                    child=subprocess.Popen(spec['argv'],cwd=ROOT,env=env,stdout=log,stderr=log,start_new_session=True)
                r['started_pid']=child.pid;previous={'last_start':now,'pid':child.pid,'argv':spec['argv']}
            state['services'][name]=previous;observed.append(r)
        state['updated']=now;atomic(p,state)
        with (DATA/'watchdog.log').open('a') as f:
            for row in observed:f.write(json.dumps(row)+'\n')
            f.flush();os.fsync(f.fileno())
        result={'updated_ms':int(now*1000),'boot':boot,'observed':observed,'gateway':'OBSERVE_ONLY: shared Hermes lifecycle not restarted','scheduler_backend':'user crontab installed separately; cron daemon must be active'}
        atomic(DATA/'status.json',result)
        return result

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--loop',action='store_true');a=ap.parse_args()
    # A separate loop lock prevents multiple fallback loops, while cron shares guard.lock.
    DATA.mkdir(parents=True,exist_ok=True)
    with (DATA/'loop.lock').open('a') as lock:
        if a.loop:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            print(json.dumps(run_once()),flush=True)
            if not a.loop:break
            time.sleep(300)
if __name__=='__main__':main()

