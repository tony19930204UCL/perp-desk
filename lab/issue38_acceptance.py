#!/usr/bin/env python3
"""Issue #38 bounded-unknown capacity/reconstruction acceptance.

The 48h capacity profiles are accelerated same-schema materializations, not
2,880 real engine polls. A separate actual SharedFeed/DiscoveryLab lifecycle
smoke verifies decision-sensitive unknown preservation, protective behavior and
same-root restart reconstruction.
"""
from __future__ import annotations
import argparse,hashlib,json,sqlite3,tempfile,time
from collections import defaultdict
from pathlib import Path

from discovery_evidence import CausalEvidence,UNKNOWN_RANGE_BUCKET_MS,canonical
from discovery_feed import SharedFeed,MAX_RAW_EVENTS
from discovery_lab import DiscoveryLab

BUDGET=32*1024*1024
ENTRY_STOP=int(BUDGET*0.9)
WINDOW_MINUTES=48*60
HEAVY_AGGTRADES_PER_MINUTE=40
HEAVY_INVALID_PER_MINUTE=42
START_MS=1_800_000_000_000
CONFIG=Path(__file__).with_name('discovery_config_v1.json')
INSTRUMENT=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
                qty_step='0.001',tick_size='0.01',min_notional='0.01',
                max_qty='100',min_qty='0.001',category='crypto')

def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()

def file_inventory(root):
    stores={}
    for p in sorted(Path(root).rglob('*')):
        if p.is_file() and not p.is_symlink():
            stores[p.relative_to(root).as_posix()]=p.stat().st_size
    sidecars={k:v for k,v in stores.items() if k.endswith('-wal') or k.endswith('-shm') or k.endswith('-journal')}
    return dict(total_bytes=sum(stores.values()),stores=stores,sqlite_sidecars=sidecars,
                sidecar_bytes=sum(sidecars.values()))

def inflight_peak_inventory(root):
    """Observe whole-root bytes with every SQLite store in an active write transaction.

    The probe changes only PRAGMA user_version inside uncommitted transactions and
    rolls every transaction back before returning. It therefore forces the
    database's configured journal/WAL sidecars to exist without changing durable
    profile data or schema.
    """
    root=Path(root)
    sqlite_paths=sorted(p for p in root.rglob('*.sqlite3') if p.is_file() and not p.is_symlink())
    if not sqlite_paths:
        raise AssertionError('inflight inventory requires SQLite stores')
    connections=[];samples=[];journal_modes={}
    start_wall_ns=time.time_ns();start_monotonic_ns=time.monotonic_ns()
    try:
        for path in sqlite_paths:
            db=sqlite3.connect(path)
            connections.append((path,db))
            rel=path.relative_to(root).as_posix()
            journal_modes[rel]=str(db.execute('PRAGMA journal_mode').fetchone()[0])
            current=int(db.execute('PRAGMA user_version').fetchone()[0])
            db.execute('BEGIN IMMEDIATE')
            db.execute('PRAGMA user_version = '+str(current+1))
            sample=file_inventory(root)
            sample.update(open_connections=len(connections),
                          active_write_transactions=len(connections),
                          observed_wall_ns=time.time_ns(),
                          observed_monotonic_ns=time.monotonic_ns())
            samples.append(sample)
        peak=max(samples,key=lambda x:x['total_bytes'])
        result=dict(peak)
        result.update(
            sqlite_files=len(sqlite_paths),
            sqlite_paths=[p.relative_to(root).as_posix() for p in sqlite_paths],
            journal_modes=journal_modes,
            sample_count=len(samples),
            open_connections_at_peak=peak['open_connections'],
            active_write_transactions_at_peak=peak['active_write_transactions'],
            all_sqlite_connections_open=(peak['open_connections']==len(sqlite_paths)),
            observation_started_wall_ns=start_wall_ns,
            observation_started_monotonic_ns=start_monotonic_ns,
            observation_elapsed_monotonic_ns=time.monotonic_ns()-start_monotonic_ns,
            probe_write='uncommitted user_version page write; rolled back on every SQLite store')
        return result
    finally:
        for _,db in reversed(connections):
            try:db.rollback()
            finally:db.close()

