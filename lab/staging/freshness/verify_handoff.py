"""Verify staged handoff, preserve raw public evidence, emit exact diffs."""
import collections
import difflib
import hashlib
import json
import sqlite3
from pathlib import Path

STAGE=Path(__file__).resolve().parent
LAB=STAGE.parents[1]
E=LAB/'evidence'
expected=json.loads((E/'freshness_inventory.json').read_text())['ids']
original=json.loads((E/'freshness_original_inventory.json').read_text())
batches=[json.loads((E/f'freshness_batch{i}.json').read_text()) for i in range(1,9)]
passed=[t for b in batches for t in b['passed']]
assert all(b['success'] and sorted(b['passed'])==b['expected'] and sorted(b['started'])==b['expected'] for b in batches)
assert sorted(passed)==expected and len(passed)==len(set(passed))
assert set(original)<=set(expected)
before=json.loads((E/'freshness_live_source_before.json').read_text())
current={name:hashlib.sha256((LAB/name).read_bytes()).hexdigest() for name in before}
assert current==before, 'deployed sources changed'
assert all((STAGE/n).read_bytes()==(LAB/n).read_bytes() for n in before if n!='paper_runtime_v2.py')
source_diff=''.join(difflib.unified_diff((LAB/'paper_runtime_v2.py').read_text().splitlines(True),
    (STAGE/'paper_runtime_v2.py').read_text().splitlines(True),fromfile='a/paper_runtime_v2.py',tofile='b/paper_runtime_v2.py'))
(E/'freshness_runtime.patch').write_text(source_diff)
test_diff=''.join(difflib.unified_diff((LAB/'tests/test_paper_v2.py').read_text().splitlines(True),
    (STAGE/'tests/test_paper_v2.py').read_text().splitlines(True),fromfile='a/tests/test_paper_v2.py',tofile='b/tests/test_paper_v2.py'))
test_diff+=''.join(difflib.unified_diff([], (STAGE/'tests/test_freshness.py').read_text().splitlines(True),
    fromfile='/dev/null',tofile='b/tests/test_freshness.py'))
(E/'freshness_tests.patch').write_text(test_diff)
actual=json.loads((E/'freshness_actual_audit_raw.json').read_text())
live=sqlite3.connect('file:'+str(LAB/'data/paper-v2/runtime.sqlite3')+'?mode=ro',uri=True)
previous=live.execute('SELECT hash FROM audit WHERE id=?',(actual[0]['id']-1,)).fetchone()[0]
for row in actual:
    payload=json.dumps(row['payload'],sort_keys=True,separators=(',',':'),default=str)
    assert row['previous_hash']==previous
    assert row['hash']==hashlib.sha256((previous+payload).encode()).hexdigest()
    previous=row['hash']
live.close()
probes={}
for name in ['freshness_public_probe','freshness_public_slow_probe','freshness_public_slow_probe_attempt2']:
    p=json.loads((E/f'{name}.json').read_text())
    db=sqlite3.connect('file:'+str(Path(p['root'])/'runtime.sqlite3')+'?mode=ro',uri=True)
    raw=[{'id':i,'payload':json.loads(s),'previous_hash':prev,'hash':h} for i,s,prev,h in db.execute('SELECT * FROM audit ORDER BY id')]
    db.close()
    (E/f'{name}_audit.json').write_text(json.dumps(raw,indent=2))
    previous='0'*64
    for row in raw:
        payload=json.dumps(row['payload'],sort_keys=True,separators=(',',':'),default=str)
        assert row['previous_hash']==previous
        assert row['hash']==hashlib.sha256((previous+payload).encode()).hexdigest()
        previous=row['hash']
    assert p['source_sha256']['paper_runtime_v2.py']==hashlib.sha256((STAGE/'paper_runtime_v2.py').read_bytes()).hexdigest()
    assert p['config_sha256']==before['paper_config_v2.json']
    assert p['account_before_restart']==p['account_after_restart']
    assert p['account_after_restart']['fills']==0 and p['account_after_restart']['signals']==0
    deliveries=[row['payload']['receipt'] for row in raw if row['payload'].get('type')=='public_receipt']
    for req in p['requests']:
        if 'receipt_sha256' in req:
            assert any(hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest()==req['receipt_sha256'] for r in deliveries)
    probes[name]={'root':p['root'],'first_error':p['first']['latest_error'],
        'restart_error':p['after_restart']['latest_error'],'fills':p['after_restart']['fills_count'],
        'cash':p['after_restart']['cash_usdt'],'warmup':p['after_restart']['engine']['warmup_received'],
        'receipts':len(deliveries),'audit_chain_verified':True}
assert probes['freshness_public_probe']['first_error'] is None
assert probes['freshness_public_slow_probe_attempt2']['first_error'] is None
slow=json.loads((E/'freshness_public_slow_probe_attempt2.json').read_text())
kline=next(x for x in slow['requests'] if x['endpoint'].endswith('klines'))
refreshed=[x for x in slow['requests'] if x['request_started_ms']>kline['returned_ms'] and x['params'].get('symbol')=='ETHUSDT'][:3]
assert len(refreshed)==3
assert all(x['source_timestamp_ms']>kline['returned_ms'] for x in refreshed)
result={'tests_expected':len(expected),'tests_passed':len(passed),'original_inventory':len(original),
    'missing':sorted(set(expected)-set(passed)),'duplicates':[x for x,c in collections.Counter(passed).items() if c>1],
    'batch_seconds':[x['elapsed_seconds'] for x in batches],
    'live_sources_unchanged':True,'actual_failure_audit_chain_verified':True,'strategy_and_risk_config_unchanged':True,'probes':probes,
    'slow_kline':kline,'refreshed_eth_requests':refreshed,
    'hashes':{str(path.relative_to(LAB)):hashlib.sha256(path.read_bytes()).hexdigest()
              for path in [STAGE/'paper_runtime_v2.py',STAGE/'tests/test_freshness.py',STAGE/'tests/test_paper_v2.py',E/'freshness_runtime.patch',E/'freshness_tests.patch']}}
(E/'freshness_validation.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
