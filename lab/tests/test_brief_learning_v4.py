"""Regression and contract tests for lab/brief_learning_v4.py."""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

import brief_learning_v3 as v3
import brief_learning_v4 as brief

BASE = 1790812800000


def make_snapshot(*, version_id='H2-PAPER-004', symbol='ETHUSDT', now_ms=BASE,
                  initial='100', strategy_start_ms=None,
                  deadline_ms=None, target_round_trips=30, complete_round_trips=0,
                  open_episodes=0, net_cash_pnl='0', signals_count=0,
                  blocked_count=0, rejections=None, blockers=None,
                  feed_connected=True, latest_error=None, fixture=False,
                  mode='paper'):
    if strategy_start_ms is None:
        strategy_start_ms = now_ms - 86400000
    if deadline_ms is None:
        deadline_ms = strategy_start_ms + 1814400000  # 21 days

    iso_time = datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()
    hash_hex = '0' * 64

    # Derive all currency fields from initial and net_cash_pnl so that the
    # paper currency invariant in validate_paper is always satisfied:
    #   cash == initial + realized
    #   equity == cash + unrealized
    #   total == realized + unrealized
    # fees_usdt and funding_pnl_usdt are both '0', so realized == net_cash_pnl.
    _initial = Decimal(initial)
    _realized = Decimal(net_cash_pnl)
    _unrealized = Decimal('0')
    _cash = _initial + _realized
    _equity = _cash + _unrealized
    _total = _realized + _unrealized
    cash = str(_cash)
    equity = str(_equity)
    realized = str(_realized)
    unrealized = str(_unrealized)
    total_pnl = str(_total)

    return {
        'schema_version': 1,
        'mode': mode,
        'fixture': fixture,
        'live_trading_enabled': False,
        'paper_trading_enabled': True,
        'updated_at': iso_time,
        'initial_equity_usdt': initial,
        'cash_usdt': cash,
        'equity_usdt': equity,
        'realized_pnl_usdt': realized,
        'unrealized_pnl_usdt': unrealized,
        'total_pnl_usdt': total_pnl,
        'gross_realized_pnl_usdt': '0',
        'fees_usdt': '0',
        'funding_pnl_usdt': '0',
        'signals_count': signals_count,
        'fills_count': 0,
        'blocked_signals_count': blocked_count,
        'blockers': blockers or [],
        'latest_error': latest_error,
        'feed': {
            'transport': 'REST polling',
            'connected': feed_connected,
            'last_success_at': iso_time,
            'errors_count': 0,
            'gaps_count': 0,
        },
        'engine': {
            'implementation': 'paper-engine-v1',
            'version_id': version_id,
            'model_sha256': hash_hex,
            'forward_start_at': iso_time,
            'status': 'ready',
            'warmup_received': 60,
            'warmup_required': 60,
        },
        'risk': {
            'approved': True,
            'risk_version': 'PAPER-RISK-001',
            'risk_config_sha256': hash_hex,
        },
        'markets': [{
            'symbol': symbol,
            'category': 'crypto',
            'bid': '100',
            'ask': '100.01',
            'mark_price': '100',
            'funding_rate': '0.0001',
            'min_notional': '10',
            'source_timestamps_ms': {
                'bookTicker': now_ms,
                'depth5': now_ms,
                'premiumIndex': now_ms,
            },
        }],
        'positions': [],
        'versions': [],
        'equity_history': [{'equity_usdt': equity}],
        'research': {
            'strategy_start_ms': strategy_start_ms,
            'deadline_ms': deadline_ms,
            'target_complete_round_trips': target_round_trips,
            'target_not_guarantee': True,
            'status': 'unproven',
            'signals_count': signals_count,
            'blocked_signals_count': blocked_count,
            'rejection_categories': rejections or {},
            'complete_round_trips': complete_round_trips,
            'open_episodes': open_episodes,
            'gross_realized_pnl_usdt': '0',
            'fees_usdt': '0',
            'funding_pnl_usdt': '0',
            'net_cash_pnl_usdt': net_cash_pnl,
        },
    }


