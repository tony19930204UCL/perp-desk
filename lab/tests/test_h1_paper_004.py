"""Synthetic/public-safe acceptance for operator-frozen H1-PAPER-004."""
import hashlib
import json
import os
import fcntl
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

LAB=Path(__file__).resolve().parents[1]


def bar(open_ms,close='100',volume='10'):
    return dict(symbol='ETHUSDT',open_time_ms=open_ms,close_time_ms=open_ms+60000,
                closed=True,open=D(close),high=D(close),low=D(close),close=D(close),volume=D(volume))


class TargetContractTests(unittest.TestCase):
    def test_config_changes_only_preregistered_strategy_identity_target_and_checkpoint(self):
        old=json.loads((LAB/'paper_config_v3.json').read_text())
        new=json.loads((LAB/'paper_config_v4.json').read_text())
        self.assertEqual(new['version_id'],'H1-PAPER-004')
        for key in ('initial_equity_usdt','max_loss_per_trade_usdt','max_daily_loss_usdt',
                    'max_effective_exposure_x','max_positions','total_loss_limit_usdt',
                    'stop_required','paper_leverage','risk_version','execution_model'):
            self.assertEqual(new[key],old[key])
        for key in ('symbol','category','interval_ms','prior_return_samples','downside_sigma',
                    'volume_multiple','stop_distance_fraction','max_holding_ms',
                    'minimum_gross_reward_to_estimated_cost','execution'):
            self.assertEqual(new['strategy'][key],old['strategy'][key])
        self.assertEqual(new['strategy']['take_profit_reference'],'prior_15_closed_1m_vwma_proxy')
        self.assertEqual(new['strategy']['target_lookback_bars'],15)
        self.assertEqual(new['research']['window_ms'],172800000)
        self.assertEqual(new['research']['throughput_checkpoint_ms'],28800000)
        self.assertEqual(new['research']['minimum_timely_minute_coverage'],'0.90')
        self.assertEqual(new['research']['minimum_independent_cost_qualified_opportunities'],4)

    def test_causal_vwma_uses_exact_prior_15_and_excludes_signal_bar(self):
        from signals_v4 import causal_vwma_target
        signal_open=16*60000
        prior=[bar(i*60000,str(100+i),str(i+1)) for i in range(1,16)]
        result=causal_vwma_target(prior,signal_open)
        from decimal import Context, localcontext
        with localcontext(Context(prec=50)):
            expected=sum((D(100+i)*D(i+1) for i in range(1,16)),D(0))/sum((D(i+1) for i in range(1,16)),D(0))
        self.assertEqual(result['value'],expected)
        self.assertEqual(result['source_open_times_ms'],[i*60000 for i in range(1,16)])
        self.assertNotIn(signal_open,result['source_open_times_ms'])
        self.assertEqual(result['decimal_precision'],'50')

    def test_falling_vwma_is_not_stretched_to_pass_cost_gate(self):
        from signals_v4 import causal_vwma_target
        from paper_runtime_v4 import cost_diagnostic
        cfg=json.loads((LAB/'paper_config_v4.json').read_text())
        spec=dict(tick_size='0.01',taker_fee='0.0005')
        signal_open=16*60000
        prior=[bar(i*60000,str(110-i),'10') for i in range(1,16)]
        frozen=causal_vwma_target(prior,signal_open)['value']
        diag=cost_diagnostic(cfg,spec,bid='102.99',ask='103.00',target=str(frozen))
        self.assertEqual(diag['frozen_target'],str(frozen))
        self.assertEqual(diag['minimum_gross_reward_to_estimated_cost'],'2')
        # Whether it passes or fails, target is the causal value and is never moved.
        self.assertEqual(diag['frozen_target'],str(causal_vwma_target(prior,signal_open)['value']))

    def test_unchanged_two_times_full_cost_gate_matches_existing_sizer(self):
        from paper_runtime_v4 import cost_diagnostic
        from paper_sizing import size_long
        cfg=json.loads((LAB/'paper_config_v4.json').read_text())
        spec=dict(symbol='ETHUSDT',tick_size='0.01',qty_step='0.001',min_qty='0.001',
                  max_qty='2000',min_notional='20',taker_fee='0.0005')
        for target,expected in [('100.05','rejected'),('110','accepted')]:
            with self.subTest(target=target):
                diag=cost_diagnostic(cfg,spec,bid='99.99',ask='100.00',target=target)
                sized=size_long(cfg,spec,equity='100',bid='99.99',ask='100.00',target=target)
                self.assertEqual(diag['minimum_gross_reward_to_estimated_cost'],'2')
                self.assertEqual(diag['cost_qualified'],sized['status']=='accepted')
                self.assertEqual(sized['status'],expected)
                if expected=='rejected':
                    self.assertEqual(sized['reason'],'insufficient_reward_after_costs')

    def test_zero_volume_missing_noncontiguous_and_invalid_bars_fail_closed(self):
        from signals_v4 import causal_vwma_target
        signal_open=16*60000
        good=[bar(i*60000,'100','0') for i in range(1,16)]
        with self.assertRaisesRegex(ValueError,'zero total volume'):
            causal_vwma_target(good,signal_open)
        with self.assertRaisesRegex(ValueError,'exactly 15'):
            causal_vwma_target(good[:-1],signal_open)
        broken=[bar(i*60000,'100','1') for i in range(1,16)]
        broken[7]['open_time_ms']+=60000;broken[7]['close_time_ms']+=60000
        with self.assertRaisesRegex(ValueError,'noncontiguous'):
            causal_vwma_target(broken,signal_open)
        invalid=[bar(i*60000,'100','1') for i in range(1,16)]
        invalid[3]['close']=D('NaN')
        with self.assertRaisesRegex(ValueError,'invalid'):
            causal_vwma_target(invalid,signal_open)


