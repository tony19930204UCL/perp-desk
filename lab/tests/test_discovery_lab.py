import json, tempfile, unittest
from decimal import Decimal as D
from pathlib import Path

LAB=Path(__file__).resolve().parents[1]

class SignalTests(unittest.TestCase):
    def bars(self,move='flat'):
        rows=[];base=1_000_000//60000*60000
        for i in range(62):
            close=D('100')
            if move=='fall' and i==61:close=D('90')
            if move=='rise' and i==61:close=D('110')
            rows.append(dict(open_time_ms=base+i*60000,close_time_ms=base+(i+1)*60000,
                             open=close,high=close,low=close,close=close,
                             volume=D('10') if i<61 else D('20')))
        return rows

    def test_symmetric_falling_rising_signals_and_frozen_prior15_target(self):
        from discovery_signal import evaluate
        falling=self.bars('fall');r=evaluate(falling[:61],falling[61])
        self.assertEqual([x['direction'] for x in r],['long'])
        self.assertEqual(r[0]['target'],'100')
        self.assertTrue(r[0]['features']['target_excludes_signal_bar'])
        rising=self.bars('rise');r=evaluate(rising[:61],rising[61])
        self.assertEqual([x['direction'] for x in r],['short'])
        self.assertEqual(r[0]['target'],'100')

    def test_target_missing_gap_zero_volume_fail_closed(self):
        from discovery_signal import evaluate
        bars=self.bars('fall')
        bad=[dict(x) for x in bars]
        bad[50]['open_time_ms']+=60000;bad[50]['close_time_ms']+=60000
        with self.assertRaises(ValueError):evaluate(bad[:61],bad[61])
        zero=[dict(x) for x in bars]
        for x in zero[46:61]:x['volume']=D('0')
        with self.assertRaisesRegex(ValueError,'zero target volume'):evaluate(zero[:61],zero[61])


class SharedFeedTests(unittest.TestCase):
    def test_aggtrade_cursor_gap_dedup_reconnect_and_bounded_retention(self):
        from discovery_feed import SharedFeed
        with tempfile.TemporaryDirectory() as td:
            f=SharedFeed(Path(td)/'feed.sqlite3',forward_start_ms=1000,retention=3)
            seen=[];f.subscribe(seen.append);f.begin_poll()
            f.mark_reconnected(next_trade_id=10,observed_ms=1000)
            a=f.ingest('aggTrade',dict(trade_id=10,source_ts=1100,price='100',qty='1',aggressor='SELL'),received_ms=1101)
            self.assertTrue(a['event']['source_valid']);self.assertEqual(a['dispatched'],1)
            d=f.ingest('aggTrade',dict(trade_id=10,source_ts=1100,price='100',qty='1',aggressor='SELL'),received_ms=1101)
            self.assertFalse(d['persisted']);self.assertEqual(len(seen),1)
            with self.assertRaisesRegex(ValueError,'conflicting duplicate'):
                f.ingest('aggTrade',dict(trade_id=10,source_ts=1100,price='100',qty='2',aggressor='SELL'),received_ms=1101)
            gap=f.ingest('aggTrade',dict(trade_id=12,source_ts=1200,price='99',qty='1',aggressor='SELL'),received_ms=1201)
            self.assertFalse(gap['event']['source_valid'])
            f.mark_reconnected(next_trade_id=13,observed_ms=1300)
            for n in range(13,17):
                f.ingest('aggTrade',dict(trade_id=n,source_ts=1300+n,price='99',qty='1',aggressor='SELL'),received_ms=1301+n)
            s=f.snapshot()
            self.assertEqual(s['retained_events'],3);self.assertEqual(s['fetch_calls'],1)
            self.assertEqual(s['reconnects'],2);self.assertGreaterEqual(s['source_invalid'],1)
            f2=SharedFeed(Path(td)/'feed.sqlite3',forward_start_ms=1000,retention=3)
            self.assertEqual(f2.snapshot()['last_agg_trade_id'],16)

    def test_binance_aggtrade_shape_maps_maker_flag_to_opposite_aggressor(self):
        from discovery_feed import binance_aggtrade_input
        sell=binance_aggtrade_input(dict(a=7,p='100.01',q='0.2',f=10,l=11,T=1234,m=True))
        buy=binance_aggtrade_input(dict(a=8,p='100.02',q='0.3',f=12,l=12,T=1235,m=False))
        self.assertEqual(sell['aggressor'],'SELL');self.assertEqual(buy['aggressor'],'BUY')
        self.assertEqual(sell['trade_id'],7);self.assertEqual(sell['source_ts'],1234)
        with self.assertRaises(ValueError):
            binance_aggtrade_input(dict(a=9,p='100',q='1',f=14,l=13,T=1236,m=True))

    def test_late_bar_is_unknown_not_timely(self):
        from discovery_feed import SharedFeed
        with tempfile.TemporaryDirectory() as td:
            f=SharedFeed(Path(td)/'f.sqlite3',forward_start_ms=1000)
            e=f.ingest('closed_bar',dict(open_time_ms=60_000,close_time_ms=120_000,
                open='100',high='100',low='100',close='100',volume='1'),received_ms=140_001)['event']
            self.assertFalse(e['source_valid'])


class BrokerArrivalQueueTests(unittest.TestCase):
    def test_arrival_queue_opt_in_and_legacy_explicit_queue_both_work(self):
        from sim_broker import SimBroker,InstrumentSettings,ExecutionModel,RiskContract,Intent
        with tempfile.TemporaryDirectory() as td:
            s=InstrumentSettings('ETHUSDT',D('.0002'),D('.0005'),D('.001'),D('.01'),D('.01'),D('10'),D('.001'),'crypto')
            r=RiskContract(True,'r',D('10'),D('20'),D('10'),1,D('50'),True)
            b=SimBroker(Path(td)/'b.sqlite3',initial_cash=D('100'),instruments=[s],
                        execution=ExecutionModel(2000,2,None,2,2),risk=r,version_id='v',forward_start=1000)
            b.on_event(dict(type='funding_status',event_id='fs',symbol='ETHUSDT',ts=1001,source_ts=1001,complete=True,valid_until_ts=100000))
            b.submit(Intent('new','ETHUSDT','BUY',D('1'),D('90'),2000,s.quantity_step,s.tick,s.min_notional,s.max_quantity,'MAKER',False,
                            limit_price=D('99'),queue_ahead_qty=None,maker_queue_from_arrival=True,expires_ts=17000))
            b.on_event(dict(type='book',event_id='book',symbol='ETHUSDT',ts=4100,source_ts=4100,bids=[['99','2.5']],asks=[['100','3']]))
            self.assertEqual(b.orders['order:new']['queue_remaining'],'2.5')
            b.submit(Intent('old','ETHUSDT','BUY',D('1'),D('90'),4200,s.quantity_step,s.tick,s.min_notional,s.max_quantity,'MAKER',False,
                            limit_price=D('98'),queue_ahead_qty=D('7')))
            b.on_event(dict(type='book',event_id='book2',symbol='ETHUSDT',ts=6300,source_ts=6300,bids=[['99','2']],asks=[['100','3']]))
            self.assertEqual(b.orders['order:old']['queue_remaining'],'7')
            b.close()


