"""One bounded public Binance time observation for engineering timing evidence.

Never strategy performance. No credentials, account endpoints, local paths or retries beyond
the PublicMarketClient's existing bounded public transport contract.
"""
from pathlib import Path
import json
import sys
import time

LAB=Path(__file__).resolve().parents[1]/'lab'
sys.path.insert(0,str(LAB))
from paper_market import PublicMarketClient


def main():
    events=[]
    outer_wall0=time.time_ns()/1_000_000
    outer_mono0=time.monotonic_ns()/1_000_000
    outcome='error';error_type=None;server_time=None
    try:
        receipt=PublicMarketClient().get_timed('/fapi/v1/time',timing=events.append)
        outcome='success';server_time=receipt.get('source_timestamp_ms')
    except Exception as exc:
        error_type=type(exc).__name__
    outer_wall1=time.time_ns()/1_000_000
    outer_mono1=time.monotonic_ns()/1_000_000
    result=dict(
        schema_version=1,
        classification='PUBLIC_HTTP_ENGINEERING_OBSERVATION_NOT_STRATEGY_PERFORMANCE',
        endpoint='/fapi/v1/time',
        outcome=outcome,error_type=error_type,server_time_ms=server_time,
        outer_wall_elapsed_ms=outer_wall1-outer_wall0,
        outer_monotonic_elapsed_ms=outer_mono1-outer_mono0,
        outer_wall_minus_monotonic_ms=(outer_wall1-outer_wall0)-(outer_mono1-outer_mono0),
        events=events,
        limitations=[
            'single bounded forward observation',
            'network path and runner are not the PAPER host',
            'wall/monotonic discrepancy is observed only; OS clock root cause is not established',
            'not strategy performance and no trading/account data'])
    print(json.dumps(result,sort_keys=True,separators=(',',':')))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