class ThroughputTests(unittest.TestCase):
    def coverage(self,count,start=1_000_000):
        return {str(start+(i+1)*60000):dict(timely=True,source_valid=True) for i in range(count)}

    def candidate(self,i,start=1_000_000,**changes):
        d=dict(signal_id='s'+str(i),routing_decision_ms=start+(i+1)*60000,
               timely=True,source_valid=True,cost_qualified=True)
        d.update(changes);return d

    def test_checkpoint_90_percent_boundary_and_four_opportunity_boundary(self):
        from paper_runtime_v4 import throughput_checkpoint,CHECKPOINT_MS
        start=1_000_000;at=start+CHECKPOINT_MS
        r=throughput_checkpoint(strategy_start_ms=start,now_ms=at,
                                coverage=self.coverage(431,start),
                                candidates=[self.candidate(i,start) for i in range(10)])
        self.assertEqual(r['status'],'data_quality_inconclusive');self.assertTrue(r['stop_new_entries'])
        r=throughput_checkpoint(strategy_start_ms=start,now_ms=at,
                                coverage=self.coverage(432,start),
                                candidates=[self.candidate(i,start) for i in range(3)])
        self.assertEqual(r['status'],'throughput_infeasible');self.assertTrue(r['stop_new_entries'])
        r=throughput_checkpoint(strategy_start_ms=start,now_ms=at,
                                coverage=self.coverage(432,start),
                                candidates=[self.candidate(i,start) for i in range(4)])
        self.assertEqual(r['status'],'passed');self.assertFalse(r['stop_new_entries'])
        self.assertEqual(r['deadline_ms'],start+172800000)

    def test_duplicate_late_invalid_and_occupied_opportunity_semantics(self):
        from paper_runtime_v4 import throughput_checkpoint,CHECKPOINT_MS
        start=1_000_000;coverage=self.coverage(480,start)
        candidates=[
            self.candidate(1,start),
            self.candidate(1,start), # duplicate signal id
            self.candidate(2,start,timely=False),
            self.candidate(3,start,source_valid=False),
            self.candidate(4,start,cost_qualified=False),
            self.candidate(5,start,admission_reason='single_position_or_pending'),
            self.candidate(6,start,admission_reason='risk_daily_loss'),
            self.candidate(7,start,admission_reason='storage_new_risk_inhibited')]
        r=throughput_checkpoint(strategy_start_ms=start,now_ms=start+CHECKPOINT_MS,
                                coverage=coverage,candidates=candidates)
        # qualified opportunity count is independent from fill/admission outcome;
        # occupied/risk/storage candidates can diagnose economics without orders.
        self.assertEqual(r['independent_cost_qualified_opportunities'],4)
        self.assertEqual(r['status'],'passed')
        self.assertFalse(r['pnl_stopping_rule'])

    def test_before_checkpoint_never_stops_or_moves_deadline(self):
        from paper_runtime_v4 import throughput_checkpoint,CHECKPOINT_MS
        start=1_000_000
        r=throughput_checkpoint(strategy_start_ms=start,now_ms=start+CHECKPOINT_MS-1,
                                coverage={},candidates=[])
        self.assertEqual(r['status'],'pending');self.assertFalse(r['stop_new_entries'])
        self.assertEqual(r['deadline_ms'],start+172800000)


