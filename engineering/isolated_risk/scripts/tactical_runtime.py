"""Runtime for public-data tactical PAPER monitor. No private API or orders."""
import json,os,pathlib,time,fcntl,argparse
from datetime import datetime,timezone
import tactical_sniper as S
import news_admission as N

def run_once(root,get=S.public_get,news_config=None):
    root=pathlib.Path(root);root.mkdir(parents=True,exist_ok=True)
    # Actual order-writer contract: orders.lock; main already owns monitor.lock.
    with (root/'orders.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _run_once_locked(root,get,news_config)

def _run_once_locked(root,get,news_config):
    root=pathlib.Path(root);root.mkdir(parents=True,exist_ok=True)
    start=int(time.time()*1000);mono=time.monotonic();now=int(get('/fapi/v1/time')['serverTime']);end=int(time.time()*1000)
    rtt=(time.monotonic()-mono)*1000
    if rtt>2000:raise RuntimeError('clock sample high RTT; entry paused, retry next tick')
    if now<start-3000 or now>end+3000:raise ValueError('clock drift >3s outside request interval')
    state=S.load_state(root);packet=json.loads((root/'orders.json').read_text());plans=packet['orders']
    if len(plans)>3:raise ValueError('max 3 plans')
    if len({p['id'] for p in plans})!=len(plans):raise ValueError('duplicate id')
    for p in plans:S.validate_plan(p)
    def admission(p,at):
        try:return N.check(N.load_config(news_config),p,at)
        except (OSError,ValueError,TypeError,KeyError) as e:return {'admissible':False,'reasons':['config:'+str(e)],'scan_id':None}
    admission_events=[dict(admission(p,now),kind='NEWS_PLAN_ADMISSION',id=p['id'],t=now) for p in plans if p['created_ms']<=now<p['expires_ms'] and p['id'] not in state['used']]
    symbols={p['symbol'] for p in plans if p['created_ms']<=now<p['expires_ms'] and p['id'] not in state['used']}
    if state['position']:symbols.add(state['position']['symbol'])
    market={};errors={}
    for sym in sorted(symbols):
        try:
            pos=state['position'];pos=pos if pos and pos['symbol']==sym else None
            q=S.collect_symbol(sym,int(time.time()*1000),pos,get)
            if q['received_ms']-q['ts']>15000:raise ValueError('slow/stale fetch')
            market[sym]=q
        except Exception as e:errors[sym]=repr(e)
    now=int(time.time()*1000);s,events=S.step(state,plans,market,now,admission=admission)
    events=admission_events+events
    if s!=state or events:S.save_state(root,s,events)
    else:S.atomic_json(root/'state.json',s)
    pos=s['position'];unreal=None
    if pos and pos['symbol'] in market:
        q=market[pos['symbol']];side=1 if pos['side']=='long' else -1;px=q['bid'] if side==1 else q['ask']
        unreal=pos['qty']*(px-pos['entry'])*side-pos['qty']*px*(S.FEE+S.SLIP)
    result={'updated_ms':now,'updated_utc':datetime.fromtimestamp(now/1000,timezone.utc).isoformat(),'pid':os.getpid(),'mode':'PAPER_ONLY','status':'DEGRADED' if errors else 'OK','errors':errors,'cash':s['cash'],'unrealized_after_exit_cost':unreal,'equity':s['cash']+(unreal or 0.),'open_positions':int(pos is not None),'used_order_ids':s['used'],'approved_orders':len(plans),'active_orders':sum(p['created_ms']<=now<p['expires_ms'] and p['id'] not in s['used'] for p in plans),'events_this_tick':events,'market':market,'limitations':['60s polling not exchange stops','missed stop is pessimistic proxy','no exact liquidation engine','AI review is operator-approved not automated']}
    S.atomic_json(root/'status.json',result)
    if errors:
        with (root/'alerts.jsonl').open('a') as f:f.write(json.dumps({'t':now,'errors':errors})+'\n');f.flush();os.fsync(f.fileno())
    return result

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--news-config',type=pathlib.Path,required=True);ap.add_argument('--root',required=True);ap.add_argument('--once',action='store_true');args=ap.parse_args()
    root=pathlib.Path(args.root);root.mkdir(parents=True,exist_ok=True)
    with (root/'monitor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            start=time.monotonic()
            try:
                r=run_once(root,news_config=args.news_config);print(json.dumps({k:v for k,v in r.items() if k!='market'}),flush=True)
            except ValueError as e:
                S.atomic_json(root/'status.json',{'updated_ms':int(time.time()*1000),'status':'HALTED','error':repr(e),'pid':os.getpid()});raise
            except Exception as e:
                S.atomic_json(root/'status.json',{'updated_ms':int(time.time()*1000),'status':'NETWORK_ERROR','error':repr(e),'pid':os.getpid()})
                with (root/'alerts.jsonl').open('a') as f:f.write(json.dumps({'t':int(time.time()*1000),'error':repr(e)})+'\n')
                print(repr(e),flush=True)
                if args.once:raise
            if args.once:return
            time.sleep(max(1,60-(time.monotonic()-start)))
