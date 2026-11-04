"""Fixed-path read adapters. No network, writes, trading imports or credentials."""
import json, os, stat, math, hashlib, time
from pathlib import Path
from collections import deque
from decimal import Decimal
from datetime import datetime, timezone

FILES={
 'state':'lab/data/tactical-sniper/state.json',
 'tactical':'lab/data/tactical-sniper/status.json',
 'orders':'lab/data/tactical-sniper/orders.json',
 'scan':'lab/data/sniper-scanner/latest.json',
 'coach':'lab/data/sniper-scanner/coach_latest.json',
 'risk':'lab/data/news-shock-sentinel/risk_assessment.json',
 'weekly':'lab/data/x1-paper/mtm.json',
 'weekly_state':'lab/data/x1-paper/state.json',
 'weekly_ledger':'lab/data/x1-paper/ledger.jsonl',
 'prediction':'lab/data/prediction-paper/status.json',
 'journal':'lab/data/tactical-sniper/journal.jsonl',
}

def n(x):return x if type(x) in (float,int) and math.isfinite(x) else None

def ts(x):
 if n(x) is not None:return x if 0<x<8640000000000000 else None
 if isinstance(x,str):
  try:
   d=datetime.fromisoformat(x.replace('Z','+00:00'))
   return d.timestamp()*1000 if d.tzinfo else None
  except (ValueError,OverflowError):pass
 return None

def opened(root,key):
 p=Path(root)/FILES[key]
 if p.resolve()!=p.absolute():raise ValueError('SYMLINK')
 fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
 f=os.fdopen(fd,'rb')
 if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):f.close();raise ValueError('NOT_REGULAR')
 return f

def parse(raw):return json.loads(raw,parse_constant=lambda x: (_ for _ in ()).throw(ValueError('NON_FINITE')))

def load(root,key):
 with opened(root,key) as f:raw=f.read(1048577)
 if len(raw)>1048576:raise ValueError('SIZE_CAP')
 d=parse(raw)
 if type(d) is not dict:raise ValueError('SCHEMA')
 return d,hashlib.sha256(raw).hexdigest()

def dec(value):
 if n(value) is None:raise ValueError('MONEY_MISSING')
 return Decimal(str(value))

def journal(root,state):
 """Stream every line and reconcile every cash step, not cash minus bankroll.
 Actual producer: ENTRY fee, exit pnl_after_exit_fee (already includes exit fee),
 FUNDING cashflow, NEWS_* admission no cash. Unknown kinds fail closed.
 No transfer schema exists in the current producer; any new kind needs review.
 """
 total=Decimal(0);fees=Decimal(0);funding=Decimal(0);previous=Decimal(150)
 tail=deque(maxlen=20);count=0;last=None;digest=hashlib.sha256();size=0;last_event=None
 with opened(root,'journal') as f:
  initial=os.fstat(f.fileno());end=initial.st_size
  while f.tell()<end:
   line=f.readline(min(1048577,end-f.tell()))
   size+=len(line);digest.update(line)
   if len(line)>1048576 or not line.endswith(b'\n'):raise ValueError('JOURNAL_LINE_OR_TAIL')
   if size>268435456:raise ValueError('JOURNAL_WORK_BUDGET')
   r=parse(line);count+=1
   if type(r) is not dict or type(r.get('seq')) is not int or r['seq']!=count or type(r.get('state')) is not dict or r['state'].get('seq')!=count or type(r.get('events')) is not list:raise ValueError('JOURNAL_SEQUENCE')
   delta=Decimal(0)
   for event in r['events']:
    if type(event) is not dict:raise ValueError('EVENT_SCHEMA')
    kind=event.get('kind');cash=Decimal(0)
    if kind=='ENTRY':
     fee=dec(event.get('fee'));fees+=fee;cash=-fee
    elif kind in ('STOP','DAY_STOP','TIME_EXIT','TP1','TP2','STALL_EXIT'):
     cash=dec(event.get('pnl_after_exit_fee'));fees+=dec(event.get('fee'))
    elif kind=='FUNDING':cash=dec(event.get('cashflow'));funding+=cash
    elif kind in ('NEWS_PLAN_ADMISSION','NEWS_ADMISSION'):continue
    else:raise ValueError('UNSUPPORTED_EVENT_KIND')
    delta+=cash;total+=cash
    last_event=ts(event.get('t')) or last_event
    tail.append({k:event.get(k) for k in ('kind','id','symbol','t','price','qty','fee','pnl_after_exit_fee','cashflow','model')})
   actual=dec(r['state'].get('cash'))
   if abs(previous+delta-actual)>Decimal('0.00000001'):raise ValueError('CASH_RECONCILIATION')
   previous=actual;last=r['state']
  final=os.fstat(f.fileno())
  if (initial.st_ino,initial.st_size,initial.st_mtime_ns)!=(final.st_ino,final.st_size,final.st_mtime_ns):raise ValueError('SOURCE_CHANGED_RETRY')
 if not count or last!=state:raise ValueError('STATE_JOURNAL_MISMATCH')
 return {'realized':float(total),'fees':float(fees),'funding':float(funding),'transfers':0.0,'initial':150.,'cash':float(previous),'count':count,'bytes':size,'sha256':digest.hexdigest(),'last_event_ms':last_event,'trades':list(reversed(tail)),'last':last,'status':'RECONCILED'}