def capacity_gate_at_measured_peak(root,peak_bytes):
    lab=DiscoveryLab(root,CONFIG,INSTRUMENT,storage_probe=lambda:int(peak_bytes))
    try:
        allowed=lab._storage_ok()
        return dict(actual_discovery_gate=True,measured_bytes=int(peak_bytes),
                    entry_stop_bytes=ENTRY_STOP,allows_new_entry=bool(allowed),
                    storage_entry_inhibited=bool(lab.state['storage_entry_inhibited']),
                    over_entry_stop=(int(peak_bytes)>=ENTRY_STOP))
    finally:lab.close()

def init_root(root):
    lab=DiscoveryLab(root,CONFIG,INSTRUMENT)
    lab.activate(at_ms=START_MS,operator_accepted=True)
    lab.close()
    SharedFeed(Path(root)/'shared-feed.sqlite3',forward_start_ms=START_MS)

def bulk_raw(root,total_events,invalid_events,profile):
    path=Path(root)/'shared-feed.sqlite3'
    retained=min(total_events,MAX_RAW_EVENTS);first=total_events-retained
    if profile=='valid':
        last_trade_id=total_events//5-1
    else:
        last_trade_id=(total_events//HEAVY_INVALID_PER_MINUTE)*HEAVY_AGGTRADES_PER_MINUTE-1
    state=dict(forward_start_ms=START_MS,last_agg_trade_id=last_trade_id,
               aggtrade_valid=True,gaps=[],duplicates=0,out_of_order=0,
               source_invalid=invalid_events,events_persisted=total_events,
               events_evicted=total_events-retained,reconnects=1)
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM feed_events')
        db.execute('INSERT OR REPLACE INTO feed_state VALUES(1,?)',(canonical(state),))
        rows=[]
        for i in range(first,total_events):
            bad=i<invalid_events
            if profile=='valid':
                minute=i//5;within=i%5
                kind=('aggTrade','book','mark','closed_bar','funding_status')[within]
                trade_id=minute if kind=='aggTrade' else None
                source=START_MS+minute*60_000+within*100
            else:
                minute=i//HEAVY_INVALID_PER_MINUTE;within=i%HEAVY_INVALID_PER_MINUTE
                kind='aggTrade' if within<HEAVY_AGGTRADES_PER_MINUTE else 'book' if within==40 else 'mark'
                trade_id=minute*HEAVY_AGGTRADES_PER_MINUTE+within if kind=='aggTrade' else None
                source=START_MS+minute*60_000+within*100
            payload=dict(type=kind,event_id=profile+':'+str(i),symbol='ETHUSDT',
                         ts=source+(20_000 if bad else 0),source_ts=source,
                         receipt_ts=source,source_valid=not bad)
            if kind=='aggTrade':
                payload.update(trade_id=trade_id,price='100.05',qty='0.1',aggressor='BUY')
            raw=canonical(payload);rows.append((payload['event_id'],raw,sha(raw)))
        db.executemany('INSERT INTO feed_events(event_id,payload,sha256) VALUES(?,?,?)',rows)
        db.commit();db.execute('VACUUM')
    return SharedFeed(path,forward_start_ms=START_MS).snapshot()

def bulk_evidence(root,counts):
    path=Path(root)/'causal_evidence.sqlite3'
    seq=[]
    for kind,count in counts.items():
        for i in range(count):
            ts=START_MS+i*1000;key=kind+':'+str(i)
            if kind=='signal_decision':
                payload=dict(arm=('A','B','C')[i%3],signal_id='sig:'+str(i),direction='long',
                             cost_qualified=(i%2==0),status='rejected',reason='capacity_fixture')
            elif kind=='order_state':
                payload=dict(arm=('A','B','C')[i%3],order_id='order:'+str(i),intent_id='intent:'+str(i),
                             status=('CANCELED' if i%2==0 else 'REJECTED'),reason='capacity_fixture',
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
            else:raise ValueError('unsupported evidence kind '+kind)
            seq.append((key,kind,ts,payload))
    previous='0'*64;rows=[]
    for key,kind,ts,payload in seq:
        raw=canonical(payload)
        digest=sha(previous+'|'+key+'|'+kind+'|'+str(ts)+'|'+raw)
        rows.append((key,kind,ts,raw,previous,digest));previous=digest
    with sqlite3.connect(path) as db:
        db.executemany('INSERT INTO evidence(evidence_key,kind,ts,payload,previous_hash,hash) VALUES(?,?,?,?,?,?)',rows)
        db.commit()
    return CausalEvidence(path).summary()

def _observe(scope,reason_code,ts,event_id,event_type,source,receipt,trade_id,reason):
    return dict(scope=scope,reason_code=reason_code,dispatch_ts=ts,source_ts=source,
                receipt_ts=receipt,event_id=event_id,trade_id=trade_id,
                event_type=event_type,reason=reason)

def bulk_heavy_ranges(root,minutes=WINDOW_MINUTES):
    groups={}
    outage_cycles=max(1,minutes//360)
    repeated_outage_failures=0

    def aggregate(scope,reason_code,ts,event_id,event_type,source,receipt,trade,reason):
        bucket=(ts//UNKNOWN_RANGE_BUCKET_MS)*UNKNOWN_RANGE_BUCKET_MS
        key=scope+'|'+reason_code+'|'+str(bucket)
        observed=_observe(scope,reason_code,ts,event_id,event_type,source,receipt,trade,reason)
        event_digest=sha(canonical(observed))
        if key not in groups:
            values=CausalEvidence._range_values(scope,reason_code,bucket,ts,ts,source,source,
                receipt,receipt,event_id,event_id,trade,trade,1,event_digest,reason,reason)
        else:
            p=groups[key]
            rolling=sha(p['rolling_hash']+'|'+event_digest)
            values=CausalEvidence._range_values(scope,reason_code,bucket,p['first_ts'],ts,
                p['first_source_ts'],source,p['first_receipt_ts'],receipt,
                p['first_event_id'],event_id,p['first_trade_id'],trade,
                p['count']+1,rolling,p['first_reason'],reason)
        groups[key]=values

    for minute in range(minutes):
        base_index=minute*HEAVY_INVALID_PER_MINUTE
        for n in range(HEAVY_AGGTRADES_PER_MINUTE):
            source=START_MS+minute*60_000+n*100
            aggregate('shared_event:aggTrade','source_valid_false',source+20_000,
                'invalid:'+str(base_index+n),'aggTrade',source,source,
                minute*HEAVY_AGGTRADES_PER_MINUTE+n,'source_valid_false')
        for within,event_type in ((40,'book'),(41,'mark')):
            source=START_MS+minute*60_000+within*100
            aggregate('shared_event:'+event_type,'source_valid_false',source+20_000,
                'invalid:'+str(base_index+within),event_type,source,source,None,'source_valid_false')

    # Explicit outage load: first gap is individual causal evidence, then one
    # repeated transport-unknown observation per minute for the remaining 29
    # minutes. Recovery is individual evidence. The repeated observations are
    # bounded ranges, not fake public polls or market payloads.
    for cycle in range(outage_cycles):
        gap_ts=START_MS+cycle*360*60_000+1
        for minute in range(1,30):
            ts=gap_ts+minute*60_000
            aggregate('runner_shared_source','transport_or_source_gap',ts,
                'runner-source-gap-repeat:'+str(cycle)+':'+str(minute),
                'shared_source_gap',None,None,None,'ARTIFICIAL outage')
            repeated_outage_failures+=1

    path=Path(root)/'causal_evidence.sqlite3'
    with sqlite3.connect(path) as db:
        rows=[]
        for key,v in sorted(groups.items()):
            rows.append((key,v['scope'],v['reason_code'],v['bucket_start_ms'],
                         v['first_ts'],v['last_ts'],v['first_source_ts'],v['last_source_ts'],
                         v['first_receipt_ts'],v['last_receipt_ts'],v['first_event_id'],v['last_event_id'],
                         v['first_trade_id'],v['last_trade_id'],v['count'],v['rolling_hash'],
                         v['first_reason'],v['last_reason'],0,CausalEvidence._row_hash(v)))
        db.executemany("""INSERT INTO unknown_ranges(range_key,scope,reason_code,bucket_start_ms,
            first_ts,last_ts,first_source_ts,last_source_ts,first_receipt_ts,last_receipt_ts,
            first_event_id,last_event_id,first_trade_id,last_trade_id,count,rolling_hash,
            first_reason,last_reason,reconstructible_events,row_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",rows)
        db.commit()
    evidence=CausalEvidence(path)
    for cycle in range(outage_cycles):
        gap_ts=START_MS+cycle*360*60_000+1
        rec_ts=gap_ts+30*60_000
        evidence.append('runner-source-gap:'+str(gap_ts),'source_unknown',gap_ts,
            dict(event_id='runner-source-gap:'+str(gap_ts),type='shared_source_gap',
                 source_ts=None,receipt_ts=None,dispatch_ts=gap_ts,reason='ARTIFICIAL outage',
                 reconstructible_market_payload=False))
        evidence.append('runner-source-recovered:'+str(rec_ts),'source_recovered',rec_ts,
            dict(event_id='runner-source-recovered:'+str(rec_ts),prior_reason='ARTIFICIAL outage',scope='all_arms'))
    summary=evidence.summary()
    summary['repeated_outage_unknowns_aggregated']=repeated_outage_failures
    summary['outage_cycles']=outage_cycles
    return summary

def materialize_profile(name):
    with tempfile.TemporaryDirectory(prefix='issue38-'+name+'-') as td:
        root=Path(td);init_root(root)
        if name=='valid':
            total=WINDOW_MINUTES*5
            raw=bulk_raw(root,total,0,'valid')
            evidence=bulk_evidence(root,dict(signal_decision=WINDOW_MINUTES*3//10,
                order_state=WINDOW_MINUTES//10,fill=WINDOW_MINUTES*3//60,
                ledger=WINDOW_MINUTES*4//60,maker_queue_arrival=WINDOW_MINUTES//30,
                maker_trade_observation=WINDOW_MINUTES*4//30))
            declared='5 valid raw/min; same PR37 valid engineering causal rates'
        elif name=='heavy':
            total=WINDOW_MINUTES*HEAVY_INVALID_PER_MINUTE
            raw=bulk_raw(root,total,total,'invalid')
            evidence=bulk_heavy_ranges(root)
            declared='40 late/invalid aggTrade + book + mark = 42 invalid/min; 30m outage every 6h'
        else:raise ValueError('unknown profile')
        inflight=inflight_peak_inventory(root)
        gate=capacity_gate_at_measured_peak(root,inflight['total_bytes'])
        before=file_inventory(root)
        # Same-root readback is read-only for Issue38 new-schema roots.
        er=CausalEvidence(root/'causal_evidence.sqlite3')
        feed=SharedFeed(root/'shared-feed.sqlite3',forward_start_ms=START_MS)
        after=file_inventory(root)
        count_proof=dict(expected_raw_total_events=total,
                         observed_raw_events_persisted=raw['events_persisted'],
                         observed_source_invalid=raw['source_invalid'],
                         unknown_events_aggregated=evidence.get('unknown_events_aggregated',0),
                         unknown_range_rows=evidence.get('unknown_range_rows',0),
                         outage_cycles=evidence.get('outage_cycles',0),
                         repeated_outage_unknowns_aggregated=evidence.get('repeated_outage_unknowns_aggregated',0))
        return dict(profile=name,declared_rate=declared,raw_total_events=total,raw=raw,
                    causal=evidence,workload_count_proof=count_proof,
                    inventory_inflight_peak=inflight,capacity_gate_at_inflight_peak=gate,
                    capacity_basis='inflight_whole_root_peak',
                    inventory_before_restart=before,inventory_after_restart=after,
                    restart_readback=dict(raw_retained=feed.snapshot()['retained_events'],
                        evidence_head=er.summary()['head_hash'],
                        unknown_range_rows=er.summary()['unknown_range_rows'],
                        unknown_events_aggregated=er.summary()['unknown_events_aggregated']),
                    capacity_pass=(inflight['total_bytes']<ENTRY_STOP and gate['allows_new_entry']),
                    accelerated_bulk_same_schema=True,engine_poll_cycles=0,
                    real_public_market_claim=False)

def reconstruction_smoke():
    with tempfile.TemporaryDirectory(prefix='issue38-engine-') as td:
        root=Path(td);lab=DiscoveryLab(root,CONFIG,INSTRUMENT)
        lab.activate(at_ms=START_MS,operator_accepted=True)
        feed=SharedFeed(root/'shared-feed.sqlite3',forward_start_ms=START_MS)
        feed.mark_reconnected(next_trade_id=1,observed_ms=START_MS);lab.bind_feed(feed)
        for arm,b in lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='fs:'+arm,symbol='ETHUSDT',
                ts=START_MS+1,source_ts=START_MS+1,complete=True,valid_until_ts=START_MS+10_000_000))
        feed.ingest('book',dict(source_ts=START_MS+5000,receipt_ts=START_MS+5000,
            bids=[['100','1']],asks=[['100.10','2']]),received_ms=START_MS+5000)
        signal=dict(signal_id='issue38',direction='long',target='105',
                    features=dict(signal_close_ms=START_MS+5000),symbol='ETHUSDT')
        lab._route_signal(signal,START_MS+5001)
        feed.ingest('book',dict(source_ts=START_MS+7100,receipt_ts=START_MS+7100,
            bids=[['100','1']],asks=[['100.10','2']]),received_ms=START_MS+7100)
        feed.ingest('aggTrade',dict(trade_id=1,source_ts=START_MS+7200,receipt_ts=START_MS+7200,
            price='99.99',qty='1.4',aggressor='SELL'),received_ms=START_MS+7201)
        feed.ingest('funding',dict(source_ts=START_MS+7250,settlement_ts=START_MS+7250,
            rate='0.001',mark='100',rate_type='Regular',finalized=True),received_ms=START_MS+7250)
        b=lab.brokers['B']
        primary_fill=sum(1 for f in b.fills if not b.orders[f['order_id']]['intent']['reduce_only'])
        fills_before_unknown=len(b.fills)
        feed.ingest('aggTrade',dict(trade_id=2,source_ts=START_MS+7300,receipt_ts=START_MS+7300,
            price='99',qty='100',aggressor='SELL'),received_ms=START_MS+27300)
        fills_after_decision_unknown=len(b.fills)
        full_unknown=[x['payload']['event_id'] for x in lab.evidence.records('source_unknown')]
        feed.ingest('aggTrade',dict(trade_id=3,source_ts=START_MS+7400,receipt_ts=START_MS+7400,
            price='98',qty='100',aggressor='SELL'),received_ms=START_MS+27400)
        fills_after_bounded_unknown=len(b.fills)
        # Later causal book can execute the already-created reduce-only protection.
        feed.ingest('book',dict(source_ts=START_MS+30000,receipt_ts=START_MS+30000,
            bids=[['99','10']],asks=[['100','10']]),received_ms=START_MS+30000)
        lab.request_source_gap(START_MS+31000,'ARTIFICIAL shared outage')
        lab.request_source_gap(START_MS+32000,'ARTIFICIAL shared outage')
        lab.clear_source_gap(START_MS+33000)
        lab.request_operator_stop(START_MS+34000)
        records=lab.evidence.records();ranges=lab.evidence.unknown_ranges()
        by_kind=defaultdict(list)
        for row in records:by_kind[row['kind']].append(row['payload'])
        fill_ids={x['fill_id'] for x in b.fills}
        evidence_fill_ids={x['fill_id'] for x in by_kind['fill']}
        ledger_ids={x['ledger_id'] for x in b.ledger}
        evidence_ledger_ids={x['ledger_id'] for x in by_kind['ledger']}
        final_orders={oid:o['status'] for oid,o in b.orders.items()}
        order_pairs={(x['order_id'],x['status']) for x in by_kind['order_state']}
        all_order_states_reconstructible=all((oid,status) in order_pairs for oid,status in final_orders.items())
        protective_cancel_reconstructed=any(x.get('reason')=='protective_aggtrade_gap'
                                            for x in by_kind['order_state'])
        broker_ledger_types=sorted({x['type'] for x in b.ledger})
        evidence_ledger_types=sorted({x['type'] for x in by_kind['ledger'] if x.get('arm')=='B'})
        protective_orders=[o for o in b.orders.values() if o['intent']['reduce_only'] and o['intent']['kind']=='TAKER']
        before=lab.evidence.summary();lab.close()
        reopened=DiscoveryLab(root,CONFIG,INSTRUMENT)
        after=reopened.evidence.summary()
        sticky=bool(reopened.state.get('operator_stop_requested'))
        reopened.close()
        return dict(actual_engine_lifecycle=True,synthetic_not_market_performance=True,
            primary_maker_entry_fills=primary_fill,full_unknown_event_ids=full_unknown,
            unknown_range_rows=len(ranges),unknown_events_aggregated=sum(x['count'] for x in ranges),
            required_kinds=sorted(k for k in ('signal_decision','maker_queue_arrival','maker_trade_observation',
                'fill','ledger','order_state','source_unknown','source_recovered') if by_kind[k]),
            broker_fill_ids_equal_evidence=(fill_ids==evidence_fill_ids),
            broker_ledger_ids_equal_evidence=(ledger_ids==evidence_ledger_ids),
            final_order_states_reconstructible=all_order_states_reconstructible,
            protective_cancel_reconstructed=protective_cancel_reconstructed,
            broker_ledger_types=broker_ledger_types,evidence_ledger_types=evidence_ledger_types,
            protective_reduce_only_orders=len(protective_orders),
            unknown_did_not_fill=(fills_before_unknown==fills_after_decision_unknown==fills_after_bounded_unknown),
            flat_after_protection=(not b.positions),
            sticky_stop_after_restart=sticky,
            causal_head_same_restart=(before['head_hash']==after['head_hash']),
            unknown_ranges_same_restart=(before['unknown_events_aggregated']==after['unknown_events_aggregated']))

def run():
    valid=materialize_profile('valid');heavy=materialize_profile('heavy')
    recon=reconstruction_smoke()
    admission='PASS' if valid['capacity_pass'] and heavy['capacity_pass'] and all((
        recon['broker_fill_ids_equal_evidence'],recon['broker_ledger_ids_equal_evidence'],
        recon['final_order_states_reconstructible'],recon['protective_cancel_reconstructed'],
        recon['protective_reduce_only_orders']>=1,'fee' in recon['broker_ledger_types'],
        'funding' in recon['broker_ledger_types'],
        recon['broker_ledger_types']==recon['evidence_ledger_types'],
        recon['unknown_did_not_fill'],recon['sticky_stop_after_restart'],recon['causal_head_same_restart'],
        recon['unknown_ranges_same_restart'])) else 'NOT_FEASIBLE'
    return dict(label='ISSUE38_ACCELERATED_CAPACITY_PLUS_ACTUAL_ENGINE_LIFECYCLE_NOT_REAL_48H',
        fixed_budget_bytes=BUDGET,entry_stop_bytes=ENTRY_STOP,raw_retention_limit=MAX_RAW_EVENTS,
        unknown_range_bucket_ms=UNKNOWN_RANGE_BUCKET_MS,
        declared_unknown_range_row_bound=(3*((WINDOW_MINUTES*60_000)//UNKNOWN_RANGE_BUCKET_MS)+
            (WINDOW_MINUTES//360)*6),
        declared_scope=dict(window_minutes=WINDOW_MINUTES,heavy_invalid_per_minute=HEAVY_INVALID_PER_MINUTE,
            heavy_late_aggtrades_per_minute=HEAVY_AGGTRADES_PER_MINUTE,
            outage_every_minutes=360,outage_duration_minutes=30),
        out_of_scope_behavior=('no arbitrary-rate/permanent-retention claim; existing 90% storage gate must '
            'halt new entry risk and preserve protective exits when actual bytes exceed the declared envelope'),
        capacity_measurement='inflight whole-root peak with every SQLite store in an active rollbackable write transaction',
        profiles=[valid,heavy],reconstruction=recon,admission=admission,
        public_source_evidence='SEPARATE_BOUNDED_PROBE_REQUIRED; accelerated capacity/lifecycle evidence is not public-source health',
        operator_root_touched=False,new_window_started=False,deployed=False)

def validate(result):
    if result['admission']!='PASS':raise AssertionError('Issue38 engineering admission not PASS')
    by={x['profile']:x for x in result['profiles']}
    for name in ('valid','heavy'):
        p=by[name]
        peak=p['inventory_inflight_peak'];gate=p['capacity_gate_at_inflight_peak']
        if p['capacity_basis']!='inflight_whole_root_peak':
            raise AssertionError(name+' profile capacity basis is not inflight peak')
        if (not peak['all_sqlite_connections_open'] or
                peak['open_connections_at_peak']!=peak['sqlite_files'] or
                peak['active_write_transactions_at_peak']!=peak['sqlite_files']):
            raise AssertionError(name+' inflight peak did not hold all SQLite writers open')
        if not peak['sqlite_sidecars'] or peak['total_bytes']!=sum(peak['stores'].values()):
            raise AssertionError(name+' inflight sidecars/whole-root accounting missing')
        if peak['total_bytes']<p['inventory_after_restart']['total_bytes']:
            raise AssertionError(name+' inflight peak below clean-close inventory')
        if gate['actual_discovery_gate'] is not True or gate['measured_bytes']!=peak['total_bytes']:
            raise AssertionError(name+' actual storage gate was not checked at measured peak')
        if gate['allows_new_entry']!=(peak['total_bytes']<ENTRY_STOP):
            raise AssertionError(name+' storage gate disagrees with measured peak')
        if not p['capacity_pass'] or peak['total_bytes']>=ENTRY_STOP:
            raise AssertionError(name+' profile exceeds fixed gate at inflight peak')
        if p['raw']['retained_events']!=MAX_RAW_EVENTS or p['raw']['events_evicted']<=0:
            raise AssertionError(name+' profile missing raw rollover')
        if p['inventory_after_restart']['sqlite_sidecars']:
            raise AssertionError(name+' clean-close SQLite sidecars unexpectedly remain')
    heavy=by['heavy']
    if heavy['raw_total_events']!=WINDOW_MINUTES*HEAVY_INVALID_PER_MINUTE:
        raise AssertionError('heavy workload reduced')
    expected_unknowns=heavy['raw_total_events']+heavy['causal']['repeated_outage_unknowns_aggregated']
    if heavy['causal']['unknown_events_aggregated']!=expected_unknowns:
        raise AssertionError('heavy unknown count mismatch')
    if heavy['causal']['unknown_range_rows']>result['declared_unknown_range_row_bound']:
        raise AssertionError('heavy unknown range row bound exceeded')
    if heavy['causal']['unknown_range_reconstructible_events']!=0:
        raise AssertionError('aggregated unknowns must not claim per-event reconstruction')
    recon=result['reconstruction']
    if recon['primary_maker_entry_fills']<1 or recon['protective_reduce_only_orders']<1:
        raise AssertionError('maker/protective lifecycle not exercised')
    if not all((recon['broker_fill_ids_equal_evidence'],recon['broker_ledger_ids_equal_evidence'],
                recon['final_order_states_reconstructible'],recon['protective_cancel_reconstructed'],
                'fee' in recon['broker_ledger_types'],'funding' in recon['broker_ledger_types'],
                recon['broker_ledger_types']==recon['evidence_ledger_types'],
                recon['flat_after_protection'],recon['unknown_did_not_fill'],
                recon['sticky_stop_after_restart'],recon['causal_head_same_restart'],
                recon['unknown_ranges_same_restart'])):
        raise AssertionError('causal reconstruction mismatch')
    return True

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--output');args=p.parse_args(argv)
    result=run();validate(result)
    raw=json.dumps(result,sort_keys=True,separators=(',',':'))
    if args.output:Path(args.output).write_text(raw+'\n')
    print(raw);return 0

if __name__=='__main__':raise SystemExit(main())
