#!/usr/bin/env python3
"""Public observe-only radar. No orders, approvals, credentials or model claims."""
import argparse, fcntl, json, os, time, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

def us_session(now):
    dt=datetime.fromtimestamp(now/1000,ZoneInfo('America/New_York'))
    return dt.weekday()<5 and (9,30)<=(dt.hour,dt.minute)<(16,0)

def absorption(rows,now):
    closed=[b for b in rows if int(b[6])<now]
    if len(closed)<2:return None
    a,b=closed[-2:]
    if int(b[0])-int(a[0])!=300000 or not 0<=now-int(b[6])<=300000:return None
    if any(float(x[7])<=0 for x in (a,b)):return None
    shares=[float(x[10])/float(x[7]) for x in (a,b)]
    if all(s>=.65 for s in shares) and all(float(x[4])<float(x[1]) for x in (a,b)) and float(b[4])<float(a[4]):return 'SELL_ABSORPTION'
    if all(s<=.35 for s in shares) and all(float(x[4])>float(x[1]) for x in (a,b)) and float(b[4])>float(a[4]):return 'BUY_ABSORPTION'
    return None

def oi_metrics(rows, now):
    if len(rows)!=49:return None
    ts=[int(r['timestamp']) for r in rows]
    if not 0<=now-ts[-1]<=900000 or any(b-a!=300000 for a,b in zip(ts,ts[1:])):return None
    q=[float(r['sumOpenInterest']) for r in rows]
    if any(x<=0 for x in q):return None
    return {'oi_15m':q[-1]/q[-4]-1,'oi_1h':q[-1]/q[-13]-1,'oi_4h':q[-1]/q[0]-1,'oi_timestamp':ts[-1],'oi_quantity':q[-1]}

WATCH={'AMD','ANET','NVDA','TSM','MU','QQQ','SOXL'}
TRADFI=WATCH|{'AAPL','AMZN','GOOG','GOOGL','MSFT','META','TSLA','PLTR','COIN','HOOD','MSTR','SPY','XAU','XAG','GOLD','SILVER','US500','US100','CRCL','INTC','ORCL'}

def session_label(base,now):
    dt=datetime.fromtimestamp(now/1000,timezone.utc)
    if base in WATCH and dt.date().isoformat()=='2026-11-03' and (20,30)<=(dt.hour,dt.minute)<(23,0):return 'AFTER_HOURS'
    if us_session(now):return 'REGULAR_HOURS'
    return None

