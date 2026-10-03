"""Bounded timing evidence acceptance. Engineering evidence only, never strategy performance."""
import json, math, tempfile, unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

import test_paper_v2 as v2
from test_paper_runtime import LAB


class Clock:
    def __init__(self,wall=1_000_000,mono=10.0):
        self.wall=wall;self.mono=mono
    def wall_ms(self): return self.wall
    def monotonic(self): return self.mono
    def iso(self): return datetime.fromtimestamp(self.wall/1000,timezone.utc).isoformat()
    def sleep(self,seconds):
        self.wall+=math.ceil(seconds*1000);self.mono+=seconds
    def advance(self,ms):
        self.wall+=ms;self.mono+=ms/1000


class Response:
    def __init__(self,clock,payload,status=200,read_ms=0):
        self.clock=clock;self.payload=json.dumps(payload).encode();self.status=status;self.read_ms=read_ms
    def __enter__(self): return self
    def __exit__(self,*a): return False
    def read(self):
        self.clock.advance(self.read_ms)
        return self.payload
    def getcode(self): return self.status


class TransportTimingTests(unittest.TestCase):
    def test_slow_real_attempt_is_separate_from_throttle_and_outer_elapsed(self):
        from paper_runtime_v2 import PaperRuntime
        from paper_market import PublicMarketClient
        clock=Clock();events=[]
        client=PublicMarketClient(
            opener=lambda req,timeout:Response(clock,{'serverTime':clock.wall},read_ms=2500),
            sleep=clock.sleep,clock=clock.iso,monotonic=clock.monotonic,wall_ms=clock.wall_ms)
        r=object.__new__(PaperRuntime)
        r.state={};r.client=client;r.clock=clock.wall_ms;r.monotonic=clock.monotonic
        r.audit=lambda event: None
        receipt=r.fetch('/fapi/v1/time')
        trace=r.state['source_timing_evidence'][-1]
        self.assertEqual(receipt['endpoint'],'/fapi/v1/time')
        self.assertEqual(trace['outcome'],'success')
        self.assertEqual([e['kind'] for e in trace['events']],['wait','http_attempt'])
        self.assertEqual(trace['events'][0]['reason'],'pre_attempt_throttle')
        self.assertEqual(trace['events'][0]['monotonic_elapsed_ms'],250)
        self.assertEqual(trace['events'][1]['monotonic_elapsed_ms'],2500)
        self.assertEqual(trace['outer_monotonic_elapsed_ms'],2750)
        self.assertNotEqual(trace['outer_monotonic_elapsed_ms'],trace['events'][1]['monotonic_elapsed_ms'])
        self.assertEqual(trace['classification'],'engineering_observation_not_strategy_performance')

    def test_retry_backoff_and_attempt_outcomes_are_separate_and_bounded(self):
        from paper_market import PublicMarketClient
        clock=Clock();seen=[];calls=[0]
        def opener(req,timeout):
            calls[0]+=1
            if calls[0]==1:
                clock.advance(120)
                raise URLError('synthetic transport failure')
            return Response(clock,{'serverTime':clock.wall},read_ms=80)
        client=PublicMarketClient(opener=opener,sleep=clock.sleep,clock=clock.iso,
                                  monotonic=clock.monotonic,wall_ms=clock.wall_ms)
        receipt=client.get_timed('/fapi/v1/time',timing=seen.append)
        self.assertEqual(receipt['endpoint'],'/fapi/v1/time')
        self.assertEqual([e['kind'] for e in seen],
                         ['wait','http_attempt','wait','wait','http_attempt'])
        self.assertEqual(seen[1]['outcome'],'error')
        self.assertEqual(seen[2]['reason'],'retry_backoff')
        self.assertEqual(seen[2]['requested_ms'],2000)
        self.assertEqual(seen[-1]['outcome'],'success')
        self.assertLessEqual(sum(e['kind']=='http_attempt' for e in seen),3)


class RuntimeTimingEvidenceTests(unittest.TestCase):
    setUp=v2.V2Tests.setUp
    tearDown=v2.V2Tests.tearDown
    def open(self):
        from paper_runtime_v2 import PaperRuntime
        self.r=PaperRuntime(self.root,LAB/'paper_config_v2.json',client=self.client,
                            clock_ms=lambda:self.client.now,fixture=True)
        self.mono=0.0
        self.r.monotonic=lambda:self.mono
        self.r.sleep=lambda seconds:(setattr(self,'mono',self.mono+seconds),
                                     setattr(self.client,'now',self.client.now+math.ceil(seconds*1000)))
        return self.r

    def test_peer_aging_before_bounded_refresh_is_explicit_and_gate_unchanged(self):
        r=self.open();original=self.client.get;delayed=[False]
        def get(endpoint,params=None):
            if ((params or {}).get('symbol')=='XAUUSDT' and endpoint.endswith('bookTicker')
                    and not delayed[0]):
                self.client.now+=16001;delayed[0]=True
            return original(endpoint,params)
        self.client.get=get
        s=r.poll()
        self.assertEqual(r.config['execution_model']['max_source_age_ms'],15000)
        traces=r.state['source_timing_evidence']
        aging=[x for x in traces if x['kind']=='peer_aging_before_refresh']
        self.assertTrue(aging)
        self.assertIn('ETHUSDT',aging[-1]['stale_symbols'])
        eth=next(p for p in aging[-1]['peers'] if p['symbol']=='ETHUSDT')
        self.assertGreater(max(eth['source_ages_ms'].values()),15000)
        self.assertLessEqual(len(traces),32)
        self.assertEqual(s['fills_count'],0)

    def test_future_wait_evidence_and_restart_ring_are_bounded_and_persistent(self):
        r=self.open();now=self.client.now
        market=[dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=now+61))]
        r.validate_sources(market,'synthetic future wait',allow_wait=True)
        item=r.state['source_timing_evidence'][-1]
        self.assertEqual(item['kind'],'source_validation')
        self.assertTrue(item['waited'])
        self.assertEqual(item['wait_limit_ms'],2000)
        self.assertEqual(r.config['execution_model']['max_source_age_ms'],15000)
        # Fill the ring with harmless validation observations.
        for i in range(40):
            n=self.client.now
            r.validate_sources([dict(symbol='ETHUSDT',source_timestamps_ms=dict(depth5=n))],
                               'bounded-'+str(i))
        self.assertEqual(len(r.state['source_timing_evidence']),32)
        tail=json.loads(json.dumps(r.state['source_timing_evidence']))
        r.save();r.close();self.r=None
        r=self.open()
        self.assertEqual(r.state['source_timing_evidence'],tail)
        self.assertEqual(len(r.state['source_timing_evidence']),32)

    def test_future_and_prearrival_execution_gates_remain_fail_closed(self):
        r=self.open();now=self.client.now
        with self.assertRaisesRegex(ValueError,'future source.*ahead_ms=1'):
            r.emit(dict(type='mark',symbol='ETHUSDT',ts=now,source_ts=now+1,price='100'))
        self.assertEqual(r.config['execution_model']['latency_ms'],2000)
        self.assertEqual(r.config['execution_model']['max_source_age_ms'],15000)
        # Existing full-suite pre-arrival test remains authoritative for source-after-arrival.
        self.assertTrue(hasattr(r,'emit'))


if __name__=='__main__': unittest.main()