def read_boss(root,*,now_ms=None):
 now=time.time_ns()//1000000 if now_ms is None else now_ms
 if n(now) is None:raise ValueError('CLOCK')
 raw={};health={}
 clocks={'tactical':('updated_ms',180000),'scan':('completed_ms',900000),'coach':('ts_ms',900000),'weekly':('updated',900000),'prediction':('updated_ms',180000)}
 for key in FILES:
  if key in ('journal','weekly_ledger'):continue
  try:
   d,h=load(root,key);raw[key]=d
   clock,ttl=clocks.get(key,(None,None));stamp=ts(d.get(clock)) if clock else None
   health[key]={'path':FILES[key],'sha256':h,'source_ms':stamp,'status':('NO_SOURCE_CLOCK' if stamp is None else 'FUTURE' if stamp>now+5000 else 'STALE' if now-stamp>ttl else 'FRESH') if clock else 'NO_SOURCE_CLOCK'}
  except (OSError,ValueError,TypeError,OverflowError) as e:
   raw[key]={};health[key]={'path':FILES[key],'source_ms':None,'status':'UNKNOWN','issue':type(e).__name__+':'+str(e)[:180]}
 s=raw['state'];t=raw['tactical'];o=raw['orders'];scan=raw['scan'];c=raw['coach'];risk=raw['risk']
 state_ok=s.get('schema')==1 and n(s.get('cash')) is not None and 'position' in s and (s['position'] is None or type(s['position']) is dict) and type(s.get('used')) is list
 pos=s.get('position') if state_ok else None
 try:
  j=journal(root,s);health['journal']={k:v for k,v in j.items() if k not in ('last','trades')}
 except (OSError,ValueError,TypeError,OverflowError,KeyError) as e:
  j={};health['journal']={'status':'UNKNOWN','issue':str(e)[:200]}
 consistent=state_ok and bool(j) and t.get('mode')=='PAPER_ONLY' and t.get('cash')==s['cash'] and t.get('used_order_ids')==s['used'] and type(t.get('open_positions')) is int and t['open_positions']==int(pos is not None)
 clock=ts(t.get('updated_ms'));fresh=health['tactical']['status']=='FRESH'
 flat=consistent and fresh and t.get('status')=='OK' and pos is None
 unreal=0. if flat else n(t.get('unrealized_after_exit_cost')) if consistent and pos else None
 equity=n(t.get('equity')) if consistent and (pos is None or unreal is not None) else None
 margin=0. if state_ok and pos is None else n(pos.get('isolated_margin')) if pos else None
 available=n(s.get('cash'))-margin if state_ok and consistent and margin is not None else None
 order_list=o.get('orders');orders_known=o.get('schema')==1 and o.get('mode')=='PAPER_ONLY' and type(order_list) is list and len(order_list)<=32 and state_ok
 active=[]
 if orders_known:
  for p in order_list:
   if type(p) is not dict:orders_known=False;continue
   expiry=ts(p.get('expires_ms'));created=ts(p.get('created_ms'))
   valid=expiry is not None and created is not None and isinstance(p.get('id'),str) and p.get('side') in ('long','short') and type(p.get('trigger')) is dict and all(n(p.get(k)) is not None for k in ('entry','stop','tp1','tp2','leverage','notional','risk_budget'))
   if not valid:orders_known=False;continue
   if p.get('approved') is True and created<=now<expiry and p['id'] not in s['used']:
    active.append({k:p.get(k) for k in ('id','symbol','side','entry','stop','tp1','tp2','leverage','trigger','expires_ms','notional','risk_budget')})
 risk_exp=n(risk.get('expires_at'));collector_exp=n(risk.get('collector_valid_until'))
 risk_allows=risk.get('mode')=='NORMAL' and risk.get('deny_new_risk') is False and risk_exp is not None and now<risk_exp*1000 and collector_exp is not None and now<collector_exp*1000
 health['risk']['source_ms']=n(risk.get('generated_at'))*1000 if n(risk.get('generated_at')) is not None else None
 health['risk']['raw_mode']=risk.get('mode');health['risk']['collector_valid_until']=collector_exp
 account={'bankroll':150.,'cash':n(s.get('cash')) if state_ok else None,'equity':equity,'available_margin':available,'available_basis':'現金扣實際隔離保證金（保守可用額，非交易所風險引擎）','realized':j.get('realized'),'fees':j.get('fees'),'funding':j.get('funding'),'transfers':j.get('transfers'),'unrealized':unreal,'flat_confirmed':flat,'position_known':state_ok,'position':{k:pos.get(k) for k in ('id','symbol','side','entry','entry_ms','qty','stop','tp1','tp2','leverage','isolated_margin')} if pos else None,'observed_ms':clock,'state':'暫停新單' if not risk_allows or not fresh or t.get('status')!='OK' else '條件監控中','valuation_state':'空倉' if flat else '尚無估值' if unreal is None else '持倉估值','orders':active if orders_known else None,'trades':j.get('trades'),'journal_count':j.get('count'),'risk_note':'強平模型尚未完整。60秒輪詢、費用與滑價為紙盤假設，非100%實盤一致。'}
 candidates=scan.get('candidates');top=[]
 if type(candidates) is list:
  for a in candidates[:3]:
   if type(a) is not dict:continue
   metrics=a.get('metrics') if type(a.get('metrics')) is dict else {}
   top.append({'symbol':a.get('symbol'),'branch':a.get('branch'),'oi4h':n(metrics.get('oi_4h')),'funding':n(a.get('lastFundingRate')),'absorption':{'BUY_ABSORPTION':'買盤吸收','SELL_ABSORPTION':'賣盤吸收'}.get(a.get('branch')),'observed_ms':ts(a.get('observed_ms'))})
 coach_stamp=ts(c.get('ts_ms')) or ts(c.get('t'));scanmatch=(c.get('scan_id')==scan.get('started_ms')) if c.get('scan_id') is not None else False
 health['coach']['source_ms']=coach_stamp;health['coach']['scan_match']=scanmatch;health['coach']['raw_reason']=c.get('reason');health['coach']['raw_risk']=c.get('risk')
 radar={'top3':top if type(candidates) is list else None,'observed_ms':ts(scan.get('completed_ms')),'waiting':health['scan']['status']!='FRESH','coach_observed_ms':coach_stamp,'coach_current':health['coach']['status']=='FRESH' and scanmatch,'coach_reason':c.get('reason') if coach_stamp and isinstance(c.get('reason'),str) else None,'eligible':n(scan.get('eligible')),'anomalies':n(scan.get('anomalies'))}
 w=raw['weekly'];ws=raw['weekly_state'];books=[];gross=n(w.get('gross_usdt_assumed'))
 opens=ws.get('open') if type(ws.get('open')) is list else []
 tranches=w.get('tranches') if type(w.get('tranches')) is list else []
 for tr in tranches[:2]:
  if type(tr) is not dict:continue
  entry=tr.get('entry_day');matched=next((a for a in opens if type(a) is dict and a.get('entry_day')==entry),{})
  obs=matched.get('obs') if type(matched.get('obs')) is dict else {}
  stamps=[ts(v.get('t')) for v in obs.values() if type(v) is dict and ts(v.get('t')) is not None]
  source_books=tr.get('books') if type(tr.get('books')) is dict else {}
  for name in ('F2_raw_quint','F3_ra_N8_fundexcl'):
   b=source_books.get(name) if type(source_books.get(name)) is dict else {}
   complete=b.get('missing')==0 and gross==150 and w.get('basis_version','linear-usdt-v2')=='linear-usdt-v2'
   def leg(key):return n(b.get(key))*gross/20000 if complete and n(b.get(key)) is not None else None
   books.append({'name':name,'entry_day':ts(entry),'exit_day':ts(tr.get('exit_day')),'observed_start_ms':min(stamps) if stamps else None,'observed_end_ms':max(stamps) if stamps else None,'long_pnl':leg('long_leg_bp'),'short_pnl':leg('short_leg_pnl_bp'),'funding':n(b.get('funding_bp'))*gross/10000 if complete and n(b.get('funding_bp')) is not None else None,'net':n(b.get('net_usdt_at_150_gross')) if complete else None,'fee_assumption':gross*14/10000 if gross is not None else None,'missing':b.get('missing')})
 weekly={'books':books,'observed_ms':ts(w.get('updated')),'waiting':health['weekly']['status']!='FRESH','gross_per_book':gross,'basis':'每個book各假設150U gross，多空各半。四個假設組合不是同一資金帳戶，不合計。資金費為進場名目本金近似，費用預扣往返14bp。','settlements':None}
 try:
  tail=deque(maxlen=10);size=0
  with opened(root,'weekly_ledger') as f:
   for line in f:
    size+=len(line)
    if len(line)>1048576 or size>268435456:raise ValueError('SIZE_CAP')
    row=parse(line)
    if type(row) is not dict:raise ValueError('SCHEMA')
    tail.append({k:row.get(k) for k in ('book','entry_day','exit_day','long_leg_bp','short_leg_bp','funding_bp','net14_bp','net_bp','obs_lag_h_entry')})
  weekly['settlements']=list(reversed(tail));health['weekly_ledger']={'status':'READ','rows_in_view':len(tail)}
 except FileNotFoundError:health['weekly_ledger']={'status':'NOT_YET_CREATED','issue':'尚無結算檔（不等於已證明零結算）'}
 except (OSError,ValueError,TypeError) as e:health['weekly_ledger']={'status':'UNKNOWN','issue':str(e)[:180]}
 p=raw['prediction']
 prediction={'state':'規則與介面核對中，尚未啟動' if p else '紙盤尚未啟動','bankroll':None,'stake':None,'net_pnl':None,'official_win_rate':None,'proxy_win_rate':None,'strike':None,'spot':None,'checkpoints':None,'trades':None,'observed_ms':ts(p.get('updated_ms')),'proxy_note':'探索性 proxy 未校準，不等於官方結算或官方勝率。'}
 # Never relabel Polymarket/other-provider output as Binance. Fail closed.
 if p and (p.get('provider')!='binance' or p.get('product')!='5m_prediction'):
  health['prediction'].update(status='REJECTED_PROVIDER',issue='只接受 provider=binance / product=5m_prediction，不接受其他平台改名')
 elif p and (p.get('schema')!='prediction-paper.v1' or p.get('rules_verified') is not True or p.get('interface_verified') is not True):
  health['prediction'].update(status='UNVERIFIED_INTERFACE',issue='需真規則與介面驗證，禁止假績效')
 elif p:
  prediction.update(state='等待更新' if health['prediction']['status']!='FRESH' else '紙盤觀測中',**{k:n(p.get(k)) for k in ('bankroll','stake','net_pnl','official_win_rate','proxy_win_rate','strike','spot')})
  cp=p.get('checkpoints')
  if type(cp) is dict:prediction['checkpoints']={label:{key:n(cp.get(label,{}).get(key)) for key in ('model_q','ask')} for label in ('T120','T60') if type(cp.get(label)) is dict}
  if type(p.get('paper_ledger')) is list:prediction['trades']=[{k:r.get(k) for k in ('t','side','stake','ask','model_q','settlement_kind','result','net_pnl')} for r in p['paper_ledger'][-10:][::-1] if type(r) is dict]
 return {'schema':'boss-dashboard.v1','served_ms':now,'tactical':account,'radar':radar,'prediction':prediction,'weekly':weekly,'engineering':{'sources':health,'contracts':'old APIs unchanged; fixed allowlist only; read-only','liquidation_model':'NOT_COMPLETE','cash_accounting':'ENTRY -fee + exit pnl_after_exit_fee + FUNDING cashflow. No transfer type in producer; unsupported event => unavailable, never cash-150.'}}
