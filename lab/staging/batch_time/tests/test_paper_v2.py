"""ARTIFICIAL engineering fixtures only, never market performance."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from contextlib import closing
from test_paper_runtime import FixtureClient, BASE, SCRATCH, LAB
import test_paper_runtime as contracts

class BootstrapClient(FixtureClient):
    def __init__(self):
        super().__init__()
        for i in range(-61, 0):
            t=BASE+i*300000
            self.bars.append([t,'100','100','100','100','10',t+299999,'0',0,'0','0','0'])
    def get(self, endpoint, params=None):
        result=super().get(endpoint,params)
        if endpoint.endswith('klines') and 'endTime' in (params or {}):
            result['payload']=[b for b in result['payload'] if b[0]<=params['endTime']]
        return result

class V2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=SCRATCH,prefix='v2-FIXTURE-')
        self.root=Path(self.tmp.name)
        self.client=BootstrapClient()
        self.r=None
    def tearDown(self):
        if self.r: self.r.close()
        self.tmp.cleanup()
    def open(self):
        self.assertIsNotNone(importlib.util.find_spec('paper_runtime_v2'),'staged bootstrap runtime missing')
        from paper_runtime_v2 import PaperRuntime
        self.r=PaperRuntime(self.root,LAB/'paper_config_v2.json',client=self.client,clock_ms=lambda:self.client.now,fixture=True)
        return self.r
    def fill_next(self,r):
        r.poll()
        self.client.closed_bar(0,'95','21')
        self.client.now=BASE+10000
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000; r.poll()
        self.client.now=BASE+301000
        self.client.price='95'; self.client.bid='94.99'; self.client.ask='95.01'
        r.poll(); self.client.now+=3000
        self.assertEqual(r.poll()['fills_count'],1)

    def test_parent_deployment_marker_persists_without_changing_account(self):
        r=self.open(); candidate=r.poll()
        self.assertTrue(candidate['candidate_not_deployed'])
        r.mark_deployed()
        deployed=r.snapshot()
        self.assertFalse(deployed['candidate_not_deployed'])
        self.assertEqual(deployed['engine']['candidate_implementation'],'paper-engine-v2')
        self.assertEqual(deployed['equity_usdt'],candidate['equity_usdt'])
        self.assertEqual(deployed['fills_count'],0)
        r.close(); self.r=None; r=self.open()
        self.assertFalse(r.snapshot()['candidate_not_deployed'])

    def test_failed_gap_bootstrap_does_not_block_position_reduction(self):
        r=self.open(); self.fill_next(r)
        self.client.now+=61000
        self.client.bars=[]
        s=r.poll()
        self.assertEqual(s['runtime_state'],'blocked')
        exits=[o for o in r.broker.orders.values() if o['intent']['reduce_only'] and o['status']=='PENDING']
        self.assertEqual(len(exits),1,'context failure must not prevent gap exit trigger')
        self.client.now+=3000
        s=r.poll()
        self.assertEqual(s['positions'],[],'later real book must reduce even while context unavailable')
        self.assertEqual(s['fills_count'],2)

    def test_bar_gap_requires_new_cutoff_context_only_rebuild(self):
        r=self.open(); r.poll()
        initial=r.state['forward_start_ms']
        # Missing closed candle in forward response, while transport heartbeat stays alive.
        self.client.closed_bar(1,'95','21')
        self.client.now=BASE+10000
        while self.client.now+50000<BASE+601000:
            self.client.now+=50000; r.poll()
        self.client.now=BASE+601000
        s=r.poll()
        self.assertGreater(r.state.get('decision_cutoff_ms',initial),initial)
        self.assertEqual(s['signals_count'],0)
        self.assertEqual(s['fills_count'],0)
        self.client.bars.insert(61,[BASE,'100','100','100','100','10',BASE+299999,'0',0,'0','0','0'])
        s=r.poll()
        self.assertIsNone(s['latest_error'])
        self.assertEqual(s['engine']['warmup_received'],61)
        self.assertEqual(s['signals_count'],0)
        self.assertEqual(s['engine']['indicator_context']['decision_cutoff_ms'],r.state['decision_cutoff_ms'])

    def test_reserved_live_namespace_refused_before_any_write(self):
        self.assertIsNotNone(importlib.util.find_spec('paper_runtime_v2'))
        from paper_runtime_v2 import PaperRuntime, publish
        with self.assertRaisesRegex(ValueError,'reserved live'):
            PaperRuntime(LAB/'data'/'paper',LAB/'paper_config_v2.json')
        with self.assertRaisesRegex(ValueError,'reserved live'):
            publish({},LAB/'shared'/'paper_status.json')

    def test_bootstrap_ready_only_next_closed_bar_then_delayed_fill(self):
        r=self.open()
        s=r.poll()
        self.assertIsNone(s['latest_error'])
        self.assertEqual(s['engine']['warmup_received'],61)
        self.assertTrue(s['paper_trading_enabled'])
        self.assertTrue(s['candidate_not_deployed'])
        self.assertEqual(s['signals_count'],0)
        self.assertEqual(s['fills_count'],0)
        self.assertEqual(s['equity_usdt'],'100')
        self.assertEqual(len(s['equity_history']),1)
        self.client.closed_bar(0,'95','21')
        # Genuine continuous heartbeat avoids deliberately triggering gap recovery.
        self.client.now=BASE+10000
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000
            r.poll()
        self.client.now=BASE+301000
        self.client.price='95'; self.client.bid='94.99'; self.client.ask='95.01'
        s=r.poll()
        self.assertEqual(s['signals_count'],1)
        self.assertEqual(s['fills_count'],0)
        with closing(sqlite3.connect(self.root/'signals.sqlite3')) as db:
            signal=json.loads(db.execute('SELECT intent FROM h1_signals').fetchone()[0])
        self.assertEqual(signal['features']['prior_closes'],['100']*61)
        self.assertEqual(signal['features']['current_close'],'95')
        self.client.price='95'; self.client.bid='94.99'; self.client.ask='95.01'
        self.client.now+=3000
        s=r.poll()
        self.assertEqual(s['fills_count'],1)
        self.assertFalse(s['live_trading_enabled'])
        self.assertEqual(Decimal(s['cash_usdt']),Decimal('100')-Decimal(s['fees_usdt']))

    def test_actual_public_clock_skew_uses_real_delivery_time_not_forged_source(self):
        from paper_market import book_event, receipt_ms
        record=json.loads((LAB/'evidence/v2_timestamp_failure_actual.json').read_text())
        receipt=next(x['receipt'] for x in record['receipts'] if x['receipt']['endpoint']=='/fapi/v1/depth' and x['receipt']['params']['symbol']=='ETHUSDT')
        event=book_event(receipt,'ETHUSDT',15000)
        self.assertGreater(event['ts_ms'],event['observed_ms'],'actual receipt reproduces negative local age')
        r=self.open(); r.poll()
        self.client.now=record['error']['at_ms']
        r.emit(dict(type='book',symbol='ETHUSDT',ts=event['observed_ms'],source_ts=event['ts_ms'],bids=event['bids'],asks=event['asks']))
        saved=list(r.broker.seen_events.values())[-1]
        self.assertEqual(saved['source_ts'],event['ts_ms'])
        self.assertEqual(saved['received_ms'],receipt_ms(receipt))
        self.assertEqual(saved['ts'],self.client.now)
        self.assertEqual(r.broker.fills,[])
        for source in (self.client.now+1,self.client.now-15001):
            with self.assertRaises(ValueError):
                r.emit(dict(type='mark',symbol='ETHUSDT',ts=self.client.now,source_ts=source,price='100'))

    def test_exact_boundary_activation_still_waits_next_closed_bar(self):
        self.client.now=BASE
        t=BASE-62*300000
        self.client.bars.insert(0,[t,'100','100','100','100','10',t+299999,'0',0,'0','0','0'])
        r=self.open(); s=r.poll()
        self.assertIsNone(s['latest_error'])
        self.assertEqual(s['signals_count'],0)
        self.assertLess(s['engine']['indicator_context']['context_end_ms'],BASE)
        self.client.closed_bar(0,'95','21')
        self.client.now=BASE
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000; r.poll()
        self.client.now=BASE+301000
        self.client.price='95';self.client.bid='94.99';self.client.ask='95.01'
        self.assertEqual(r.poll()['signals_count'],1)

    def test_restart_does_not_fetch_or_change_historical_baseline(self):
        r=self.open(); r.poll()
        manifest=r.detector.manifest()
        with closing(sqlite3.connect(self.root/'signals.sqlite3')) as db:
            baseline=db.execute('SELECT bars FROM h1_state').fetchone()[0]
        r.close(); self.client.calls=[]
        r=self.open(); s=r.poll()
        self.assertEqual(r.detector.manifest(),manifest)
        self.assertFalse(any('endTime' in p for e,p in self.client.calls))
        with closing(sqlite3.connect(self.root/'signals.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT bars FROM h1_state').fetchone()[0],baseline)
        self.assertEqual(s['equity_usdt'],'100')

    def test_stale_discontinuous_and_unclosed_history_fail_closed(self):
        for mode in ('missing','discontinuous','unclosed','stale_receipt','invalid_volume'):
            with self.subTest(mode=mode):
                if self.r: self.r.close()
                self.root=Path(self.tmp.name)/mode
                self.client=BootstrapClient()
                if mode=='missing': self.client.bars.pop()
                if mode=='discontinuous': self.client.bars[10][0]+=300000
                if mode=='unclosed': self.client.bars[-1][6]+=300000
                if mode=='invalid_volume': self.client.bars[-1][5]='-1'
                original=self.client.get
                def get(e,p=None):
                    receipt=original(e,p)
                    if mode=='stale_receipt' and 'endTime' in (p or {}):
                        from test_paper_runtime import iso
                        receipt['received_at']=iso(self.client.now-16000)
                    return receipt
                self.client.get=get
                s=self.open().poll()
                self.assertIsNotNone(s['latest_error'])
                self.assertFalse(s['paper_trading_enabled'])
                self.assertEqual(s['signals_count'],0)
                self.assertEqual(s['fills_count'],0)
                self.assertEqual(s['equity_usdt'],'100')
                self.assertIsNone(self.r.detector.manifest())

    def test_preactivation_and_current_unclosed_never_intents(self):
        from datetime import datetime,timezone
        r=self.open(); r.poll()
        bar=dict(symbol='ETHUSDT',open_time_ms=BASE-300000,close_time_ms=BASE,closed=True,open=Decimal('95'),high=Decimal('95'),low=Decimal('95'),close=Decimal('95'),volume=Decimal('21'))
        now=datetime.fromtimestamp(self.client.now/1000,timezone.utc)
        self.assertEqual(r.detector.process(bar,now=now)['diagnostic'],'reject_before_decision_cutoff')
        bar.update(open_time_ms=BASE,close_time_ms=BASE+300000)
        self.assertEqual(r.detector.process(bar,now=now)['diagnostic'],'reject_future')
        bar['closed']=False
        self.assertEqual(r.detector.process(bar,now=now)['diagnostic'],'reject_invalid_bar')
        with closing(sqlite3.connect(self.root/'signals.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM h1_signals').fetchone()[0],0)

    def test_historical_trigger_is_not_performance_or_intent(self):
        self.client.bars[-1][1:6]=['95','95','95','95','21']
        s=self.open().poll()
        self.assertIsNone(s['latest_error'])
        self.assertEqual(s['signals_count'],0)
        self.assertEqual(s['fills_count'],0)
        self.assertEqual(len(s['equity_history']),1)
        self.assertEqual(self.r.broker.ledger,[])


class V2UnchangedContracts(unittest.TestCase):
    """Exercise original broker/risk/data tests against staged runtime, not old code."""
    setUp=contracts.RuntimeTests.setUp
    tearDown=contracts.RuntimeTests.tearDown
    def open(self):
        from paper_runtime_v2 import PaperRuntime
        if not isinstance(self.client,BootstrapClient):
            old=self.client
            self.client=BootstrapClient()
            for k,v in old.__dict__.items():
                if k not in ('bars','calls'): setattr(self.client,k,v)
        self.config=LAB/'paper_config_v2.json'
        self.runtime=PaperRuntime(self.root,self.config,client=self.client,clock_ms=lambda:self.client.now,fixture=True)
        return self.runtime
    def warm_signal(self,r):
        self.client.closed_bar(0,'95','21')
        self.client.now=BASE+10000
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000; r.poll()
        self.client.now=BASE+301000
        self.client.price='95'; self.client.bid='94.99'; self.client.ask='95.01'
        return r.poll()
    test_broker_arrival_rejections_are_visible_in_snapshot=contracts.RuntimeTests.test_broker_arrival_rejections_are_visible_in_snapshot
    test_changed_funding_deadline_blocks_unverified_schedule=contracts.RuntimeTests.test_changed_funding_deadline_blocks_unverified_schedule
    def test_quote_age_rechecked_after_slow_kline_request(self):
        # Original gate denied old inputs. The engineering repair may refresh,
        # but must still deny if those actual refreshed inputs remain stale.
        self.client.kline_delay_ms=16000
        r=self.open()
        original=self.client.get
        def get(endpoint, params=None):
            receipt=original(endpoint,params)
            if endpoint.endswith('klines'):
                self.client.stale=True
            return receipt
        self.client.get=get
        s=r.poll()
        self.assertEqual(s['runtime_state'],'blocked')
        self.assertIn('stale',s['latest_error'])
        self.assertEqual(s['fills_count'],0)
        self.assertEqual(s['signals_count'],0)
    test_corrupt_persisted_cash_cannot_become_fictitious_pnl=contracts.RuntimeTests.test_corrupt_persisted_cash_cannot_become_fictitious_pnl
    def test_frozen_config_rejects_relaxed_rule_risk_and_leverage(self):
        from paper_runtime_v2 import PaperRuntime
        for field in ('max_daily_loss_usdt','paper_leverage','strategy'):
            c=json.loads((LAB/'paper_config_v2.json').read_text())
            if field=='strategy': c[field]['downside_sigma']='1'
            else: c[field]='4'
            path=self.root/'changed.json';path.write_text(json.dumps(c))
            with self.assertRaises(ValueError):
                PaperRuntime(self.root/field,path,client=self.client,clock_ms=lambda:self.client.now,fixture=True)
    def test_all_exit_reasons_use_delayed_actual_book_under_staged_runtime(self):
        import shutil
        from paper_runtime_v2 import PaperRuntime
        r=self.open(); r.poll();self.warm_signal(r);self.client.now+=3000
        self.assertEqual(r.poll()['fills_count'],1)
        entered=self.client.now
        r.close()
        seed=self.root/'seed';seed.mkdir()
        for p in self.root.glob('*.sqlite3'): shutil.copy2(p,seed/p.name)
        for reason,price,elapsed in [('stop','94',1000),('take_profit','100',1000),('max_hold','95',1800001),('data_gap','95',61000)]:
            with self.subTest(reason=reason):
                branch=self.root/reason;shutil.copytree(seed,branch)
                self.client.now=entered
                self.client.price='95';self.client.bid='94.99';self.client.ask='95.01'
                r=PaperRuntime(branch,self.config,client=self.client,clock_ms=lambda:self.client.now,fixture=True)
                self.runtime=r
                while reason!='data_gap' and self.client.now+50000<entered+elapsed:
                    self.client.now+=50000;r.poll()
                self.client.now=entered+elapsed
                self.client.price=price; self.client.bid=str(Decimal(price)-Decimal('0.01'));self.client.ask=str(Decimal(price)+Decimal('0.01'))
                s=r.poll()
                self.assertEqual(s['fills_count'],1)
                pending=[o for o in r.broker.orders.values() if o['intent']['reduce_only'] and o['status']=='PENDING']
                self.assertEqual(len(pending),1)
                self.client.now+=3000
                s=r.poll()
                self.assertEqual(s['positions'],[])
                self.assertEqual(s['fills_count'],2)
                self.assertEqual(s['fills'][-1]['observed_price'],self.client.bid)
                self.assertEqual(Decimal(s['fills'][-1]['price']),Decimal(self.client.bid)-Decimal('0.02'))
                self.assertEqual(Decimal(s['cash_usdt']),Decimal('100')+Decimal(s['gross_realized_pnl_usdt'])-Decimal(s['fees_usdt'])+Decimal(s['funding_pnl_usdt']))
                r.close()
    def test_gap_cancels_unfilled_entry_before_resume_book(self):
        r=self.open();r.poll();self.warm_signal(r)
        self.assertEqual(len(r.broker.orders),1)
        self.client.now+=61000
        s=r.poll()
        self.assertEqual(s['fills_count'],0)
        self.assertTrue(all(o['status']=='CANCELED' for o in r.broker.orders.values()))
    def test_risk_halt_and_later_real_book_exit_persist_across_restart(self):
        r=self.open(); r.poll(); self.warm_signal(r)
        self.client.now+=3000
        self.assertEqual(r.poll()['fills_count'],1)
        self.client.now+=1000
        self.client.price='85'; self.client.bid='84.99';self.client.ask='85.01'
        s=r.poll()
        self.assertIn('risk_total_loss',s['blockers'])
        self.client.now+=3000
        s=r.poll()
        self.assertEqual(s['positions'],[])
        self.assertIn('risk_daily_loss',s['blockers'])
        self.assertLess(Decimal(s['cash_usdt']),Decimal('90'))
        r.close();r=self.open()
        self.assertIn('risk_total_loss',r.poll()['blockers'])

