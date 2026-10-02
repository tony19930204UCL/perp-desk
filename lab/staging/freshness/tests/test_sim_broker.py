"""Artificial engineering fixtures only. NOT market performance/risk approval."""
import importlib
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal as D

ROOT = Path('/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch')

class BrokerTests(unittest.TestCase):
    def setUp(self):
        try:
            self.m = importlib.import_module('sim_broker')
        except ModuleNotFoundError:
            self.fail('offline simulator library is not implemented')
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT, prefix='artificial-broker-')
        self.addCleanup(self.tmp.cleanup)
        self.brokers = []
        self.addCleanup(lambda: [b.close() for b in self.brokers])

    def broker(self, risk=None, state_path=None, funding_ready=True, **kwargs):
        settings = self.m.InstrumentSettings('TEST', D('.0002'), D('.0005'), D('.1'), D('.1'), D('5'), D('100'))
        model = kwargs.pop('execution', self.m.ExecutionModel(latency_ms=10, exit_slippage_ticks=2, depth_extra_ticks=None))
        b = self.m.SimBroker(state_path or Path(self.tmp.name)/('isolated-'+str(len(self.brokers))+'.sqlite'), initial_cash=D('100'), instruments=[settings], execution=model, risk=risk, version_id='artificial-v1', forward_start=100, **kwargs)
        self.brokers.append(b)
        b.on_event(dict(type='mark',event_id='mark-initial-artificial',symbol='TEST',ts=100,price='100'))
        if funding_ready:
            b.on_event(dict(type='funding_status',event_id='funding-ready-artificial',symbol='TEST',ts=100,complete=True,valid_until_ts=1000000000))
        return b

    def artificial_risk(self, **kwargs):
        values = dict(approved=True, version='ARTIFICIAL-UNIT-TEST-ONLY', max_loss_per_trade_usdt=D('30'), max_daily_loss_usdt=D('40'), max_effective_exposure_x=D('10'), max_positions=2, total_loss_limit_usdt=D('60'), require_stop=True)
        values.update(kwargs)
        return self.m.RiskContract(**values)

    def intent(self, id='i1', side='BUY', qty='1', stop='90', **kwargs):
        values = dict(intent_id=id, symbol='TEST', side=side, qty=D(qty), stop=D(stop) if stop is not None else None, decision_ts=100, quantity_step=D('.1'), tick=D('.1'), min_notional=D('5'), max_quantity=D('100'), kind='TAKER', reduce_only=False)
        values.update(kwargs)
        return self.m.Intent(**values)

    def test_missing_risk_rejects_opening_records_reason(self):
        b = self.broker()
        result = b.submit(self.intent())
        self.assertEqual(result['status'], 'REJECTED')
        self.assertEqual(result['reason'], 'risk_missing_or_unapproved')
        self.assertEqual(b.audit[-1]['intent_id'], 'i1')
        self.assertEqual(b.cash, D('100'))

    def book(self, ts=111, bids=None, asks=None, id=None):
        return dict(event_id=id or 'book:'+str(ts), type='book', ts=ts, symbol='TEST', bids=bids or [['99','10']], asks=asks or [['100','10']])

    def test_taker_waits_latency_consumes_depth_charges_each_fill(self):
        b = self.broker(self.artificial_risk())
        self.assertEqual(b.submit(self.intent(qty='2'))['status'], 'PENDING')
        b.on_event(self.book(ts=110))
        self.assertFalse(b.fills)
        b.on_event(self.book(asks=[['100','1'],['101','1']]))
        self.assertEqual([f['qty'] for f in b.fills], ['1','1'])
        self.assertEqual([f['price'] for f in b.fills], ['100','101'])
        self.assertEqual(b.cash, D('99.8995'))
        self.assertEqual(b.positions['TEST']['qty'], '2')
        self.assertEqual(b.positions['TEST']['entry'], '100.5')
        self.assertEqual(len(b.ledger), 2)
        self.assertTrue(all(f['version_id']=='artificial-v1' for f in b.fills))

    def test_incomplete_unapproved_nonfinite_contract_rejects(self):
        for change in ({'approved':False}, {'version':None}, {'max_positions':None}, {'max_loss_per_trade_usdt':D('NaN')}, {'max_effective_exposure_x':None}, {'require_stop':False}):
            with self.subTest(change=change):
                b = self.broker(self.artificial_risk(**change))
                self.assertEqual(b.submit(self.intent())['reason'], 'risk_missing_or_unapproved')
                b.close()

    def test_invalid_intent_filters_fail_closed(self):
        b = self.broker(self.artificial_risk())
        cases = [dict(qty=D('NaN')), dict(qty=D('0')), dict(qty=D('.15')), dict(qty=D('101')), dict(side='LONG'), dict(symbol='OTHER'), dict(quantity_step=D('.2')), dict(tick=D('0')), dict(stop=None), dict(stop=D('90.01')), dict(kind='LIVE'), dict(decision_ts=99)]
        for n, change in enumerate(cases):
            with self.subTest(change=change):
                result = b.submit(self.intent(id='bad'+str(n), **change))
                self.assertEqual(result['status'], 'REJECTED')
                self.assertTrue(result['reason'])

    def test_arrival_risk_checks_stop_loss_exit_cost_exposure_min_notional(self):
        scenarios = [({'max_loss_per_trade_usdt':D('10.1')}, '90', '1', 'risk_per_trade'), ({'max_effective_exposure_x':D('1')}, '90', '1', 'risk_exposure'), ({},'100','1','wrong_stop_side'), ({},'90','.1','min_notional')]
        for n, (changes, stop, qty, expected) in enumerate(scenarios):
            with self.subTest(expected=expected):
                b = self.broker(self.artificial_risk(**changes))
                b.submit(self.intent(stop=stop, qty=qty))
                b.on_event(self.book(asks=[['10','10']],bids=[['9','10']]) if expected=='min_notional' else self.book())
                self.assertFalse(b.fills)
                self.assertEqual(b.orders['order:i1']['reason'], expected)
                b.close()

    def test_depth_shortfall_reject_or_explicit_adverse_slippage(self):
        b = self.broker(self.artificial_risk())
        b.submit(self.intent(qty='2'))
        b.on_event(self.book(asks=[['100','1']]))
        self.assertFalse(b.fills)
        self.assertEqual(b.orders['order:i1']['reason'], 'insufficient_depth')
        b.close()
        b = self.broker(self.artificial_risk(),execution=self.m.ExecutionModel(10, 2, 5))
        b.submit(self.intent(qty='2'))
        b.on_event(self.book(asks=[['100','1']]))
        self.assertEqual([f['price'] for f in b.fills], ['100','100.5'])
        self.assertTrue(b.fills[-1]['modeled_depth'])

    def trade(self, ts, price, qty, aggressor='SELL', id=None):
        return dict(type='aggTrade', event_id=id or 'trade:'+str(ts), symbol='TEST', ts=ts, price=price, qty=qty, aggressor=aggressor)

    def test_maker_strict_trade_through_queue_partial_fee(self):
        b = self.broker(self.artificial_risk())
        b.submit(self.intent(kind='MAKER', limit_price=D('99'), queue_ahead_qty=D('.5')))
        b.on_event(self.book())
        self.assertFalse(b.fills)
        self.assertEqual(b.orders['order:i1']['status'], 'RESTING')
        b.on_event(self.trade(112, '99', '10'))
        b.on_event(self.trade(113, '98', '10', 'BUY'))
        self.assertFalse(b.fills)
        b.on_event(self.trade(114, '98', '.7'))
        self.assertEqual(b.fills[-1]['qty'], '0.2')
        self.assertEqual(b.fills[-1]['price'], '99')
        self.assertEqual(b.fills[-1]['fee'], '0.00396')
        b.on_event(self.trade(115, '98', '.8'))
        self.assertEqual(sum(D(f['qty']) for f in b.fills), D('1'))
        self.assertEqual(b.orders['order:i1']['status'], 'FILLED')

    def test_post_only_cross_unknown_queue_cancel_expiry_no_fill(self):
        b = self.broker(self.artificial_risk())
        self.assertEqual(b.submit(self.intent(id='unknown',kind='MAKER',limit_price=D('99')))['reason'], 'unknown_queue')
        b.submit(self.intent(id='cross',kind='MAKER',limit_price=D('100'),queue_ahead_qty=D('1')))
        b.on_event(self.book())
        self.assertEqual(b.orders['order:cross']['reason'], 'post_only_cross')
        b.submit(self.intent(id='cancel',kind='MAKER',limit_price=D('99'),queue_ahead_qty=D('1'),decision_ts=111))
        b.on_event(self.book(ts=122))
        b.cancel('order:cancel', ts=123)
        b.on_event(self.trade(124,'98','10'))
        b.submit(self.intent(id='expiry',kind='MAKER',limit_price=D('99'),queue_ahead_qty=D('1'),decision_ts=124,expires_ts=136))
        b.on_event(self.book(ts=135))
        b.on_event(self.trade(136,'98','10'))
        self.assertFalse(b.fills)
        self.assertEqual(b.orders['order:expiry']['status'], 'EXPIRED')

    def open_position(self, b, side='BUY', stop='90'):
        b.submit(self.intent(side=side,stop=stop))
        b.on_event(self.book())

    def test_adverse_reduce_closes_without_risk_with_realized_ledger(self):
        b = self.broker(self.artificial_risk())
        self.open_position(b)
        b.risk = None
        result = b.submit(self.intent(id='close',side='SELL',stop=None,decision_ts=112,reduce_only=True))
        self.assertEqual(result['status'], 'PENDING')
        b.on_event(self.book(ts=123,bids=[['80','10']]))
        self.assertNotIn('TEST', b.positions)
        self.assertEqual(b.cash,D('79.9100'))
        self.assertEqual([x['type'] for x in b.ledger], ['fee','realized','fee'])
        self.assertEqual(b.fills[-1]['price'], '80')
        self.assertEqual(b.submit(self.intent(id='overclose',side='SELL',stop=None,decision_ts=124,reduce_only=True))['reason'],'no_reducible_position')

    def mark(self, ts, price, id=None):
        return dict(type='mark',event_id=id or 'mark:'+str(ts),ts=ts,symbol='TEST',price=price)

    def test_mark_stop_only_gap_no_quote_no_fabricated_closure(self):
        b = self.broker(self.artificial_risk())
        self.open_position(b)
        b.on_event(self.trade(112,'80','10'))
        self.assertEqual(len(b.orders),1)
        b.on_event(self.mark(113,'80'))
        self.assertEqual(len(b.orders),2)
        self.assertIn('TEST', b.positions)
        self.assertEqual(len(b.fills),1)
        b.on_event(self.book(ts=123,bids=[['80','10']]))
        self.assertIn('TEST',b.positions)
        b.on_event(self.book(ts=124,bids=[['78','10']]))
        self.assertNotIn('TEST',b.positions)
        self.assertEqual(b.fills[-1]['price'],'78')
        self.assertEqual(b.cash,D('77.9110'))

    def funding(self, ts, rate='.01', id=None):
        return dict(type='funding', event_id=id or 'funding:'+str(ts), symbol='TEST', ts=ts, settlement_ts=ts, rate=rate, mark='100', finalized=True)

    def test_exact_finalized_funding_long_short_idempotent_boundaries(self):
        for side,stop,expected in [('BUY','90',D('98.9500')),('SELL','110',D('100.9505'))]:
            with self.subTest(side=side):
                b=self.broker(self.artificial_risk())
                self.open_position(b,side=side,stop=stop)
                f=self.funding(112)
                b.on_event(f)
                self.assertEqual(b.cash,expected)
                b.on_event(f)
                b.on_event(dict(f,event_id='duplicate-source-id'))
                self.assertEqual(b.cash,expected)
                self.assertEqual(len([x for x in b.ledger if x['type']=='funding']),1)
                b.close()
        b=self.broker(self.artificial_risk())
        self.open_position(b)
        b.on_event(self.funding(111))
        self.assertEqual(b.cash,D('99.9500'))
        with self.assertRaises(ValueError):
            b.on_event(dict(self.funding(113),finalized=False))

    def test_duplicate_intent_event_restart_lock_and_version_freeze(self):
        b=self.broker(self.artificial_risk())
        self.open_position(b)
        state_path=b.path
        first=b.submit(self.intent())
        self.assertEqual(first['status'],'FILLED')
        b.on_event(self.book())
        self.assertEqual(len(b.fills),1)
        b.on_event(self.funding(112))
        cash=b.cash
        with self.assertRaises(RuntimeError):
            self.broker(self.artificial_risk(), state_path=state_path)
        b.close()
        restarted=self.broker(self.artificial_risk(), state_path=state_path)
        self.assertEqual(restarted.cash,cash)
        self.assertEqual(len(restarted.fills),1)
        restarted.on_event(self.funding(112))
        self.assertEqual(restarted.cash,cash)
        restarted.on_event(self.book())
        self.assertEqual(len(restarted.fills),1)
        restarted.close()
        with self.assertRaises(ValueError):
            self.m.SimBroker(state_path,initial_cash=D('100'),instruments=list(b.instruments.values()),execution=b.execution,risk=self.artificial_risk(),version_id='changed',forward_start=100)

    def test_incomplete_funding_blocks_entries_not_reductions(self):
        b=self.broker(self.artificial_risk(),funding_ready=False)
        self.assertEqual(b.submit(self.intent(id='missing'))['reason'],'funding_feed_incomplete')
        b.on_event(dict(type='funding_status',event_id='ready',symbol='TEST',ts=100,complete=True,valid_until_ts=120))
        self.open_position(b)
        b.on_event(dict(type='funding_status',event_id='lost',symbol='TEST',ts=112,complete=False,valid_until_ts=120))
        self.assertEqual(b.submit(self.intent(id='blocked',decision_ts=112))['reason'],'funding_feed_incomplete')
        self.assertEqual(b.submit(self.intent(id='reduce',side='SELL',stop=None,decision_ts=112,reduce_only=True))['status'],'PENDING')

    def test_equity_uses_mark_unrealized_daily_total_and_position_risk(self):
        b=self.broker(self.artificial_risk())
        self.open_position(b)
        b.on_event(self.mark(112,'95'))
        self.assertEqual(getattr(b,'equity',None),D('94.9500'))
        b.risk=self.artificial_risk(max_daily_loss_usdt=D('5'))
        b.submit(self.intent(id='daily',qty='.1',decision_ts=112,stop='90'))
        b.on_event(self.book(ts=123))
        self.assertEqual(b.orders['order:daily']['reason'],'risk_daily_loss')
        b.risk=self.artificial_risk(total_loss_limit_usdt=D('5'))
        b.submit(self.intent(id='total',qty='.1',decision_ts=123))
        b.on_event(self.book(ts=134))
        self.assertEqual(b.orders['order:total']['reason'],'risk_total_loss')

    def test_opposite_opening_position_limit_and_concurrent_depth_budget(self):
        b=self.broker(self.artificial_risk())
        self.open_position(b)
        self.assertEqual(b.submit(self.intent(id='reverse',side='SELL',stop='110',decision_ts=112))['reason'],'opposite_open_requires_reduce_only')
        b.close()
        b=self.broker(self.artificial_risk(max_positions=1))
        b.submit(self.intent(id='first'))
        b.submit(self.intent(id='second'))
        b.on_event(self.book(asks=[['100','1']]))
        self.assertEqual(sum(D(f['qty']) for f in b.fills),D('1'))
        self.assertEqual(b.orders['order:second']['reason'],'insufficient_depth')

    def test_invalid_public_events_are_atomic_pre_forward_out_of_order_conflicts(self):
        b=self.broker(self.artificial_risk())
        for event in [self.book(ts=99),self.book(ts=101,asks=[['NaN','1']]),self.book(ts=102,asks=[['101','1'],['100','1']]),self.trade(102,'100','-1'),self.mark(102,'Infinity')]:
            with self.subTest(event=event):
                with self.assertRaises(ValueError):
                    b.on_event(event)
        b.on_event(self.book(ts=105))
        with self.assertRaises(ValueError):
            b.on_event(self.book(ts=104))
        with self.assertRaises(ValueError):
            b.on_event(dict(self.book(ts=105),asks=[['101','10']]))
        self.assertEqual(len(b.fills),0)
        with self.assertRaises(ValueError):
            b.submit(self.intent(id='valid',decision_ts=104))

    def test_constructor_rejects_nonfinite_fees_filters_and_negative_models(self):
        s=list(self.broker().instruments.values())[0]
        from dataclasses import replace
        for settings,model,cash in [(replace(s,taker_fee=D('NaN')),self.m.ExecutionModel(10,2,None),D('100')),(replace(s,tick=D('0')),self.m.ExecutionModel(10,2,None),D('100')),(s,self.m.ExecutionModel(-1,2,None),D('100')),(s,self.m.ExecutionModel(10,-1,None),D('100')),(s,self.m.ExecutionModel(10,2,0),D('100')),(s,self.m.ExecutionModel(10,2,None),D('Infinity'))]:
            with self.subTest(settings=settings,model=model,cash=cash):
                with self.assertRaises(ValueError):
                    self.m.SimBroker(Path(self.tmp.name)/'invalid.sqlite',initial_cash=cash,instruments=[settings],execution=model,risk=None,version_id='v',forward_start=100)

    def test_max_positions_and_aggregate_same_symbol_stop_risk(self):
        b=self.broker(self.artificial_risk(max_loss_per_trade_usdt=D('15')))
        self.open_position(b)
        b.on_event(self.mark(112,'100'))
        b.submit(self.intent(id='add',decision_ts=112))
        b.on_event(self.book(ts=123))
        self.assertEqual(b.orders['order:add']['reason'],'risk_per_trade')
        self.assertEqual(len(b.fills),1)
        b.close()
        from dataclasses import replace
        settings=list(b.instruments.values())
        settings.append(replace(settings[0],symbol='OTHER'))
        b=self.m.SimBroker(Path(self.tmp.name)/'two-symbol.sqlite',initial_cash=D('100'),instruments=settings,execution=b.execution,risk=self.artificial_risk(max_positions=1),version_id='artificial-v1',forward_start=100)
        self.brokers.append(b)
        for symbol in ('TEST','OTHER'):
            b.on_event(dict(type='funding_status',event_id='ready:'+symbol,symbol=symbol,ts=100,complete=True,valid_until_ts=10000))
        self.open_position(b)
        b.on_event(self.mark(112,'100'))
        b.submit(self.intent(id='other',symbol='OTHER',decision_ts=112))
        b.on_event(dict(self.book(ts=123),symbol='OTHER'))
        self.assertEqual(b.orders['order:other']['reason'],'risk_positions')
        self.assertEqual(len(b.positions),1)

    def test_maker_rechecks_funding_gate_before_partial_fill(self):
        b=self.broker(self.artificial_risk())
        b.submit(self.intent(kind='MAKER',limit_price=D('99'),queue_ahead_qty=D('1')))
        b.on_event(self.book())
        b.on_event(dict(type='funding_status',event_id='lost',symbol='TEST',ts=112,complete=False,valid_until_ts=200))
        b.on_event(self.trade(113,'98','10'))
        self.assertFalse(b.fills)
        self.assertEqual(b.orders['order:i1']['reason'],'funding_feed_incomplete')

    def test_stop_partial_depth_retries_and_no_overclose_after_manual_close(self):
        b=self.broker(self.artificial_risk())
        self.open_position(b)
        b.on_event(self.mark(112,'80'))
        stop_id=b.positions['TEST']['stop_order']
        b.on_event(self.book(ts=123,bids=[['80','.4']]))
        self.assertEqual(b.positions['TEST']['qty'],'0.6')
        self.assertEqual(b.orders[stop_id]['status'],'PENDING')
        b.on_event(self.book(ts=124,bids=[['79','.6']]))
        self.assertNotIn('TEST',b.positions)
        self.assertEqual(sum(D(f['qty']) for f in b.fills if f['side']=='SELL'),D('1'))
        b.close()
        b=self.broker(self.artificial_risk())
        self.open_position(b)
        b.submit(self.intent(id='c1',side='SELL',stop=None,decision_ts=112,reduce_only=True))
        b.submit(self.intent(id='c2',side='SELL',stop=None,decision_ts=112,reduce_only=True))
        b.on_event(self.book(ts=123))
        self.assertNotIn('TEST',b.positions)
        self.assertEqual(len(b.fills),2)

    def test_late_funding_close_boundary_conflicts_and_cancel_persist(self):
        b=self.broker(self.artificial_risk())
        self.open_position(b)
        b.submit(self.intent(id='close',side='SELL',stop=None,decision_ts=112,reduce_only=True))
        b.on_event(self.book(ts=123))
        b.on_event(dict(self.funding(123),ts=124))
        self.assertEqual(b.ledger[-1]['qty'],'1')
        cash=b.cash
        with self.assertRaises(ValueError):
            b.on_event(dict(self.funding(123,rate='.02',id='conflict-funding'),ts=125))
        self.assertEqual(b.cash,cash)
        b.submit(self.intent(id='cancel-persist',decision_ts=124))
        b.cancel('order:cancel-persist',ts=125)
        path=b.path
        b.close()
        b=self.broker(self.artificial_risk(),state_path=path)
        self.assertEqual(b.orders['order:cancel-persist']['status'],'CANCELED')

    def test_artificial_selfcheck_callable_and_sim_only_interface(self):
        check=getattr(self.m,'self_check',None)
        self.assertTrue(callable(check),'missing runnable engineering self-check')
        result=check()
        self.assertEqual(result['label'],'ARTIFICIAL ENGINEERING SELF-CHECK — NOT MARKET PERFORMANCE')
        self.assertTrue(result['ok'])
        self.assertFalse(hasattr(self.m.SimBroker,'live_order'))

    def test_sell_maker_short_stop_negative_funding_and_source_time(self):
        b=self.broker(self.artificial_risk())
        b.submit(self.intent(side='SELL',stop='110',kind='MAKER',limit_price=D('101'),queue_ahead_qty=D('1')))
        b.on_event(self.book())
        b.on_event(self.trade(112,'101','10','BUY'))
        self.assertFalse(b.fills)
        b.on_event(self.trade(113,'102','1.5','BUY'))
        self.assertEqual(b.positions['TEST']['qty'],'-0.5')
        b.on_event(self.funding(114,rate='-.01'))
        self.assertEqual(b.ledger[-1]['amount'],'-0.500')
        b.on_event(self.mark(115,'111'))
        b.on_event(dict(self.book(ts=126,asks=[['120','10']],bids=[['119','10']]),source_ts=124))
        self.assertIn('TEST',b.positions)
        b.on_event(dict(self.book(ts=127,asks=[['121','10']],bids=[['120','10']]),source_ts=127))
        self.assertNotIn('TEST',b.positions)
        self.assertEqual(b.fills[-1]['side'],'BUY')
        self.assertEqual(b.fills[-1]['price'],'121')

    def test_restart_preserves_submission_fifo_not_lexical_ids(self):
        b=self.broker(self.artificial_risk())
        for id in ('z-first','a-second'):
            b.submit(self.intent(id=id,kind='MAKER',limit_price=D('99'),queue_ahead_qty=D('.1')))
        b.on_event(self.book())
        path=b.path
        b.close()
        b=self.broker(self.artificial_risk(),state_path=path)
        b.on_event(self.trade(112,'98','1.1'))
        self.assertEqual(b.fills[0]['intent_id'],'z-first')

    def test_incomplete_risk_revoked_at_arrival_and_snapshot_return_not_mutable(self):
        b=self.broker(self.artificial_risk())
        result=b.submit(self.intent())
        result['intent']['side']='SELL'
        self.assertEqual(b.orders['order:i1']['intent']['side'],'BUY')
        b.risk=self.m.RiskContract(approved=True)
        b.on_event(self.book())
        self.assertFalse(b.fills)
        self.assertEqual(b.orders['order:i1']['reason'],'risk_missing_or_unapproved')

    def test_decimal_context_and_model_metadata_cannot_change_past_version(self):
        from decimal import localcontext
        b=self.broker(self.artificial_risk())
        with localcontext() as ctx:
            ctx.prec=3
            b.submit(self.intent())
            b.on_event(self.book(asks=[['100.1','10']]))
        self.assertEqual(b.fills[0]['fee'],'0.05005')
        b.version_id='forbidden-mutation'
        with self.assertRaises(ValueError):
            b.on_event(self.mark(112,'100'))

    def test_parent_event_aliases_late_funding_after_close_exact_milliseconds(self):
        b=self.broker(self.artificial_risk())
        self.open_position(b)
        b.submit(self.intent(id='close',side='SELL',stop=None,decision_ts=112,reduce_only=True))
        b.on_event(self.book(ts=123))
        b.on_event(dict(kind='funding',symbol='TEST',ts_ms=120,rate='.01',mark_price='100',id='history:120',source='/fapi/v1/fundingRate',finalized=True))
        self.assertEqual(b.ledger[-1]['qty'],'1')
        self.assertEqual(b.ledger[-1]['ts'],120)
        self.assertEqual(b.last_ts,123)
        b.on_event(dict(kind='book',symbol='TEST',ts_ms=125,observed_ms=126,bids=[['99','10']],asks=[['100','10']],id='depth:125',source='/fapi/v1/depth'))
        self.assertEqual(b.last_ts,126)

    def test_daily_budget_reserves_existing_open_stop_losses(self):
        b=self.broker(self.artificial_risk(max_daily_loss_usdt=D('15')))
        self.open_position(b)
        b.submit(self.intent(id='more',qty='.6',decision_ts=112))
        b.on_event(self.book(ts=123))
        self.assertEqual(b.orders['order:more']['reason'],'risk_daily_loss')
        self.assertEqual(len(b.fills),1)

    def test_triggered_stop_cancels_unfilled_opening_remainder(self):
        b=self.broker(self.artificial_risk())
        b.submit(self.intent(kind='MAKER',limit_price=D('99'),queue_ahead_qty=D('.1')))
        b.on_event(self.book())
        b.on_event(self.trade(112,'98','.6'))
        self.assertEqual(b.positions['TEST']['qty'],'0.5')
        b.on_event(self.mark(113,'80'))
        self.assertEqual(b.orders['order:i1']['status'],'CANCELED')
        b.on_event(self.trade(114,'98','10'))
        self.assertEqual(b.positions['TEST']['qty'],'0.5')

    def test_external_min_quantity_category_filters_and_fill_tags(self):
        from dataclasses import replace
        self.assertIn('min_quantity',self.m.InstrumentSettings.__dataclass_fields__)
        self.assertIn('category',self.m.InstrumentSettings.__dataclass_fields__)
        old=self.broker(self.artificial_risk())
        s=replace(list(old.instruments.values())[0],min_quantity=D('.5'),category='ARTIFICIAL')
        old.close()
        b=self.m.SimBroker(Path(self.tmp.name)/'category.sqlite',initial_cash=D('100'),instruments=[s],execution=old.execution,risk=self.artificial_risk(),version_id='artificial-v1',forward_start=100)
        self.brokers.append(b)
        b.on_event(dict(type='funding_status',event_id='ready',symbol='TEST',ts=100,complete=True,valid_until_ts=1000))
        self.assertEqual(b.submit(self.intent(id='small',qty='.1'))['reason'],'instrument_filters')
        self.open_position(b)
        self.assertEqual(b.fills[-1]['category'],'ARTIFICIAL')
        self.assertTrue(b.fills[-1]['sim_only'])

    def test_explicit_entry_exit_fill_slippage_ticks_are_adverse(self):
        self.assertIn('entry_slippage_ticks',self.m.ExecutionModel.__dataclass_fields__)
        self.assertIn('exit_fill_slippage_ticks',self.m.ExecutionModel.__dataclass_fields__)
        b=self.broker(self.artificial_risk(),execution=self.m.ExecutionModel(10,2,None,entry_slippage_ticks=2,exit_fill_slippage_ticks=2))
        self.open_position(b)
        self.assertEqual(b.fills[-1]['price'],'100.2')
        self.assertEqual(b.fills[-1]['fill_slippage_ticks'],2)
        b.submit(self.intent(id='exit',side='SELL',stop=None,decision_ts=112,reduce_only=True))
        b.on_event(self.book(ts=123,bids=[['80','10']]))
        self.assertEqual(b.fills[-1]['price'],'79.8')
        self.assertEqual(b.cash,D('79.51000'))

    def test_regular_and_special_funding_same_exact_timestamp_are_distinct(self):
        b=self.broker(self.artificial_risk())
        self.open_position(b)
        b.on_event(dict(self.funding(112),rate_type='Regular'))
        self.assertEqual(b.ledger[-1].get('rate_type'),'Regular')
        b.on_event(dict(self.funding(112,rate='.02',id='special'),rate_type='Special'))
        self.assertEqual(b.cash,D('96.9500'))
        self.assertEqual(len([r for r in b.ledger if r['type']=='funding']),2)
        b.on_event(dict(self.funding(112,rate='.02',id='special-repeat'),rate_type='Special'))
        self.assertEqual(b.cash,D('96.9500'))

    def test_postfill_exposure_reserves_immediate_adverse_mark_difference(self):
        cap=(D('100')/D('99.95')+D('99')/D('98.95'))/2
        b=self.broker(self.artificial_risk(max_effective_exposure_x=cap))
        b.on_event(self.mark(101,'99'))
        b.submit(self.intent(decision_ts=101))
        b.on_event(self.book(ts=112))
        self.assertFalse(b.fills)
        self.assertEqual(b.orders['order:i1']['reason'],'risk_exposure')

if __name__ == '__main__':
    unittest.main()