class BriefV4Tests(unittest.TestCase):
    def run_brief(self, snapshots, *, clock_ms=BASE):
        with tempfile.TemporaryDirectory() as td:
            paths = []
            for i, snap in enumerate(snapshots):
                p = Path(td) / f'status_{i}.json'
                p.write_text(json.dumps(snap))
                paths.append(str(p))

            argv = ['brief']
            for p in paths:
                argv.extend(['--status', p])

            with patch('sys.argv', argv), patch.object(brief.time, 'time', return_value=clock_ms / 1000), redirect_stdout(io.StringIO()) as stdout:
                rc = brief.main()
            return rc, stdout.getvalue()

    def test_healthy_single_h2_snapshot(self):
        s = make_snapshot(version_id='H2-PAPER-004')
        rc, text = self.run_brief([s])
        self.assertEqual(rc, 0)
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 8)
        self.assertIn('H2-PAPER-004', lines[0])
        self.assertIn('尚未證明優勢', lines[0])
        self.assertIn('權益 100.00 USDT', lines[1])
        self.assertIn('新窗口完整往返', lines[2])
        self.assertIn('學習：樣本不足不下結論', lines[7])

    def test_stale_snapshot_reports_unavailable_with_reason_and_exit_1(self):
        s = make_snapshot(version_id='H2-PAPER-004', now_ms=BASE)
        rc, text = self.run_brief([s], clock_ms=BASE + 60001)
        self.assertEqual(rc, 1)
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 8)
        self.assertIn('即時估值不可用', text)
        self.assertIn('stale/future snapshot', text)
        self.assertIn('最後已驗證快照', text)

    def test_fixture_rejected(self):
        s = make_snapshot(version_id='H2-PAPER-004', fixture=True)
        rc, text = self.run_brief([s])
        self.assertEqual(rc, 1)
        self.assertIn('簡報不可用', text)
        self.assertIn('fixture', text)
        self.assertNotIn('權益', text)

    def test_wrong_mode_rejected(self):
        s = make_snapshot(version_id='H2-PAPER-004', mode='shadow')
        rc, text = self.run_brief([s])
        self.assertEqual(rc, 1)
        self.assertIn('簡報不可用', text)
        self.assertTrue(text.startswith('PAPER 簡報不可用'))
        self.assertNotIn('權益', text)

    def test_h1_paper_003_rejected(self):
        s = make_snapshot(version_id='H1-PAPER-003')
        rc, text = self.run_brief([s])
        self.assertEqual(rc, 1)
        self.assertIn('簡報不可用', text)
        self.assertIn('H1-PAPER-003', text)
        self.assertNotIn('權益', text)

    def test_unknown_version_rejected(self):
        s = make_snapshot(version_id='H3-PAPER-001')
        rc, text = self.run_brief([s])
        self.assertEqual(rc, 1)
        self.assertIn('簡報不可用', text)
        self.assertNotIn('權益', text)

    def test_window_days_and_target_from_snapshot(self):
        start = BASE
        deadline = start + 7 * 86400000
        now = start + 86400000
        s = make_snapshot(
            version_id='H2-PAPER-004',
            now_ms=now,
            strategy_start_ms=start,
            deadline_ms=deadline,
            target_round_trips=10,
            complete_round_trips=3,
        )
        rc, text = self.run_brief([s], clock_ms=now)
        self.assertEqual(rc, 0)
        self.assertIn('3/10', text)
        self.assertIn('窗口 7 天', text)
        self.assertIn('已過 1 天', text)
        self.assertIn('剩餘 6 天', text)

    def test_expired_window_text(self):
        start = BASE - 10 * 86400000
        deadline = BASE - 86400000
        now = BASE
        s = make_snapshot(
            version_id='H2-PAPER-004',
            now_ms=now,
            strategy_start_ms=start,
            deadline_ms=deadline,
            target_round_trips=20,
            complete_round_trips=5,
        )
        rc, text = self.run_brief([s], clock_ms=now)
        self.assertEqual(rc, 0)
        self.assertIn('5/20', text)
        self.assertIn('窗口已過期', text)

    def test_four_arm_family_within_eight_lines(self):
        s1 = make_snapshot(version_id='H2-PAPER-004', symbol='ETHUSDT', complete_round_trips=2, net_cash_pnl='1.5')
        s2 = make_snapshot(version_id='F1H-BTCUSDT-PAPER', symbol='BTCUSDT', complete_round_trips=3, net_cash_pnl='2.0')
        s3 = make_snapshot(version_id='F1H-SOLUSDT-PAPER', symbol='SOLUSDT', complete_round_trips=1, net_cash_pnl='-0.5')
        s4 = make_snapshot(version_id='F1H-XRPUSDT-PAPER', symbol='XRPUSDT', complete_round_trips=4, net_cash_pnl='0.8')
        rc, text = self.run_brief([s1, s2, s3, s4])
        self.assertEqual(rc, 0)
        lines = text.strip().splitlines()
        self.assertLessEqual(len(lines), 8)
        self.assertEqual(len(lines), 5)
        self.assertIn('4 運作中', lines[0])
        self.assertIn('0 不可用', lines[0])
        self.assertIn('合計完整往返 10', lines[0])
        self.assertIn('3.80 USDT', lines[0])
        self.assertIn('非獨立證據', lines[0])
        self.assertIn('H2-PAPER-004: 權益 101.50 USDT，完整往返 2', lines[1])
        self.assertIn('F1H-BTCUSDT-PAPER: 權益 102.00 USDT，完整往返 3', lines[2])
        self.assertIn('F1H-SOLUSDT-PAPER: 權益 99.50 USDT，完整往返 1', lines[3])
        self.assertIn('F1H-XRPUSDT-PAPER: 權益 100.80 USDT，完整往返 4', lines[4])

    def test_ten_arms_report_omitted_count(self):
        snapshots = [
            make_snapshot(version_id=f'F1H-ARM{i}-PAPER', complete_round_trips=i)
            for i in range(10)
        ]
        rc, text = self.run_brief(snapshots)
        self.assertEqual(rc, 0)
        lines = text.strip().splitlines()
        self.assertLessEqual(len(lines), 8)
        self.assertEqual(len(lines), 8)
        self.assertIn('10 運作中', lines[0])
        self.assertIn('其餘 4 個分支省略', lines[7])

    def test_one_unavailable_arm_among_healthy_ones_named_and_exit_1(self):
        s1 = make_snapshot(version_id='H2-PAPER-004')
        s2 = make_snapshot(version_id='F1H-BTCUSDT-PAPER', now_ms=BASE - 70000)
        s3 = make_snapshot(version_id='F1H-SOLUSDT-PAPER')
        rc, text = self.run_brief([s1, s2, s3], clock_ms=BASE)
        self.assertEqual(rc, 1)
        lines = text.strip().splitlines()
        self.assertLessEqual(len(lines), 8)
        self.assertIn('2 運作中', lines[0])
        self.assertIn('1 不可用', lines[0])
        self.assertIn('F1H-BTCUSDT-PAPER: 不可用 (stale/future snapshot)', text)
        self.assertNotIn('F1H-BTCUSDT-PAPER: 權益', text)

    def test_v3_module_still_unchanged(self):
        s = make_snapshot(version_id='H1-PAPER-003')
        rendered = v3.render(s)
        self.assertIn('H1-PAPER-003', rendered)
        self.assertIn('48h目標', rendered)
        self.assertIn('0/30', rendered)
        self.assertEqual(len(rendered.splitlines()), 8)

    def test_feed_disconnected_single_arm(self):
        s = make_snapshot(version_id='H2-PAPER-004', feed_connected=False)
        rc, text = self.run_brief([s])
        self.assertEqual(rc, 1)
        self.assertIn('feed disconnected', text)
        self.assertIn('即時估值不可用', text)
        self.assertEqual(len(text.splitlines()), 8)

    def test_latest_error_single_arm(self):
        s = make_snapshot(version_id='F1H-BTCUSDT-PAPER', latest_error='depth stale', blockers=['depth stale'])
        rc, text = self.run_brief([s])
        self.assertEqual(rc, 1)
        self.assertIn('depth stale', text)
        self.assertIn('即時估值不可用', text)
        self.assertEqual(len(text.splitlines()), 8)

    def test_render_single_and_family_direct_calls(self):
        s = make_snapshot(version_id='H2-PAPER-004')
        out_single = brief.render(s, now_ms=BASE)
        self.assertEqual(len(out_single.splitlines()), 8)
        self.assertIn('H2-PAPER-004', out_single)

        out_family = brief.render([s, s], now_ms=BASE)
        self.assertIn('PAPER 家族結論', out_family)


