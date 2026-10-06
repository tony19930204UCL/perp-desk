#!/usr/bin/env python3
"""Artificial isolated end-to-end evidence for Issue #23. No network or live account."""
import argparse, json, tempfile
from pathlib import Path
from discovery_feed import SharedFeed, binance_aggtrade_input
from discovery_lab import DiscoveryLab

def run():
    here=Path(__file__).resolve().parent
    start=60000
    instrument=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
                    qty_step='0.001',tick_size='0.01',min_notional='0.01',
                    max_qty='100',min_qty='0.001',category='crypto')
    with tempfile.TemporaryDirectory(prefix='issue23-artificial-') as td:
        root=Path(td)/'lab'
        lab=DiscoveryLab(root,here/'discovery_config_v1.json',instrument)
        feed=SharedFeed(root/'shared-feed.sqlite3',forward_start_ms=start,retention=256)
        lab.bind_feed(feed)
        lab.activate(at_ms=start,operator_accepted=True)
        feed.begin_poll()
        feed.mark_reconnected(next_trade_id=1,observed_ms=start)
        feed.ingest('funding_status',dict(source_ts=start+1,complete=True,
                    valid_until_ts=start+10_000_000),received_ms=start+1)
        for i in range(61):
            open_ms=start+i*60000;close_ms=open_ms+60000
            feed.ingest('closed_bar',dict(open_time_ms=open_ms,close_time_ms=close_ms,
                        open='100',high='100',low='100',close='100',volume='10'),
                        received_ms=close_ms+10)
        signal_open=start+61*60000;signal_close=signal_open+60000
        feed.ingest('book',dict(source_ts=signal_close-1000,bids=[['98','0.1']],asks=[['98.1','3']]),
                    received_ms=signal_close-990)
        feed.ingest('closed_bar',dict(open_time_ms=signal_open,close_time_ms=signal_close,
                    open='90',high='90',low='90',close='90',volume='20'),
                    received_ms=signal_close+10)
        feed.ingest('book',dict(source_ts=signal_close+2200,bids=[['98','0.1']],asks=[['98.1','3']]),
                    received_ms=signal_close+2200)
        raw_trade=dict(a=1,p='97.99',q='5',f=100,l=104,T=signal_close+2300,m=True)
        feed.ingest('aggTrade',binance_aggtrade_input(raw_trade),received_ms=signal_close+2301)
        report=lab.report(signal_close+2301)
        b=lab.brokers['B']
        result=dict(
            label='ARTIFICIAL ENGINEERING PIPELINE - NOT MARKET PERFORMANCE',
            deployed=False,live_orders=False,default_account_touched=False,
            shared_upstream_poll_cycles=feed.snapshot()['fetch_calls'],
            shared_retained_events=feed.snapshot()['retained_events'],
            arm_ids=sorted(lab.brokers),
            independent_initial_cash={arm:str(broker.initial_cash) for arm,broker in lab.brokers.items()},
            b_maker_entry_fills=sum(1 for fill in b.fills
                if fill['liquidity']=='MAKER' and not b.orders[fill['order_id']]['intent']['reduce_only']),
            b_queue_stress_recorded=bool(report['arms']['B']['queue_stress_2x']),
            a_submitted=report['arms']['A']['submitted'],
            b_submitted=report['arms']['B']['submitted'],
            c_submitted=report['arms']['C']['submitted'])
        lab.close()
        if (result['shared_upstream_poll_cycles']!=1 or result['arm_ids']!=['A','B','C']
                or set(result['independent_initial_cash'].values())!={'100'}
                or result['b_maker_entry_fills']<1 or not result['b_queue_stress_recorded']):
            raise RuntimeError('artificial discovery pipeline acceptance failed')
        return result

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--self-check',action='store_true',required=True)
    p.parse_args(argv)
    print(json.dumps(run(),sort_keys=True))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
