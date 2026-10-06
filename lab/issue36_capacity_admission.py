#!/usr/bin/env python3
"""Issue #36 fixed-budget 48h admission evidence.

Isolated synthetic engineering only. No operator root, private API, activation,
deployment or market-performance claim. The 48h byte load is materialized in the
same SQLite schemas/hash-chain format in bulk so CI does not pretend to wait 48h.
A small actual SharedFeed/DiscoveryLab smoke separately verifies dispatch,
fail-closed gap/recovery and restart readback.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,tempfile
from pathlib import Path

from discovery_evidence import canonical
from discovery_feed import SharedFeed,MAX_RAW_EVENTS
from discovery_lab import DiscoveryLab

BUDGET=32*1024*1024
ENTRY_STOP=int(BUDGET*0.9)
WINDOW_MINUTES=48*60
START_MS=1_800_000_000_000
CONFIG=Path(__file__).with_name('discovery_config_v1.json')
INSTRUMENT=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
                qty_step='0.001',tick_size='0.01',min_notional='0.01',
                max_qty='100',min_qty='0.001',category='crypto')

def file_sizes(root):
    root=Path(root);stores={}
    for p in sorted(root.rglob('*')):
        if p.is_file() and not p.is_symlink():
            stores[p.relative_to(root).as_posix()]=p.stat().st_size
    return dict(total_bytes=sum(stores.values()),stores=stores)

def init_root(root):
    lab=DiscoveryLab(root,CONFIG,INSTRUMENT)
    lab.activate(at_ms=START_MS,operator_accepted=True)
    lab.close()
    # Create the actual raw-feed schema.
    SharedFeed(Path(root)/'shared-feed.sqlite3',forward_start_ms=START_MS)

def bulk_raw(root,*,total_events,invalid_events,profile):
    path=Path(root)/'shared-feed.sqlite3'
    retained=min(total_events,MAX_RAW_EVENTS)
    first=total_events-retained
    state=dict(forward_start_ms=START_MS,last_agg_trade_id=total_events-1,
               aggtrade_valid=True,gaps=[],duplicates=0,out_of_order=0,
               source_invalid=invalid_events,events_persisted=total_events,
               events_evicted=total_events-retained,reconnects=1)
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM feed_events')
        db.execute('INSERT OR REPLACE INTO feed_state VALUES(1,?)',(canonical(state),))
        rows=[]
        for i in range(first,total_events):
            source=START_MS+i*1000
            bad=i < invalid_events
            kind=('aggTrade','book','mark','closed_bar','funding_status')[i%5] if profile=='valid' else ('aggTrade','book','mark')[i%3]
            payload=dict(type=kind,event_id=profile+':'+str(i),symbol='ETHUSDT',
                         ts=source+(20_000 if bad else 0),source_ts=source,
                         receipt_ts=source,source_valid=not bad)
            if kind=='aggTrade':payload.update(trade_id=i,price='100.05',qty='0.1',aggressor='BUY')
            raw=canonical(payload)
            rows.append((payload['event_id'],raw,hashlib.sha256(raw.encode()).hexdigest()))
        db.executemany('INSERT INTO feed_events(event_id,payload,sha256) VALUES(?,?,?)',rows)
        db.commit();db.execute('VACUUM')
    snap=SharedFeed(path,forward_start_ms=START_MS).snapshot()
    return snap

def bulk_evidence(root,counts):
    path=Path(root)/'causal_evidence.sqlite3'
    seq=[]
    for kind,count in counts.items():
        for i in range(count):
            ts=START_MS+i*1000
            key=kind+':'+str(i)
            if kind=='source_unknown':
                payload=dict(event_id='unknown:'+str(i),type='late_or_invalid_source',
                             source_ts=ts-20_000,trade_id=i,price='100.05',qty='0.1',
                             aggressor='BUY',reconstructible_market_payload=False)
            elif kind=='source_recovered':
                payload=dict(event_id='recovered:'+str(i),prior_reason='ARTIFICIAL outage',scope='all_arms')
            elif kind=='signal_decision':
                payload=dict(arm=('A','B','C')[i%3],signal_id='sig:'+str(i),direction='long',
                             cost_qualified=(i%2==0),status='rejected',reason='synthetic_capacity_fixture')
            elif kind=='order_state':
                payload=dict(arm=('A','B','C')[i%3],order_id='order:'+str(i),intent_id='intent:'+str(i),
                             status=('CANCELED' if i%3==0 else 'REJECTED' if i%3==1 else 'PENDING'),
                             reason=('source_gap' if i%3==0 else 'risk_fixture' if i%3==1 else None),
                             remaining='0.1',queue_remaining='0.2',intent=dict(reduce_only=False))
            elif kind=='fill':
                payload=dict(arm=('A','B','C')[i%3],fill_id='fill:'+str(i),order_id='order:'+str(i),
                             side='BUY',qty='0.1',price='100.05',liquidity='MAKER')
            elif kind=='ledger':
                payload=dict(arm=('A','B','C')[i%3],ledger_id='ledger:'+str(i),
                             type=('fee' if i%2==0 else 'funding'),amount='-0.00001')
            elif kind=='maker_queue_arrival':
                payload=dict(order_id='B:'+str(i),event_id='book:'+str(i),source_ts=ts,
                             side='BUY',limit_price='100',queue_1x='1',queue_2x='2',qty='0.1')
            elif kind=='maker_trade_observation':
                payload=dict(event_id='aggTrade:'+str(i),trade_id=i,source_ts=ts,
                             price='99.99',qty='0.1',aggressor='SELL')
            else:
                raise ValueError('unsupported evidence kind '+kind)
            seq.append((key,kind,ts,payload))
    previous='0'*64;rows=[]
    for key,kind,ts,payload in seq:
        raw=canonical(payload)
        digest=hashlib.sha256((previous+'|'+key+'|'+kind+'|'+str(ts)+'|'+raw).encode()).hexdigest()
        rows.append((key,kind,ts,raw,previous,digest));previous=digest
    with sqlite3.connect(path) as db:
        db.executemany('INSERT INTO evidence(evidence_key,kind,ts,payload,previous_hash,hash) VALUES(?,?,?,?,?,?)',rows)
        db.commit()
    return evidence_metrics(path)

def evidence_metrics(path):
    with sqlite3.connect(path) as db:
        rows=db.execute("SELECT kind,count(*),coalesce(sum(length(payload)),0) FROM evidence GROUP BY kind ORDER BY kind").fetchall()
        head=db.execute('SELECT hash FROM evidence ORDER BY seq DESC LIMIT 1').fetchone()
    return dict(records=sum(r[1] for r in rows),kinds={r[0]:r[1] for r in rows},
                payload_bytes={r[0]:r[2] for r in rows},head_hash=head[0] if head else None)

def lifecycle_smoke():
    with tempfile.TemporaryDirectory(prefix='issue36-lifecycle-') as td:
        root=Path(td);lab=DiscoveryLab(root,CONFIG,INSTRUMENT)
        lab.activate(at_ms=START_MS,operator_accepted=True)
        feed=SharedFeed(root/'shared-feed.sqlite3',forward_start_ms=START_MS)
        feed.mark_reconnected(next_trade_id=1,observed_ms=START_MS);lab.bind_feed(feed)
        valid=feed.ingest('book',dict(source_ts=START_MS+1000,receipt_ts=START_MS+1000,
                    bids=[['100','1']],asks=[['100.1','1']]),received_ms=START_MS+1000)['event']
        late=feed.ingest('aggTrade',dict(trade_id=1,source_ts=START_MS+2000,receipt_ts=START_MS+2000,
                    price='100',qty='0.1',aggressor='BUY'),received_ms=START_MS+22000)['event']
        lab.request_source_gap(START_MS+23000,'ARTIFICIAL transport outage for admission smoke')
        lab.clear_source_gap(START_MS+24000)
        before=lab.report(START_MS+24000);lab.close()
        reopened=DiscoveryLab(root,CONFIG,INSTRUMENT)
        after=reopened.report(START_MS+24000);reopened.close()
        return dict(valid_source_receipt_dispatch={k:valid.get(k) for k in ('source_ts','receipt_ts','ts','source_valid')},
                    late_source_receipt_dispatch={k:late.get(k) for k in ('source_ts','receipt_ts','ts','source_valid')},
                    source_gaps=after['source_gaps'],unknown_inputs=after['unknown_inputs'],
                    causal_head_before=before['causal_evidence']['head_hash'],
                    causal_head_after_restart=after['causal_evidence']['head_hash'],
                    restart_readback=True)

def materialize_profile(name,minutes=WINDOW_MINUTES):
    with tempfile.TemporaryDirectory(prefix='issue36-'+name+'-') as td:
        root=Path(td);init_root(root)
        if name=='valid':
            raw_rate=5;total=minutes*raw_rate
            feed=bulk_raw(root,total_events=total,invalid_events=0,profile='valid')
            # Explicit engineering load rates; enough to retain reconstructable
            # decision/order/fill/fee/funding/queue causality without claiming they
            # are observed market frequencies.
            counts=dict(signal_decision=minutes*3//10,order_state=minutes//10,
                        fill=minutes*3//60,ledger=minutes*4//60,
                        maker_queue_arrival=minutes//30,maker_trade_observation=minutes*4//30,
                        source_unknown=0,source_recovered=0)
            rate='5 raw/min; 3 arm signal decisions/10min; order/10min; 3 fills/hour; 4 ledger/hour; maker arrival/30min; 4 relevant maker trades/30min'
        elif name=='invalid':
            raw_rate=42;total=minutes*raw_rate
            feed=bulk_raw(root,total_events=total,invalid_events=total,profile='invalid')
            # Heavy late/invalid case: every raw input is unknown and therefore
            # currently creates one immutable source_unknown causal row.
            counts=dict(signal_decision=0,order_state=minutes//60,fill=0,ledger=0,
                        maker_queue_arrival=0,maker_trade_observation=0,
                        source_unknown=total,source_recovered=max(1,minutes//360))
            rate='42 late/invalid raw/min (40 aggTrade + book + mark); 30m transport outage every 6h with recovery marker'
        else:raise ValueError('unknown profile')
        evidence=bulk_evidence(root,counts)
        sizes=file_sizes(root)
        readback=SharedFeed(root/'shared-feed.sqlite3',forward_start_ms=START_MS).snapshot()
        return dict(profile=name,window_minutes=minutes,explicit_rate=rate,raw_total_events=total,
                    raw=feed,evidence=evidence,size_after_materialization=sizes,
                    restart_readback=dict(retained_events=readback['retained_events'],
                                          events_persisted=readback['events_persisted'],
                                          events_evicted=readback['events_evicted']),
                    capacity_pass=sizes['total_bytes']<ENTRY_STOP,
                    accelerated_bulk_same_schema=True,real_public_market_claim=False)

def run_profiles(minutes=WINDOW_MINUTES):
    valid=materialize_profile('valid',minutes);invalid=materialize_profile('invalid',minutes)
    admission='PASS' if valid['capacity_pass'] and invalid['capacity_pass'] else 'NOT_FEASIBLE'
    blockers=[]
    if not valid['capacity_pass']:blockers.append('continuous_valid_profile_exceeds_fixed_90pct_gate')
    if not invalid['capacity_pass']:blockers.append('heavy_invalid_outage_profile_exceeds_fixed_90pct_gate')
    return dict(label='ISSUE36_ACCELERATED_48H_ENGINEERING_NOT_REAL_MARKET',
                fixed_budget_bytes=BUDGET,entry_stop_bytes=ENTRY_STOP,raw_retention_limit=MAX_RAW_EVENTS,
                requested_window_minutes=minutes,profiles=[valid,invalid],lifecycle_smoke=lifecycle_smoke(),
                admission=admission,blockers=blockers,
                causal_policy='append-only evidence unchanged; rolled raw context unavailable after retention remains unknown',
                preserved_contracts=['fixed_32MiB','90pct_entry_stop','raw_4096_rollover','immutable_causal',
                                     'source_age_arrival_chronology','no_synthetic_fills','sticky_stop_unchanged',
                                     'protective_exit_unchanged','PR33_readonly_UI_unchanged'],
                public_probe='BLOCKED_IN_GITHUB_IF_HTTP451_OR_NETWORK_UNAVAILABLE',
                operator_root_touched=False,new_window_started=False,deployed=False)

def validate_full_result(result):
    if result['requested_window_minutes']!=WINDOW_MINUTES:raise AssertionError('full admission must cover 48h')
    by={x['profile']:x for x in result['profiles']}
    valid=by['valid'];invalid=by['invalid']
    for p in (valid,invalid):
        if p['raw']['retained_events']!=MAX_RAW_EVENTS or p['raw']['events_evicted']<=0:
            raise AssertionError('48h profile did not exercise raw rollover')
        stores=p['size_after_materialization']['stores']
        for required in ('shared-feed.sqlite3','causal_evidence.sqlite3','arm-a/broker.sqlite3','arm-b/broker.sqlite3','arm-c/broker.sqlite3'):
            if required not in stores:raise AssertionError('missing store '+required)
    if not valid['capacity_pass']:
        raise AssertionError('continuous valid engineering profile must fit fixed gate for this admission candidate')
    if invalid['capacity_pass']:
        raise AssertionError('heavy invalid/outage profile unexpectedly fit fixed gate; review workload')
    if invalid['evidence']['kinds'].get('source_unknown')!=invalid['raw_total_events']:
        raise AssertionError('invalid causal count mismatch')
    if result['admission']!='NOT_FEASIBLE':raise AssertionError('combined required profiles must be NOT_FEASIBLE')
    smoke=result['lifecycle_smoke']
    if smoke['valid_source_receipt_dispatch']['source_valid'] is not True or smoke['late_source_receipt_dispatch']['source_valid'] is not False:
        raise AssertionError('source timing smoke mismatch')
    if smoke['causal_head_before']!=smoke['causal_head_after_restart']:
        raise AssertionError('causal restart readback mismatch')
    return True

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--output');args=p.parse_args(argv)
    result=run_profiles();validate_full_result(result)
    raw=json.dumps(result,sort_keys=True,separators=(',',':'))
    if args.output:Path(args.output).write_text(raw+'\n')
    print(raw);return 0

if __name__=='__main__':raise SystemExit(main())
