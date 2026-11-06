#!/usr/bin/env python3
"""Observe-only Tactical Sniper. Public Binance GETs only. Paper, never exchange orders."""
import json, os, re, math, time, pathlib, fcntl, argparse, urllib.request, urllib.error
from datetime import datetime, timezone
FEE = .0007  # assumed taker fee per side
SLIP = .0003 # adverse price slippage per fill, extra to observed spread
ROOT = pathlib.Path(__file__).resolve().parent.parent

def run_once(root, get=None):
    import sys
    sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
    import tactical_runtime
    return tactical_runtime.run_once(root,public_get if get is None else get)

def validate_plan(p):
    if not isinstance(p, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',p.get('id','')): raise ValueError('id')
    if not re.fullmatch(r'[A-Z0-9]{3,30}',p.get('symbol','')): raise ValueError('symbol')
    for k in ('entry','stop','tp1','tp2','notional','leverage','risk_budget','created_ms','expires_ms'):
        if isinstance(p.get(k),bool) or not isinstance(p.get(k),(int,float)) or not math.isfinite(p[k]) or p[k]<=0: raise ValueError(k)
    if p.get('approved') is not True or p.get('event_status') not in ('VERIFIED','STRUCTURE_ONLY'): raise ValueError('approval')
    if not 10<=p['leverage']<=20 or not 10<=p['notional']/p['leverage']<=20: raise ValueError('margin/leverage')
    if not 0<p['risk_budget']<=5 or p['expires_ms']<=p['created_ms']: raise ValueError('risk/expiry')
    e,st,a,b=p['entry'],p['stop'],p['tp1'],p['tp2']
    if p.get('side')=='long': ok=st<e<a<b
    elif p.get('side')=='short': ok=b<a<e<st
    else: ok=False
    if not ok: raise ValueError('price order')
    risk=p['notional']*(abs(st/e-1)+2*(FEE+SLIP))
    if risk>p['risk_budget']: raise ValueError('risk budget exceeded')
    t=p.get('trigger',{})
    if t.get('closed_bars')!=2 or not .5<t.get('buy_share',0)<.9 or t.get('depth_usd',0)<5000 or not 0<t.get('spread_bp',0)<=10: raise ValueError('trigger')
    return risk

def initial_state():
    return {'schema':1,'cash':150.,'position':None,'used':[],'day':None,'day_pnl':0.,'seq':0}

def atomic_json(path, data):
    path=pathlib.Path(path); tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w') as f:json.dump(data,f,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def save_state(root, state, events):
    root=pathlib.Path(root);root.mkdir(parents=True,exist_ok=True)
    state['seq']+=1
    data=(json.dumps({'seq':state['seq'],'events':events,'state':state},allow_nan=False)+'\n').encode()
    with (root/'journal.jsonl').open('ab') as f:f.write(data);f.flush();os.fsync(f.fileno())
    atomic_json(root/'state.json',state)

def load_state(root):
    p=pathlib.Path(root)/'journal.jsonl'
    if not p.exists():return initial_state()
    last=None;seq=0
    with p.open() as f:
        for l in f:
            try:r=json.loads(l)
            except Exception as e:raise ValueError('corrupt journal, stopped') from e
            if r['seq']!=seq+1:raise ValueError('journal sequence')
            seq=r['seq'];last=r['state']
    return last if last else initial_state()

def step(state, plans, market, now, admission=None):
    import copy
    s=copy.deepcopy(state); events=[]
    day=datetime.fromtimestamp(now/1000,timezone.utc).date().isoformat()
    if s['day']!=day: s.update(day=day,day_pnl=0.)
    def money(x): s['cash']+=x; s['day_pnl']+=x
    def close(pos, frac, kind, q, proxy=None):
        qty=pos['qty']*frac;side=1 if pos['side']=='long' else -1
        raw=q['bid'] if side==1 else q['ask']
        if proxy is not None: raw=min(raw,proxy) if side==1 else max(raw,proxy)
        price=raw*(1-SLIP*side);fee=qty*price*FEE;pnl=qty*(price-pos['entry'])*side-fee
        money(pnl);pos['qty']-=qty
        events.append({'kind':kind,'id':pos['id'],'symbol':pos['symbol'],'t':now,'price':price,'bid':q['bid'],'ask':q['ask'],'qty':qty,'pnl_after_exit_fee':pnl,'fee':fee,'model':'MISSED_STOP_PROXY' if proxy is not None else 'OBSERVED_QUOTE_WITH_SLIP'})
        if pos['qty']<1e-12:s['position']=None
    pos=s['position']
    if pos:
        q=market.get(pos['symbol'])
        if not q or abs(now-q['ts'])>15000:return s,events
        side=1 if pos['side']=='long' else -1;raw=q['bid'] if side==1 else q['ask']
        for f in q.get('funding',[]):
            key=str(f['fundingTime'])
            if f['fundingTime']>pos['entry_ms'] and key not in pos['funded']:
                cost=-side*pos['qty']*float(f.get('markPrice') or raw)*float(f['fundingRate']);money(cost);pos['funded'].append(key)
                events.append({'kind':'FUNDING','t':now,'id':pos['id'],'cashflow':cost,'funding_time':f['fundingTime']})
        crossed=raw<=pos['stop'] if side==1 else raw>=pos['stop']
        minute=[b for b in q.get('minute',[]) if b['open_ms']>=pos.get('stop_since_ms',pos['entry_ms'])]
        missed=any((b['low']<=pos['stop'] if side==1 else b['high']>=pos['stop']) for b in minute)
        if crossed or missed: close(pos,1.,'STOP',q,pos['stop'] if missed and not crossed else None);return s,events
        if s['day_pnl']<=-10:close(pos,1.,'DAY_STOP',q);return s,events
        if now-pos['entry_ms']>=4*3600000:close(pos,1.,'TIME_EXIT',q);return s,events
        if not pos['tp1_done'] and side*(raw-pos['tp1'])>=0:
            close(pos,.5,'TP1',q);pos['tp1_done']=True
            pos['stop']=pos['entry']*(1+side*2*(FEE+SLIP));pos['stop_since_ms']=now
        if s['position'] and side*(raw-pos['tp2'])>=0:close(pos,1.,'TP2',q)
        bars=[b for b in q.get('bars',[]) if b['open_ms']>=pos['entry_ms']]
        if s['position'] and pos['tp1_done'] and len(bars)>=2:
            if all((b['share']<.45 if side==1 else b['share']>.55) for b in bars[-2:]):close(pos,1.,'STALL_EXIT',q)
        return s,events
    if s['day_pnl']<=-10 or s['cash']<20:return s,events
    for p in plans:
        try:validate_plan(p)
        except (ValueError,TypeError):continue
        if p['id'] in s['used'] or not p['created_ms']<=now<p['expires_ms']:continue
        q=market.get(p['symbol'])
        if not q or abs(now-q['ts'])>15000:continue
        if min(q['depth_bid'],q['depth_ask'])<p['trigger']['depth_usd']:continue
        spread=(q['ask']-q['bid'])/((q['bid']+q['ask'])/2)*10000
        if not 0<=spread<=p['trigger']['spread_bp']:continue
        bars=q.get('bars',[])[-2:]
        if len(bars)!=2 or bars[1]['open_ms']-bars[0]['open_ms']!=300000 or now-bars[-1]['close_ms']>360000:continue
        if any(b['open_ms']<p['created_ms'] for b in bars):continue
        side=1 if p['side']=='long' else -1
        if not all(side*(b['close']-p['entry'])>=0 and (b['share']>=p['trigger']['buy_share'] if side==1 else b['share']<=1-p['trigger']['buy_share']) for b in bars):continue
        raw=q['ask'] if side==1 else q['bid']
        if side*(raw-p['entry'])<0 or abs(raw/p['entry']-1)>.003:continue
        verdict=admission(p,now) if admission else {'admissible':False,'reasons':['sentinel_not_configured'],'scan_id':None}
        events.append(dict(verdict,kind='NEWS_ADMISSION',id=p['id'],t=now))
        if verdict.get('admissible') is not True:continue
        price=raw*(1+side*SLIP);qty=p['notional']/price
        risk=qty*(abs(price-p['stop'])+(price+p['stop'])*FEE+price*SLIP)
        if risk>p['risk_budget'] or qty*price/p['leverage']>s['cash']:continue
        money(-qty*price*FEE)
        s['position']={'id':p['id'],'symbol':p['symbol'],'side':p['side'],'entry':price,'entry_ms':now,'qty':qty,'initial_qty':qty,'stop':p['stop'],'tp1':p['tp1'],'tp2':p['tp2'],'tp1_done':False,'leverage':p['leverage'],'isolated_margin':p['notional']/p['leverage'],'funded':[]}
        s['used'].append(p['id']);events.append({'kind':'ENTRY','t':now,'id':p['id'],'symbol':p['symbol'],'price':price,'bid':q['bid'],'ask':q['ask'],'qty':qty,'fee':qty*price*FEE,'risk_estimate':risk,'model':'OBSERVED_QUOTE_WITH_SLIP'})
        break
    return s,events

_COOLDOWN = 0.
def public_get(path):
    global _COOLDOWN
    allowed=('/fapi/v1/time','/fapi/v1/depth','/fapi/v1/klines','/fapi/v1/fundingRate','/fapi/v1/premiumIndex','/fapi/v1/exchangeInfo','/fapi/v1/ticker/24hr','/futures/data/openInterestHist')
    if path.split('?')[0] not in allowed:raise ValueError('public endpoint only')
    if time.time()<_COOLDOWN:raise RuntimeError('REST cooldown')
    last=None
    for i in range(3):
        try:
            with urllib.request.urlopen('https://fapi.binance.com'+path,timeout=8) as r:data=json.load(r)
            time.sleep(.3)
            return data
        except urllib.error.HTTPError as e:
            if e.code in (418,429):
                _COOLDOWN=time.time()+max(60,int(e.headers.get('Retry-After','900')));raise
            last=e
        except (OSError,ValueError) as e:last=e
        time.sleep(.5*(2**i))
    raise RuntimeError('public GET failed: '+repr(last))

def collect_symbol(symbol, now, position=None, get=public_get):
    def bars(interval,limit):
        r=get('/fapi/v1/klines?symbol='+symbol+'&interval='+interval+'&limit='+str(limit))
        return [{'open_ms':int(v[0]),'close_ms':int(v[6]),'high':float(v[2]),'low':float(v[3]),'close':float(v[4]),'share':float(v[10])/float(v[7]) if float(v[7]) else .5} for v in r if int(v[6])<now]
    bb=bars('5m',5);mm=bars('1m',1000) if position else []
    funding=[]
    if position:funding=get('/fapi/v1/fundingRate?symbol='+symbol+'&startTime='+str(position['entry_ms'])+'&limit=1000')
    d=get('/fapi/v1/depth?symbol='+symbol+'&limit=20')
    bid=float(d['bids'][0][0]);ask=float(d['asks'][0][0])
    if not 0<bid<=ask:raise ValueError('invalid quote')
    if 'T' in d and abs(now-int(d['T']))>120000:raise ValueError('stale exchange book')
    return {'ts':now,'received_ms':int(time.time()*1000),'bid':bid,'ask':ask,'depth_bid':sum(float(p)*float(q) for p,q in d['bids']),'depth_ask':sum(float(p)*float(q) for p,q in d['asks']),'bars':bb,'minute':mm,'funding':funding}

if __name__=='__main__':
    from tactical_runtime import main
    main()

