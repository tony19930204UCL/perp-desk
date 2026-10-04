#!/usr/bin/env python
"""Fail-closed same-account H1-PAPER-003 -> H1-PAPER-004 staged migration.

No account backup/restore is created here. The operator owns stopped/locked
deployment capture. This migration changes only runtime strategy registration
state after proving the existing account stores are internally consistent.
"""
import argparse, fcntl, hashlib, json, os, sqlite3, time
from contextlib import closing
from decimal import Decimal as D, Context, localcontext
from pathlib import Path

from paper_runtime_v2 import canonical
from paper_runtime_v4 import PaperRuntime, CONFIG_HASH, WINDOW_MS

OLD_CONFIG_HASH='622b8de7d554d36ae77f913944746c8c32b5ea00e59e8385fe5716553fe6a069'


def _quick_check(path,required):
    if not path.is_file() or path.is_symlink():
        raise ValueError('missing/unsafe '+path.name)
    try:
        with closing(sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True)) as db:
            if db.execute('PRAGMA quick_check').fetchone()!=('ok',):
                raise ValueError('corrupt '+path.name)
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(required)<=tables:
                raise ValueError('missing required tables in '+path.name)
    except sqlite3.DatabaseError as exc:
        raise ValueError('corrupt '+path.name) from exc


def _verify_audit(path):
    previous='0'*64
    with closing(sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True)) as db:
        for payload,prev,h in db.execute('SELECT payload,previous_hash,hash FROM audit ORDER BY id'):
            expected=hashlib.sha256((previous+payload).encode()).hexdigest()
            if prev!=previous or h!=expected:
                raise ValueError('runtime audit chain mismatch')
            previous=h
    return previous


def _read_closeout(path,old,now_ms):
    raw=Path(path).read_bytes()
    data=json.loads(raw)
    required={
        'schema_version':1,
        'version_id':'H1-PAPER-003',
        'operator_accepted':True,
        'account_forward_start_ms':old.get('forward_start_ms'),
        'strategy_start_ms':old.get('strategy_start_ms'),
        'research_deadline_ms':old.get('research_deadline_ms')}
    for key,value in required.items():
        if data.get(key)!=value:
            raise ValueError('operator closeout acceptance mismatch: '+key)
    accepted=data.get('accepted_at_ms')
    if type(accepted) is not int or accepted<old['research_deadline_ms'] or accepted>now_ms:
        raise ValueError('operator closeout acceptance time invalid')
    return data,hashlib.sha256(raw).hexdigest()