class CompactBrokerEventTests(unittest.TestCase):
    def test_discovery_opt_in_keeps_only_event_hash_and_preserves_dedup_after_restart(self):
        from sim_broker import SimBroker,InstrumentSettings,ExecutionModel,RiskContract
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'compact.sqlite3'
            s=InstrumentSettings('ETHUSDT',D('.0002'),D('.0005'),D('.001'),D('.01'),D('.01'),D('10'),D('.001'),'crypto')
            risk=RiskContract(True,'r',D('1'),D('3'),D('3'),1,D('10'),True)
            kwargs=dict(initial_cash=D('100'),instruments=[s],execution=ExecutionModel(2000,2,None,2,2),
                        risk=risk,version_id='compact',forward_start=1000,compact_seen_events=True)
            event=dict(type='book',event_id='book-1',symbol='ETHUSDT',ts=1100,source_ts=1100,
                       bids=[['100','1']],asks=[['101','1']])
            b=SimBroker(path,**kwargs);b.on_event(event)
            saved=b.seen_events['book-1']
            self.assertIsInstance(saved,str);self.assertEqual(len(saved),64);b.close()
            b=SimBroker(path,**kwargs)
            try:
                before=len(b.seen_events);b.on_event(event);self.assertEqual(len(b.seen_events),before)
                with self.assertRaisesRegex(ValueError,'conflicting event ID'):
                    b.on_event(dict(event,asks=[['102','1']]))
            finally:b.close()


class DiscoveryLabTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'discovery'
        self.instrument=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
                             qty_step='0.001',tick_size='0.01',min_notional='0.01',
                             max_qty='100',min_qty='0.001',category='crypto')
        from discovery_lab import DiscoveryLab
        self.lab=DiscoveryLab(self.root,LAB/'discovery_config_v1.json',self.instrument)
        self.start=1_000_000
        self.lab.activate(at_ms=self.start,operator_accepted=True)

    def tearDown(self):
        self.lab.close();self.tmp.cleanup()

    def ready(self,now=1_010_000,bid='100',ask='100.10'):
        for arm,b in self.lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='fs:'+arm,symbol='ETHUSDT',ts=self.start+1,source_ts=self.start+1,complete=True,valid_until_ts=self.start+10_000_000))
        event=dict(type='book',event_id='book:'+str(now),symbol='ETHUSDT',ts=now,source_ts=now,bids=[[bid,'1']],asks=[[ask,'2']],source_valid=True)
        self.lab.on_shared_event(event)

    def signal(self,direction='long',target='105',sid='sig'):
        return dict(signal_id=sid,direction=direction,target=target,
                    features=dict(signal_close_ms=self.start+5000),symbol='ETHUSDT')

    def test_three_accounts_are_independent_and_no_default_sentinel_mutation(self):
        sentinel=Path(self.tmp.name)/'default-paper.sqlite3';sentinel.write_bytes(b'default-account-sentinel')
        before=sentinel.read_bytes()
        self.ready();self.lab._route_signal(self.signal(),1_010_100)
        self.assertEqual(sentinel.read_bytes(),before)
        self.assertEqual({b.initial_cash for b in self.lab.brokers.values()},{'100'})
        paths={b.path for b in self.lab.brokers.values()};self.assertEqual(len(paths),3)
        self.assertTrue(all(p.is_relative_to(self.root) for p in paths))
        self.assertEqual(self.lab.state['deadline_ms']-self.lab.state['start_ms'],172800000)

    def test_maker_trade_through_queue_partial_expiry_and_no_touch_fill(self):
        from discovery_feed import SharedFeed
        feed=SharedFeed(self.root/'shared-feed.sqlite3',forward_start_ms=self.start)
        self.lab.bind_feed(feed);feed.mark_reconnected(next_trade_id=1,observed_ms=self.start)
        for arm,b in self.lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='fund:'+arm,symbol='ETHUSDT',ts=self.start+1,source_ts=self.start+1,complete=True,valid_until_ts=self.start+100000))
        feed.ingest('book',dict(source_ts=self.start+5000,bids=[['100','1']],asks=[['100.10','2']]),received_ms=self.start+5000)
        self.lab._route_signal(self.signal(target='105',sid='maker'),self.start+5001)
        order=self.lab.brokers['B'].orders['order:B:maker']
        self.assertTrue(order['intent']['maker_queue_from_arrival'])
        feed.ingest('book',dict(source_ts=self.start+7100,bids=[['100','1']],asks=[['100.10','2']]),received_ms=self.start+7100)
        self.assertEqual(self.lab.brokers['B'].orders['order:B:maker']['queue_remaining'],'1')
        feed.ingest('aggTrade',dict(trade_id=1,source_ts=self.start+7200,price='100',qty='10',aggressor='SELL'),received_ms=self.start+7201)
        self.assertFalse(self.lab.brokers['B'].fills,'price touch must not fill')
        # A late-received trade whose source time predates maker resting cannot prove a fill.
        feed.ingest('aggTrade',dict(trade_id=2,source_ts=self.start+7000,price='99.99',qty='10',aggressor='SELL'),received_ms=self.start+7250)
        self.assertFalse(self.lab.brokers['B'].fills,'pre-arrival source trade must not fill')
        feed.ingest('aggTrade',dict(trade_id=3,source_ts=self.start+7300,price='99.99',qty='1.4',aggressor='SELL'),received_ms=self.start+7301)
        entry=[f for f in self.lab.brokers['B'].fills if not self.lab.brokers['B'].orders[f['order_id']]['intent']['reduce_only']]
        self.assertEqual(sum(D(f['qty']) for f in entry),D('.4'))
        self.assertTrue(all(f['liquidity']=='MAKER' for f in entry))
        sensitivity=self.lab.report(self.start+7301)['arms']['B']['queue_stress_sensitivity'][0]
        self.assertEqual(D(sensitivity['primary_1x']['fill_qty']),D('.4'))
        self.assertEqual(D(sensitivity['stress_2x']['fill_qty']),D(0))
        self.assertEqual(sensitivity['stress_2x']['status'],'WAITING')
        self.assertEqual(sensitivity['comparison'],'primary_fill_vs_stress_no_fill')
        feed.ingest('book',dict(source_ts=self.start+21000,bids=[['99','2']],asks=[['100','2']]),received_ms=self.start+21000)
        self.assertEqual(self.lab.brokers['B'].orders['order:B:maker']['status'],'EXPIRED')
        self.assertIn('ETHUSDT',self.lab.brokers['B'].positions)

    def test_queue_2x_same_event_waiting_then_partial_vs_primary_fill(self):
        from discovery_feed import SharedFeed
        feed=SharedFeed(self.root/'stress-feed.sqlite3',forward_start_ms=self.start)
        self.lab.bind_feed(feed);feed.mark_reconnected(next_trade_id=1,observed_ms=self.start)
        for arm,b in self.lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='stress-f:'+arm,symbol='ETHUSDT',
                            ts=self.start+1,source_ts=self.start+1,complete=True,valid_until_ts=self.start+100000))
        feed.ingest('book',dict(source_ts=self.start+5000,bids=[['100','1']],asks=[['100.10','2']]),
                    received_ms=self.start+5000)
        self.lab._route_signal(self.signal(target='105',sid='stress'),self.start+5001)
        feed.ingest('book',dict(source_ts=self.start+7100,bids=[['100','1']],asks=[['100.10','2']]),
                    received_ms=self.start+7100)
        feed.ingest('aggTrade',dict(trade_id=1,source_ts=self.start+7200,price='99.99',qty='1.4',aggressor='SELL'),
                    received_ms=self.start+7201)
        first=self.lab.report(self.start+7201)['arms']['B']['queue_stress_sensitivity'][0]
        self.assertEqual(D(first['primary_1x']['fill_qty']),D('.4'))
        self.assertEqual(D(first['stress_2x']['fill_qty']),D(0))
        self.assertEqual(first['stress_2x']['status'],'WAITING')
        self.assertEqual(first['comparison'],'primary_fill_vs_stress_no_fill')
        feed.ingest('mark',dict(source_ts=self.start+7250,price='100'),received_ms=self.start+7250)
        feed.ingest('aggTrade',dict(trade_id=2,source_ts=self.start+7300,price='99.98',qty='1.0',aggressor='SELL'),
                    received_ms=self.start+7301)
        second=self.lab.report(self.start+7301)['arms']['B']['queue_stress_sensitivity'][0]
        self.assertEqual(self.lab.brokers['B'].orders['order:B:stress']['status'],'RESTING')
        self.assertGreater(D(second['primary_1x']['fill_qty']),D(second['stress_2x']['fill_qty']))
        self.assertGreater(D(second['stress_2x']['fill_qty']),D(0))
        self.assertEqual(second['stress_2x']['status'],'PARTIAL')
        outcomes=[x['outcome'] for x in second['transitions']]
        self.assertIn('waiting',outcomes);self.assertIn('partial',outcomes)

    def test_maker_profit_cross_is_explicit_taker_fallback(self):
        from discovery_feed import SharedFeed
        feed=SharedFeed(self.root/'sf.sqlite3',forward_start_ms=self.start);self.lab.bind_feed(feed)
        feed.mark_reconnected(next_trade_id=1,observed_ms=self.start)
        for arm,b in self.lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='f:'+arm,symbol='ETHUSDT',ts=self.start+1,source_ts=self.start+1,complete=True,valid_until_ts=self.start+100000))
        feed.ingest('book',dict(source_ts=self.start+5000,bids=[['100','0.1']],asks=[['100.1','2']]),received_ms=self.start+5000)
        self.lab._route_signal(self.signal(target='101',sid='x'),self.start+5001)
        feed.ingest('book',dict(source_ts=self.start+7100,bids=[['100','0.1']],asks=[['100.1','2']]),received_ms=self.start+7100)
        feed.ingest('aggTrade',dict(trade_id=1,source_ts=self.start+7200,price='99.99',qty='5',aggressor='SELL'),received_ms=self.start+7201)
        profits=[o for o in self.lab.brokers['B'].orders.values() if o['intent']['reduce_only'] and o['intent']['kind']=='MAKER']
        self.assertTrue(profits)
        feed.ingest('book',dict(source_ts=self.start+9400,bids=[['102','1']],asks=[['102.1','1']]),received_ms=self.start+9400)
        self.assertEqual(profits[0]['reason'],'post_only_cross')
        fallbacks=[o for o in self.lab.brokers['B'].orders.values() if o['intent']['reduce_only'] and o['intent']['kind']=='TAKER']
        self.assertTrue(fallbacks)
        feed.ingest('book',dict(source_ts=self.start+11500,bids=[['101.9','2']],asks=[['102','2']]),received_ms=self.start+11500)
        exits=[f for f in self.lab.brokers['B'].fills if self.lab.brokers['B'].orders[f['order_id']]['intent']['reduce_only']]
        self.assertTrue(exits);self.assertTrue(all(f['liquidity']=='TAKER' for f in exits))

    def test_short_taker_sign_fee_funding_target_and_risk(self):
        self.ready(now=self.start+5000,bid='100',ask='100.1')
        self.lab._route_signal(self.signal('short','95','short1'),self.start+5001)
        c=self.lab.brokers['C']
        self.lab.on_shared_event(dict(type='book',event_id='fill-short',symbol='ETHUSDT',ts=self.start+7100,source_ts=self.start+7100,bids=[['100','5']],asks=[['100.1','5']],source_valid=True))
        self.assertLess(D(c.positions['ETHUSDT']['qty']),0)
        entry=c.fills[-1];self.assertEqual(entry['side'],'SELL');self.assertEqual(entry['liquidity'],'TAKER')
        self.assertEqual(D(entry['fee']),D(entry['price'])*D(entry['qty'])*D('.0005'))
        qty=D(c.positions['ETHUSDT']['qty'])
        self.lab.on_shared_event(dict(type='funding',event_id='funding-short',symbol='ETHUSDT',ts=self.start+8000,source_ts=self.start+8000,
            settlement_ts=self.start+7900,rate='.001',mark='100',rate_type='Regular',finalized=True,source_valid=True))
        funding=[x for x in c.ledger if x['type']=='funding'][-1]
        self.assertGreater(D(funding['amount']),0)
        self.assertEqual(D(funding['amount']),-qty*D('100')*D('.001'))
        self.lab.on_shared_event(dict(type='mark',event_id='target-mark',symbol='ETHUSDT',ts=self.start+9000,source_ts=self.start+9000,price='94',source_valid=True))
        self.lab.on_shared_event(dict(type='book',event_id='close-short',symbol='ETHUSDT',ts=self.start+11100,source_ts=self.start+11100,bids=[['93.9','5']],asks=[['94','5']],source_valid=True))
        self.assertNotIn('ETHUSDT',c.positions)
        self.assertEqual(c.fills[-1]['side'],'BUY');self.assertEqual(c.fills[-1]['liquidity'],'TAKER')
        cash_replay=D(c.initial_cash)+sum((D(x['amount']) for x in c.ledger),D(0))
        self.assertEqual(c.cash,cash_replay)

    def test_checkpoint_boundaries_and_restart_preserve_window(self):
        for i in range(432):
            self.lab.state['coverage'][str(self.start+(i+1)*60000)]=dict(timely=True,source_valid=True)
        for i in range(4):
            for arm in ('A','C'):
                self.lab.state['diagnostics'].append(dict(arm=arm,signal_id=f'{arm}-{i}',direction='long',
                    at_ms=self.start+1000+i,cost_qualified=True))
        now=self.lab.state['checkpoint_ms']
        self.assertEqual(self.lab.checkpoint('A',now)['status'],'passed')
        self.lab.state['coverage'].pop(str(self.start+432*60000))
        self.assertEqual(self.lab.checkpoint('A',now)['status'],'data_quality_inconclusive')
        self.lab.state['coverage'][str(self.start+432*60000)]=dict(timely=True,source_valid=True)
        self.lab._save();deadline=self.lab.state['deadline_ms'];self.lab.close()
        from discovery_lab import DiscoveryLab
        self.lab=DiscoveryLab(self.root,LAB/'discovery_config_v1.json',self.instrument)
        self.assertEqual(self.lab.state['deadline_ms'],deadline)
        self.assertEqual(self.lab.checkpoint('A',now)['status'],'passed')

    def test_operator_stop_cancels_pending_entries_and_blocks_new_entries(self):
        from discovery_feed import SharedFeed
        feed=SharedFeed(self.root/'stop-feed.sqlite3',forward_start_ms=self.start)
        self.lab.bind_feed(feed);feed.mark_reconnected(next_trade_id=1,observed_ms=self.start)
        for arm,b in self.lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='stop-f:'+arm,symbol='ETHUSDT',
                            ts=self.start+1,source_ts=self.start+1,complete=True,
                            valid_until_ts=self.start+100000))
        feed.ingest('book',dict(source_ts=self.start+5000,bids=[['100','1']],asks=[['100.1','2']]),
                    received_ms=self.start+5000)
        self.lab._route_signal(self.signal(target='105',sid='stop-pending'),self.start+5001)
        for broker in self.lab.brokers.values():
            self.assertTrue(any(not o['intent']['reduce_only'] and o['status']=='PENDING'
                                for o in broker.orders.values()))
        stop_at=self.start+5100
        self.lab.request_operator_stop(stop_at)
        self.assertTrue(self.lab.state['operator_stop_requested'])
        self.assertEqual(self.lab.state['operator_stop_requested_ms'],stop_at)
        for broker in self.lab.brokers.values():
            self.assertTrue(all(o['status']=='CANCELED'
                                for o in broker.orders.values() if not o['intent']['reduce_only']))
        before=sum(1 for b in self.lab.brokers.values() for o in b.orders.values()
                   if not o['intent']['reduce_only'])
        self.lab._route_signal(self.signal(target='106',sid='after-stop'),self.start+5200)
        after=sum(1 for b in self.lab.brokers.values() for o in b.orders.values()
                  if not o['intent']['reduce_only'])
        self.assertEqual(after,before,'operator stop must inhibit new entry orders')

    def test_operator_stop_keeps_existing_exposure_reduce_only_protection(self):
        self.ready(now=self.start+10000)
        self.lab._route_signal(self.signal(target='105',sid='stop-open'),self.start+10001)
        self.lab.on_shared_event(dict(type='book',event_id='stop-arrival',symbol='ETHUSDT',
            ts=self.start+12100,source_ts=self.start+12100,
            bids=[['100','5']],asks=[['100.1','5']],source_valid=True))
        self.assertIn('ETHUSDT',self.lab.brokers['A'].positions)
        self.assertIn('ETHUSDT',self.lab.brokers['C'].positions)
        stop_at=self.start+12200
        self.lab.request_operator_stop(stop_at)
        stop_price=min(D(self.lab.brokers[a].positions['ETHUSDT']['stop']) for a in ('A','C'))
        self.lab.on_shared_event(dict(type='mark',event_id='stop-protect-mark',symbol='ETHUSDT',
            ts=self.start+13000,source_ts=self.start+13000,price=str(stop_price-D('.01')),source_valid=True))
        self.lab.on_shared_event(dict(type='book',event_id='stop-protect-book',symbol='ETHUSDT',
            ts=self.start+15100,source_ts=self.start+15100,
            bids=[[str(stop_price-D('.02')),'10']],asks=[[str(stop_price-D('.01')),'10']],source_valid=True))
        for arm in ('A','C'):
            broker=self.lab.brokers[arm]
            self.assertNotIn('ETHUSDT',broker.positions)
            self.assertTrue(any(o['intent']['reduce_only'] and o['intent']['kind']=='TAKER' and o['status']=='FILLED'
                                for o in broker.orders.values()))

    def test_runner_source_gap_scope_is_durable_and_fail_closed(self):
        from discovery_feed import SharedFeed
        feed=SharedFeed(self.root/'scope-feed.sqlite3',forward_start_ms=self.start)
        self.lab.bind_feed(feed);feed.mark_reconnected(next_trade_id=1,observed_ms=self.start)
        for arm,b in self.lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='scope-f:'+arm,symbol='ETHUSDT',
                            ts=self.start+1,source_ts=self.start+1,complete=True,
                            valid_until_ts=self.start+100000))
        feed.ingest('book',dict(source_ts=self.start+5000,bids=[['100','1']],asks=[['100.1','2']]),
                    received_ms=self.start+5000)
        self.lab._route_signal(self.signal(target='105',sid='scope'),self.start+5001)
        self.assertTrue(all(any(not o['intent']['reduce_only'] and o['status']=='PENDING'
                                for o in b.orders.values()) for b in self.lab.brokers.values()))
        self.lab.request_maker_source_gap(self.start+5100,'ARTIFICIAL aggTrade transport failure')
        self.assertTrue(self.lab.state['maker_source_gap_open'])
        self.assertFalse(self.lab.state['shared_source_gap_open'])
        self.assertTrue(all(o['status']=='CANCELED' for o in self.lab.brokers['B'].orders.values()
                            if not o['intent']['reduce_only']))
        for arm in ('A','C'):
            self.assertTrue(any(not o['intent']['reduce_only'] and o['status']=='PENDING'
                                for o in self.lab.brokers[arm].orders.values()))
        self.lab.clear_maker_source_gap(self.start+5200)
        self.assertFalse(self.lab.state['maker_source_gap_open'])
        self.lab.request_source_gap(self.start+5300,'ARTIFICIAL shared market transport failure')
        self.assertTrue(self.lab.state['shared_source_gap_open'])
        for arm in ('A','C'):
            self.assertTrue(all(o['status']=='CANCELED' for o in self.lab.brokers[arm].orders.values()
                                if not o['intent']['reduce_only']))
        kinds=[r['kind'] for r in self.lab.evidence.records()]
        self.assertIn('source_unknown',kinds);self.assertIn('source_recovered',kinds)
        self.lab._save();self.lab.close()
        from discovery_lab import DiscoveryLab
        self.lab=DiscoveryLab(self.root,LAB/'discovery_config_v1.json',self.instrument)
        self.assertTrue(self.lab.state['shared_source_gap_open'])
        self.assertFalse(self.lab.state['maker_source_gap_open'])
        self.lab.clear_source_gap(self.start+5400)
        self.assertFalse(self.lab.state['shared_source_gap_open'])

    def test_storage_budget_inhibits_new_entries_without_touching_exits(self):
        self.lab.storage_probe=lambda:self.lab.config['storage']['budget_bytes']
        self.ready();self.lab._route_signal(self.signal(),self.start+10001)
        blocked=[d for d in self.lab.state['diagnostics'] if d['arm']=='A'][-1]
        self.assertEqual(blocked['reason'],'storage_entry_inhibited')
        self.assertTrue(self.lab.state['storage_entry_inhibited'])

    def test_inactive_state_does_not_auto_activate_or_create_accounts(self):
        from discovery_lab import DiscoveryLab
        with tempfile.TemporaryDirectory() as td:
            lab=DiscoveryLab(Path(td)/'inactive',LAB/'discovery_config_v1.json',self.instrument)
            try:
                self.assertFalse(lab.state['activated']);self.assertEqual(lab.brokers,{})
                self.assertIsNone(lab.state['start_ms']);self.assertIsNone(lab.state['deadline_ms'])
                with self.assertRaisesRegex(ValueError,'not operator-activated'):
                    lab.on_shared_event(dict(type='book',event_id='x',symbol='ETHUSDT',ts=1,
                                             source_ts=1,bids=[['1','1']],asks=[['2','1']],source_valid=True))
            finally:lab.close()

    def test_maker_gap_blocks_new_b_entries_until_explicit_forward_reconnect(self):
        from discovery_feed import SharedFeed
        feed=SharedFeed(self.root/'gap-feed.sqlite3',forward_start_ms=self.start)
        self.lab.bind_feed(feed);feed.mark_reconnected(next_trade_id=1,observed_ms=self.start)
        for arm,b in self.lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='fg:'+arm,symbol='ETHUSDT',
                            ts=self.start+1,source_ts=self.start+1,complete=True,valid_until_ts=self.start+100000))
        feed.ingest('book',dict(source_ts=self.start+5000,bids=[['100','1']],asks=[['100.1','2']]),
                    received_ms=self.start+5000)
        self.lab._route_signal(self.signal(target='105',sid='pre-gap'),self.start+5001)
        feed.ingest('book',dict(source_ts=self.start+7100,bids=[['100','1']],asks=[['100.1','2']]),
                    received_ms=self.start+7100)
        resting=self.lab.brokers['B'].orders['order:B:pre-gap']
        self.assertEqual(resting['status'],'RESTING')
        gap=feed.ingest('aggTrade',dict(trade_id=2,source_ts=self.start+7200,price='99',qty='1',aggressor='SELL'),
                        received_ms=self.start+7201)
        self.assertFalse(gap['event']['source_valid'])
        stress=self.lab.report(self.start+7201)['arms']['B']['queue_stress_sensitivity'][0]
        self.assertEqual(stress['stress_2x']['status'],'UNKNOWN_SOURCE_GAP')
        self.assertEqual(D(stress['stress_2x']['fill_qty']),D(0))
        self.assertEqual(resting['status'],'CANCELED')
        self.assertEqual(resting['reason'],'maker_aggtrade_unknown')
        self.lab._route_signal(self.signal(target='105',sid='gap-signal'),self.start+7300)
        bdiag=[d for d in self.lab.state['diagnostics'] if d['arm']=='B' and d['signal_id']=='gap-signal'][0]
        self.assertEqual(bdiag['reason'],'maker_aggtrade_unknown')
        self.assertNotIn('order:B:gap-signal',self.lab.brokers['B'].orders)
        feed.mark_reconnected(next_trade_id=3,observed_ms=self.start+7400)
        self.lab._route_signal(self.signal(target='105',sid='after-reconnect'),self.start+7500)
        bdiag=[d for d in self.lab.state['diagnostics'] if d['arm']=='B' and d['signal_id']=='after-reconnect'][0]
        self.assertEqual(bdiag['status'],'submitted')

    def test_b_checkpoint_counts_unique_actual_maker_filled_entry_orders_only(self):
        for i in range(432):
            self.lab.state['coverage'][str(self.start+(i+1)*60000)]=dict(timely=True,source_valid=True)
        b=self.lab.brokers['B']
        for i in range(3):
            oid='order:synthetic-'+str(i)
            b.orders[oid]=dict(order_id=oid,status='FILLED',intent=dict(reduce_only=False,kind='MAKER'))
            b.fills.append(dict(order_id=oid,liquidity='MAKER',ts=self.start+1000+i))
        now=self.lab.state['checkpoint_ms']
        self.assertEqual(self.lab.checkpoint('B',now)['status'],'throughput_infeasible')
        oid='order:synthetic-3'
        b.orders[oid]=dict(order_id=oid,status='FILLED',intent=dict(reduce_only=False,kind='MAKER'))
        b.fills.append(dict(order_id=oid,liquidity='MAKER',ts=self.start+1004))
        self.assertEqual(self.lab.checkpoint('B',now)['status'],'passed')
        oid='order:synthetic-late'
        b.orders[oid]=dict(order_id=oid,status='FILLED',intent=dict(reduce_only=False,kind='MAKER'))
        b.fills.append(dict(order_id=oid,liquidity='MAKER',ts=self.lab.state['checkpoint_ms']+1))
        self.assertEqual(self.lab.checkpoint('B',now)['throughput'],4)

    def test_c_checkpoint_reports_long_short_separately_and_ab_pairing(self):
        for i in range(432):
            self.lab.state['coverage'][str(self.start+(i+1)*60000)]=dict(timely=True,source_valid=True)
        for i,direction in enumerate(('long','long','short','short')):
            self.lab.state['diagnostics'].append(dict(arm='C',signal_id='c'+str(i),direction=direction,
                at_ms=self.start+100+i,cost_qualified=True,status='rejected',reason='synthetic-boundary'))
        for arm in ('A','B'):
            self.lab.state['diagnostics'].append(dict(arm=arm,signal_id='paired',direction='long',
                at_ms=self.start+200,cost_qualified=True,status='rejected',reason='synthetic-pair'))
        cp=self.lab.checkpoint('C',self.lab.state['checkpoint_ms'])
        self.assertEqual(cp['status'],'passed')
        self.assertEqual(cp['direction_cost_qualified'],{'long':2,'short':2})
        report=self.lab.report(self.start+1000)
        self.assertEqual(report['paired_ab'][0]['signal_id'],'paired')
        self.assertEqual(report['arms']['C']['direction_counts']['short']['cost_qualified'],2)

    def test_b_stop_cancels_resting_profit_before_reduce_only_taker(self):
        from discovery_feed import SharedFeed
        feed=SharedFeed(self.root/'race-feed.sqlite3',forward_start_ms=self.start)
        self.lab.bind_feed(feed);feed.mark_reconnected(next_trade_id=1,observed_ms=self.start)
        for arm,b in self.lab.brokers.items():
            b.on_event(dict(type='funding_status',event_id='fr:'+arm,symbol='ETHUSDT',
                            ts=self.start+1,source_ts=self.start+1,complete=True,valid_until_ts=self.start+100000))
        feed.ingest('book',dict(source_ts=self.start+5000,bids=[['100','0.1']],asks=[['100.1','2']]),
                    received_ms=self.start+5000)
        self.lab._route_signal(self.signal(target='105',sid='race'),self.start+5001)
        feed.ingest('book',dict(source_ts=self.start+7100,bids=[['100','0.1']],asks=[['100.1','2']]),
                    received_ms=self.start+7100)
        feed.ingest('aggTrade',dict(trade_id=1,source_ts=self.start+7200,price='99.99',qty='2',aggressor='SELL'),
                    received_ms=self.start+7201)
        b=self.lab.brokers['B'];self.assertIn('ETHUSDT',b.positions)
        profit=next(o for o in b.orders.values() if o['intent']['reduce_only'] and o['intent']['kind']=='MAKER')
        feed.ingest('book',dict(source_ts=self.start+9300,bids=[['100','1']],asks=[['100.1','2'],['105','1']]),
                    received_ms=self.start+9300)
        self.assertEqual(profit['status'],'RESTING')
        stop=D(b.positions['ETHUSDT']['stop'])
        feed.ingest('mark',dict(source_ts=self.start+9400,price=str(stop-D('.01'))),received_ms=self.start+9400)
        self.assertEqual(profit['status'],'CANCELED')
        stops=[o for o in b.orders.values() if o['intent']['reduce_only'] and o['intent']['kind']=='TAKER']
        self.assertTrue(stops)
        self.assertTrue(all(o['intent']['reduce_only'] for o in stops))
        feed.ingest('book',dict(source_ts=self.start+11500,bids=[['99','5']],asks=[['99.1','5']]),
                    received_ms=self.start+11500)
        self.assertNotIn('ETHUSDT',b.positions)
        exits=[f for f in b.fills if b.orders[f['order_id']]['intent']['reduce_only']]
        self.assertTrue(exits);self.assertTrue(all(f['liquidity']=='TAKER' for f in exits))
        self.assertEqual(self.lab.report(self.start+12000)['arms']['B']['flat_to_flat_count'],1)


