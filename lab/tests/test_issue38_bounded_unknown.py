import sqlite3,tempfile,unittest
from decimal import Decimal as D
from pathlib import Path

LAB=Path(__file__).resolve().parents[1]
INSTRUMENT=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
                qty_step='0.001',tick_size='0.01',min_notional='0.01',
                max_qty='100',min_qty='0.001',category='crypto')
START=1_800_000_000_000

class UnknownRangeStoreTests(unittest.TestCase):
    def test_repeated_unknowns_are_bounded_with_integrity_and_boundaries(self):
        from discovery_evidence import CausalEvidence
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'e.sqlite3';e=CausalEvidence(path)
            for i in range(100):
                e.record_unknown_range('shared_event:aggTrade','source_valid_false',START+i,
                    dict(event_id='e'+str(i),type='aggTrade',source_ts=START-20_000+i,
                         receipt_ts=START-20_000+i,trade_id=i,reason='late'))
            rows=e.unknown_ranges()
            self.assertEqual(len(rows),1);r=rows[0]
            self.assertEqual(r['count'],100)
            self.assertEqual((r['first_event_id'],r['last_event_id']),('e0','e99'))
            self.assertEqual((r['first_trade_id'],r['last_trade_id']),(0,99))
            self.assertEqual(r['reconstructible_events'],0)
            self.assertEqual(CausalEvidence(path).unknown_ranges()[0]['rolling_hash'],r['rolling_hash'])
            with sqlite3.connect(path) as db:
                db.execute('UPDATE unknown_ranges SET count=count+1')
            with self.assertRaisesRegex(ValueError,'integrity mismatch'):
                CausalEvidence(path).unknown_ranges()

    def test_legacy_evidence_open_is_byte_stable_and_write_fails_closed(self):
        from discovery_evidence import CausalEvidence
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'legacy.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE evidence(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_key TEXT UNIQUE NOT NULL,
                    kind TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    hash TEXT NOT NULL)""")
            before=path.read_bytes()
            e=CausalEvidence(path)
            self.assertFalse(e.supports_unknown_ranges)
            self.assertEqual(path.read_bytes(),before)
            with self.assertRaisesRegex(ValueError,'legacy root'):
                e.record_unknown_range('shared_event:book','source_valid_false',START,
                    dict(event_id='legacy',type='book',source_ts=START,receipt_ts=START,reason='late'))
            self.assertEqual(path.read_bytes(),before)

    def test_time_bucket_boundary_creates_bounded_new_range(self):
        from discovery_evidence import CausalEvidence,UNKNOWN_RANGE_BUCKET_MS
        with tempfile.TemporaryDirectory() as td:
            e=CausalEvidence(Path(td)/'e.sqlite3')
            base=(START//UNKNOWN_RANGE_BUCKET_MS)*UNKNOWN_RANGE_BUCKET_MS
            for ts in (base+1,base+UNKNOWN_RANGE_BUCKET_MS-1,base+UNKNOWN_RANGE_BUCKET_MS):
                e.record_unknown_range('shared_event:book','source_valid_false',ts,
                    dict(event_id='e'+str(ts),type='book',source_ts=ts-20_000,receipt_ts=ts-20_000,reason='late'))
            rows=e.unknown_ranges()
            self.assertEqual([r['count'] for r in rows],[2,1])
            self.assertTrue(all(r['reconstructible_events']==0 for r in rows))

    def test_range_chronology_regression_fails_closed(self):
        from discovery_evidence import CausalEvidence
        with tempfile.TemporaryDirectory() as td:
            e=CausalEvidence(Path(td)/'e.sqlite3')
            e.record_unknown_range('shared_event:mark','source_valid_false',START+100,
                dict(event_id='m1',type='mark',source_ts=START,receipt_ts=START,reason='late'))
            with self.assertRaisesRegex(ValueError,'chronology regression'):
                e.record_unknown_range('shared_event:mark','source_valid_false',START+99,
                    dict(event_id='m0',type='mark',source_ts=START-1,receipt_ts=START-1,reason='late'))

    def test_shared_feed_duplicate_conflict_and_cursor_are_unchanged(self):
        from discovery_feed import SharedFeed
        with tempfile.TemporaryDirectory() as td:
            feed=SharedFeed(Path(td)/'feed.sqlite3',forward_start_ms=START,retention=8)
            feed.mark_reconnected(next_trade_id=7,observed_ms=START)
            first=feed.ingest('aggTrade',dict(trade_id=7,source_ts=START+100,
                price='100',qty='1',aggressor='BUY'),received_ms=START+101)
            self.assertTrue(first['persisted'])
            duplicate=feed.ingest('aggTrade',dict(trade_id=7,source_ts=START+100,
                price='100',qty='1',aggressor='BUY'),received_ms=START+101)
            self.assertFalse(duplicate['persisted'])
            with self.assertRaisesRegex(ValueError,'conflicting duplicate'):
                feed.ingest('aggTrade',dict(trade_id=7,source_ts=START+100,
                    price='100',qty='2',aggressor='BUY'),received_ms=START+101)
            self.assertEqual(feed.snapshot()['last_agg_trade_id'],7)

class DiscoveryBoundedUnknownTests(unittest.TestCase):
    def setUp(self):
        from discovery_lab import DiscoveryLab
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'root'
        self.lab=DiscoveryLab(self.root,LAB/'discovery_config_v1.json',INSTRUMENT)
        self.lab.activate(at_ms=START,operator_accepted=True)

    def tearDown(self):
        self.lab.close();self.tmp.cleanup()

    def _funding_ready(self):
        for arm,b in self.lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='fs:'+arm,symbol='ETHUSDT',
                            ts=START+1,source_ts=START+1,complete=True,valid_until_ts=START+10_000_000))

    def test_nondecision_invalid_events_aggregate_without_fake_reconstruction(self):
        for i in range(100):
            ts=START+10_000+i
            self.lab.on_shared_event(dict(type='book',event_id='bad-book:'+str(i),symbol='ETHUSDT',
                ts=ts,source_ts=ts-20_000,receipt_ts=ts-20_000,bids=[['100','1']],asks=[['101','1']],
                source_valid=False))
        self.assertEqual(self.lab.state['unknown_inputs'],100)
        self.assertEqual(self.lab.evidence.records('source_unknown'),[])
        ranges=self.lab.evidence.unknown_ranges()
        self.assertEqual(sum(r['count'] for r in ranges),100)
        self.assertTrue(all(r['reconstructible_events']==0 for r in ranges))
        self.assertFalse(any(b.fills for b in self.lab.brokers.values()))

    def test_decision_changing_unknown_is_preserved_and_never_makes_maker_fill(self):
        from discovery_feed import SharedFeed
        feed=SharedFeed(self.root/'issue38-feed.sqlite3',forward_start_ms=START)
        self.lab.bind_feed(feed);feed.mark_reconnected(next_trade_id=1,observed_ms=START)
        self._funding_ready()
        self.lab.on_shared_event(dict(type='book',event_id='book0',symbol='ETHUSDT',
            ts=START+5000,source_ts=START+5000,bids=[['100','1']],asks=[['100.10','2']],source_valid=True))
        signal=dict(signal_id='bounded',direction='long',target='105',
                    features=dict(signal_close_ms=START+5000),symbol='ETHUSDT')
        self.lab._route_signal(signal,START+5001)
        self.lab.on_shared_event(dict(type='book',event_id='book1',symbol='ETHUSDT',
            ts=START+7100,source_ts=START+7100,bids=[['100','1']],asks=[['100.10','2']],source_valid=True))
        order=self.lab.brokers['B'].orders['order:B:bounded']
        self.assertEqual(order['status'],'RESTING')
        self.lab.on_shared_event(dict(type='aggTrade',event_id='bad-trade:1',symbol='ETHUSDT',
            ts=START+7201,source_ts=START+1000,receipt_ts=START+1000,trade_id=1,
            price='99',qty='100',aggressor='SELL',source_valid=False))
        self.assertEqual(order['status'],'CANCELED')
        full=self.lab.evidence.records('source_unknown')
        self.assertEqual([x['payload']['event_id'] for x in full],['bad-trade:1'])
        self.assertFalse(self.lab.brokers['B'].fills)
        self.lab.on_shared_event(dict(type='aggTrade',event_id='bad-trade:2',symbol='ETHUSDT',
            ts=START+7202,source_ts=START+1001,receipt_ts=START+1001,trade_id=2,
            price='98',qty='100',aggressor='SELL',source_valid=False))
        self.assertEqual(len(self.lab.evidence.records('source_unknown')),1)
        self.assertEqual(sum(r['count'] for r in self.lab.evidence.unknown_ranges()),1)
        self.assertFalse(self.lab.brokers['B'].fills)

    def test_first_runner_gap_and_recovery_remain_full_repeated_gap_is_bounded(self):
        self.lab.request_source_gap(START+1000,'ARTIFICIAL outage')
        self.lab.request_source_gap(START+2000,'ARTIFICIAL outage')
        self.lab.clear_source_gap(START+3000)
        self.assertEqual(len(self.lab.evidence.records('source_unknown')),1)
        self.assertEqual(len(self.lab.evidence.records('source_recovered')),1)
        ranges=self.lab.evidence.unknown_ranges()
        self.assertEqual(sum(r['count'] for r in ranges),1)
        self.assertEqual(ranges[0]['scope'],'runner_shared_source')


class HistoricalInflightProbeContractTests(unittest.TestCase):
    def test_declared_profiles_no_longer_execute_user_version_probe(self):
        import inspect
        from issue38_acceptance import materialize_profile
        self.assertNotIn('inflight_peak_inventory(root)',inspect.getsource(materialize_profile))


class ActualWritePeakTests(unittest.TestCase):
    def test_actual_declared_writes_capture_natural_transient_sidecars_and_phases(self):
        from issue38_acceptance import ENTRY_STOP,materialize_profile
        profile=materialize_profile('heavy')
        peak=profile['actual_write_peak_sampled_lower_bound']
        samples=profile['actual_write_samples']
        phases={s['phase'] for s in samples}
        self.assertEqual(profile['capacity_basis'],'conservative_transient_upper_bound')
        self.assertIn('raw_after_executemany_before_commit',phases)
        self.assertIn('raw_after_commit_before_vacuum',phases)
        self.assertIn('raw_after_vacuum',phases)
        self.assertIn('heavy_ranges_after_executemany_before_commit',phases)
        self.assertIn('heavy_ranges_after_commit',phases)
        self.assertTrue(any(s['sqlite_sidecars'] for s in samples))
        self.assertTrue(all(s['sqlite_files']>=5 for s in samples))
        self.assertEqual(peak['total_bytes'],max(s['total_bytes'] for s in samples))
        bound=profile['conservative_transient_envelope']
        self.assertEqual(profile['capacity_pass'],bound['upper_bound_bytes']<ENTRY_STOP)
        self.assertEqual(profile['capacity_gate_at_conservative_bound']['measured_bytes'],bound['upper_bound_bytes'])
        self.assertIn('heavy_after_boundary_evidence_appends',phases)
        self.assertGreaterEqual(peak['total_bytes'],profile['inventory_after_restart']['total_bytes'])

    def test_actual_write_peak_is_not_user_version_probe_or_claimed_exhaustive(self):
        from issue38_acceptance import materialize_profile
        profile=materialize_profile('valid')
        self.assertNotIn('user_version',profile['actual_write_peak_sampled_lower_bound']['phase'])
        self.assertEqual(profile['actual_write_peak_kind'],'sampled_lower_bound_not_exhaustive_peak')
        self.assertFalse(profile['actual_write_peak_exhaustive'])
        self.assertIn('sampling',profile['actual_write_peak_limitation'].lower())
        self.assertEqual(profile['workload_count_proof']['expected_raw_total_events'],
                         profile['workload_count_proof']['observed_raw_events_persisted'])


class ConservativeEnvelopeContractTests(unittest.TestCase):
    def test_declared_profiles_use_conservative_upper_bound_not_sampled_lower_bound(self):
        from issue38_acceptance import ENTRY_STOP,materialize_profile
        profile=materialize_profile('valid')
        self.assertEqual(profile['capacity_basis'],'conservative_transient_upper_bound')
        bound=profile['conservative_transient_envelope']
        self.assertEqual(bound['kind'],'conservative_upper_bound')
        self.assertTrue(bound['covers_all_sqlite_stores'])
        self.assertTrue(bound['covers_metadata'])
        self.assertTrue(bound['covers_transaction_commit_rollover_compaction'])
        self.assertGreaterEqual(bound['upper_bound_bytes'],
                                profile['actual_write_peak_sampled_lower_bound']['total_bytes'])
        self.assertEqual(profile['capacity_pass'],bound['upper_bound_bytes']<ENTRY_STOP)
        self.assertIn(profile['capacity_status'],('PROVEN_WITHIN_GATE','NO_GO_BOUND_EXCEEDS_GATE'))

class RootlessSourcePreflightContractTests(unittest.TestCase):
    def _client(self):
        from datetime import datetime,timezone
        now=1_800_000_600_000
        received=datetime.fromtimestamp(now/1000,timezone.utc).isoformat()
        class Client:
            def __init__(self):self.calls=[]
            def get(self,endpoint,params=None):
                params=params or {};self.calls.append((endpoint,dict(params)))
                if endpoint=='/fapi/v1/exchangeInfo':
                    payload={'symbols':[{'symbol':'ETHUSDT','contractType':'PERPETUAL','status':'TRADING',
                        'quoteAsset':'USDT','marginAsset':'USDT','filters':[
                            {'filterType':'PRICE_FILTER','tickSize':'0.01'},
                            {'filterType':'LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'100'},
                            {'filterType':'MARKET_LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'100'},
                            {'filterType':'MIN_NOTIONAL','notional':'0.01'}]}]}
                    return {'endpoint':endpoint,'params':params,'source_timestamp_ms':None,'received_at':received,'payload':payload}
                if endpoint=='/fapi/v1/depth':
                    return {'endpoint':endpoint,'params':params,'source_timestamp_ms':now-100,
                        'received_at':received,'payload':{'bids':[['100','1']],'asks':[['100.10','1']]}}
                if endpoint=='/fapi/v1/premiumIndex':
                    return {'endpoint':endpoint,'params':params,'source_timestamp_ms':now-100,
                        'received_at':received,'payload':{'symbol':'ETHUSDT','markPrice':'100.05',
                            'nextFundingTime':now+28_800_000}}
                if endpoint=='/fapi/v1/fundingInfo':
                    return {'endpoint':endpoint,'params':params,'source_timestamp_ms':None,
                        'received_at':received,'payload':[]}
                if endpoint=='/fapi/v1/fundingRate':
                    return {'endpoint':endpoint,'params':params,'source_timestamp_ms':None,
                        'received_at':received,'payload':[{'symbol':'ETHUSDT','fundingTime':now-28_800_000,
                            'fundingRate':'0.0001','markPrice':'100','rateType':'Regular'}]}
                if endpoint=='/fapi/v1/aggTrades':
                    return {'endpoint':endpoint,'params':params,'source_timestamp_ms':None,
                        'received_at':received,'payload':[
                            {'a':10,'p':'100','q':'0.1','f':10,'l':10,'T':now-200,'m':True},
                            {'a':11,'p':'100.01','q':'0.1','f':11,'l':11,'T':now-100,'m':False}]}
                if endpoint=='/fapi/v1/klines':
                    rows=[]
                    base=now-62*60_000
                    for i in range(61):
                        open_ms=base+i*60_000
                        rows.append([open_ms,'100','101','99','100','1',open_ms+59_999])
                    return {'endpoint':endpoint,'params':params,'source_timestamp_ms':None,
                        'received_at':received,'payload':rows}
                raise AssertionError(endpoint)
        return Client(),now

    def test_preflight_is_rootless_all_source_and_fail_closed_contract(self):
        from discovery_runner import public_source_preflight
        client,now=self._client()
        result=public_source_preflight(client=client,config_path=LAB/'discovery_config_v2.json',
                                       clock_ms=lambda:now)
        self.assertEqual(result['action'],'preflight')
        self.assertTrue(result['pass'])
        self.assertFalse(result['root_touched'])
        self.assertFalse(result['window_started'])
        self.assertFalse(result['activated'])
        endpoints={x[0] for x in client.calls}
        self.assertEqual(endpoints,{'/fapi/v1/exchangeInfo','/fapi/v1/depth','/fapi/v1/premiumIndex',
                                    '/fapi/v1/fundingInfo','/fapi/v1/fundingRate',
                                    '/fapi/v1/aggTrades','/fapi/v1/klines'})
        self.assertEqual(result['warmup_bars'],61)
        self.assertTrue(result['aggtrade_contiguous'])
        self.assertTrue(result['source_age_valid'])

if __name__=='__main__':unittest.main()
