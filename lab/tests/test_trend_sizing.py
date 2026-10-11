"""Offline differential and configuration checks for daily trend sizing."""
import copy
import json
from decimal import Decimal as D
from pathlib import Path
import sys
import unittest

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
from paper_sizing import size_long
from trend_sizing import size_trend_long

FILTERS = {
    'BTCUSDT': ('0.1', '0.001', '0.001', '100', '65000', '64999.9'),
    'ETHUSDT': ('0.01', '0.001', '0.001', '20', '3200', '3199.99'),
    'SOLUSDT': ('0.01', '1', '1', '5', '150', '149.99'),
    'XRPUSDT': ('0.0001', '0.1', '0.1', '5', '2.5', '2.4999'),
}


def fixture(symbol):
    tick, step, minimum, notional, ask, bid = FILTERS[symbol]
    config = json.loads((LAB / ('paper_config_t1_' + symbol.lower() + '.json')).read_text())
    instrument = dict(tick_size=tick, qty_step=step, min_qty=minimum,
                      max_qty='1000000', min_notional=notional, taker_fee='0.0005')
    return config, instrument, dict(equity='100', bid=bid, ask=ask)


class TrendSizingTests(unittest.TestCase):
    def test_differential_btc(self): self.check_differential('BTCUSDT')
    def test_differential_eth(self): self.check_differential('ETHUSDT')
    def test_differential_sol(self): self.check_differential('SOLUSDT')
    def test_differential_xrp(self): self.check_differential('XRPUSDT')

    def check_differential(self, symbol):
        config, instrument, book = fixture(symbol)
        trend = size_trend_long(config, instrument, **book)
        baseline = size_long(config, instrument, **book, target=str(D(book['ask']) * D('1.5')))
        self.assertEqual(trend['status'], baseline['status'])
        self.assertEqual(trend['status'], 'accepted')
        for field in ('qty', 'stop_price', 'entry_price_bound', 'planned_loss_per_unit',
                      'execution_qty_step'):
            self.assertEqual(trend[field], baseline[field])
        self.assertLessEqual(D(trend['qty']) * D(trend['planned_loss_per_unit']), D('15'))
        self.assertLessEqual(D(trend['qty']) * D(trend['entry_price_bound']), D('100'))
        self.assertNotIn('target', trend)
        self.assertNotIn('estimated_round_trip_cost_per_unit', trend)

    def test_invalid_decimal(self):
        c, i, b = fixture('BTCUSDT')
        b['equity'] = 'NaN'
        self.assertEqual(size_trend_long(c, i, **b)['reason'],
                         size_long(c, i, **b, target='100000')['reason'])

    def test_crossed_book(self):
        c, i, b = fixture('BTCUSDT')
        b['bid'] = b['ask']
        self.assertEqual(size_trend_long(c, i, **b)['reason'],
                         size_long(c, i, **b, target='100000')['reason'])

    def test_below_min_qty(self):
        c, i, b = fixture('BTCUSDT')
        i['min_qty'] = '100'
        self.assertEqual(size_trend_long(c, i, **b)['reason'],
                         size_long(c, i, **b, target='100000')['reason'])

    def test_below_min_notional(self):
        c, i, b = fixture('BTCUSDT')
        i['min_notional'] = '1000'
        self.assertEqual(size_trend_long(c, i, **b)['reason'],
                         size_long(c, i, **b, target='100000')['reason'])

    def test_no_reward_gate(self):
        c, i, b = fixture('BTCUSDT')
        self.assertEqual(size_trend_long(c, i, **b)['status'], 'accepted')
        self.assertEqual(size_long(c, i, **b, target=b['ask'])['reason'],
                         'insufficient_reward_after_costs')

    def test_unapproved(self):
        c, i, b = fixture('BTCUSDT')
        c['approved'] = False
        self.assertEqual(size_trend_long(c, i, **b)['reason'], 'unapproved_paper_config')

    def test_invalid_market_step(self):
        c, i, b = fixture('BTCUSDT')
        i['market_qty_step'] = '-1'
        self.assertEqual(size_trend_long(c, i, **b)['reason'], 'invalid_market_step')

    def test_missing_fee(self):
        c, i, b = fixture('BTCUSDT')
        del i['taker_fee']
        self.assertEqual(size_trend_long(c, i, **b)['reason'], 'invalid_sizing_input')

    def test_market_step_lcm(self):
        c, i, b = fixture('BTCUSDT')
        i['market_qty_step'] = '0.003'
        r = size_trend_long(c, i, **b)
        self.assertEqual(r['status'], 'accepted')
        self.assertEqual(D(r['qty']) % D('0.003'), 0)

    def test_config_exact_changes(self):
        base = json.loads((LAB / 'paper_config_f1h_btcusdt.json').read_text())
        for symbol in FILTERS:
            with self.subTest(symbol=symbol):
                actual = json.loads((LAB / ('paper_config_t1_' + symbol.lower() + '.json')).read_text())
                expected = copy.deepcopy(base)
                expected.update(version_id='T1-' + symbol + '-PAPER',
                                risk_version='PAPER-RISK-T1-001',
                                max_loss_per_trade_usdt='15', max_daily_loss_usdt='20',
                                total_loss_limit_usdt='25', max_effective_exposure_x='1',
                                paper_leverage='1', max_positions=1)
                expected['strategy'].update(symbol=symbol, interval_ms=86400000,
                                            stop_distance_fraction='0.15',
                                            entry_lookback=20, exit_lookback=10)
                expected['research']['window_ms'] = 15552000000
                self.assertEqual(actual, expected)
                self.assertEqual(list(actual), list(expected))
                self.assertEqual(list(actual['strategy']), list(expected['strategy']))

    def test_config_risk_limits(self):
        for symbol in FILTERS:
            c, _, _ = fixture(symbol)
            with self.subTest(symbol=symbol):
                self.assertEqual(D(c['max_loss_per_trade_usdt']), D('15'))
                self.assertEqual(D(c['max_daily_loss_usdt']), D('20'))
                self.assertEqual(D(c['total_loss_limit_usdt']), D('25'))


if __name__ == '__main__':
    unittest.main()