class RetentionEvidenceTests(unittest.TestCase):
    def test_raw_rollover_keeps_compact_causal_reconstruction_and_storage_exit(self):
        from discovery_feed import SharedFeed
        from discovery_lab import DiscoveryLab
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'lab'
            instrument=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
                            qty_step='0.001',tick_size='0.01',min_notional='0.01',
                            max_qty='100',min_qty='0.001',category='crypto')
            lab=DiscoveryLab(root,LAB/'discovery_config_v1.json',instrument)
            start=1_000_000;lab.activate(at_ms=start,operator_accepted=True)
            feed=SharedFeed(root/'shared-feed.sqlite3',forward_start_ms=start,retention=3)
            lab.bind_feed(feed);feed.mark_reconnected(next_trade_id=1,observed_ms=start)
            try:
                for arm,b in lab.brokers.items():
                    b.on_event(dict(type='funding_status',event_id='fs:'+arm,symbol='ETHUSDT',
                                    ts=start+1,source_ts=start+1,complete=True,valid_until_ts=start+100000))
                feed.ingest('book',dict(source_ts=start+5000,bids=[['100','1']],asks=[['100.1','3']]),received_ms=start+5000)
                signal=dict(signal_id='retain',direction='long',target='105',features=dict(signal_close_ms=start+4000),symbol='ETHUSDT')
                lab._route_signal(signal,start+5001)
                feed.ingest('book',dict(source_ts=start+7100,bids=[['100','1']],asks=[['100.1','3']]),received_ms=start+7100)
                feed.ingest('aggTrade',dict(trade_id=1,source_ts=start+7200,price='99.99',qty='1.5',aggressor='SELL'),received_ms=start+7201)
                gap=feed.ingest('aggTrade',dict(trade_id=3,source_ts=start+7300,price='99.98',qty='1',aggressor='SELL'),received_ms=start+7301)
                self.assertFalse(gap['event']['source_valid'])
                for i in range(10):
                    feed.ingest('mark',dict(source_ts=start+8000+i,price='100'),received_ms=start+8000+i)
                snap=feed.snapshot()
                self.assertEqual(snap['retained_events'],3);self.assertGreater(snap['events_evicted'],0)
                self.assertIsNotNone(snap['retained_first_event_id']);self.assertIsNotNone(snap['retained_last_event_id'])
                keys={r['kind'] for r in lab.evidence.records()}
                self.assertTrue({'signal_decision','maker_queue_arrival','maker_trade_observation','fill','ledger','order_state','source_unknown'}<=keys)
                arrival=lab.evidence.records('maker_queue_arrival')[0]['payload']
                trade=lab.evidence.records('maker_trade_observation')[0]['payload']
                fill=next(r['payload'] for r in lab.evidence.records('fill') if r['payload']['arm']=='B')
                fee=next(r['payload'] for r in lab.evidence.records('ledger')
                         if r['payload']['arm']=='B' and r['payload']['type']=='fee')
                self.assertEqual(arrival['queue_1x'],'1');self.assertEqual(arrival['queue_2x'],'2')
                self.assertEqual(trade['aggressor'],'SELL');self.assertEqual(trade['price'],'99.99')
                self.assertEqual(fee['fill_id'],fill['fill_id'])
                canceled=[r['payload'] for r in lab.evidence.records('order_state')
                          if r['payload']['arm']=='B' and r['payload'].get('reason')=='maker_aggtrade_unknown']
                self.assertTrue(canceled)
                self.assertFalse(lab.evidence.summary()['automatic_pruning'])

                # Force the isolated storage gate after exposure exists; entry risk stops,
                # but a normal protective reduce-only exit still completes.
                pressure=root/'synthetic-storage-pressure.bin'
                with pressure.open('wb') as stream:
                    stream.truncate(int(lab.config['storage']['budget_bytes']*0.91))
                a=lab.brokers['A']
                if 'ETHUSDT' not in a.positions:
                    lab._route_signal(dict(signal_id='a-open',direction='long',target='105',
                                           features=dict(signal_close_ms=start+9000),symbol='ETHUSDT'),start+9001)
                    lab.on_shared_event(dict(type='book',event_id='a-fill-book',symbol='ETHUSDT',
                                             ts=start+11200,source_ts=start+11200,
                                             bids=[['100','5']],asks=[['100.1','5']],source_valid=True))
                self.assertIn('ETHUSDT',a.positions)
                stop=D(a.positions['ETHUSDT']['stop'])
                lab.on_shared_event(dict(type='mark',event_id='storage-stop-mark',symbol='ETHUSDT',
                                         ts=start+12000,source_ts=start+12000,price=str(stop-D('.01')),source_valid=True))
                lab.on_shared_event(dict(type='book',event_id='storage-exit-book',symbol='ETHUSDT',
                                         ts=start+14100,source_ts=start+14100,
                                         bids=[[str(stop-D('.02')),'5']],asks=[[str(stop-D('.01')),'5']],source_valid=True))
                self.assertNotIn('ETHUSDT',a.positions)
                self.assertTrue(lab.state['storage_entry_inhibited'])
                self.assertTrue(any(o['intent']['reduce_only'] and o['status']=='FILLED' for o in a.orders.values()))
            finally:lab.close()


