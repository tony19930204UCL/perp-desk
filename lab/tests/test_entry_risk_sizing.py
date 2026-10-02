"""Isolated synthetic execution fixtures. Never forward fills or performance."""
import importlib.util
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from sim_broker import SimBroker, InstrumentSettings, ExecutionModel, RiskContract, Intent
from paper_sizing import size_long

LAB = Path(__file__).resolve().parents[1]

class EntryRiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='entry-risk-')
        self.addCleanup(self.tmp.cleanup)
        self.config = json.loads((LAB/'paper_config_v2.json').read_text())
        self.spec = dict(symbol='ETHUSDT', tick_size='0.01', qty_step='0.001',
                         market_qty_step='0.001', min_qty='0.001', max_qty='2000',
                         min_notional='20', taker_fee='0.0005')
        self.settings = InstrumentSettings('ETHUSDT', D('.0002'), D('.0005'), D('.001'),
                                           D('.01'), D('20'), D('2000'), D('.001'))
        self.risk = RiskContract(True, 'PAPER-RISK-001', D('1'), D('3'), D('3'), 1, D('10'), True)
        self.decision = 1790910609267
        self.b = SimBroker(Path(self.tmp.name)/'broker.sqlite', initial_cash=D('100'),
                           instruments=[self.settings], execution=ExecutionModel(2000, 2, None, 2, 2),
                           risk=self.risk, version_id='ARTIFICIAL-ENTRY-RISK', forward_start=self.decision)
        self.addCleanup(self.b.close)
        self.b.on_event(dict(type='mark', event_id='mark', symbol='ETHUSDT', ts=self.decision, price='2716.03'))
        self.b.on_event(dict(type='funding_status', event_id='funding', symbol='ETHUSDT', ts=self.decision,
                             complete=True, valid_until_ts=self.decision+100000))

    def sizing(self):
        return size_long(self.config, self.spec, equity='100', bid='2716.00', ask='2716.01', target='2722.84')

    def intent(self, **changes):
        values = dict(intent_id='rebound', symbol='ETHUSDT', side='BUY', qty=D('.061'), stop=D('2702.44'),
                      decision_ts=self.decision, quantity_step=D('.001'), tick=D('.01'),
                      min_notional=D('20'), max_quantity=D('2000'), kind='TAKER', reduce_only=False,
                      expires_ts=self.decision+15000)
        values.update(changes)
        return Intent(**values)

    def book(self, ask='2716.48', qty='11.102', *, elapsed=3000, source_elapsed=None, bids=None, asks=None):
        return dict(type='book', event_id='book:'+str(elapsed), symbol='ETHUSDT', ts=self.decision+elapsed,
                    source_ts=self.decision+(elapsed if source_elapsed is None else source_elapsed),
                    bids=bids or [[str(D(ask)-D('.01')), '20']], asks=asks or [[ask, qty]])

    def planned_loss(self):
        p=self.b.positions['ETHUSDT']; q=abs(D(p['qty'])); stop=D(p['stop'])
        exit_price=stop-D('.02') if D(p['qty'])>0 else stop+D('.02')
        entry_fees=sum(D(f['fee']) for f in self.b.fills if f['side']==('BUY' if D(p['qty'])>0 else 'SELL'))
        return q*abs(D(p['entry'])-exit_price)+entry_fees+q*exit_price*D('.0005')

    def test_fast_rebound_executes_smaller_quantity_at_delayed_price_under_original_cap(self):
        sized=self.sizing()
        self.assertEqual(sized['qty'], '0.061')
        self.assertEqual(sized['stop_price'], '2702.44')
        intent=self.intent(risk_limited=True)
        self.b.submit(intent)
        self.b.on_event(self.book(elapsed=2000))
        self.assertEqual(self.b.fills, [])
        self.b.on_event(self.book())
        self.assertEqual(sum(D(f['qty']) for f in self.b.fills), D('.059'))
        self.assertEqual(self.b.fills[0]['price'], '2716.50')
        self.assertEqual(self.b.fills[0]['ts'], self.decision+3000)
        self.assertLessEqual(self.planned_loss(), D('1'))
        order=self.b.orders['order:rebound']
        self.assertEqual(D(order['intent']['qty']), D('.061'))
        self.assertEqual(D(order['canceled_qty']), D('.002'))
        self.assertEqual(order['status'], 'CANCELED')
        self.assertEqual(order['reason'], 'risk_limited_remainder')
        self.assertEqual(D(order['remaining']), 0)

    def test_unused_adverse_depth_does_not_shrink_executable_quantity(self):
        self.b.submit(self.intent(risk_limited=True))
        self.b.on_event(self.book(asks=[['2716.48', '1'], ['9000', '1']]))
        self.assertEqual(sum(D(f['qty']) for f in self.b.fills), D('.059'))
        self.assertEqual({f['observed_price'] for f in self.b.fills}, {'2716.48'})
        self.assertLessEqual(self.planned_loss(), D('1'))

    def test_risk_limited_contract_rejects_maker_reduce_only_and_nonboolean(self):
        for n, changes in enumerate((dict(kind='MAKER', limit_price=D('2715'), queue_ahead_qty=D('0')),
                                     dict(reduce_only=True), dict(risk_limited=1))):
            with self.subTest(changes=changes):
                values=dict(intent_id='invalid'+str(n), risk_limited=True)
                values.update(changes)
                result=self.b.submit(self.intent(**values))
                self.assertEqual(result['reason'], 'invalid_risk_limited_contract')
                self.assertEqual(result['status'], 'REJECTED')
        self.assertEqual(self.b.fills, [])

    def test_sell_depth_bounds_exposure_by_highest_consumed_price(self):
        self.b.close()
        settings=replace(self.settings, min_notional=D('1'))
        risk=replace(self.risk, max_loss_per_trade_usdt=D('1000'), max_daily_loss_usdt=D('1000'),
                     total_loss_limit_usdt=D('1000'), max_effective_exposure_x=D('.3'))
        self.b=SimBroker(Path(self.tmp.name)/'short.sqlite', initial_cash=D('100'), instruments=[settings],
                         execution=ExecutionModel(2000, 2, None, 2, 2), risk=risk,
                         version_id='ARTIFICIAL-SHORT-EXPOSURE', forward_start=self.decision)
        self.addCleanup(self.b.close)
        self.b.on_event(dict(type='mark', event_id='low-mark', symbol='ETHUSDT', ts=self.decision, price='100'))
        self.b.on_event(dict(type='funding_status', event_id='ready', symbol='ETHUSDT', ts=self.decision,
                             complete=True, valid_until_ts=self.decision+100000))
        self.b.submit(self.intent(side='SELL', stop=D('3000'), min_notional=D('1'), risk_limited=True))
        self.b.on_event(self.book(bids=[['2700', '.010'], ['100', '1']], asks=[['2700.01', '1']]))
        filled=sum(D(f['qty']) for f in self.b.fills)
        self.assertEqual(filled, D('.011'))
        gross=sum(D(f['qty'])*max(D(f['price']), D('100')) for f in self.b.fills)
        self.assertLessEqual(gross/self.b.equity, D('.3'))
        self.assertTrue(all(D(f['price'])==D(f['observed_price'])-D('.02') for f in self.b.fills))

    def test_fixed_quantity_add_counts_already_paid_entry_fees(self):
        self.b.submit(self.intent(intent_id='first', qty=D('.040')))
        self.b.on_event(self.book(ask='2716.01'))
        self.assertEqual(len(self.b.fills), 1)
        self.b.submit(self.intent(intent_id='second', qty=D('.021'), decision_ts=self.decision+3000))
        self.b.on_event(self.book(elapsed=6001))
        self.assertEqual(self.b.orders['order:second']['reason'], 'risk_per_trade')
        self.assertEqual(len(self.b.fills), 1)
        self.assertLessEqual(self.planned_loss(), D('1'))

    def test_arrival_quantity_preserves_common_market_and_lot_grid(self):
        spec={**self.spec, 'market_qty_step': '0.003'}
        sized=size_long(self.config, spec, equity='100', bid='2716.00', ask='2716.01', target='2722.84')
        self.assertEqual(sized['qty'], '0.060')
        intent=self.intent(qty=D(sized['qty']), risk_limited=True, risk_quantity_step=D('.003'))
        self.b.submit(intent)
        self.b.on_event(self.book())
        self.assertEqual(sum(D(f['qty']) for f in self.b.fills), D('.057'))
        self.assertEqual(sized['execution_qty_step'], '0.003')

    def test_invalid_execution_grid_fails_closed(self):
        for n, step in enumerate((D('0'), D('NaN'), D('.0005'), D('.003'))):
            with self.subTest(step=step):
                result=self.b.submit(self.intent(intent_id='grid'+str(n), risk_limited=True, risk_quantity_step=step))
                self.assertEqual(result['reason'], 'invalid_risk_limited_contract')
                self.assertEqual(result['status'], 'REJECTED')
        result=self.b.submit(self.intent(intent_id='unscoped-grid', risk_quantity_step=D('.001')))
        self.assertEqual(result['reason'], 'invalid_risk_limited_contract')

    def test_best_entry_beyond_fixed_stop_rejects_even_with_safe_deeper_level(self):
        self.b.submit(self.intent(risk_limited=True))
        self.b.on_event(self.book(asks=[['2702.00', '.01'], ['2716.48', '1']], bids=[['2701.99', '1']]))
        self.assertEqual(self.b.fills, [])
        self.assertEqual(self.b.orders['order:rebound']['reason'], 'wrong_stop_side')

    def test_legacy_intent_missing_new_fields_remains_idempotent_without_rewriting_history(self):
        intent=self.intent()
        self.b.submit(intent)
        saved=self.b.orders['order:rebound']['intent']
        saved.pop('risk_limited'); saved.pop('risk_quantity_step')
        before=json.dumps(saved, default=str, sort_keys=True)
        try:
            result=self.b.submit(intent)
        except ValueError as exc:
            self.fail(f'legacy identical intent was incorrectly rejected: {exc}')
        self.assertEqual(result['status'], 'PENDING')
        self.assertEqual(json.dumps(saved, default=str, sort_keys=True), before)
        with self.assertRaisesRegex(ValueError, 'conflicting intent'):
            self.b.submit(replace(intent, risk_limited=True))

    def test_legacy_fixed_rebound_still_rejects_with_no_fill(self):
        self.b.submit(self.intent())
        self.b.on_event(self.book())
        self.assertEqual(self.b.fills, [])
        self.assertEqual(self.b.orders['order:rebound']['reason'], 'risk_per_trade')
        self.assertEqual(self.b.cash, D('100'))

    def test_favorable_buy_never_increases_original_quantity_ceiling(self):
        self.b.submit(self.intent(risk_limited=True))
        self.b.on_event(self.book(ask='2715.01'))
        self.assertEqual(sum(D(f['qty']) for f in self.b.fills), D('.061'))
        self.assertLessEqual(self.planned_loss(), D('1'))
        self.assertEqual(self.b.orders['order:rebound']['status'], 'FILLED')

    def test_adverse_sell_downsizes_without_changing_stop_or_fees(self):
        self.b.submit(self.intent(side='SELL', stop=D('2730'), risk_limited=True))
        self.b.on_event(self.book(ask='2715.01'))
        self.assertEqual(sum(D(f['qty']) for f in self.b.fills), D('.056'))
        self.assertLessEqual(self.planned_loss(), D('1'))
        self.assertEqual(self.b.positions['ETHUSDT']['stop'], '2730')
        self.assertEqual(self.b.fills[0]['price'], '2714.98')

    def test_observed_depth_partial_multilevel_fills_cancel_remainder_and_do_not_retry(self):
        self.b.submit(self.intent(risk_limited=True))
        self.b.on_event(self.book(asks=[['2716.48', '.017'], ['2716.49', '.0235']]))
        self.assertEqual([D(f['qty']) for f in self.b.fills], [D('.017'), D('.023')])
        self.assertLessEqual(self.planned_loss(), D('1'))
        self.assertEqual(D(self.b.orders['order:rebound']['canceled_qty']), D('.021'))
        self.assertTrue(all(not f['modeled_depth'] for f in self.b.fills))
        cash=self.b.cash
        self.b.on_event(self.book(elapsed=4000))
        self.assertEqual(len(self.b.fills), 2)
        self.assertEqual(self.b.cash, cash)

    def test_safe_risk_quantity_below_min_notional_rejects_without_rounding_up(self):
        self.b.submit(self.intent(risk_limited=True))
        self.b.on_event(self.book(ask='3000.01'))
        self.assertEqual(self.b.fills, [])
        self.assertEqual(self.b.orders['order:rebound']['reason'], 'min_notional')

    def test_daily_remaining_budget_limits_quantity_without_raising_daily_cap(self):
        self.b.day_baselines[str(self.decision//86400000)]='102.5'
        self.b.submit(self.intent(risk_limited=True))
        self.b.on_event(self.book())
        self.assertEqual(sum(D(f['qty']) for f in self.b.fills), D('.029'))
        self.assertLessEqual(D('2.5')+self.planned_loss(), D('3'))

    def test_total_remaining_budget_limits_quantity_without_raising_total_cap(self):
        self.b.cash=D('90.5')
        self.b.day_baselines[str(self.decision//86400000)]='90.5'
        self.b.submit(self.intent(risk_limited=True))
        self.b.on_event(self.book())
        self.assertEqual(sum(D(f['qty']) for f in self.b.fills), D('.029'))
        self.assertLessEqual(D('9.5')+self.planned_loss(), D('10'))

    def test_risk_limited_latency_source_boundary_expiry_and_revocation_fail_closed(self):
        self.b.submit(self.intent(risk_limited=True))
        self.b.on_event(self.book(elapsed=3000, source_elapsed=2000))
        self.assertEqual(self.b.fills, [])
        self.b.on_event(self.book(elapsed=15000))
        self.assertEqual(self.b.fills, [])
        self.assertEqual(self.b.orders['order:rebound']['status'], 'EXPIRED')
        self.b.submit(self.intent(intent_id='revoked', decision_ts=self.decision+15000,
                                  expires_ts=self.decision+30000, risk_limited=True))
        self.b.risk=replace(self.risk, approved=False)
        self.b.on_event(self.book(elapsed=18000))
        self.assertEqual(self.b.fills, [])
        self.assertEqual(self.b.orders['order:revoked']['reason'], 'risk_missing_or_unapproved')

    def test_grid_maximum_and_next_step_unsafe_across_rebound_prices(self):
        from decimal import ROUND_FLOOR
        for ticks in range(0, 121):
            ask=D('2716.01')+D('.01')*ticks
            order=dict(intent=self.intent(risk_limited=True).__dict__, remaining='.061')
            qty, reason=self.b._risk_limited_quantity(order, [[ask, D('1')]])
            unit=(ask+D('.02'))-D('2702.42')+D('.0005')*((ask+D('.02'))+D('2702.42'))
            expected=min(D('.061'), (D('1')/unit/D('.001')).to_integral_value(rounding=ROUND_FLOOR)*D('.001'))
            self.assertIsNone(reason)
            self.assertEqual(qty, expected)
            self.assertLessEqual(qty*unit, D('1'))
            if qty<D('.061'): self.assertGreater((qty+D('.001'))*unit, D('1'))

    def test_old_position_fee_fallback_and_partial_reduction_preserve_fee_reserve(self):
        self.b.submit(self.intent(qty=D('.040')))
        self.b.on_event(self.book(ask='2716.01'))
        p=self.b.positions['ETHUSDT']
        fees=D(p.pop('entry_fees'))
        self.assertEqual(self.b._entry_fees('ETHUSDT', p), fees)
        self.b.submit(self.intent(intent_id='reduce', side='SELL', qty=D('.020'), stop=None,
                                  decision_ts=self.decision+3000, reduce_only=True))
        self.b.on_event(self.book(elapsed=6001))
        self.assertEqual(D(self.b.positions['ETHUSDT']['entry_fees']), fees/2)

    def test_min_notional_uses_selected_multilevel_execution_not_worst_price_times_qty(self):
        from decimal import ROUND_FLOOR
        worst=D('2717.02'); exit_price=D('2702.42')
        unit=worst-exit_price+D('.0005')*(worst+exit_price)
        qty=(D('1')/unit/D('.001')).to_integral_value(rounding=ROUND_FLOOR)*D('.001')
        actual=D('.010')*D('2716.50')+(qty-D('.010'))*worst
        minimum=(actual+qty*worst)/2
        self.b.close()
        settings=replace(self.settings, min_notional=minimum)
        self.b=SimBroker(Path(self.tmp.name)/'notional.sqlite', initial_cash=D('100'), instruments=[settings],
                         execution=ExecutionModel(2000, 2, None, 2, 2), risk=self.risk,
                         version_id='ARTIFICIAL-NOTIONAL', forward_start=self.decision)
        self.addCleanup(self.b.close)
        self.b.on_event(dict(type='mark', event_id='mark', symbol='ETHUSDT', ts=self.decision, price='2716.03'))
        self.b.on_event(dict(type='funding_status', event_id='ready', symbol='ETHUSDT', ts=self.decision,
                             complete=True, valid_until_ts=self.decision+100000))
        self.b.submit(self.intent(min_notional=minimum, risk_limited=True))
        self.b.on_event(self.book(asks=[['2716.48', '.010'], ['2717.00', '1']]))
        self.assertEqual(self.b.fills, [])
        self.assertEqual(self.b.orders['order:rebound']['reason'], 'min_notional')

class RuntimeEntryRiskTests(unittest.TestCase):
    import test_paper_v2 as v2
    setUp = v2.V2Tests.setUp
    tearDown = v2.V2Tests.tearDown
    open = v2.V2Tests.open

    def test_runtime_routes_risk_limited_entry_and_restart_preserves_result(self):
        from test_paper_runtime import BASE
        r=self.open(); r.poll()
        self.client.closed_bar(0, '95', '21')
        self.client.now=BASE+10000
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000; r.poll()
        self.client.now=BASE+301000
        self.client.price='95'; self.client.bid='94.99'; self.client.ask='95.01'
        s=r.poll()
        self.assertEqual(s['signals_count'], 1)
        original_qty=D(s['orders'][0]['intent']['qty'])
        self.client.now+=3000
        self.client.price='96'; self.client.bid='95.99'; self.client.ask='96.01'
        s=r.poll()
        self.assertGreater(s['fills_count'], 0)
        self.assertLess(sum(D(f['qty']) for f in s['fills']), original_qty)
        self.assertTrue(s['orders'][0]['intent']['risk_limited'])
        self.assertEqual(s['orders'][0]['status'], 'CANCELED')
        before=json.dumps((s['fills'], s['orders'], s['cash_usdt']), default=str, sort_keys=True)
        r.close(); self.r=None
        r=self.open(); s=r.poll()
        self.assertEqual(json.dumps((s['fills'], s['orders'], s['cash_usdt']), default=str, sort_keys=True), before)
        self.assertEqual(r.config['max_loss_per_trade_usdt'], '1')
        self.assertEqual(r.config['max_daily_loss_usdt'], '3')
        self.assertEqual(r.config['total_loss_limit_usdt'], '10')
