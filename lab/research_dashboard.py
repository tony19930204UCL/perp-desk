"""Read-only adapter for explicitly configured discovery research evidence."""
import json
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path

ID_RE=re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
ARMS=('A','B','C')
DECIMAL_FIELDS=('cash','initial_cash','fees_usdt','funding_pnl_usdt','gross_realized_pnl_usdt','net_ledger_usdt')
COUNT_FIELDS=('positions','raw_candidates','cost_qualified','submitted','filled_entry_orders','flat_to_flat_count','risk_rejections')

def _safe_path(root, rel, label):
    if not isinstance(rel,str):
        raise ValueError('invalid '+label+' path')
    p=Path(rel)
    if p.is_absolute() or not p.parts or '..' in p.parts or '.' in p.parts:
        raise ValueError(label+' must be a safe relative path')
    out=(root/p).resolve(strict=False)
    if not out.is_relative_to(root):
        raise ValueError(label+' escapes configured research root')
    cursor=root
    for part in p.parts:
        cursor=cursor/part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError(label+' symlink rejected')
    return out

def load_research_config(path):
    if path is None:
        return None
    path=Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('research config must be an absolute regular path')
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or data.get('schema_version')!=1:
        raise ValueError('invalid research config schema')
    root_raw=data.get('root')
    if not isinstance(root_raw,str) or not Path(root_raw).is_absolute():
        raise ValueError('research root must be absolute')
    root_path=Path(root_raw)
    if root_path.is_symlink():
        raise ValueError('research root symlink rejected')
    root=root_path.resolve()
    if not root.is_dir():
        raise ValueError('research root unavailable or unsafe')
    current=data.get('current')
    if not isinstance(current,dict):
        raise ValueError('current research config required')
    rid=current.get('id');label=current.get('label')
    if not isinstance(rid,str) or ID_RE.fullmatch(rid) is None:
        raise ValueError('invalid research id')
    if not isinstance(label,str) or not label.strip() or len(label)>120:
        raise ValueError('invalid research label')
    fresh=current.get('fresh_seconds',15)
    if type(fresh) is not int or not 1<=fresh<=300:
        raise ValueError('invalid research freshness threshold')
    result=dict(schema_version=1,root=root,id=rid,label=label.strip(),fresh_seconds=fresh)
    for key in ('report','feed_db'):
        result[key+'_path']=_safe_path(root,current.get(key),key)
    arms=current.get('arms')
    if not isinstance(arms,dict) or set(arms)!=set(ARMS):
        raise ValueError('research arm paths must be exactly A/B/C')
    result['arm_paths']={arm:_safe_path(root,arms[arm],arm+' broker') for arm in ARMS}
    proof=current.get('process_proof')
    result['process_proof_path']=_safe_path(root,proof,'process proof') if proof is not None else None
    return result

def _dec(value):
    if not isinstance(value,str):
        raise ValueError('research financial values must be decimal strings')
    d=Decimal(value)
    if not d.is_finite():
        raise ValueError('invalid research decimal')
    return d

def validate_report(report):
    if not isinstance(report,dict):
        raise ValueError('research report must be an object')
    if not isinstance(report.get('version_id'),str) or not report['version_id']:
        raise ValueError('research version missing')
    if type(report.get('activated')) is not bool:
        raise ValueError('research activation flag invalid')
    if type(report.get('start_ms')) is not int or type(report.get('deadline_ms')) is not int or report['deadline_ms']<=report['start_ms']:
        raise ValueError('research window invalid')
    if report.get('shared_window') is not True or report.get('capital_pooled') is not False or report.get('default_account_touched') is not False:
        raise ValueError('research isolation flags invalid')
    arms=report.get('arms')
    if not isinstance(arms,dict) or set(arms)!=set(ARMS):
        raise ValueError('research report arms invalid')
    for arm in ARMS:
        row=arms[arm]
        if not isinstance(row,dict):
            raise ValueError('arm report invalid')
        for key in DECIMAL_FIELDS:_dec(row.get(key))
        for key in COUNT_FIELDS:
            if type(row.get(key)) is not int or row[key]<0:raise ValueError('arm counter invalid')
        checkpoint=row.get('checkpoint')
        if not isinstance(checkpoint,dict) or checkpoint.get('arm')!=arm or type(checkpoint.get('covered')) is not int or type(checkpoint.get('expected')) is not int:
            raise ValueError('checkpoint invalid')
        _dec(checkpoint.get('coverage'))
    runner=report.get('runner')
    if runner is not None and not isinstance(runner,dict):
        raise ValueError('runner report invalid')
    return report