class RunnerSubprocessTests(unittest.TestCase):
    @staticmethod
    def _iso(ms):
        from datetime import datetime,timezone
        return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat().replace('+00:00','Z')

    def _fixture(self,path):
        activation=3_960_000
        rows=[]
        first_open=300_000
        for i in range(61):
            o=first_open+i*60000
            rows.append([o,'100','100','100','100','10',o+59999])
        rows.append([3_960_000,'90','90','90','90','20',4_019_999])
        exchange=dict(received_at=self._iso(4_020_500),source_timestamp_ms=None,payload={'symbols':[{
            'symbol':'ETHUSDT','contractType':'PERPETUAL','status':'TRADING','quoteAsset':'USDT','marginAsset':'USDT',
            'filters':[{'filterType':'PRICE_FILTER','tickSize':'0.01'},
                       {'filterType':'LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'100'},
                       {'filterType':'MARKET_LOT_SIZE','stepSize':'0.001','minQty':'0.001','maxQty':'100'},
                       {'filterType':'MIN_NOTIONAL','notional':'0.01'}]}]})
        def depth(ts,bid='100',ask='100.1'):
            return dict(received_at=self._iso(ts+50),source_timestamp_ms=ts,
                        payload={'E':ts,'bids':[[bid,'1']], 'asks':[[ask,'3']]})
        def mark(ts):
            return dict(received_at=self._iso(ts+50),source_timestamp_ms=ts,
                        payload={'symbol':'ETHUSDT','time':ts,'markPrice':'100','nextFundingTime':32_800_000})
        data={'label':'PUBLIC-SHAPED SYNTHETIC TRANSPORT - NOT MARKET PERFORMANCE','responses':{
            '/fapi/v1/exchangeInfo':[exchange],
            '/fapi/v1/klines':[
                dict(received_at=self._iso(4_020_500),source_timestamp_ms=None,payload=rows,
                     fixture_match={'interval':'1m','limit':100}),
                dict(received_at=self._iso(4_020_500),source_timestamp_ms=None,payload=rows,
                     fixture_match={'interval':'1m','limit':1000}),
                dict(received_at=self._iso(4_023_000),source_timestamp_ms=None,payload=rows,
                     fixture_match={'interval':'1m','limit':1000})],
            '/fapi/v1/depth':[depth(4_020_100),depth(4_022_700)],
            '/fapi/v1/premiumIndex':[mark(4_020_200),mark(4_022_800)],
            '/fapi/v1/fundingInfo':[dict(received_at=self._iso(4_020_250),source_timestamp_ms=None,payload=[])],
            '/fapi/v1/fundingRate':[dict(received_at=self._iso(4_020_300),source_timestamp_ms=None,payload=[
                                        {'symbol':'ETHUSDT','fundingTime':4_000_000,'fundingRate':'0.0001','markPrice':'100','rateType':'Regular'}]),
                                    dict(received_at=self._iso(4_022_900),source_timestamp_ms=None,payload=[])],
            '/fapi/v1/aggTrades':[
                dict(received_at=self._iso(4_020_400),source_timestamp_ms=None,payload=[
                    {'a':10,'p':'99.99','q':'0.5','f':10,'l':10,'T':4_020_350,'m':True}]),
                dict(received_at=self._iso(4_022_950),source_timestamp_ms=None,payload=[
                    {'a':11,'p':'99.99','q':'2','f':11,'l':11,'T':4_022_940,'m':True}])]
        }}
        path.write_text(json.dumps(data))
        return activation

    def test_operator_cli_prepare_activate_run_report_stop_restart(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'runner';fixture=Path(td)/'transport.json';activation=self._fixture(fixture)
            base=[sys.executable,str(LAB/'discovery_runner.py'),'--root',str(root),
                  '--engineering-fixture','--transport-fixture',str(fixture)]
            def call(now,*cmd):
                result=subprocess.run(base+['--now-ms',str(now),*cmd],cwd=LAB,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
            prepared=call(3_950_000,'prepare')
            self.assertFalse(prepared['window_started'])
            self.assertFalse(json.loads((root/'runner_state.json').read_text()).get('activated_at_ms',False))
            activated=call(activation,'activate','--operator-accepted')
            self.assertEqual(activated['start_ms'],activation);self.assertEqual(activated['warmup_bars'],61)
            ran=call(4_020_500,'run','--once')
            self.assertEqual(ran['start_ms'],activation);self.assertEqual(ran['runner']['polls'],1)
            self.assertTrue((root/'reports/latest.json').is_file())
            report=call(4_021_000,'report')
            self.assertEqual(report['start_ms'],activation);deadline=report['deadline_ms']
            stopped=call(4_021_100,'stop')
            self.assertTrue(stopped['new_entries_will_stop']);self.assertFalse(stopped['kills_process'])
            # A new runner process must observe the external stop request, keep the
            # original window, cancel entry risk, and exit flat without replaying old data.
            fixture2=Path(td)/'transport-later.json'
            later=json.loads(fixture.read_text())
            for endpoint in ('/fapi/v1/depth','/fapi/v1/premiumIndex','/fapi/v1/fundingRate',
                             '/fapi/v1/aggTrades','/fapi/v1/klines'):
                later['responses'][endpoint]=[later['responses'][endpoint][-1]]
            fixture2.write_text(json.dumps(later))
            base2=[sys.executable,str(LAB/'discovery_runner.py'),'--root',str(root),
                   '--engineering-fixture','--transport-fixture',str(fixture2)]
            result=subprocess.run(base2+['--now-ms','4023000','run','--once'],cwd=LAB,
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr);restarted=json.loads(result.stdout)
            self.assertEqual(restarted['start_ms'],activation);self.assertEqual(restarted['deadline_ms'],deadline)
            self.assertTrue(restarted['operator_stop_requested'])
            self.assertEqual(restarted['runner']['polls'],1)
            self.assertTrue(restarted['runner']['stop_requested'])
            self.assertTrue(all(x['positions']==0 for x in restarted['arms'].values()))
            self.assertNotIn(str(Path(td)),json.dumps(restarted))
            checkpoint_ms=activation+28_800_000
            timed=call(checkpoint_ms,'report')
            self.assertEqual(timed['start_ms'],activation);self.assertEqual(timed['deadline_ms'],deadline)
            self.assertTrue((root/'reports/checkpoint-8h.json').is_file())
            final=call(deadline,'report')
            self.assertEqual(final['deadline_ms'],deadline)
            self.assertTrue((root/'reports/final-48h.json').is_file())


    def test_continuous_runner_completes_two_valid_cycles_same_root(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'runner';fixture=Path(td)/'transport.json';activation=self._fixture(fixture)
            base=[sys.executable,str(LAB/'discovery_runner.py'),'--root',str(root),
                  '--engineering-fixture','--transport-fixture',str(fixture)]
            def call(now,*cmd):
                result=subprocess.run(base+['--now-ms',str(now),*cmd],cwd=LAB,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
            call(3_950_000,'prepare');activated=call(activation,'activate','--operator-accepted')
            report=call(4_023_000,'run','--max-cycles','2','--poll-seconds','0')
            state=json.loads((root/'runner_state.json').read_text())
            durable=json.loads((root/'lab_state.json').read_text())
            self.assertEqual(report['runner']['polls'],2);self.assertEqual(state['polls'],2)
            self.assertIsNone(state['last_error']);self.assertFalse(state['stop_requested'])
            self.assertEqual((durable['start_ms'],durable['checkpoint_ms'],durable['deadline_ms']),
                             (activation,activated['checkpoint_ms'],activated['deadline_ms']))
            self.assertTrue((root/'reports/latest.json').is_file())

    def test_bounded_future_source_quarantine_preserves_raw_receipt(self):
        import sys
        sys.path.insert(0,str(LAB))
        import discovery_runner as dr
        from discovery_feed import SharedFeed
        with tempfile.TemporaryDirectory() as td:
            now=[1_000_000]
            runner=dr.DiscoveryRunner(Path(td)/'runner',client=object(),clock_ms=lambda:now[0])
            original_sleep=dr.time.sleep
            def advance(seconds):
                now[0]+=max(1,int(seconds*1000))
            dr.time.sleep=advance
            try:
                dispatch=runner._causal_dispatch_ms(1_000_232,1_000_000,'book')
            finally:
                dr.time.sleep=original_sleep
            self.assertGreaterEqual(dispatch,1_000_232)
            feed=SharedFeed(Path(td)/'feed.sqlite3',forward_start_ms=999_000,max_source_age_ms=15000)
            result=feed.ingest('book',dict(source_ts=1_000_232,receipt_ts=1_000_000,
                              bids=[['100','1']],asks=[['100.1','1']]),received_ms=dispatch)
            self.assertEqual(result['event']['source_ts'],1_000_232)
            self.assertEqual(result['event']['receipt_ts'],1_000_000)
            self.assertEqual(result['event']['ts'],dispatch)
            with self.assertRaisesRegex(ValueError,'too far in future'):
                runner._causal_dispatch_ms(1_006_000,1_000_000,'book')

    def test_real_subprocess_stop_is_sticky_across_slow_success_and_restart(self):
        import subprocess,sys,time as walltime
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'runner';fixture=Path(td)/'transport.json';activation=self._fixture(fixture)
            base=[sys.executable,str(LAB/'discovery_runner.py'),'--root',str(root),
                  '--engineering-fixture','--transport-fixture',str(fixture)]
            def call(now,*cmd):
                result=subprocess.run(base+['--now-ms',str(now),*cmd],cwd=LAB,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
            call(3_950_000,'prepare');activated=call(activation,'activate','--operator-accepted')
            data=json.loads(fixture.read_text())
            data['responses']['/fapi/v1/depth'][0]['fixture_delay_ms']=900
            fixture.write_text(json.dumps(data))
            pump=subprocess.Popen(base+['--now-ms','4020500','run','--max-cycles','3','--poll-seconds','0.05'],
                                  cwd=LAB,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            walltime.sleep(.2)
            stop=subprocess.run(base+['--now-ms','4020500','stop'],cwd=LAB,capture_output=True,text=True,timeout=10)
            self.assertEqual(stop.returncode,0,stop.stderr)
            out,err=pump.communicate(timeout=15)
            self.assertEqual(pump.returncode,0,err)
            report=json.loads(out)
            state=json.loads((root/'runner_state.json').read_text())
            durable=json.loads((root/'lab_state.json').read_text())
            self.assertTrue(state['stop_requested']);self.assertTrue(durable['operator_stop_requested'])
            self.assertTrue(report['runner']['stop_requested'])
            self.assertEqual(state['polls'],1)
            self.assertTrue(all(x['positions']==0 for x in report['arms'].values()))
            self.assertEqual(sum(x['submitted'] for x in report['arms'].values()),0)
            self.assertEqual((durable['start_ms'],durable['checkpoint_ms'],durable['deadline_ms']),
                             (activation,activated['checkpoint_ms'],activated['deadline_ms']))
            # Same-root restart must retain the completed stop and exit flat without
            # another public poll or clearing the request.
            restarted=call(4_021_000,'run','--once')
            self.assertTrue(restarted['runner']['stop_requested'])
            self.assertEqual(restarted['runner']['polls'],1)
            self.assertTrue(json.loads((root/'runner_state.json').read_text())['stop_requested'])

    def test_real_subprocess_stop_survives_slow_failure_retry(self):
        import subprocess,sys,time as walltime
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'runner';fixture=Path(td)/'transport.json';activation=self._fixture(fixture)
            base=[sys.executable,str(LAB/'discovery_runner.py'),'--root',str(root),
                  '--engineering-fixture','--transport-fixture',str(fixture)]
            def call(now,*cmd):
                result=subprocess.run(base+['--now-ms',str(now),*cmd],cwd=LAB,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr);return json.loads(result.stdout)
            call(3_950_000,'prepare');activated=call(activation,'activate','--operator-accepted')
            data=json.loads(fixture.read_text())
            bad=data['responses']['/fapi/v1/depth'][0]
            bad['source_timestamp_ms']=activation-1;bad['payload']['E']=activation-1
            bad['received_at']=self._iso(activation+50);bad['fixture_delay_ms']=500
            data['responses']['/fapi/v1/depth']=[bad]
            fixture.write_text(json.dumps(data))
            pump=subprocess.Popen(base+['--now-ms','4020500','run','--max-cycles','5','--poll-seconds','0.05'],
                                  cwd=LAB,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            walltime.sleep(.75)
            stop=subprocess.run(base+['--now-ms','4020500','stop'],cwd=LAB,capture_output=True,text=True,timeout=10)
            self.assertEqual(stop.returncode,0,stop.stderr)
            out,err=pump.communicate(timeout=15)
            self.assertEqual(pump.returncode,0,err)
            report=json.loads(out);state=json.loads((root/'runner_state.json').read_text())
            durable=json.loads((root/'lab_state.json').read_text())
            self.assertTrue(state['stop_requested']);self.assertTrue(durable['operator_stop_requested'])
            self.assertGreaterEqual(state['poll_failures'],1);self.assertEqual(state['polls'],0)
            self.assertTrue(report['runner']['stop_requested'])
            self.assertTrue(all(x['positions']==0 for x in report['arms'].values()))
            self.assertEqual((durable['start_ms'],durable['checkpoint_ms'],durable['deadline_ms']),
                             (activation,activated['checkpoint_ms'],activated['deadline_ms']))


class Issue34TransportCapacityTests(unittest.TestCase):
    def test_reference_dns_is_retryable_but_reference_validation_is_terminal(self):
        import sys
        from urllib.error import URLError
        sys.path.insert(0,str(LAB))
        import discovery_runner as dr
        self.assertTrue(dr.DiscoveryRunner._retryable_failure('reference',URLError('temporary DNS')))
        self.assertTrue(dr.DiscoveryRunner._retryable_failure('reference',TimeoutError('timeout')))
        self.assertFalse(dr.DiscoveryRunner._retryable_failure('reference',ValueError('filters changed')))
        self.assertTrue(dr.DiscoveryRunner._retryable_failure('shared',ValueError('invalid public receipt')))

    def test_strict_failure_persists_explicit_terminal_contract(self):
        import sys
        sys.path.insert(0,str(LAB))
        import discovery_runner as dr
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'runner'
            runner=dr.DiscoveryRunner(root,client=object(),clock_ms=lambda:1234)
            runner.state.update(last_error='URLError: temporary DNS',
                                last_failure_retryable=True,lifecycle='retryable_failure')
            runner._save()
            reports=root/'reports';reports.mkdir()
            (reports/'latest.json').write_text(json.dumps(dict(runner={})))
            runner._mark_terminal_failure('terminal_one_shot_failure')
            state=json.loads((root/'runner_state.json').read_text())
            report=json.loads((reports/'latest.json').read_text())
            self.assertTrue(state['terminal_blocked'])
            self.assertEqual(state['lifecycle'],'terminal_one_shot_failure')
            self.assertTrue(report['runner']['terminal_blocked'])
            self.assertTrue(report['runner']['retryable_source_failure'])

    def test_same_root_open_rolls_raw_window_down_and_compacts_without_losing_counters(self):
        import sqlite3,sys
        sys.path.insert(0,str(LAB))
        from discovery_feed import SharedFeed
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'feed.sqlite3'
            feed=SharedFeed(path,forward_start_ms=1000,retention=5)
            for i in range(5):
                ts=1100+i
                feed.ingest('book',dict(source_ts=ts,receipt_ts=ts,
                            bids=[['100','1']],asks=[['100.1','1']]),received_ms=ts)
            before=path.stat().st_size
            reopened=SharedFeed(path,forward_start_ms=1000,retention=3)
            snap=reopened.snapshot()
            self.assertEqual(snap['retained_events'],3)
            self.assertEqual(snap['events_persisted'],5)
            self.assertEqual(snap['events_evicted'],2)
            self.assertLessEqual(path.stat().st_size,before)
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM feed_events').fetchone()[0],3)


class Issue34CapacityHaltTests(unittest.TestCase):
    def test_flat_root_at_entry_stop_halts_before_public_poll(self):
        import sys
        sys.path.insert(0,str(LAB))
        import discovery_runner as dr
        with tempfile.TemporaryDirectory() as td:
            runner=dr.DiscoveryRunner(Path(td)/'runner',client=object(),clock_ms=lambda:2000)
            class Lab:
                state={'checkpoint_ms':999999,'deadline_ms':9999999}
                def report(self,now):
                    return dict(storage_used_bytes=90,storage_budget_bytes=100,
                                arms={a:dict(positions=0) for a in 'ABC'})
                def close(self):pass
            class Feed:
                def snapshot(self):return dict(retained_events=0)
            runner._open=lambda:(Lab(),Feed())
            report=runner._storage_capacity_halt_report()
            self.assertEqual(report['runner']['lifecycle'],'terminal_storage_capacity')
            self.assertTrue(report['runner']['terminal_blocked'])
            state=json.loads((runner.root/'runner_state.json').read_text())
            self.assertEqual(state['storage_halt_used_bytes'],90)
            self.assertTrue(state['terminal_blocked'])


class PipelineSubprocessTests(unittest.TestCase):
    def test_isolated_pipeline_subprocess_is_public_safe_and_no_live_claim(self):
        import subprocess,sys
        result=subprocess.run([sys.executable,str(LAB/'discovery_pipeline_check.py'),'--self-check'],
                              cwd=LAB,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        data=json.loads(result.stdout)
        self.assertEqual(data['label'],'ARTIFICIAL ENGINEERING PIPELINE - NOT MARKET PERFORMANCE')
        self.assertFalse(data['deployed']);self.assertFalse(data['live_orders'])
        self.assertFalse(data['default_account_touched'])
        self.assertEqual(data['shared_upstream_poll_cycles'],1)
        self.assertEqual(data['arm_ids'],['A','B','C'])
        self.assertGreaterEqual(data['b_maker_entry_fills'],1)
        self.assertNotIn('/home/',result.stdout)
        self.assertNotIn('C:\\',result.stdout)


if __name__=='__main__':
    unittest.main()
