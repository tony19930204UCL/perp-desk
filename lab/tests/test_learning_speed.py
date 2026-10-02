import unittest, json, tempfile, sqlite3
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal as D
LAB=Path(__file__).resolve().parents[1]
SCRATCH=Path('/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch')
class LearningSpeedTests(unittest.TestCase):
    def test_frozen_config_preserves_risk_cost_exit(self):
        p=LAB/'paper_config_v3.json'
        self.assertTrue(p.exists(), 'learning-speed preregistered configuration missing')
        old=json.loads((LAB/'paper_config_v2.json').read_text()); new=json.loads(p.read_text())
        for k in old:
            if k not in ('version_id','strategy','decision','indicator_bootstrap'):
                self.assertEqual(new[k],old[k],k)
        for k in ('stop_distance_fraction','take_profit_reference','max_holding_ms','minimum_gross_reward_to_estimated_cost'):
            self.assertEqual(new['strategy'][k],old['strategy'][k])
        self.assertEqual(new['strategy']['interval_ms'],60000)
        self.assertEqual(new['strategy']['downside_sigma'],'1.5')
        self.assertEqual(new['strategy']['volume_multiple'],'1.2')
        self.assertEqual(new['research']['target_complete_round_trips'],30)
        self.assertEqual(new['research']['window_ms'],172800000)
    def test_one_minute_forward_signal_and_restart_dedup(self):
        self.assertTrue((LAB/'signals_v3.py').exists(), 'one-minute detector missing')
        from signals_v3 import Detector
        with tempfile.TemporaryDirectory(dir=SCRATCH) as t:
            cutoff=120*60000+1000
            d=Detector(Path(t)/'signals.sqlite3','H1-PAPER-003',datetime.fromtimestamp(cutoff/1000,timezone.utc),{'ETHUSDT':'crypto'})
            end=120*60000
            rows=[[end-61*60000+i*60000,'100','100','100','100','10',end-61*60000+(i+1)*60000-1] for i in range(61)]
            receipt=dict(endpoint='/fapi/v1/klines',params=dict(symbol='ETHUSDT',interval='1m',startTime=end-61*60000,endTime=end-1,limit=61),received_at=datetime.fromtimestamp(cutoff/1000,timezone.utc).isoformat(),payload=rows)
            m=d.seed(receipt,cutoff_ms=cutoff,now_ms=cutoff)
            self.assertFalse(m['performance_sample'])
            with sqlite3.connect(d.path) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM h1_signals').fetchone()[0],0)
            bar=dict(symbol='ETHUSDT',open_time_ms=end,close_time_ms=end+60000,closed=True,open=D('100'),high=D('100'),low=D('99.7'),close=D('99.7'),volume=D('13'))
            r=d.process(bar,now=datetime.fromtimestamp((end+60001)/1000,timezone.utc))
            self.assertEqual(r['diagnostic'],'research_intent')
            self.assertEqual(r['intent']['features']['volume_threshold'],'12.0')
            d=Detector(d.path,'H1-PAPER-003',datetime.fromtimestamp(cutoff/1000,timezone.utc),{'ETHUSDT':'crypto'})
            self.assertEqual(d.process(bar,now=datetime.fromtimestamp((end+60001)/1000,timezone.utc))['diagnostic'],'duplicate')
    def test_runtime_restart_keeps_research_deadline_and_cumulative_account(self):
        self.assertTrue((LAB/'paper_runtime_v3.py').exists(), 'v3 reusable runtime missing')
        from paper_runtime_v3 import PaperRuntime
        with tempfile.TemporaryDirectory(dir=SCRATCH) as t:
            r=PaperRuntime(t,LAB/'paper_config_v3.json',clock_ms=lambda:123456789,fixture=True)
            s=r.snapshot(); r.close()
            self.assertEqual(s['research']['strategy_start_ms'],123456789)
            self.assertEqual(s['research']['deadline_ms'],296256789)
            self.assertEqual(s['research']['complete_round_trips'],0)
            self.assertTrue(s['candidate_not_deployed'])
            r=PaperRuntime(t,LAB/'paper_config_v3.json',clock_ms=lambda:123457789,fixture=True)
            self.assertEqual(r.snapshot()['research']['deadline_ms'],296256789)
            self.assertEqual(r.state['forward_start_ms'],123456789)
            r.close()
    def test_deadline_stops_entries_and_snapshot_counts_expiry(self):
        from paper_runtime_v3 import PaperRuntime
        with tempfile.TemporaryDirectory(dir=SCRATCH) as t:
            now=[100000000]
            r=PaperRuntime(t,LAB/'paper_config_v3.json',clock_ms=lambda:now[0],fixture=True)
            r.state['handled_signals']={'signal':dict(status='submitted')}
            r.state['strategy_signal_baseline']=[]
            now[0]+=172800001
            s=r.snapshot()
            self.assertIn('research_window_closed',s['blockers'])
            self.assertEqual(s['research']['signals_count'],1)
            self.assertEqual(s['research']['status'],'insufficient_samples')
            r.close()
    def test_migration_keeps_entire_losing_account_and_previous_outcomes(self):
        self.assertTrue((LAB/'paper_migrate_v3.py').exists(),'continuity migration missing')
        from paper_migrate_v3 import migrate
        from paper_runtime_v2 import PaperRuntime as OldRuntime
        from paper_runtime_v3 import PaperRuntime
        spec=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',qty_step='0.001',tick_size='0.01',min_notional='20',max_qty='2000',min_qty='0.001',category='crypto')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as t:
            state=Path(t)/'account'; backup=Path(t)/'backup'
            r=OldRuntime(state,LAB/'paper_config_v2.json',clock_ms=lambda:100000000,fixture=True)
            r.ensure_broker(spec)
            r.broker.cash=D('99'); r.broker.ledger=[dict(type='realized',amount='-1',ts=100000001)]
            r.broker.day_baselines={'1':'100'}; r.broker._save()
            r.state['total_halted']=True
            r.state['handled_signals']={'old':dict(status='rejected',reason='late_closed_bar_signal')}
            r.audit(dict(type='old_outcome',loss='1'));r.save();r.close()
            original=(state/'broker.sqlite3').read_bytes()
            report=migrate(state,LAB/'paper_config_v3.json',backup,now_ms=100100000)
            self.assertEqual((state/'broker.sqlite3').read_bytes(),original)
            self.assertEqual(report['cash_usdt'],'99')
            r=PaperRuntime(state,LAB/'paper_config_v3.json',clock_ms=lambda:100100001,fixture=True)
            # Restore account before any public request; even failed fetch must not show a fresh 100.
            s=r.snapshot()
            self.assertEqual(s['cash_usdt'],'99')
            self.assertEqual(s['total_pnl_usdt'],'-1')
            self.assertEqual(s['research']['net_cash_pnl_usdt'],'0')
            self.assertEqual(s['research']['signals_count'],0)
            self.assertEqual(r.state['forward_start_ms'],100000000)
            self.assertTrue(r.state['total_halted'])
            self.assertIn('old',r.state['handled_signals'])
            self.assertTrue(s['candidate_not_deployed'])
            r.close()
    def test_migration_refuses_running_runtime_without_writing_backup(self):
        from paper_migrate_v3 import migrate
        from paper_runtime_v2 import PaperRuntime
        spec=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',qty_step='0.001',tick_size='0.01',min_notional='20',max_qty='2000',min_qty='0.001',category='crypto')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as t:
            r=PaperRuntime(Path(t)/'account',LAB/'paper_config_v2.json',clock_ms=lambda:100000000,fixture=True)
            r.ensure_broker(spec)
            try:
                with self.assertRaisesRegex(RuntimeError,'locked'):
                    migrate(r.root,LAB/'paper_config_v3.json',Path(t)/'backup',now_ms=100100000)
                self.assertFalse((Path(t)/'backup').exists())
            finally:r.close()
    def test_acceptance_rejects_tampered_audit(self):
        self.assertTrue((LAB/'accept_learning_v3.py').exists(),'executable audit acceptance missing')
        from accept_learning_v3 import verify_audit
        with tempfile.TemporaryDirectory(dir=SCRATCH) as t:
            p=Path(t)/'runtime.sqlite3'
            with sqlite3.connect(p) as db:
                db.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY,payload TEXT,previous_hash TEXT,hash TEXT)')
                db.execute('INSERT INTO audit VALUES(1,?,?,?)',('{"type":"tampered"}','0'*64,'0'*64))
            with self.assertRaisesRegex(ValueError,'audit'):
                verify_audit(p)
    def test_acceptance_rejects_stale_snapshot_and_future_source(self):
        import accept_learning_v3 as a
        self.assertTrue(hasattr(a,'verify_freshness'),'read-time freshness gate missing')
        s=dict(updated_at=datetime.fromtimestamp(100,timezone.utc).isoformat(),markets=[dict(source_timestamps_ms={'depth':100000})])
        with self.assertRaisesRegex(ValueError,'snapshot'):
            a.verify_freshness(s,now_ms=160001)
        s['updated_at']=datetime.fromtimestamp(99,timezone.utc).isoformat()
        with self.assertRaisesRegex(ValueError,'source'):
            a.verify_freshness(s,now_ms=99999)
        a.verify_freshness(s,now_ms=100000)
    def test_daily_brief_includes_learning_costs_and_block_reasons(self):
        self.assertTrue((LAB/'brief_learning_v3.py').exists(),'learning brief missing')
        from brief_learning_v3 import render
        s=dict(engine=dict(version_id='H1-PAPER-003'),equity_usdt='99',total_pnl_usdt='-1',fees_usdt='0.2',funding_pnl_usdt='-0.01',blockers=['stale_source'],research=dict(complete_round_trips=2,signals_count=8,blocked_signals_count=3,rejection_categories={'reward_not_above_cost_floor':3},net_cash_pnl_usdt='-0.5',deadline_ms=172800000,status='unproven'))
        text=render(s)
        self.assertLessEqual(len(text.splitlines()),8)
        self.assertIn('2/30',text)
        self.assertIn('reward_not_above_cost_floor',text)
        self.assertIn('-0.5',text)
        self.assertIn('未證明',text)
    def test_migration_rejects_corrupted_cash_before_backup(self):
        from paper_migrate_v3 import migrate
        from paper_runtime_v2 import PaperRuntime
        spec=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',qty_step='0.001',tick_size='0.01',min_notional='20',max_qty='2000',min_qty='0.001',category='crypto')
        with tempfile.TemporaryDirectory(dir=SCRATCH) as t:
            r=PaperRuntime(Path(t)/'account',LAB/'paper_config_v2.json',clock_ms=lambda:100000000,fixture=True)
            r.ensure_broker(spec);r.broker.cash=D('99');r.broker._save();r.close()
            with self.assertRaisesRegex(ValueError,'cash'):
                migrate(Path(t)/'account',LAB/'paper_config_v3.json',Path(t)/'backup',now_ms=100100000)
            self.assertFalse((Path(t)/'backup').exists())
    def test_research_cost_totals_keep_broker_decimal_precision(self):
        from paper_runtime_v3 import research_metrics
        from decimal import localcontext
        ledger=[dict(type='realized',amount='0.10000000000000000000000000000000001'),dict(type='fee',amount='-0.00000000000000000000000000000000002')]
        with localcontext() as ctx:
            ctx.prec=40
            expected=D(ledger[0]['amount'])+D(ledger[1]['amount'])
        actual=research_metrics([],ledger,baseline_fills=0,baseline_ledger=0)
        self.assertEqual(D(actual['net_cash_pnl_usdt']),expected)
    def test_artificial_one_minute_pipeline_entry_exit_restart_and_cost_gate(self):
        # Regression/control coverage. ARTIFICIAL quotes are not confirmation samples.
        from test_paper_runtime import FixtureClient,BASE
        from paper_runtime_v3 import PaperRuntime
        class OneMinuteClient(FixtureClient):
            def __init__(self):
                super().__init__();self.price='2700';self.bid='2699.99';self.ask='2700.01'
                self.bars=[[BASE+i*60000,'2700','2700','2700','2700','10',BASE+(i+1)*60000-1] for i in range(-61,0)]
            def get(self,e,p=None):
                s=super().get(e,p)
                if e.endswith('klines') and 'endTime' in p:
                    s['payload']=[b for b in s['payload'] if b[0]<=p['endTime']]
                return s
        for close,expect_fill in [('2690',True),('2699.5',False)]:
            with self.subTest(close=close),tempfile.TemporaryDirectory(dir=SCRATCH) as t:
                c=OneMinuteClient();r=PaperRuntime(t,LAB/'paper_config_v3.json',client=c,clock_ms=lambda:c.now,fixture=True)
                try:
                    self.assertIsNone(r.poll()['latest_error'])
                    c.bars.append([BASE,close,close,close,close,'13',BASE+59999]);c.now=BASE+61000;c.price=close;c.bid=str(D(close)-D('.01'));c.ask=str(D(close)+D('.01'))
                    s=r.poll();self.assertIsNone(s['latest_error']);self.assertEqual(s['research']['signals_count'],1);self.assertEqual(s['fills_count'],0)
                    c.now+=3001;s=r.poll()
                    if not expect_fill:
                        self.assertEqual(s['fills_count'],0)
                        self.assertEqual(s['research']['rejection_categories'],{'insufficient_reward_after_costs':1})
                        continue
                    self.assertEqual(s['fills_count'],1);self.assertEqual(s['research']['complete_round_trips'],0)
                    c.now+=1000;c.price='2700';c.bid='2699.99';c.ask='2700.01';s=r.poll()
                    self.assertEqual(s['fills_count'],1,'target mark is not immediate exit fill')
                    c.now+=3001;s=r.poll()
                    self.assertEqual(s['positions'],[]);self.assertEqual(s['research']['complete_round_trips'],1)
                    self.assertGreater(D(s['fees_usdt']),0);self.assertTrue(s['fixture'])
                    r.close();r=PaperRuntime(t,LAB/'paper_config_v3.json',client=c,clock_ms=lambda:c.now,fixture=True)
                    self.assertEqual(r.snapshot()['research']['complete_round_trips'],1)
                finally:r.close()
    def test_partials_are_one_round_trip_and_costs_are_net(self):
        import paper_runtime_v3
        self.assertTrue(hasattr(paper_runtime_v3,'research_metrics'),'complete episode accounting missing')
        research_metrics=paper_runtime_v3.research_metrics
        fills=[dict(side=side,qty=q,ts=i,symbol='ETHUSDT') for i,(side,q) in enumerate([('BUY','0.02'),('BUY','0.03'),('SELL','0.01'),('SELL','0.04'),('BUY','0.01')])]
        ledger=[dict(type='realized',amount='0.6'),dict(type='fee',amount='-0.1'),dict(type='funding',amount='-0.02')]
        m=research_metrics(fills,ledger,baseline_fills=0,baseline_ledger=0)
        self.assertEqual(m['complete_round_trips'],1)
        self.assertEqual(m['open_episodes'],1)
        self.assertEqual(m['net_cash_pnl_usdt'],'0.48')
        self.assertEqual(research_metrics(fills,ledger,baseline_fills=4,baseline_ledger=3)['complete_round_trips'],0)
