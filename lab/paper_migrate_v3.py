#!/usr/bin/env python
"""Parent-only in-place PAPER strategy migration, retaining account identity."""
import argparse, hashlib, json, shutil, sqlite3, time
from pathlib import Path
from contextlib import closing
from paper_runtime_v2 import canonical
from paper_runtime_v3 import PaperRuntime

OLD_HASH='dd1aa331f86df0892190dc4e06bb96a8b2086a605352e96702063dbc91879c96'

import os, fcntl

def migrate(state_dir,config_path,backup_dir,*,now_ms=None):
    root=Path(state_dir).resolve()
    descriptors=[]
    try:
        for name in ('runtime.lock','broker.sqlite3'):
            fd=os.open(root/name,os.O_RDONLY|os.O_NOFOLLOW)
            descriptors.append(fd)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('account locked; stop exact runtime before migration') from None
        return _migrate(root,config_path,backup_dir,now_ms=now_ms)
    finally:
        for fd in descriptors:
            os.close(fd)

def _migrate(state_dir,config_path,backup_dir,*,now_ms=None):
    root=Path(state_dir).resolve(); backup=Path(backup_dir).resolve()
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    config=Path(config_path).read_bytes()
    if hashlib.sha256(config).hexdigest()!=PaperRuntime.CONFIG_HASH:
        raise ValueError('immutable candidate config mismatch')
    with closing(sqlite3.connect(root/'runtime.sqlite3')) as db:
        old=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
    with closing(sqlite3.connect(root/'broker.sqlite3')) as db:
        broker=json.loads(db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()[0])
    if old['config_hash']!=OLD_HASH: raise ValueError('not original H1-PAPER-002 account')
    from decimal import Decimal as D,localcontext
    with localcontext() as ctx:
        ctx.prec=40;cash=D(broker['initial_cash']);index=0
        while index<len(broker['ledger']):
            x=broker['ledger'][index]
            if x['type']=='realized' and index+1<len(broker['ledger']) and broker['ledger'][index+1]['type']=='fee':
                cash+=D(broker['ledger'][index+1]['amount']);cash+=D(x['amount']);index+=2
            else:cash+=D(x['amount']);index+=1
        if cash!=D(broker['cash']):raise ValueError('cash ledger invariant failed before migration')
    if broker['positions'] or any(o['status'] in ('PENDING','RESTING') for o in broker['orders'].values()):
        raise ValueError('flat account without pending orders required; do not discard positions')
    backup.mkdir(parents=True,exist_ok=False)
    for name in ('runtime.sqlite3','broker.sqlite3','signals.sqlite3','indicator_context.sqlite3'):
        if (root/name).exists():
            with closing(sqlite3.connect(root/name)) as source, closing(sqlite3.connect(backup/name)) as dest:
                source.backup(dest)
    new=dict(old)
    new.update(config_hash=PaperRuntime.CONFIG_HASH,account_version_id=broker['meta']['version_id'],strategy_start_ms=now_ms,research_deadline_ms=now_ms+172800000,strategy_fill_baseline=len(broker['fills']),strategy_ledger_baseline=len(broker['ledger']),strategy_signal_baseline=list(old.get('handled_signals',{})),decision_cutoff_ms=now_ms,cursor=None,warmup=0,gap_open=False,diagnostic='v3_waiting_new_context')
    previous_deployment=new.pop('deployment',None)
    event=dict(type='strategy_migration',at_ms=now_ms,version_id='H1-PAPER-003',previous_state=old,previous_deployment=previous_deployment,backup_path=str(backup),account_preserved=True)
    with closing(sqlite3.connect(root/'runtime.sqlite3')) as db, db:
        previous=db.execute('SELECT hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
        previous=previous[0] if previous else '0'*64
        payload=canonical(event)
        db.execute('INSERT INTO audit(payload,previous_hash,hash) VALUES(?,?,?)',(payload,previous,hashlib.sha256((previous+payload).encode()).hexdigest()))
        db.execute('UPDATE state SET payload=? WHERE id=1',(canonical(new),))
    report=dict(cash_usdt=broker['cash'],initial_cash=broker['initial_cash'],fills=len(broker['fills']),ledger_rows=len(broker['ledger']),orders=len(broker['orders']),original_forward_start_ms=old['forward_start_ms'],strategy_start_ms=now_ms,deadline_ms=now_ms+172800000,backup_path=str(backup),candidate_not_deployed=True)
    (backup/'migration_report.json').write_text(json.dumps(report,indent=2)+'\n')
    return report

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir',type=Path,required=True);p.add_argument('--backup-dir',type=Path,required=True)
    p.add_argument('--config',type=Path,default=Path(__file__).with_name('paper_config_v3.json'))
    p.add_argument('--apply',action='store_true',required=True)
    a=p.parse_args();print(json.dumps(migrate(a.state_dir,a.config,a.backup_dir),indent=2))
if __name__=='__main__':main()