class CoverageDurabilityTests(unittest.TestCase):
    def test_failed_batch_cannot_be_retrovalidated_by_later_success(self):
        from paper_runtime_v4 import PaperRuntime
        with tempfile.TemporaryDirectory() as td:
            now=10_000_000
            r=PaperRuntime(td,LAB/'paper_config_v4.json',clock_ms=lambda:now,fixture=True)
            try:
                r.state['strategy_start_ms']=now-120000
                close_ms=now-1000
                r._coverage_batch_keys=[]
                r._record_minute_detection(dict(
                    type='detector',
                    bar=dict(close_time_ms=close_ms)))
                key=str(close_ms)
                self.assertIn(key,r._coverage_batch_keys)
                self.assertFalse(r.state['minute_coverage'][key]['source_valid'])
                # Simulate the collect failing before decision source validation.
                r._coverage_batch_keys=[]
                markets=[dict(symbol='ETHUSDT',source_timestamps_ms={
                    'bookTicker':now,'depth5':now,'premiumIndex':now})]
                r.validate_sources(markets,'decision after candle work')
                self.assertFalse(r.state['minute_coverage'][key]['source_valid'])
            finally:
                r.close()


class PollDiagnosticTests(unittest.TestCase):
    def test_poll_errors_consecutive_period_and_recovery_are_distinct(self):
        from test_paper_runtime import FixtureClient,BASE
        from paper_runtime_v4 import PaperRuntime
        with tempfile.TemporaryDirectory() as td:
            client=FixtureClient()
            client.now=BASE+10000
            client.bars=[
                [BASE+i*60000,'100','100','100','100','10',BASE+(i+1)*60000-1]
                for i in range(-61,0)]
            client.missing='/fapi/v1/exchangeInfo'
            r=PaperRuntime(td,LAB/'paper_config_v4.json',client=client,
                           clock_ms=lambda:client.now,fixture=True)
            try:
                first=r.poll()
                self.assertIsNotNone(first['latest_error'])
                d=r.state['poll_diagnostics']
                self.assertEqual(d['poll_error_events'],1)
                self.assertEqual(d['error_periods_started'],1)
                self.assertEqual(d['consecutive_error_polls'],1)
                self.assertIsNotNone(d['active_error_period'])
                client.missing=None
                second=r.poll()
                self.assertIsNone(second['latest_error'])
                d=r.state['poll_diagnostics']
                self.assertEqual(d['poll_error_events'],1)
                self.assertEqual(d['error_periods_started'],1)
                self.assertEqual(d['recovery_observations'],1)
                self.assertEqual(d['consecutive_error_polls'],0)
                self.assertIsNone(d['active_error_period'])
                self.assertEqual(len(d['error_periods']),1)
            finally:r.close()