def _broker_state(path):
    with closing(sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()
    if row is None:
        raise ValueError('missing broker state row')
    broker=json.loads(row[0])
    with localcontext(Context(prec=50)):
        cash=D(broker['initial_cash'])
        for item in broker['ledger']:
            cash+=D(item['amount'])
        if cash!=D(broker['cash']):
            raise ValueError('cash ledger invariant failed before migration')
    if broker.get('positions') or any(
            order.get('status') in ('PENDING','RESTING')
            for order in broker.get('orders',{}).values()):
        raise ValueError('flat account without pending orders required; do not discard exposure')
    return broker


def migrate(state_dir,config_path,closeout_path,*,now_ms=None):
    root=Path(state_dir).resolve()
    now_ms=int(time.time()*1000) if now_ms is None else now_ms
    config=Path(config_path).read_bytes()
    if hashlib.sha256(config).hexdigest()!=CONFIG_HASH:
        raise ValueError('immutable H1-PAPER-004 config mismatch')
    descriptors=[]
    try:
        for name in ('runtime.lock','broker.sqlite3'):
            target=root/name
            fd=os.open(target,os.O_RDONLY|os.O_NOFOLLOW)
            descriptors.append(fd)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('account locked; stop exact runtime before migration') from None
        return _migrate(root,closeout_path,now_ms=now_ms)
    finally:
        for fd in descriptors:
            os.close(fd)


def _migrate(root,closeout_path,*,now_ms):
    _quick_check(root/'runtime.sqlite3',('state','audit'))
    _quick_check(root/'broker.sqlite3',('sim_broker_state',))
    _quick_check(root/'signals.sqlite3',('h1_versions','h1_state','h1_signals'))
    if (root/'indicator_context.sqlite3').exists():
        _quick_check(root/'indicator_context.sqlite3',('context',))
    audit_head=_verify_audit(root/'runtime.sqlite3')
    with closing(sqlite3.connect('file:'+str((root/'runtime.sqlite3').resolve())+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT payload FROM state WHERE id=1').fetchone()
    if row is None:
        raise ValueError('missing runtime state row')
    old=json.loads(row[0])
    if old.get('config_hash')!=OLD_CONFIG_HASH:
        raise ValueError('not exact H1-PAPER-003 configuration')
    deployment=old.get('deployment')
    if not isinstance(deployment,dict) or deployment.get('version_id')!='H1-PAPER-003':
        raise ValueError('H1-PAPER-003 deployment marker required')
    if type(old.get('research_deadline_ms')) is not int or now_ms<old['research_deadline_ms']:
        raise ValueError('H1-PAPER-003 fixed window not closed')
    closeout,closeout_hash=_read_closeout(closeout_path,old,now_ms)
    broker=_broker_state(root/'broker.sqlite3')
    broker_meta=broker.get('meta')
    if not isinstance(broker_meta,dict):
        raise ValueError('missing broker account identity')
    account_version_id=old.get('account_version_id')
    if (not isinstance(account_version_id,str) or not account_version_id
            or broker_meta.get('version_id')!=account_version_id):
        raise ValueError('broker/runtime account identity mismatch')
    if broker_meta.get('forward_start')!=old.get('forward_start_ms'):
        raise ValueError('broker/runtime forward_start identity mismatch')

    old_window=dict(
        version_id='H1-PAPER-003',
        strategy_start_ms=old['strategy_start_ms'],
        research_deadline_ms=old['research_deadline_ms'],
        strategy_fill_baseline=old.get('strategy_fill_baseline'),
        strategy_ledger_baseline=old.get('strategy_ledger_baseline'),
        closeout_acceptance_sha256=closeout_hash,
        closeout_accepted_at_ms=closeout['accepted_at_ms'])
    history=list(old.get('strategy_window_history',[]))
    history.append(old_window)

    new=dict(old)
    new.update(
        config_hash=CONFIG_HASH,
        account_version_id=broker['meta']['version_id'],
        strategy_start_ms=now_ms,
        research_deadline_ms=now_ms+WINDOW_MS,
        strategy_fill_baseline=len(broker['fills']),
        strategy_ledger_baseline=len(broker['ledger']),
        strategy_signal_baseline=list(old.get('handled_signals',{})),
        decision_cutoff_ms=now_ms,
        cursor=None,warmup=0,gap_open=False,
        diagnostic='v4_waiting_new_context',
        candidate_diagnostics=[],
        minute_coverage={},
        poll_diagnostics=dict(
            poll_error_events=0,error_periods_started=0,recovery_observations=0,
            consecutive_error_polls=0,active_error_period=None,error_periods=[]),
        strategy_window_history=history,
        h1_paper_004_closeout_acceptance_sha256=closeout_hash)
    previous_deployment=new.pop('deployment',None)

    event=dict(
        type='strategy_migration',at_ms=now_ms,version_id='H1-PAPER-004',
        previous_version_id='H1-PAPER-003',
        previous_deployment=previous_deployment,
        previous_research_deadline_ms=old['research_deadline_ms'],
        operator_closeout_acceptance_sha256=closeout_hash,
        account_preserved=True,broker_unchanged=True,
        old_audit_head=audit_head)
    payload=canonical(event)
    new_hash=hashlib.sha256((audit_head+payload).encode()).hexdigest()
    with closing(sqlite3.connect(root/'runtime.sqlite3')) as db, db:
        current=db.execute('SELECT hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
        current=current[0] if current else '0'*64
        if current!=audit_head:
            raise ValueError('runtime audit changed during migration validation')
        db.execute('INSERT INTO audit(payload,previous_hash,hash) VALUES(?,?,?)',
                   (payload,audit_head,new_hash))
        db.execute('UPDATE state SET payload=? WHERE id=1',(canonical(new),))

    return dict(
        version_id='H1-PAPER-004',candidate_not_deployed=True,
        cash_usdt=broker['cash'],initial_cash=broker['initial_cash'],
        fills=len(broker['fills']),ledger_rows=len(broker['ledger']),
        orders=len(broker['orders']),
        original_forward_start_ms=old['forward_start_ms'],
        previous_strategy_start_ms=old['strategy_start_ms'],
        previous_deadline_ms=old['research_deadline_ms'],
        strategy_start_ms=now_ms,deadline_ms=now_ms+WINDOW_MS,
        strategy_fill_baseline=len(broker['fills']),
        strategy_ledger_baseline=len(broker['ledger']),
        old_audit_head=audit_head,new_audit_head=new_hash,
        operator_closeout_acceptance_sha256=closeout_hash)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state-dir',type=Path,required=True)
    p.add_argument('--config',type=Path,default=Path(__file__).with_name('paper_config_v4.json'))
    p.add_argument('--accepted-closeout',type=Path,required=True)
    p.add_argument('--apply',action='store_true',required=True)
    a=p.parse_args(argv)
    print(json.dumps(migrate(a.state_dir,a.config,a.accepted_closeout),indent=2,sort_keys=True))


if __name__=='__main__':
    raise SystemExit(main())