def _read_broker(path):
    uri='file:'+str(path)+'?mode=ro'
    with sqlite3.connect(uri,uri=True) as db:
        row=db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()
    if row is None:raise ValueError('broker state missing')
    state=json.loads(row[0])
    cash=_dec(str(state.get('cash')))
    initial=_dec(str(state.get('initial_cash')))
    orders=state.get('orders');positions=state.get('positions');marks=state.get('marks')
    if not isinstance(orders,dict) or not isinstance(positions,dict) or not isinstance(marks,dict):
        raise ValueError('broker state collections invalid')
    pending=sum(1 for order in orders.values() if isinstance(order,dict) and order.get('status') in ('PENDING','RESTING'))
    pos=[]
    unrealized=Decimal(0);equity_known=True
    with localcontext() as ctx:
        ctx.prec=60
        for symbol,p in positions.items():
            if not isinstance(symbol,str) or not isinstance(p,dict):raise ValueError('position invalid')
            qty=_dec(str(p.get('qty')));entry=_dec(str(p.get('entry')))
            mark_raw=marks.get(symbol)
            if mark_raw is None:
                equity_known=False;mark=None
            else:
                mark=_dec(str(mark_raw));unrealized += qty*(mark-entry)
            pos.append(dict(symbol=symbol,direction='long' if qty>0 else 'short',qty=str(qty),entry=str(entry),
                            mark=str(mark) if mark is not None else None))
        equity=cash+unrealized if equity_known else None
        pnl=equity-initial if equity is not None else None
    return dict(cash=str(cash),initial_cash=str(initial),equity=str(equity) if equity is not None else None,
                total_pnl=str(pnl) if pnl is not None else None,unrealized_pnl=str(unrealized) if equity_known else None,
                pending_orders=pending,positions=pos,equity_confirmed=equity_known)

def _feed_evidence(path):
    uri='file:'+str(path)+'?mode=ro'
    with sqlite3.connect(uri,uri=True) as db:
        rows=db.execute('SELECT payload FROM feed_events ORDER BY seq DESC LIMIT 64').fetchall()
    fallback=None
    for (raw,) in rows:
        event=json.loads(raw)
        if isinstance(event,dict) and type(event.get('source_ts')) is int and type(event.get('ts')) is int:
            receipt=event.get('receipt_ts')
            if receipt is not None and type(receipt) is not int:continue
            item=dict(type=event.get('type'),event_id=event.get('event_id'),
                      source_ts=event['source_ts'],receipt_ts=receipt,dispatch_ts=event['ts'],
                      source_valid=event.get('source_valid'))
            if receipt is not None:return item
            if fallback is None:fallback=item
    return fallback

def _process_proof(path,now_ms,fresh_seconds):
    if path is None or not path.exists():
        return dict(configured=path is not None,confirmed=False,state='unconfirmed',reason='process proof unavailable')
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(data,dict) or data.get('schema_version')!=1 or type(data.get('active')) is not bool
                or type(data.get('observed_at_ms')) is not int or data.get('proof_kind') not in ('owned_process','supervisor')):
            raise ValueError('invalid process proof')
        age=(now_ms-data['observed_at_ms'])/1000
        if age<0:return dict(configured=True,confirmed=False,state='future',age_seconds=age,proof_kind=data['proof_kind'])
        confirmed=bool(data['active'] and age<=fresh_seconds)
        return dict(configured=True,confirmed=confirmed,state='confirmed' if confirmed else ('inactive' if not data['active'] else 'stale'),
                    age_seconds=age,proof_kind=data['proof_kind'])
    except (OSError,ValueError,TypeError,KeyError,json.JSONDecodeError):
        return dict(configured=True,confirmed=False,state='malformed',reason='process proof invalid')