class RestartAndBlockerTests(unittest.TestCase):
    spec=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
              qty_step='0.001',tick_size='0.01',min_notional='20',
              max_qty='2000',min_qty='0.001',category='crypto')

    def test_checkpoint_evidence_and_original_48h_deadline_survive_restart(self):
        from paper_runtime_v4 import PaperRuntime,CHECKPOINT_MS
        with tempfile.TemporaryDirectory() as td:
            start=1_000_000
            now=start+CHECKPOINT_MS
            r=PaperRuntime(td,LAB/'paper_config_v4.json',clock_ms=lambda:now,fixture=True)
            r.state['strategy_start_ms']=start
            r.state['research_deadline_ms']=start+172800000
            r.state['minute_coverage']={
                str(start+(i+1)*60000):dict(timely=True,source_valid=True)
                for i in range(432)}
            r.state['candidate_diagnostics']=[
                dict(signal_id='q'+str(i),routing_decision_ms=start+(i+1)*60000,
                     timely=True,source_valid=True,cost_qualified=True,
                     admission_status='rejected',admission_reason='single_position_or_pending')
                for i in range(4)]
            before_checkpoint=r.checkpoint()
            self.assertEqual(before_checkpoint['status'],'passed')
            self.assertEqual(before_checkpoint['deadline_ms'],start+172800000)
            r.save();r.close()
            r=PaperRuntime(td,LAB/'paper_config_v4.json',clock_ms=lambda:now+1000,fixture=True)
            try:
                self.assertEqual(r.state['strategy_start_ms'],start)
                self.assertEqual(r.state['research_deadline_ms'],start+172800000)
                self.assertEqual(len(r.state['minute_coverage']),432)
                self.assertEqual(len(r.state['candidate_diagnostics']),4)
                after=r.checkpoint()
                self.assertEqual(after['status'],'passed')
                self.assertFalse(after['stop_new_entries'])
                self.assertEqual(after['deadline_ms'],start+172800000)
            finally:r.close()

    def test_checkpoint_pass_does_not_clear_existing_loss_or_storage_entry_blocks(self):
        from paper_runtime_v4 import PaperRuntime,CHECKPOINT_MS
        from storage_protection import StoragePolicy
        with tempfile.TemporaryDirectory() as td:
            start=1_000_000;now=start+CHECKPOINT_MS
            probe=lambda root:dict(used_bytes=210,free_bytes=1000,total_bytes=2000,files={})
            policy=StoragePolicy(warning_bytes=100,new_risk_limit_bytes=200,
                                 exit_reserve_bytes=100,min_free_bytes=50,max_exit_cycle_bytes=20)
            r=PaperRuntime(td,LAB/'paper_config_v4.json',clock_ms=lambda:now,fixture=True,
                           storage_policy=policy,storage_probe=probe)
            try:
                r.ensure_broker(self.spec)
                r.state['strategy_start_ms']=start
                r.state['research_deadline_ms']=start+172800000
                r.state['minute_coverage']={
                    str(start+(i+1)*60000):dict(timely=True,source_valid=True)
                    for i in range(432)}
                r.state['candidate_diagnostics']=[
                    dict(signal_id='q'+str(i),routing_decision_ms=start+(i+1)*60000,
                         timely=True,source_valid=True,cost_qualified=True)
                    for i in range(4)]
                r.state['total_halted']=True
                checkpoint=r.checkpoint()
                self.assertEqual(checkpoint['status'],'passed')
                blockers=r.risk_blockers()
                self.assertIn('risk_total_loss',blockers)
                self.assertIn('storage_new_risk_inhibited',blockers)
                self.assertFalse(any(x.startswith('throughput_checkpoint_') for x in blockers))
                self.assertEqual(r.state['research_deadline_ms'],start+172800000)
            finally:r.close()


class RoutingDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        from paper_runtime_v4 import PaperRuntime
        self.tmp=tempfile.TemporaryDirectory()
        self.now=10_000_000
        self.r=PaperRuntime(self.tmp.name,LAB/'paper_config_v4.json',
                            clock_ms=lambda:self.now,fixture=True)
        self.spec=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
                       qty_step='0.001',tick_size='0.01',min_notional='20',
                       max_qty='2000',min_qty='0.001',category='crypto')
        self.r.ensure_broker(self.spec)
        self.r.state['markets']=[dict(symbol='ETHUSDT',bid='99.99',ask='100.00',
                                      source_timestamps_ms={'bookTicker':self.now,'depth5':self.now,'premiumIndex':self.now})]
        self.r.state['strategy_start_ms']=self.now-100000
        self.r.state['research_deadline_ms']=self.now+172800000
        self.r.save()

    def tearDown(self):
        self.r.close();self.tmp.cleanup()

    def insert_signal(self,sid,target='110'):
        intent=dict(signal_id=sid,version_id='H1-PAPER-004',symbol='ETHUSDT',side='long',
                    entry_reference='100',reversion_target=target,
                    features=dict(bar_close_ms=str(self.now-1000),detection_ms=str(self.now-900),
                                  detection_timestamp_domain='runtime_utc_wall_input',
                                  target_method='prior_15_closed_1m_volume_weighted_close_vwma_proxy',
                                  target_eligible=target is not None,target_error=None if target is not None else 'synthetic',
                                  target_excludes_signal_bar=True,target_frozen=True))
        with sqlite3.connect(Path(self.tmp.name)/'signals.sqlite3') as db:
            db.execute('INSERT INTO h1_signals VALUES(?,?,?)',(sid,'H1-PAPER-004',json.dumps(intent,sort_keys=True)))
        return intent

    def test_occupied_candidate_keeps_cost_diagnostic_but_submits_no_order(self):
        self.insert_signal('occupied','110')
        self.r.broker.orders['synthetic-pending']=dict(
            order_id='synthetic-pending',status='PENDING',reason=None,
            intent=dict(reduce_only=False))
        before=set(self.r.broker.orders)
        self.r.route_signals(self.spec)
        self.assertEqual(set(self.r.broker.orders),before)
        self.assertEqual(self.r.state['handled_signals']['occupied']['reason'],'single_position_or_pending')
        d=self.r.state['candidate_diagnostics'][0]
        self.assertTrue(d['raw_candidate']);self.assertTrue(d['cost_qualified'])
        self.assertEqual(d['admission_reason'],'single_position_or_pending')
        self.assertEqual(d['economics']['minimum_gross_reward_to_estimated_cost'],'2')
        self.assertIsNotNone(d['economics']['entry_price_bound'])
        self.assertIsNotNone(d['economics']['stop_price'])
        self.assertEqual(d['routing_timestamp_domain'],'runtime_utc_wall')

    def test_cost_rejection_keeps_precise_economics_and_true_reason(self):
        self.insert_signal('cost-reject','100.05')
        self.r.route_signals(self.spec)
        result=self.r.state['handled_signals']['cost-reject']
        self.assertEqual(result['status'],'rejected')
        self.assertEqual(result['reason'],'insufficient_reward_after_costs')
        d=self.r.state['candidate_diagnostics'][0]
        self.assertFalse(d['cost_qualified'])
        self.assertEqual(d['admission_reason'],'insufficient_reward_after_costs')
        self.assertIsNotNone(d['economics']['gross_to_cost_ratio'])
        self.assertEqual(self.r.broker.fills,[])

    def test_late_candidate_keeps_timing_and_true_late_reason_even_if_economics_would_pass(self):
        intent=self.insert_signal('late','110')
        with sqlite3.connect(Path(self.tmp.name)/'signals.sqlite3') as db:
            row=json.loads(db.execute('SELECT intent FROM h1_signals WHERE signal_id=?',('late',)).fetchone()[0])
            row['features']['bar_close_ms']=str(self.now-16000)
            db.execute('UPDATE h1_signals SET intent=? WHERE signal_id=?',(json.dumps(row,sort_keys=True),'late'))
        self.r.route_signals(self.spec)
        result=self.r.state['handled_signals']['late']
        self.assertEqual(result['reason'],'late_closed_bar_signal')
        d=self.r.state['candidate_diagnostics'][0]
        self.assertFalse(d['timely'])
        self.assertFalse(d['cost_qualified'])
        self.assertTrue(d['economics']['cost_qualified'])
        self.assertEqual(d['signal_bar_timestamp_domain'],'exchange_closed_bar_epoch_ms')
        self.assertEqual(d['detection_timestamp_domain'],'runtime_utc_wall_input')
        self.assertEqual(d['admission_reason'],'late_closed_bar_signal')
        self.assertEqual(self.r.broker.fills,[])

    def test_invalid_target_is_raw_candidate_with_null_economics_not_fake_order(self):
        self.insert_signal('bad-target',None)
        self.r.route_signals(self.spec)
        self.assertEqual(self.r.state['handled_signals']['bad-target']['reason'],'invalid_frozen_target')
        d=self.r.state['candidate_diagnostics'][0]
        self.assertFalse(d['cost_qualified']);self.assertFalse(d['economics']['computable'])
        self.assertIsNone(d['frozen_target'])
        self.assertNotIn('order:bad-target',self.r.broker.orders)


