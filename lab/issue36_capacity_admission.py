#!/usr/bin/env python3
"""Issue #36 fixed-budget 48h admission evidence.

Isolated synthetic engineering only. This does not activate an operator root, poll a
real venue, or claim market/performance validity. It exercises the repository's
actual SharedFeed, DiscoveryLab and append-only CausalEvidence persistence paths.
"""
from __future__ import annotations
import argparse,json,sqlite3,tempfile
from pathlib import Path

from discovery_feed import SharedFeed,MAX_RAW_EVENTS
from discovery_lab import DiscoveryLab

BUDGET=32*1024*1024
ENTRY_STOP=int(BUDGET*0.9)
WINDOW_MINUTES=48*60
START_MS=1_800_000_000_000
INSTRUMENT=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
                qty_step='0.001',tick_size='0.01',min_notional='0.01',
                max_qty='100',min_qty='0.001',category='crypto')
CONFIG=Path(__file__).with_name('discovery_config_v1.json')

def file_sizes(root):
    root=Path(root)
    named={}
    for p in sorted(root.rglob('*')):
        if p.is_file() and not p.is_symlink():
            named[p.relative_to(root).as_posix()]=p.stat().st_size
    return dict(total_bytes=sum(named.values()),stores=named)

def evidence_metrics(path):
    if not Path(path).exists():
        return dict(records=0,kinds={},payload_bytes={})
    with sqlite3.connect(path) as db:
        rows=db.execute("SELECT kind,count(*),coalesce(sum(length(payload)),0) FROM evidence GROUP BY kind ORDER BY kind").fetchall()
    return dict(records=sum(r[1] for r in rows),
                kinds={r[0]:r[1] for r in rows},
                payload_bytes={r[0]:r[2] for r in rows})

def source_sample(feed_path):
    with sqlite3.connect(feed_path) as db:
        rows=db.execute("SELECT payload FROM feed_events ORDER BY seq").fetchall()
    if not rows:return dict(first=None,last=None)
    def slim(raw):
        x=json.loads(raw)
        return {k:x.get(k) for k in ('event_id','type','source_ts','receipt_ts','ts','source_valid')}
    return dict(first=slim(rows[0][0]),last=slim(rows[-1][0]))

def new_lab(root):
    lab=DiscoveryLab(root,CONFIG,INSTRUMENT)
    lab.activate(at_ms=START_MS,operator_accepted=True)
    feed=SharedFeed(Path(root)/'shared-feed.sqlite3',forward_start_ms=START_MS)
    feed.mark_reconnected(next_trade_id=1,observed_ms=START_MS)
    lab.bind_feed(feed)
    return lab,feed

def valid_profile(root):
    lab,feed=new_lab(root)
    counts=dict(polls=0,poll_successes=0,poll_failures=0,gaps=0,late_invalid=0,
                raw_events=0,closed_bars=0,aggtrades=0)
    trade_id=1
    for minute in range(1,WINDOW_MINUTES+1):
        now=START_MS+minute*60_000
        feed.begin_poll();counts['polls']+=1
        feed.ingest('book',dict(source_ts=now,receipt_ts=now,bids=[['100','2']],asks=[['100.10','2']]),received_ms=now)
        feed.ingest('mark',dict(source_ts=now,receipt_ts=now,price='100.05'),received_ms=now)
        feed.ingest('funding_status',dict(source_ts=now,complete=True,valid_until_ts=now+28_800_000),received_ms=now)
        feed.ingest('aggTrade',dict(trade_id=trade_id,source_ts=now,receipt_ts=now,price='100.05',qty='0.1',aggressor='BUY'),received_ms=now)
        trade_id+=1
        feed.ingest('closed_bar',dict(open_time_ms=now-60_000,close_time_ms=now,
                    open='100',high='101',low='99',close='100',volume='10'),received_ms=now)
        counts['raw_events']+=5;counts['closed_bars']+=1;counts['aggtrades']+=1
        counts['poll_successes']+=1
        if lab._storage_used()>=ENTRY_STOP:
            break
    end_minute=counts['polls']
    before_close=file_sizes(root)
    feed_snapshot=feed.snapshot();report=lab.report(START_MS+end_minute*60_000)
    lab.close()
    reopened=DiscoveryLab(root,CONFIG,INSTRUMENT)
    reopened_feed=SharedFeed(Path(root)/'shared-feed.sqlite3',forward_start_ms=START_MS)
    reopened.bind_feed(reopened_feed)
    readback=reopened.report(START_MS+end_minute*60_000);reopened.close()
    sizes=file_sizes(root)
    return dict(profile='continuous_valid_source',window_minutes=WINDOW_MINUTES,
                completed_minutes=end_minute,explicit_rate='1 poll/min; 1 each book/mark/funding_status/aggTrade/closed_bar per min',
                counters=counts,feed=feed_snapshot,source_receipt_dispatch=source_sample(Path(root)/'shared-feed.sqlite3'),
                evidence=evidence_metrics(Path(root)/'causal_evidence.sqlite3'),
                size_before_close=before_close,size_after_reopen=sizes,
                report_storage_bytes=report['storage_used_bytes'],
                restart_readback_storage_bytes=readback['storage_used_bytes'],
                restart_readback_causal_head=readback['causal_evidence']['head_hash'],
                capacity_pass=(end_minute==WINDOW_MINUTES and sizes['total_bytes']<ENTRY_STOP),
                real_public_market_claim=False)

