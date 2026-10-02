#!/usr/bin/env python
"""Read-only parent acceptance of PAPER v3 artifacts and account continuity."""
import argparse, hashlib, json, sqlite3, time
from contextlib import closing
from decimal import Decimal as D, localcontext
from pathlib import Path
from dashboard import validate_snapshot
from paper_runtime_v2 import canonical
from datetime import datetime

def verify_freshness(s,*,now_ms):
    updated=int(datetime.fromisoformat(s['updated_at'].replace('Z','+00:00')).timestamp()*1000)
    if not 0<=now_ms-updated<=60000:raise ValueError('stale/future snapshot')
    for market in s['markets']:
        for source in market['source_timestamps_ms'].values():
            if type(source) is not int or not 0<=now_ms-source<=15000:
                raise ValueError('stale/future source at acceptance read')

def verify_audit(path):
    previous='0'*64; count=receipts=0
    with closing(sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True)) as db:
        for i,p,prev,h in db.execute('SELECT id,payload,previous_hash,hash FROM audit ORDER BY id'):
            if prev!=previous or hashlib.sha256((prev+p).encode()).hexdigest()!=h:
                raise ValueError('audit chain mismatch at '+str(i))
            previous=h;count+=1;receipts+=json.loads(p).get('type')=='public_receipt'
    return dict(audit_rows=count,public_receipts=receipts,audit_head=previous)

def accept(state_dir,status_path,*,backup_dir=None,now_ms=None):
    root=Path(state_dir); lab=Path(__file__).resolve().parent
    s=json.loads(Path(status_path).read_text());validate_snapshot(s)
    verify_freshness(s,now_ms=int(time.time()*1000) if now_ms is None else now_ms)
    if s['fixture'] or s['live_trading_enabled']:raise ValueError('public PAPER only')
    if s['engine']['version_id']!='H1-PAPER-003':raise ValueError('wrong strategy')
    if s['latest_error']:raise ValueError('public probe blocked: '+s['latest_error'])
    for name,h in s['engine']['source_sha256'].items():
        if hashlib.sha256((lab/name).read_bytes()).hexdigest()!=h:raise ValueError('source manifest mismatch: '+name)
    report=verify_audit(root/'runtime.sqlite3')
    with closing(sqlite3.connect(f'file:{root.resolve()}/broker.sqlite3?mode=ro',uri=True)) as db:
        b=json.loads(db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()[0])
    with localcontext() as ctx:
        ctx.prec=40;cash=D(b['initial_cash']);index=0
        while index<len(b['ledger']):
            x=b['ledger'][index]
            if x['type']=='realized' and index+1<len(b['ledger']) and b['ledger'][index+1]['type']=='fee':
                cash+=D(b['ledger'][index+1]['amount']);cash+=D(x['amount']);index+=2
            else:cash+=D(x['amount']);index+=1
        if cash!=D(b['cash']) or cash!=D(s['cash_usdt']):raise ValueError('cash ledger mismatch')
    if backup_dir:
        backup=Path(backup_dir)
        with closing(sqlite3.connect(f'file:{backup.resolve()}/broker.sqlite3?mode=ro',uri=True)) as db:
            old=json.loads(db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()[0])
        for field in ('fills','ledger','audit'):
            if b[field][:len(old[field])]!=old[field]:raise ValueError('previous '+field+' overwritten')
        for key,value in old['orders'].items():
            if b['orders'].get(key)!=value:raise ValueError('previous order changed')
        if b['meta']!=old['meta'] or b['initial_cash']!=old['initial_cash']:
            raise ValueError('account identity reset')
        with closing(sqlite3.connect(f'file:{backup.resolve()}/runtime.sqlite3?mode=ro',uri=True)) as olddb, closing(sqlite3.connect(f'file:{root.resolve()}/runtime.sqlite3?mode=ro',uri=True)) as newdb:
            old_rows=list(olddb.execute('SELECT * FROM audit ORDER BY id'))
            if list(newdb.execute('SELECT * FROM audit ORDER BY id LIMIT ?',(len(old_rows),)))!=old_rows:raise ValueError('old runtime audit prefix changed')
            old_state=json.loads(olddb.execute('SELECT payload FROM state WHERE id=1').fetchone()[0]);new_state=json.loads(newdb.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
            for k in ('forward_start_ms','total_halted','daily_halted_day'):
                if old_state.get(k)!=new_state.get(k):raise ValueError('risk/account baseline changed: '+k)
        report['previous_account_prefix_preserved']=True
    report.update(cash_usdt=s['cash_usdt'],equity_usdt=s['equity_usdt'],fills=s['fills_count'],research=s['research'],candidate_not_deployed=s['candidate_not_deployed'],latest_error=s['latest_error'],source_manifest_verified=True,fixture=False)
    return report

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state-dir',required=True,type=Path);p.add_argument('--status',required=True,type=Path);p.add_argument('--backup-dir',type=Path);p.add_argument('--out',required=True,type=Path)
    a=p.parse_args();r=accept(a.state_dir,a.status,backup_dir=a.backup_dir);a.out.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r,indent=2))
if __name__=='__main__':main()