class OperationalIdentityTests(unittest.TestCase):
    def test_health_accepts_only_exact_v4_script_version_implementation_pair(self):
        from test_health_watchdog import module as health_module, snapshot as health_snapshot, NOW
        mod=health_module();s=health_snapshot()
        s['engine']['version_id']='H1-PAPER-004'
        s['engine']['candidate_implementation']='paper-engine-v4'
        s['engine']['deployment']['version_id']='H1-PAPER-004'
        s['versions']=[dict(created_at=NOW.isoformat(),status='observing',version_id='H1-PAPER-004')]
        report=mod.evaluate(s,{'tasks':[]},NOW,True,
                            expected_version='H1-PAPER-004',
                            expected_implementation='paper-engine-v4')
        self.assertNotIn('snapshot-unavailable',report['faults'])
        wrong=json.loads(json.dumps(s));wrong['engine']['candidate_implementation']='paper-engine-v3'
        report=mod.evaluate(wrong,{'tasks':[]},NOW,True,
                            expected_version='H1-PAPER-004',
                            expected_implementation='paper-engine-v4')
        self.assertIn('snapshot-unavailable',report['faults'])

    def test_real_process_identity_distinguishes_v3_and_v4_scripts(self):
        import subprocess,sys,time
        from test_health_watchdog import module as health_module
        mod=health_module()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);state=root/'state';state.mkdir();status=root/'status.json'
            (root/'paper_runtime_v4.py').write_text('import time;time.sleep(30)\n')
            proc=subprocess.Popen([sys.executable,'-u','paper_runtime_v4.py',
                                   '--state-dir',str(state),'--status',str(status)],cwd=root)
            try:
                deadline=time.time()+3
                while time.time()<deadline and proc.pid not in mod.runtime_processes(
                        root,state,status,'paper_runtime_v4.py'):
                    time.sleep(.02)
                self.assertEqual(mod.runtime_processes(root,state,status,'paper_runtime_v4.py'),[proc.pid])
                self.assertEqual(mod.runtime_processes(root,state,status,'paper_runtime_v3.py'),[])
            finally:
                proc.terminate();proc.wait(timeout=5)

    def test_supervisor_v4_identity_is_explicit_and_v3_default_remains_unchanged(self):
        import test_startup_recovery as sr
        import startup_recovery as m
        with tempfile.TemporaryDirectory() as td:
            case=sr.StartupRecoveryTests()
            fx=case.make_fixture(td)
            legacy=m.load_config(fx['startup'])
            self.assertEqual(legacy.runtime_script,'paper_runtime_v3.py')
            self.assertIn('paper_runtime_v3.py',m.runtime_command(legacy)[2])
            # Convert only the synthetic identity fields to v4.
            (fx['runtime']/'paper_runtime_v4.py').write_text('import time;time.sleep(30)\n')
            data=json.loads(fx['startup'].read_text());data['runtime_script']='paper_runtime_v4.py'
            fx['startup'].write_text(json.dumps(data))
            health=json.loads(fx['health_cfg'].read_text());health['runtime_script']='paper_runtime_v4.py'
            fx['health_cfg'].write_text(json.dumps(health))
            with sqlite3.connect(fx['state']/'runtime.sqlite3') as db:
                state=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
                state['deployment']['version_id']='H1-PAPER-004'
                db.execute('UPDATE state SET payload=? WHERE id=1',(json.dumps(state,sort_keys=True,separators=(',',':')),))
            cfg=m.load_config(fx['startup'])
            self.assertEqual(cfg.runtime_script,'paper_runtime_v4.py')
            self.assertIn('paper_runtime_v4.py',m.runtime_command(cfg)[2])
            self.assertEqual(m.preflight(cfg)['state'],'ready-to-start')


