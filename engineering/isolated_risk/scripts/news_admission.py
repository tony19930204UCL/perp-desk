"""Restriction-only v1 collector consumer. Never reads model risk_assessment.json."""
import hashlib, json, math, os, pathlib, re, stat

def _decode(path):
 p=pathlib.Path(path)
 with p.open('rb') as f:
  b=f.read(1024*1024+1)
 if len(b)>1024*1024:raise ValueError('size')
 def pairs(items):
  out={}
  for k,v in items:
   if k in out:raise ValueError('duplicate key')
   out[k]=v
  return out
 return json.loads(b,object_pairs_hook=pairs,parse_constant=lambda x:(_ for _ in ()).throw(ValueError(x))),hashlib.sha256(b).hexdigest()

def load_config(path):
 p=pathlib.Path(path)
 st=p.lstat()
 if not p.is_absolute() or not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o022:raise ValueError('untrusted parent config')
 return _decode(p)[0]

def check(cfg,plan,now_ms):
 out=dict(admissible=False,reasons=[],scan_id=None,source_sha256=None,policy_sha256=None,policy_addendum_sha256=None,payload_sha256=None,scope='RESTRICTION_ONLY_NOT_TRADE_APPROVAL')
 try:
  if cfg['schema_version']!=1:raise ValueError('config schema')
  path=pathlib.Path(cfg['latest_path'])
  if not path.is_absolute() or path.name!='latest.json':raise ValueError('collector path')
  p,sha=_decode(path);out['payload_sha256']=sha
  if type(p['schema_version']) is not int or p['schema_version']!=1:raise ValueError('schema')
  for k in ('source_sha256','policy_sha256','policy_addendum_sha256'):
   if not re.fullmatch('[0-9a-f]{64}',cfg[k]) or p[k]!=cfg[k]:raise ValueError('binding:'+k)
   out[k]=p[k]
  if not isinstance(p['scan_id'],str) or not re.fullmatch(r'\d{8}T\d{6}-[0-9a-f]{8}',p['scan_id']):raise ValueError('scan id')
  out['scan_id']=p['scan_id'];now=now_ms/1000
  def number(x):return type(x) in (int,float) and math.isfinite(x)
  if not number(now):raise ValueError('clock')
  for k in ('generated_at','valid_until','started_at','completed_at'):
   if not number(p[k]):raise ValueError('clock:'+k)
  if not p['started_at']<=p['completed_at']==p['generated_at']<=now<p['valid_until']:raise ValueError('expired_or_future')
  if not 0<p['valid_until']-p['generated_at']<=min(180,cfg['max_validity_seconds']) or p['completed_at']-p['started_at']>120:raise ValueError('validity')
  if p['mode']!='NORMAL':raise ValueError('mode_not_normal')
  for k in ('deny_new_risk','deny_countertrend','requires_review'):
   if p[k] is not False:raise ValueError(k)
  if p['source_stale']!=[] or p['source_health']['healthy'] is not True or p['source_health']['stale']!=[] or type(p['collector_rc']) is not int or p['collector_rc']!=0:raise ValueError('source_incomplete')
  # Independent explicit structure label, never infer from headline, direction or side.
  # Parent-owned strategy taxonomy is independent of any news/model sidecar.
  strategy=plan.get('strategy_id');taxonomy=cfg['strategy_countertrend']
  if not isinstance(strategy,str) or not strategy or type(taxonomy.get(strategy)) is not bool:raise ValueError('unknown_strategy_classification')
  if taxonomy[strategy] is not False:raise ValueError('countertrend_strategy')
  if type(plan.get('countertrend')) is not bool:raise ValueError('unknown_countertrend')
  if plan['countertrend']:raise ValueError('countertrend')
  if cfg['venue']!='binance' or plan.get('venue')!=cfg['venue']:raise ValueError('unknown_venue')
  venues=p['affected_venues']
  if not isinstance(venues,list) or any(not isinstance(v,str) or not v or v.lower()=='unknown' for v in venues):raise ValueError('venue_schema')
  if cfg['venue'] in venues or '*' in venues or 'all' in venues:raise ValueError('affected_venue')
  sources=p['sources']
  if not isinstance(sources,list) or len({s['source'] for s in sources})!=len(sources):raise ValueError('source_schema')
  if not any(s.get('kind')=='rss' and s.get('ok') is True and s.get('fresh') is True and number(s.get('published_at')) and -120<=now-s['published_at']<=21600 for s in sources):raise ValueError('news_clock')
  if not any(s.get('kind')=='status' and s.get('ok') is True and s.get('fresh') is True and number(s.get('response_date')) and -120<=now-s['response_date']<=180 for s in sources):raise ValueError('status_clock')
  by={s['source']:s for s in sources}
  for a in ('BTC','ETH','SOL','XRP','PARITY'):
   if by['market_'+a].get('ok') is not True or by['market_'+a].get('fresh') is not True:raise ValueError('market_source')
  if by['binance_incident'].get('ok') is not True:raise ValueError('critical_source')
  for k in ('common_close_at','parity_close_at'):
   if not number(p['market'][k]) or not 0<=now-p['market'][k]<=180:raise ValueError('market_clock')
  out['admissible']=True
 except (OSError,ValueError,TypeError,KeyError,AttributeError,OverflowError) as e:out['reasons'].append(str(e)[:200])
 return out
