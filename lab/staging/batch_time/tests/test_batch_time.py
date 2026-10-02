"""Isolated ARTIFICIAL replay of measured skew, never performance."""
import json,math,unittest
from pathlib import Path
import test_paper_v2 as v2
from test_paper_runtime import LAB,iso,BASE

class BatchTimeTests(unittest.TestCase):
    setUp=v2.V2Tests.setUp
    tearDown=v2.V2Tests.tearDown
    fill_next=v2.V2Tests.fill_next
    def open(self):
        from paper_runtime_v2 import PaperRuntime
        self.r=PaperRuntime(self.root,LAB/'paper_config_v2.json',client=self.client,clock_ms=lambda:self.client.now,fixture=True)
        self.sleeps=[];self.mono=0.0
        def sleep(seconds):
            self.sleeps.append(seconds);self.mono+=seconds
            self.client.now+=math.ceil(seconds*1000)
        self.r.sleep=sleep;self.r.monotonic=lambda:self.mono
        return self.r
    def skew(self,delta):
        original=self.client.get
        def get(e,p=None):
            q=original(e,p)
            if e.endswith('premiumIndex') and (p or {}).get('symbol')=='ETHUSDT':
                q['source_timestamp_ms']+=delta;q['payload']['time']+=delta
            return q
        self.client.get=get
    def test_dispatch_and_request_timing_have_actual_clock_provenance(self):
        r=self.open();s=r.poll()
        self.assertIsNone(s['latest_error'])
        rows=[json.loads(x[0]) for x in r.db.execute('SELECT payload FROM audit')]
        requests=[x for x in rows if x['type']=='public_receipt']
        self.assertTrue(requests)
        for x in requests:
            self.assertIn('request_start_ms',x)
            self.assertGreaterEqual(x['request_return_monotonic_ms'],x['request_start_monotonic_ms'])
        dispatches=[x for x in rows if x['type']=='broker_dispatch']
        self.assertEqual(len(dispatches),2)
        for x in dispatches:
            self.assertLessEqual(x['source_ms'],x['dispatch_ms'])
            event=r.broker.seen_events[x['event_id']]
            self.assertEqual(x['received_ms'],event['received_ms'])
            self.assertEqual(x['dispatch_ms'],event['ts'])
        with self.assertRaisesRegex(ValueError,'future source.*ahead_ms=1'):
            r.emit(dict(type='mark',symbol='ETHUSDT',ts=self.client.now,source_ts=self.client.now+1,price='100'))
        with self.assertRaisesRegex(ValueError,'stale source.*age_ms=15001'):
            r.emit(dict(type='mark',symbol='ETHUSDT',ts=self.client.now,source_ts=self.client.now-15001,price='100'))

    def test_real_wall_clock_wait_does_not_admit_future(self):
        import time
        r=self.open();r.clock=lambda:int(time.time()*1000)
        r.monotonic=time.monotonic;r.sleep=time.sleep
        source=r.clock()+30
        market=[dict(symbol='ETHUSDT',source_timestamps_ms=dict(premiumIndex=source))]
        before=time.monotonic()
        admitted=r.validate_sources(market,'actual local clock test',allow_wait=True)
        self.assertGreaterEqual(admitted,source)
        self.assertGreaterEqual(time.monotonic()-before,0.02)
        self.assertLess(time.monotonic()-before,2.2)

    def test_persistent_future_stalled_wall_and_large_skew_fail_closed(self):
        r=self.open();now=self.client.now
        for delta in (2001,5001):
            with self.assertRaisesRegex(ValueError,'future source'):
                r.validate_sources([dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=now+delta))],'batch',allow_wait=True)
        self.assertEqual(self.sleeps,[])
        def stalled(seconds):
            self.sleeps.append(seconds);self.mono+=seconds
        r.sleep=stalled
        with self.assertRaisesRegex(ValueError,'future source'):
            r.validate_sources([dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=now+61))],'batch',allow_wait=True)
        self.assertLessEqual(sum(self.sleeps),2.000001)
        self.assertEqual(r.broker,None)

    def test_wait_rechecks_every_source_and_rejects_newly_stale_book(self):
        r=self.open();now=self.client.now
        market=[dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=now-14990,premiumIndex=now+61))]
        with self.assertRaisesRegex(ValueError,'stale source ETHUSDT/depth5'):
            r.validate_sources(market,'batch',allow_wait=True)
        self.assertEqual(r.broker,None)
        self.assertGreater(self.client.now,now)

    def test_source_age_boundaries_and_sleep_overshoot_fail_closed(self):
        r=self.open();now=self.client.now
        r.validate_sources([dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=now-15000))],'batch')
        with self.assertRaisesRegex(ValueError,'stale source.*age_ms=15001'):
            r.validate_sources([dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=now-15001))],'batch',allow_wait=True)
        with self.assertRaisesRegex(ValueError,'invalid source'):
            r.validate_sources([dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=True))],'batch',allow_wait=True)
        with self.assertRaisesRegex(ValueError,'future source'):
            r.validate_sources([dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=now+1))],'batch')
        def overshoot(seconds):
            self.mono+=3;self.client.now+=3000
        r.sleep=overshoot
        with self.assertRaisesRegex(ValueError,'wait budget exceeded'):
            r.validate_sources([dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=now+1))],'batch',allow_wait=True)

    def test_waited_book_still_requires_source_after_order_arrival(self):
        r=self.open();r.poll()
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000;r.poll()
        self.client.closed_bar(0,'95','21')
        self.client.price='95';self.client.bid='94.99';self.client.ask='95.01'
        r.poll();self.assertEqual(len(r.broker.orders),1)
        arrival=next(iter(r.broker.orders.values()))['arrival_ts']
        original=self.client.get
        def get(e,p=None):
            q=original(e,p)
            if e.endswith('depth') and (p or {}).get('symbol')=='ETHUSDT':
                q['source_timestamp_ms']=arrival;q['payload']['E']=arrival
            return q
        self.client.get=get;self.client.now=arrival-61
        self.assertEqual(r.poll()['fills_count'],0,'source exactly at arrival still cannot fill after waiting')
        self.assertGreaterEqual(self.client.now,arrival)
        self.client.get=original;self.client.now+=1
        s=r.poll();self.assertEqual(s['fills_count'],1)
        self.assertGreater(s['fills'][0]['ts'],arrival)
        self.assertEqual(r.poll()['fills_count'],1)
        cash=r.broker.cash;forward=r.state['forward_start_ms'];r.close();self.r=None
        r=self.open();self.assertEqual(r.poll()['fills_count'],1)
        self.assertEqual(r.broker.cash,cash);self.assertEqual(r.state['forward_start_ms'],forward)

    def test_future_eth_exit_precedes_unavailable_xau_and_is_not_duplicated(self):
        r=self.open();self.fill_next(r)
        self.client.now+=61000
        self.skew(61);original=self.client.get
        def get(e,p=None):
            if (p or {}).get('symbol')=='XAUUSDT':raise OSError('ARTIFICIAL unrelated XAU failure')
            return original(e,p)
        self.client.get=get
        s=r.poll();self.assertIsNotNone(s['latest_error'])
        pending=[o for o in r.broker.orders.values() if o['intent']['reduce_only'] and o['status']=='PENDING']
        self.assertEqual(len(pending),1)
        self.client.now+=3000;s=r.poll()
        self.assertEqual(s['positions'],[]);self.assertEqual(s['fills_count'],2)
        self.assertEqual(r.poll()['fills_count'],2)

    def test_stop_and_max_hold_keep_delayed_book_execution_with_future_mark(self):
        import shutil
        from paper_runtime_v2 import PaperRuntime
        r=self.open();self.fill_next(r);entered=self.client.now
        r.close();self.r=None
        seed=self.root/'seed';seed.mkdir()
        for p in self.root.glob('*.sqlite3'):shutil.copy2(p,seed/p.name)
        for reason,price,elapsed in [('stop','94',1000),('max_hold','95',1800001)]:
            with self.subTest(reason=reason):
                branch=self.root/reason;shutil.copytree(seed,branch)
                self.client.now=entered
                r=PaperRuntime(branch,LAB/'paper_config_v2.json',client=self.client,clock_ms=lambda:self.client.now,sleep=lambda seconds:setattr(self.client,'now',self.client.now+math.ceil(seconds*1000)),monotonic=lambda:0.0,fixture=True)
                self.r=r
                while self.client.now+50000<entered+elapsed:
                    self.client.now+=50000;r.poll()
                original=self.client.get;self.skew(61)
                self.client.now=entered+elapsed;self.client.price=price
                from decimal import Decimal
                self.client.bid=str(Decimal(price)-Decimal('0.01'));self.client.ask=str(Decimal(price)+Decimal('0.01'))
                s=r.poll();self.assertEqual(s['fills_count'],1)
                exits=[o for o in r.broker.orders.values() if o['intent']['reduce_only'] and o['status']=='PENDING']
                self.assertEqual(len(exits),1)
                self.client.now+=3000;s=r.poll()
                self.assertEqual(s['positions'],[]);self.assertEqual(s['fills_count'],2)
                self.assertEqual(s['fills'][-1]['observed_price'],self.client.bid)
                self.assertEqual(r.poll()['fills_count'],2)
                r.close();self.r=None;self.client.get=original
                self.client.price='95';self.client.bid='94.99';self.client.ask='95.01'

    def test_validation_failure_records_exact_future_or_old_evidence(self):
        r=self.open();now=self.client.now
        for delta,label in ((2001,'future'),(-15001,'stale')):
            with self.assertRaisesRegex(ValueError,label+' source'):
                r.validate_sources([dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=now+delta))],'batch',allow_wait=True)
            row=r.db.execute('SELECT payload FROM audit ORDER BY id DESC LIMIT 1').fetchone()
            self.assertIsNotNone(row,'exact validation evidence is missing')
            event=json.loads(row[0])
            self.assertEqual(event['type'],'source_validation')
            self.assertEqual(event['validation_ms'],now)
            self.assertEqual(event['sources'][0]['source_ms'],now+delta)
            self.assertIn(label+' source',event['error'])

    def test_measured_future_becomes_valid_only_after_actual_clock_reaches_source(self):
        record=json.loads((LAB/'evidence/batch_time_actual_failure.json').read_text())
        source=record[-2]['receipt']['source_timestamp_ms'];validation_upper=record[-1]['at_ms']
        delta=source-validation_upper
        self.assertEqual(delta,61)
        r=self.open();self.skew(delta)
        s=r.poll()
        self.assertIsNone(s['latest_error'],'actual measured +61ms must wait, not reject or admit future')
        self.assertGreaterEqual(sum(self.sleeps)*1000,delta)
        events=[e for e in r.broker.seen_events.values() if e['type']=='mark']
        self.assertTrue(events)
        e=events[0];self.assertLessEqual(e['source_ts'],e['ts'])
        self.assertEqual(e['source_ts']-e['received_ms'],delta)
        self.assertEqual(s['fills_count'],0)
        self.assertEqual(r.config['execution_model']['max_source_age_ms'],15000)