def read_research(config,now=None):
    if config is None:
        return dict(schema_version=1,configured=False,available=False,state='unconfigured',
                    error='Current research is not configured')
    now=now or datetime.now(timezone.utc);now_ms=int(now.timestamp()*1000)
    result=dict(schema_version=1,configured=True,available=False,id=config['id'],label=config['label'])
    try:
        report=validate_report(json.loads(config['report_path'].read_text(encoding='utf-8')))
        mtime_ms=int(config['report_path'].stat().st_mtime*1000)
        runner=report.get('runner') or {}
        last_poll=runner.get('last_poll_ms')
        if last_poll is not None and type(last_poll) is not int:raise ValueError('last poll timestamp invalid')
        last_failure=runner.get('last_failure_ms')
        if last_failure is not None and type(last_failure) is not int:raise ValueError('last failure timestamp invalid')
        poll_age=(now_ms-last_poll)/1000 if last_poll is not None else None
        if poll_age is None:activity='unconfirmed'
        elif poll_age<0:activity='future'
        elif runner.get('source_failure') is True:activity='source_gap'
        elif poll_age<=config['fresh_seconds']:activity='fresh'
        else:activity='stale'
        feed=_feed_evidence(config['feed_db_path'])
        if feed:
            feed['source_age_seconds']=(now_ms-feed['source_ts'])/1000
            feed['receipt_age_seconds']=(now_ms-feed['receipt_ts'])/1000 if feed['receipt_ts'] is not None else None
        arms={}
        for arm in ARMS:
            broker=_read_broker(config['arm_paths'][arm])
            row=dict(report['arms'][arm])
            # Broker state is the authority for open exposure/pending/equity; report remains
            # the authority for diagnostics/checkpoint/fee/funding/sample counters.
            row['account']=broker
            arms[arm]=row
        process=_process_proof(config['process_proof_path'],now_ms,config['fresh_seconds'])
        blockers=[]
        if activity!='fresh':blockers.append('market_activity_'+activity)
        if report.get('source_gaps',0):blockers.append('source_gaps_recorded')
        if any(a.get('checkpoint',{}).get('stop_new_entries') for a in report['arms'].values()):blockers.append('checkpoint_entry_stop')
        if report.get('storage_used_bytes',0)>=report.get('storage_budget_bytes',1)*0.9:blockers.append('storage_entry_stop')
        if not process['confirmed']:blockers.append('process_unconfirmed')
        result.update(available=True,state=activity,version_id=report['version_id'],activated=report['activated'],
                      start_ms=report['start_ms'],deadline_ms=report['deadline_ms'],
                      checkpoint_ms=report['start_ms']+28800000,report_file_mtime_ms=mtime_ms,
                      last_successful_poll_ms=last_poll,last_failure_ms=last_failure,
                      poll_age_seconds=poll_age,source_evidence=feed,process_proof=process,
                      source_gaps=report.get('source_gaps',0),unknown_inputs=report.get('unknown_inputs',0),
                      storage_used_bytes=report.get('storage_used_bytes'),storage_budget_bytes=report.get('storage_budget_bytes'),
                      operator_stop_requested=report.get('operator_stop_requested') is True,
                      arms=arms,blockers=blockers,capital_pooled=False,default_account_touched=False,
                      queue_diagnostic_role='B 2x diagnostic only; not an account or selectable fourth arm')
        return result
    except FileNotFoundError:
        result.update(state='missing',error='Current research report or configured state is missing');return result
    except (OSError,ValueError,TypeError,KeyError,ArithmeticError,json.JSONDecodeError,sqlite3.Error) as exc:
        result.update(state='malformed',error='Current research report/state is unavailable or invalid');return result