class FmtUsdtTests(unittest.TestCase):
    """Unit tests for the fmt_usdt() helper introduced in revision 2."""

    def test_long_decimal_string_formats_to_two_places(self):
        """'100.0000000000000000' → '100.00'"""
        self.assertEqual(brief.fmt_usdt('100.0000000000000000'), '100.00')

    def test_exponent_notation_zero_formats_to_zero(self):
        """'0E-16' → '0.00'  (not '0E-16' or '-0.00')"""
        self.assertEqual(brief.fmt_usdt('0E-16'), '0.00')

    def test_negative_half_formats_to_two_places(self):
        """'-0.5' → '-0.50'"""
        self.assertEqual(brief.fmt_usdt('-0.5'), '-0.50')

    def test_tiny_negative_rounds_to_positive_zero(self):
        """'-0.000001' rounds to 0.00, not -0.00"""
        self.assertEqual(brief.fmt_usdt('-0.000001'), '0.00')

    def test_full_single_arm_render_no_exponent_or_long_decimal(self):
        """A full single-arm render must contain no 'E-' substring and no USDT
        amount with more than 2 digits after the decimal point."""
        import re
        # Use long-form Decimal strings similar to the real H2 snapshot
        s = make_snapshot(
            version_id='H2-PAPER-004',
            initial='100.0000000000000000',
            net_cash_pnl='0E-16',
        )
        # Manually override the snapshot fields with the long-form strings that
        # come from a real engine to simulate the exact problem scenario.
        s['equity_usdt'] = '100.0000000000000000'
        s['total_pnl_usdt'] = '0E-16'
        s['fees_usdt'] = '0E-16'
        s['funding_pnl_usdt'] = '0E-16'
        s['research']['net_cash_pnl_usdt'] = '0E-16'

        text = brief.render_single(s, now_ms=BASE)

        self.assertNotIn('E-', text,
                         msg='Output must not contain exponent notation')
        # No USDT amount should have more than 2 digits after the decimal point
        self.assertIsNone(
            re.search(r'\d\.\d{3,}', text),
            msg='Output must not have more than 2 digits after a decimal point'
        )


if __name__ == '__main__':
    unittest.main()