class MigrationTests(unittest.TestCase):
    spec=dict(symbol='ETHUSDT',maker_fee='0.0002',taker_fee='0.0005',
              qty_step='0.001',tick_size='0.01',min_notional='20',
              max_qty='2000',min_qty='0.001',category='crypto')

    def make_v3(self,td,*,deadline=2_000_000,now=3_000_000):
        from paper_runtime_v3 import PaperRuntime
        root=Path(td)/'account'
        r=PaperRuntime(root,LAB/'paper_config_v3.json',clock_ms=lambda:1_000_000,fixture=True)
        r.ensure_broker(self.spec)
        r.broker.cash=D('99.5')
        r.broker.ledger=[dict(type='realized',amount='-0.4',ts=1_100_000),
                         dict(type='fee',amount='-0.1',ts=1_100_001)]
        r.broker.day_baselines={'0':'100','1':'99.5'}
        r.broker.orders={'old-rejected':dict(order_id='old-rejected',status='REJECTED',reason='old',
                                             intent=dict(reduce_only=False))}
        r.broker._save()
        r.state['strategy_start_ms']=1_200_000
        r.state['research_deadline_ms']=deadline
        r.state['strategy_fill_baseline']=0;r.state['strategy_ledger_baseline']=0
        r.state['total_halted']=True
        r.state['handled_signals']={'old-signal':dict(status='rejected',reason='late_closed_bar_signal')}
        r.state['deployment']=dict(type='parent_deployment',at_ms=1_300_000,
                                   version_id='H1-PAPER-003',config_hash=r.config_hash)
        r.audit(dict(type='old-v3-proof',value='preserve'));r.save();r.close()
        acceptance=Path(td)/'closeout.json'
        with sqlite3.connect(root/'runtime.sqlite3') as db:
            old=json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        acceptance.write_text(json.dumps(dict(
            schema_version=1,version_id='H1-PAPER-003',operator_accepted=True,
            account_forward_start_ms=old['forward_start_ms'],
            strategy_start_ms=old['strategy_start_ms'],
            research_deadline_ms=old['research_deadline_ms'],
            accepted_at_ms=max(deadline,2_500_000)),sort_keys=True))
        return root,acceptance,now

    def audit_rows(self,path):
        with sqlite3.connect(path) as db:
            return list(db.execute('SELECT payload,previous_hash,hash FROM audit ORDER BY id'))

    def test_same_account_migration_preserves_all_history_and_starts_new_window_only_after_closeout(self):
        from paper_migrate_v4 import migrate
        from paper_runtime_v4 import PaperRuntime
        with tempfile.TemporaryDirectory() as td:
            root,acceptance,now=self.make_v3(td)
            broker_before=(root/'broker.sqlite3').read_bytes()
            signals_before=(root/'signals.sqlite3').read_bytes()
            audit_before=self.audit_rows(root/'runtime.sqlite3')
            with sqlite3.connect(root/'broker.sqlite3') as db:
                broker_before_payload=json.loads(db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()[0])
            report=migrate(root,LAB/'paper_config_v4.json',acceptance,now_ms=now)
            self.assertEqual((root/'broker.sqlite3').read_bytes(),broker_before)
            self.assertEqual((root/'signals.sqlite3').read_bytes(),signals_before)
            audit_after=self.audit_rows(root/'runtime.sqlite3')
            self.assertEqual(audit_after[:len(audit_before)],audit_before)
            self.assertEqual(len(audit_after),len(audit_before)+1)
            self.assertEqual(report['cash_usdt'],'99.5')
            self.assertEqual(report['original_forward_start_ms'],1_000_000)
            self.assertEqual(report['previous_deadline_ms'],2_000_000)
            self.assertEqual(report['strategy_start_ms'],now)
            self.assertEqual(report['deadline_ms'],now+172800000)
            r=PaperRuntime(root,LAB/'paper_config_v4.json',clock_ms=lambda:now+1,fixture=True)
            try:
                self.assertEqual(r.broker.cash,D('99.5'))
                self.assertEqual(r.broker.day_baselines,broker_before_payload['day_baselines'])
                self.assertTrue(r.state['total_halted'])
                self.assertIn('old-signal',r.state['handled_signals'])
                self.assertEqual(r.state['forward_start_ms'],1_000_000)
                self.assertEqual(r.state['research_deadline_ms'],now+172800000)
                self.assertFalse(r.state.get('deployment'))
                with sqlite3.connect(root/'signals.sqlite3') as db:
                    versions={row[0] for row in db.execute('SELECT version_id FROM h1_versions')}
                self.assertIn('H1-PAPER-003',versions);self.assertIn('H1-PAPER-004',versions)
            finally:r.close()

    def test_migration_fails_closed_before_deadline_closeout_mismatch_open_pending_lock_and_ledger_corruption(self):
        from paper_migrate_v4 import migrate
        from paper_runtime_v3 import PaperRuntime
        cases=('before-deadline','bad-closeout','open','pending','ledger')
        for case in cases:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as td:
                deadline=4_000_000 if case=='before-deadline' else 2_000_000
                root,acceptance,now=self.make_v3(td,deadline=deadline,now=3_000_000)
                if case=='bad-closeout':
                    data=json.loads(acceptance.read_text());data['research_deadline_ms']+=1
                    acceptance.write_text(json.dumps(data))
                if case in ('open','pending','ledger'):
                    with sqlite3.connect(root/'broker.sqlite3') as db:
                        saved=json.loads(db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()[0])
                        if case=='open': saved['positions']={'ETHUSDT':{'qty':'0.001'}}
                        elif case=='pending': saved['orders']['pending']=dict(status='PENDING',intent=dict(reduce_only=False))
                        else: saved['cash']='123'
                        db.execute('UPDATE sim_broker_state SET payload=? WHERE singleton=1',(json.dumps(saved,sort_keys=True),))
                before=(root/'runtime.sqlite3').read_bytes()
                with self.assertRaises((ValueError,RuntimeError)):
                    migrate(root,LAB/'paper_config_v4.json',acceptance,now_ms=now)
                self.assertEqual((root/'runtime.sqlite3').read_bytes(),before)

    def test_running_runtime_lock_blocks_migration(self):
        from paper_migrate_v4 import migrate
        with tempfile.TemporaryDirectory() as td:
            root,acceptance,now=self.make_v3(td)
            fd=os.open(root/'runtime.lock',os.O_RDONLY|os.O_NOFOLLOW)
            try:
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaisesRegex(RuntimeError,'locked'):
                    migrate(root,LAB/'paper_config_v4.json',acceptance,now_ms=now)
            finally:
                fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)


if __name__=='__main__':
    unittest.main()