def atomic(path,value):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w') as f:
        os.chmod(tmp,0o600);json.dump(value,f,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def append(path,value):
    with path.open('a') as f:
        os.chmod(path,0o600);f.write(json.dumps(value,allow_nan=False)+'\n');f.flush()

class Cooldown(Exception):pass
class PublicClient:
    ALLOWED={'/fapi/v1/time','/fapi/v1/exchangeInfo','/fapi/v1/premiumIndex','/fapi/v1/ticker/24hr','/futures/data/openInterestHist','/fapi/v1/klines','/fapi/v1/depth'}
    def __init__(self,root,deadline):
        self.root=root;self.deadline=deadline;self.last=0;self.cooldown_until=0;self.requests=0
        p=root/'cooldown.json'
        if p.exists():self.cooldown_until=float(json.loads(p.read_text())['until'])
    def get(self,path,**params):
        from urllib.parse import urlencode
        from email.utils import parsedate_to_datetime
        if path not in self.ALLOWED:raise ValueError('public allowlist')
        for attempt in range(2):
            if time.time()<self.cooldown_until:raise Cooldown('persisted HTTP/weight cooldown')
            delay=max(0,.7-(time.monotonic()-self.last))
            if time.monotonic()+delay+9>self.deadline:raise TimeoutError('cycle budget exhausted')
            time.sleep(delay);self.last=time.monotonic();self.requests+=1
            url='https://fapi.binance.com'+path+('?' + urlencode(params) if params else '')
            record={'received_ms':int(time.time()*1000),'path':path,'params':params,'attempt':attempt+1}
            try:
                with urllib.request.urlopen(urllib.request.Request(url,method='GET'),timeout=8) as r:
                    data=json.load(r); headers=dict(r.headers);record.update(http_status=r.status,headers=headers,data=data)
                append(self.root/'raw.jsonl',record)
                used=max([int(v) for k,v in headers.items() if k.lower().startswith('x-mbx-used-weight')]+[0])
                if used>=1800:
                    self.cooldown_until=time.time()+65;atomic(self.root/'cooldown.json',{'until':self.cooldown_until,'reason':'weight >=1800'})
                return data
            except urllib.error.HTTPError as e:
                record.update(http_status=e.code,headers=dict(e.headers),error=str(e));append(self.root/'raw.jsonl',record)
                if e.code in (418,429):
                    raw=e.headers.get('Retry-After','900')
                    try:seconds=float(raw)
                    except ValueError:seconds=parsedate_to_datetime(raw).timestamp()-time.time()
                    self.cooldown_until=time.time()+max(60,seconds)
                    atomic(self.root/'cooldown.json',{'until':self.cooldown_until,'reason':'HTTP '+str(e.code)})
                    raise Cooldown('HTTP '+str(e.code)) from e
                if e.code<500:raise
                if attempt:raise
            except (OSError,ValueError) as e:
                record['error']=repr(e);append(self.root/'raw.jsonl',record)
                if attempt:raise
            time.sleep(1)

def scan(root,client):
    root.mkdir(parents=True,exist_ok=True);os.chmod(root,0o700)
    started=int(time.time()*1000)
    r={'started_ms':started,'pid':os.getpid(),'status':'SCANNING','eligible':0,'oi_evaluated':0,'oi_missing':0,'flow_evaluated':0,'flow_missing':0,'candidates':[],'errors':[],'event_history':'UNKNOWN','holiday_calendar':'UNKNOWN: weekday/time only','funding_semantics':'lastFundingRate; not next prediction','thresholds':{'oi_1h':.05,'oi_4h':.10,'abs_lastFundingRate':.0005,'taker_share':.65,'quoteVolume':20000000,'depth_each_usd':15000,'max_spread_bp':10}}
    universe=[];eligible=[];anomalies=[];evaluated=set();flow_expected=[];flow_done=set()
    atomic(root/'candidates.json',{'status':'SCANNING','started_ms':started,'candidates':[]})
    def progress():
        r['updated_ms']=int(time.time()*1000);r['elapsed_s']=(r['updated_ms']-started)/1000
        r['oi_missing']=len(eligible)-len(evaluated);r['oi_missing_symbols']=[s['symbol'] for s in eligible if s['symbol'] not in evaluated]
        r['flow_missing']=len(flow_expected)-len(flow_done)
        atomic(root/'status.json',r)
    try:
        clock=client.get('/fapi/v1/time'); now=int(clock['serverTime']);r['server_ms']=now;r['server_sample_received_ms']=int(time.time()*1000)
        info=client.get('/fapi/v1/exchangeInfo'); ticks=client.get('/fapi/v1/ticker/24hr'); funding=client.get('/fapi/v1/premiumIndex')
        if not isinstance(info.get('symbols'),list) or not isinstance(ticks,list) or not isinstance(funding,list):raise ValueError('batch schema')
        tick={x['symbol']:x for x in ticks};fund={x['symbol']:x for x in funding}
        universe=[s for s in info['symbols'] if s['status']=='TRADING'];r['trading_universe']=len(universe);r['exchange_rate_limits']=info.get('rateLimits',[])
        eligible=sorted([s for s in universe if s['symbol'] in tick and float(tick[s['symbol']]['quoteVolume'])>20000000],key=lambda s:-float(tick[s['symbol']]['quoteVolume']))
        r['eligible']=len(eligible);r['missing_ticker_symbols']=[s['symbol'] for s in universe if s['symbol'] not in tick]
        def server_now():return now+int(time.time()*1000)-r['server_sample_received_ms']
        def is_trad(s):return s.get('baseAsset') in TRADFI or s.get('underlyingType','COIN')!='COIN' or any(x in {'STOCK','STOCKS','COMMODITY','COMMODITIES','INDEX','TRADFI'} for x in s.get('underlyingSubType',[]))
        flow_expected=[s for s in eligible if is_trad(s) and session_label(s.get('baseAsset'),server_now())]
        r['flow_eligible']=len(flow_expected);r['tradfi_eligible_symbols']=[s['symbol'] for s in eligible if is_trad(s)];progress()
        for s in eligible:
            sym=s['symbol'];metrics=None
            try:
                rows=client.get('/futures/data/openInterestHist',symbol=sym,period='5m',limit=49)
                metrics=oi_metrics(rows,server_now())
                if metrics is None:raise ValueError('OI stale/gapped/nonpositive/incomplete')
                evaluated.add(sym);r['oi_evaluated']=len(evaluated)
                f=fund.get(sym);rate=float(f['lastFundingRate']) if f else None
                fresh=f and 'time' in f and 0<=server_now()-int(f['time'])<=900000
                if fresh and abs(rate)>=.0005 and (metrics['oi_1h']>=.05 or metrics['oi_4h']>=.10):
                    anomalies.append({'symbol':sym,'branch':'OI_FUNDING','metrics':metrics,'lastFundingRate':rate,'funding_timestamp':int(f['time']),'score':max(metrics['oi_1h']/.05,metrics['oi_4h']/.10)+abs(rate)/.0005})
            except (Cooldown,TimeoutError):raise
            except Exception as e:r['errors'].append({'symbol':sym,'stage':'OI','error':repr(e)})
            # Absorption is independent of OI availability or OI direction.
            if s in flow_expected:
                try:
                    bars=client.get('/fapi/v1/klines',symbol=sym,interval='5m',limit=5)
                    closed=[b for b in bars if int(b[6])<server_now()]
                    if len(closed)<2 or server_now()-int(closed[-1][6])>300000:raise ValueError('stale/incomplete flow')
                    signal=absorption(bars,server_now());flow_done.add(sym);r['flow_evaluated']=len(flow_done)
                    if signal:anomalies.append({'symbol':sym,'branch':signal,'metrics':metrics,'session':session_label(s.get('baseAsset'),server_now()),'closed_bar_times':[int(b[6]) for b in closed[-2:]],'score':2.0,'event_status':'WAIT_AI','event_source':'parent primary metadata only; release content NOT fetched'})
                except (Cooldown,TimeoutError):raise
                except Exception as e:r['errors'].append({'symbol':sym,'stage':'FLOW','error':repr(e)})
            progress()
        finalists=sorted(anomalies,key=lambda x:-x['score']);seen=set();r['anomalies']=len(finalists);r['depth_evaluated']=0
        for a in finalists:
            if a['symbol'] in seen:continue
            seen.add(a['symbol'])
            if r['depth_evaluated']>=20:break
            try:
                d=client.get('/fapi/v1/depth',symbol=a['symbol'],limit=20);r['depth_evaluated']+=1
                if not (len(d['bids'])>=20 and len(d['asks'])>=20 and 0<=server_now()-int(d['T'])<=15000):raise ValueError('stale/incomplete depth')
                bid=float(d['bids'][0][0]);ask=float(d['asks'][0][0]);spread=(ask/bid-1)*10000
                db=sum(float(p)*float(q) for p,q in d['bids'][:20]);da=sum(float(p)*float(q) for p,q in d['asks'][:20])
                if db>15000 and da>15000 and 0<=spread<=10:
                    a.update(status='WAIT_AI',event_status='WAIT_AI',event_history='UNKNOWN',depth_bid_usd=db,depth_ask_usd=da,spread_bp=spread,depth_timestamp=int(d['T']),observed_ms=server_now(),observation_only=True)
                    r['candidates'].append(a)
                    if len(r['candidates'])==3:break
            except (Cooldown,TimeoutError):raise
            except Exception as e:r['errors'].append({'symbol':a['symbol'],'stage':'DEPTH','error':repr(e)})
        r['status']='COMPLETE' if not r['errors'] else 'PARTIAL'
    except Exception as e:
        r['status']='COOLDOWN' if isinstance(e,Cooldown) else 'ERROR';r['errors'].append({'stage':'CYCLE','error':repr(e)})
        append(root/'alerts.jsonl',{'at_ms':int(time.time()*1000),'status':r['status'],'error':repr(e)})
    r['completed_ms']=int(time.time()*1000);r['cooldown_until']=client.cooldown_until;r['requests']=getattr(client,'requests',None)
    progress();append(root/'scans.jsonl',r);atomic(root/'latest.json',r);atomic(root/'candidates.json',{'status':r['status'],'completed_ms':r['completed_ms'],'candidates':r['candidates']})
    print(json.dumps({k:r[k] for k in ('status','eligible','oi_evaluated','oi_missing','flow_evaluated','flow_missing','elapsed_s')}),flush=True)
    return r

def main():
    p=argparse.ArgumentParser();p.add_argument('--data-dir',type=Path,default=Path(__file__).resolve().parents[1]/'lab/data/sniper-scanner');p.add_argument('--once',action='store_true');p.add_argument('--interval',type=int,choices=(900,1800),default=900);a=p.parse_args()
    os.umask(0o077);a.data_dir.mkdir(parents=True,exist_ok=True)
    with (a.data_dir/'scanner.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SystemExit('duplicate scanner refused')
        atomic(a.data_dir/'process.json',{'pid':os.getpid(),'argv':__import__('sys').argv,'cwd':os.getcwd(),'started_ms':int(time.time()*1000),'proc_stat':Path('/proc/self/stat').read_text()})
        while True:
            start=time.monotonic();client=PublicClient(a.data_dir,start+720);r=scan(a.data_dir,client)
            if a.once:return 0 if r['status']=='COMPLETE' else 1
            next_epoch=max(time.time()+max(0,a.interval-(time.monotonic()-start)),client.cooldown_until)
            atomic(a.data_dir/'schedule.json',{'pid':os.getpid(),'next_cycle_epoch':next_epoch,'interval_s':a.interval,'last_status':r['status']})
            time.sleep(max(0,next_epoch-time.time()))
if __name__=='__main__':raise SystemExit(main())
