import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard_v2_model import build_model


NOW = 1791800000000
AT = '2026-10-12T11:33:20+00:00'
# Use a deterministic instant derived from the timestamp in the fixture.
from datetime import datetime, timezone
NOW = int(datetime.fromisoformat(AT).timestamp() * 1000)


def arm(name='A'):
    return dict(arm=name, days_in=0, days_left=0, signals=0,
                blocked=0, fills=0, round_trips=0, pnl='0',
                fees='0', max_dd='0', open_positions=0,
                feed_errors=0, feed_gaps=0, stale_min=0, loss_limit='0')


def score(*rows, at=AT):
    return dict(at=at, arms=list(rows))


def build(source=None, journal=None, ledger=None, **kw):
    return build_model(source, [] if journal is None else journal,
                       {'items': []} if ledger is None else ledger,
                       now_ms=kw.get('now_ms', NOW),
                       max_age_ms=kw.get('max_age_ms', 1000))


class DashboardV2ModelTests(unittest.TestCase):
    def test_valid_zero_is_known(self):
        result = build(score(arm()))
        self.assertEqual(result['scorecard_state'], 'known')
        self.assertEqual(result['arms'][0]['pnl'], {'exact': '0', 'display': '0.00'})
        self.assertEqual(result['arms'][0]['fills'], 0)

    def test_high_precision_exact(self):
        a = arm()
        a['pnl'] = '123.123456789012345678901234567890'
        a['fees'] = '0.00000000000000000000001'
        result = build(score(a))['arms'][0]
        self.assertEqual(result['pnl']['exact'], a['pnl'])
        self.assertEqual(result['pnl']['display'], '123.12')
        self.assertEqual(result['fees']['display'], '0.00')

    def test_half_up_negative_and_positive(self):
        a = arm()
        a.update(pnl='-1.005', fees='1.005', max_dd='-0.005',
                 loss_limit='99999999999999.995')
        result = build(score(a))['arms'][0]
        self.assertEqual([result[k]['display'] for k in
                          ('pnl', 'fees', 'max_dd', 'loss_limit')],
                         ['-1.01', '1.01', '-0.01', '100000000000000.00'])

    def test_missing_scorecard(self):
        result = build(None)
        self.assertEqual(result['scorecard_state'], 'unknown')
        self.assertEqual(result['arms'], [])
        self.assertTrue(result['reasons'])

    def test_stale_boundary_inclusive(self):
        result = build(score(arm()), now_ms=NOW + 1000)
        self.assertEqual(result['scorecard_state'], 'known')

    def test_stale_one_ms_later(self):
        result = build(score(arm()), now_ms=NOW + 1001)
        self.assertEqual(result['scorecard_state'], 'unknown')
        self.assertEqual(result['arms'], [])

    def test_future_timestamp(self):
        result = build(score(arm()), now_ms=NOW - 1)
        self.assertEqual(result['scorecard_state'], 'unknown')
        self.assertEqual(result['arms'], [])

    def test_invalid_timestamp(self):
        for at in (None, 'tomorrow', '2026-10-12T11:33:20'):
            result = build(score(arm(), at=at))
            self.assertEqual(result['scorecard_state'], 'unknown')
            self.assertEqual(result['arms'], [])

    def test_top_level_invalid(self):
        for source in ('bad', {}, {'at': AT, 'arms': {}}):
            self.assertEqual(build(source)['scorecard_state'], 'unknown')

    def test_duplicate_arm_id(self):
        result = build(score(arm('X'), arm('X')))
        self.assertEqual(result['scorecard_state'], 'unknown')
        self.assertEqual(result['arms'], [])

    def test_invalid_and_boolean_counts(self):
        a = arm()
        a.update(signals=True, blocked=-1, fills=1.2, open_positions='1')
        result = build(score(a))
        self.assertEqual(result['scorecard_state'], 'known')
        for name in ('signals', 'blocked', 'fills', 'open_positions'):
            self.assertIsNone(result['arms'][0][name])
        self.assertEqual(result['arms'][0]['days_left'], 0)
        self.assertTrue(result['reasons'])

    def test_fractional_days_preserved_without_rounding(self):
        a = arm()
        a.update(days_in=1.07, days_left=19.93)
        result = build(score(a))
        self.assertEqual(result['scorecard_state'], 'known')
        self.assertEqual(result['arms'][0]['days_in'], 1.07)
        self.assertEqual(result['arms'][0]['days_left'], 19.93)
        self.assertNotIn('第1臂 days_in 無效', result['reasons'])
        self.assertNotIn('第1臂 days_left 無效', result['reasons'])

    def test_days_accept_zero_integers_and_exact_decimal_strings(self):
        for elapsed, remaining in ((0, 0), (7, 21),
                                   ('1.070', '19.930'), ('0', '0')):
            with self.subTest(elapsed=elapsed, remaining=remaining):
                a = arm()
                a.update(days_in=elapsed, days_left=remaining)
                result = build(score(a))
                row = result['arms'][0]
                for name, original in (('days_in', elapsed),
                                       ('days_left', remaining)):
                    self.assertEqual(row[name], original)
                    self.assertIs(type(row[name]), type(original))
                    self.assertNotIn(f'第1臂 {name} 無效', result['reasons'])

    def test_invalid_days_become_field_specific_unknown(self):
        invalid = (True, False, -1, -0.1, float('nan'),
                   float('inf'), float('-inf'), 'NaN', 'Infinity',
                   '-0.01', 'not-a-day', '', None, [], {})
        for name in ('days_in', 'days_left'):
            for value in invalid:
                with self.subTest(name=name, value=repr(value)):
                    a = arm()
                    a[name] = value
                    result = build(score(a))
                    self.assertEqual(result['scorecard_state'], 'known')
                    self.assertIsNone(result['arms'][0][name])
                    counterpart = 'days_left' if name == 'days_in' else 'days_in'
                    self.assertEqual(result['arms'][0][counterpart], 0)
                    self.assertIn(f'第1臂 {name} 無效', result['reasons'])
                    json.dumps(result, allow_nan=False)

    def test_true_counts_still_reject_fraction_bool_and_string(self):
        names = ('signals', 'blocked', 'fills', 'round_trips',
                 'open_positions', 'feed_errors', 'feed_gaps')
        for name in names:
            for value in (1.25, True, '2'):
                with self.subTest(name=name, value=value):
                    a = arm()
                    a.update(days_in=1.07, days_left=19.93)
                    a[name] = value
                    result = build(score(a))
                    self.assertIsNone(result['arms'][0][name])
                    self.assertIn(f'第1臂 {name} 無效', result['reasons'])
                    self.assertEqual(result['arms'][0]['days_in'], 1.07)
                    self.assertEqual(result['arms'][0]['days_left'], 19.93)

    def test_nonfinite_money_and_numeric_types(self):
        a = arm()
        a.update(pnl='NaN', fees='Infinity', max_dd=2, loss_limit='-Infinity')
        result = build(score(a))['arms'][0]
        for key in ('pnl', 'fees', 'max_dd', 'loss_limit'):
            self.assertEqual(result[key], {'exact': None, 'display': '未知'})

    def test_invalid_row_is_retained(self):
        result = build(score(arm('good'), None, {'arm': 'partial'}))
        self.assertEqual(len(result['arms']), 3)
        self.assertEqual(result['arms'][0]['arm'], 'good')
        self.assertIsNone(result['arms'][1]['arm'])
        self.assertEqual(result['arms'][2]['arm'], 'partial')
        self.assertIsNone(result['arms'][2]['pnl']['exact'])

    def test_dynamic_arm_count_and_order(self):
        names = [f'ARM{i}' for i in range(17)]
        result = build(score(*(arm(name) for name in names)))
        self.assertEqual([a['arm'] for a in result['arms']], names)

    def test_journal_last_five_reverse_filtered(self):
        entries = [dict(id=str(i), at=AT, kind='K', title='T',
                        body='B', verdict='V', secret='never') for i in range(8)]
        result = build(score(arm()), journal=entries)
        self.assertEqual([item['id'] for item in result['journal']],
                         ['7', '6', '5', '4', '3'])
        self.assertEqual(set(result['journal'][0]),
                         {'id', 'at', 'kind', 'title', 'body', 'verdict'})

    def test_ledger_terminal_filter_and_privacy(self):
        items = [dict(id='a', text='todo', state='OPEN', due='soon',
                      handle='hidden', shell_command='hidden', path='hidden'),
                 dict(id='b', state='VERIFIED'), dict(id='c', state='DROPPED'),
                 dict(id='d', text='later', state='BLOCKED', due=None)]
        result = build(score(arm()), ledger={'items': items})
        self.assertEqual([c['id'] for c in result['commitments']], ['a', 'd'])
        self.assertEqual(set(result['commitments'][0]),
                         {'id', 'text', 'state', 'due'})

    def test_health_remains_unknown(self):
        result = build(score(arm()))
        self.assertEqual({k: result['health'][k] for k in
                          ('liveness', 'clock', 'scheduler')},
                         dict(liveness='unknown', clock='unknown', scheduler='unknown'))
        self.assertEqual(len(result['health']['reasons']), 3)

    def test_missing_journal_and_ledger(self):
        result = build_model(score(arm()), None, None,
                             now_ms=NOW, max_age_ms=1000)
        self.assertEqual(result['journal'], [])
        self.assertEqual(result['commitments'], [])
        self.assertGreaterEqual(len(result['reasons']), 2)

    def test_no_mutation_json_safe(self):
        src = score(arm())
        journal = [{'id': 'j', 'at': AT, 'kind': '', 'title': '', 'body': '', 'verdict': ''}]
        ledger = {'items': [{'id': 'c', 'state': 'OPEN', 'handle': 'hidden'}]}
        before = copy.deepcopy((src, journal, ledger))
        result = build_model(src, journal, ledger, now_ms=NOW, max_age_ms=1000)
        self.assertEqual((src, journal, ledger), before)
        json.dumps(result, allow_nan=False)

    def test_reject_invalid_time_parameters(self):
        for now in (True, 1.0, None):
            with self.assertRaises(ValueError):
                build(score(arm()), now_ms=now)
        for age in (-1, True, 1.2, None):
            with self.assertRaises(ValueError):
                build(score(arm()), max_age_ms=age)

    def test_stale_min_invalid_per_field(self):
        a = arm()
        a['stale_min'] = float('inf')
        result = build(score(a))
        self.assertIsNone(result['arms'][0]['stale_min'])
        self.assertEqual(result['arms'][0]['pnl']['exact'], '0')


if __name__ == '__main__':
    unittest.main()
