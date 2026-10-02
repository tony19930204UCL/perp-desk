"""Isolated real public GET probe. No private endpoints, no live namespace."""
import argparse
import hashlib
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from paper_runtime_v2 import PaperRuntime, RuntimeClient, canonical
from paper_market import receipt_ms
from dashboard import validate_snapshot

STAGE=Path(__file__).resolve().parent
LAB=STAGE.parents[1]
EVIDENCE=LAB/'evidence'
SCRATCH=Path('/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch')
ROOT=Path(tempfile.mkdtemp(dir=SCRATCH,prefix='freshness-public-'))
args_parser=argparse.ArgumentParser()
args_parser.add_argument('--candle-wait-seconds',type=float,default=0)
args_parser.add_argument('--tag',default='')
args=args_parser.parse_args()
assert args.tag.replace('_','').isalnum() or not args.tag
assert 0<=args.candle_wait_seconds<=30
label='freshness_public_slow_probe' if args.candle_wait_seconds else 'freshness_public_probe'
if args.tag: label+='_'+args.tag
requests=[]; errors=[]
class MeasuredClient(RuntimeClient):
    def __init__(self):
        super().__init__(on_error=errors.append)
    def get(self,endpoint,params=None):
        started_ms=int(time.time()*1000); begin=time.monotonic()
        injected_wait=args.candle_wait_seconds if endpoint.endswith('klines') else 0
        if injected_wait:
            # Explicit engineering delay BEFORE the real request. Receipt and
            # exchange source timestamps remain the untouched actual response.
            time.sleep(injected_wait)
        api_begin=time.monotonic()
        try:
            result=super().get(endpoint,params)
            api_elapsed=(time.monotonic()-api_begin)*1000
            requests.append(dict(endpoint=endpoint,params=params or {},request_started_ms=started_ms,
                returned_ms=int(time.time()*1000),elapsed_ms=(time.monotonic()-begin)*1000,
                injected_local_wait_seconds=injected_wait,actual_api_elapsed_ms=api_elapsed,
                received_at=result['received_at'],received_ms=receipt_ms(result),
                source_timestamp_ms=result['source_timestamp_ms'],receipt_sha256=hashlib.sha256(canonical(result).encode()).hexdigest()))
            return result
        except Exception as exc:
            requests.append(dict(endpoint=endpoint,params=params or {},request_started_ms=started_ms,
                returned_ms=int(time.time()*1000),elapsed_ms=(time.monotonic()-begin)*1000,error=f'{type(exc).__name__}: {exc}'))
            raise
client=MeasuredClient()
r=PaperRuntime(ROOT,LAB/'paper_config_v2.json',client=client,fixture=False)
try:
    first=r.poll(); validate_snapshot(first)
    before=dict(cash=first['cash_usdt'],fills=first['fills_count'],signals=first['signals_count'],forward_start=r.state['forward_start_ms'],cursor=r.state['cursor'])
finally: r.close()
time.sleep(1)
r=PaperRuntime(ROOT,LAB/'paper_config_v2.json',client=client,fixture=False)
try:
    second=r.poll(); validate_snapshot(second)
    after=dict(cash=second['cash_usdt'],fills=second['fills_count'],signals=second['signals_count'],forward_start=r.state['forward_start_ms'],cursor=r.state['cursor'])
    audit=list(r.db.execute('SELECT id,payload,previous_hash,hash FROM audit ORDER BY id'))
    previous='0'*64
    for i,payload,prev,h in audit:
        assert prev==previous and h==hashlib.sha256((prev+payload).encode()).hexdigest()
        previous=h
    result=dict(mode='isolated_real_public_paper_probe_not_performance',root=str(ROOT),requests=requests,
        request_errors=errors,first=first,after_restart=second,account_before_restart=before,
        account_after_restart=after,audit_records=len(audit),audit_chain_verified=True,
        audit_head=previous,source_sha256=r.source_hashes,config_sha256=r.config_hash)
finally: r.close()
(EVIDENCE/f'{label}.json').write_text(json.dumps(result,indent=2))
print(json.dumps(dict(root=str(ROOT),first_error=first['latest_error'],restart_error=second['latest_error'],
    fills=second['fills_count'],cash=second['cash_usdt'],warmup=second['engine']['warmup_received'],
    requests=len(requests),audit_records=len(audit),receipts=requests),indent=2))