def invalid_profile(root):
    lab,feed=new_lab(root)
    counts=dict(polls=0,poll_successes=0,poll_failures=0,gaps=0,recoveries=0,
                late_invalid=0,raw_events=0,aggtrades=0)
    trade_id=1
    gate_minute=None
    for minute in range(1,WINDOW_MINUTES+1):
        now=START_MS+minute*60_000
        feed.begin_poll();counts['polls']+=1
        # Every six hours, exercise an explicit 30-minute transport outage/recovery
        # lifecycle. The synthetic invalid stream continues independently.
        phase=(minute-1)%360
        if phase==0:
            lab.request_source_gap(now,'ARTIFICIAL transport outage for Issue36 admission')
            counts['poll_failures']+=1;counts['gaps']+=1
        elif phase==30:
            lab.clear_source_gap(now);counts['recoveries']+=1
        else:
            counts['poll_successes']+=1
        # Heavy late/invalid load: 20 aggTrades + late book + late mark each minute.
        # They are retained raw, fail closed and each currently creates causal unknown
        # evidence; no synthetic fill is permitted.
        dispatch=now+20_000
        for n in range(20):
            feed.ingest('aggTrade',dict(trade_id=trade_id,source_ts=now+n,receipt_ts=now+n,
                        price='100.05',qty='0.1',aggressor='BUY'),received_ms=dispatch+n)
            trade_id+=1;counts['aggtrades']+=1;counts['late_invalid']+=1;counts['raw_events']+=1
        feed.ingest('book',dict(source_ts=now,receipt_ts=now,bids=[['100','2']],asks=[['100.10','2']]),received_ms=dispatch)
        feed.ingest('mark',dict(source_ts=now,receipt_ts=now,price='100.05'),received_ms=dispatch)
        counts['late_invalid']+=2;counts['raw_events']+=2
        if lab._storage_used()>=ENTRY_STOP:
            gate_minute=minute;break
    end_minute=counts['polls']
    feed_snapshot=feed.snapshot();report=lab.report(START_MS+end_minute*60_000)
    lab.close()
    reopened=DiscoveryLab(root,CONFIG,INSTRUMENT)
    reopened_feed=SharedFeed(Path(root)/'shared-feed.sqlite3',forward_start_ms=START_MS)
    reopened.bind_feed(reopened_feed)
    readback=reopened.report(START_MS+end_minute*60_000);reopened.close()
    sizes=file_sizes(root)
    return dict(profile='heavy_late_invalid_transport_outage_recovery',window_minutes=WINDOW_MINUTES,
                completed_minutes=end_minute,gate_minute=gate_minute,
                explicit_rate='1 poll/min; 20 late aggTrades + 1 late book + 1 late mark per min; 30m outage every 6h',
                counters=counts,feed=feed_snapshot,source_receipt_dispatch=source_sample(Path(root)/'shared-feed.sqlite3'),
                evidence=evidence_metrics(Path(root)/'causal_evidence.sqlite3'),
                size_after_reopen=sizes,report_storage_bytes=report['storage_used_bytes'],
                restart_readback_storage_bytes=readback['storage_used_bytes'],
                restart_readback_causal_head=readback['causal_evidence']['head_hash'],
                capacity_pass=(gate_minute is None and end_minute==WINDOW_MINUTES and sizes['total_bytes']<ENTRY_STOP),
                real_public_market_claim=False)

def run_profiles():
    with tempfile.TemporaryDirectory(prefix='issue36-valid-') as td:
        valid=valid_profile(Path(td))
    with tempfile.TemporaryDirectory(prefix='issue36-invalid-') as td:
        invalid=invalid_profile(Path(td))
    overall='PASS' if valid['capacity_pass'] and invalid['capacity_pass'] else 'NOT_FEASIBLE'
    blockers=[]
    if not valid['capacity_pass']:blockers.append('continuous_valid_profile_exceeds_fixed_90pct_gate')
    if not invalid['capacity_pass']:blockers.append('heavy_invalid_outage_profile_exceeds_fixed_90pct_gate_before_48h')
    return dict(label='ISSUE36_ISOLATED_ACCELERATED_48H_ENGINEERING_NOT_MARKET_PERFORMANCE',
                fixed_budget_bytes=BUDGET,entry_stop_bytes=ENTRY_STOP,raw_retention_limit=MAX_RAW_EVENTS,
                profiles=[valid,invalid],admission=overall,blockers=blockers,
                causal_policy='append-only evidence unchanged; unavailable rolled raw context remains unknown',
                preserved_contracts=['fixed_32MiB','90pct_entry_stop','raw_4096_rollover','immutable_causal',
                                     'source_age_arrival_chronology','no_synthetic_fills','sticky_stop_unchanged'],
                public_probe='BLOCKED_IN_GITHUB_IF_HTTP451_OR_NETWORK_UNAVAILABLE',
                operator_root_touched=False,new_window_started=False,deployed=False)

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--output')
    args=p.parse_args(argv)
    result=run_profiles()
    raw=json.dumps(result,sort_keys=True,separators=(',',':'))
    if args.output:Path(args.output).write_text(raw+'\n')
    print(raw)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
