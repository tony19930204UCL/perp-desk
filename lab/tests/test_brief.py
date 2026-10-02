"""Hand-made snapshots for tests only; never copied to runtime data."""
import importlib
import unittest
import json
import io
import tempfile
from pathlib import Path
from contextlib import redirect_stdout
from datetime import datetime, timezone

try:
    brief = importlib.import_module('brief')
except ModuleNotFoundError:
    brief = None


def handmade_snapshot():
    stamp = '2026-10-01T00:00:00Z'
    ms = int(datetime.fromisoformat(stamp).timestamp() * 1000)
    return {'schema_version': 1, 'mode': 'shadow', 'initial_equity_usdt': '100',
            'cash_usdt': '100.000000000000000001', 'equity_usdt': '100.000000000000000001',
            'realized_pnl_usdt': '0', 'unrealized_pnl_usdt': '0', 'total_pnl_usdt': '0',
            'positions': [], 'fills_count': 0, 'signals_count': 0, 'blocked_signals_count': 0,
            'paper_trading_enabled': False, 'live_trading_enabled': False,
            'blockers': ['risk_limits_not_set', 'sim_broker_not_implemented', 'signals_not_implemented'],
            'updated_at': stamp, 'feed': {'transport': 'REST polling', 'connected': True,
                                        'last_success_at': stamp, 'errors_count': 0, 'gaps_count': 0},
            'markets': [{'symbol': symbol, 'category': category, 'bid': '100.000000000001',
                         'ask': '100.000000000002', 'mark_price': '100.000000000001',
                         'last_received_at': stamp, 'source_timestamps_ms': {'bookTicker': ms, 'premiumIndex': ms, 'depth5': ms}}
                        for symbol, category in [('ETHUSDT', 'crypto'), ('XAUUSDT', 'TradFi')]],
            'latest_error': None}


class BriefTests(unittest.TestCase):
    def test_ninety_second_old_data_is_not_reported_as_fresh(self):
        message = brief.render(handmade_snapshot(), now=datetime(2026, 10, 1, 0, 1, 30, tzinfo=timezone.utc))
        self.assertIn('過期', message, 'brief must follow the documented 60-second freshness contract')
        self.assertNotIn('資料：新鮮', message)

    def test_multiline_or_wrong_transport_metadata_is_malformed(self):
        for corruption in ('category', 'transport'):
            snapshot = handmade_snapshot()
            if corruption == 'category':
                snapshot['markets'][0]['category'] = 'crypto\nextra misleading line'
            else:
                snapshot['feed']['transport'] = 'WebSocket\nextra misleading line'
            message = brief.render(snapshot)
            self.assertIn('資料缺失或格式錯誤', message)
            self.assertLessEqual(len(message.splitlines()), 8)
            self.assertNotIn('extra misleading line', message)

    def test_cli_reads_same_snapshot_and_reports_missing_or_bad_file(self):
        self.assertTrue(hasattr(brief, 'main'), 'missing daily message-only CLI')
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir(), prefix='brief-unit-') as tmp:
            path = Path(tmp) / 'handmade-test-snapshot.json'
            for contents in (None, '{malformed', json.dumps(handmade_snapshot())):
                if contents is not None:
                    path.write_text(contents)
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(brief.main(['--status', str(path)]), 0)
                report = output.getvalue()
                self.assertLessEqual(len(report.splitlines()), 8)
                if contents is None or contents == '{malformed':
                    self.assertIn('資料缺失或格式錯誤', report)
                else:
                    self.assertIn('100.000000000000000001', report)
            snapshot = handmade_snapshot()
            snapshot['feed']['connected'] = False
            snapshot['latest_error'] = 'hand-made failure\nsecond line'
            report = brief.render(snapshot)
            self.assertIn('hand-made failure second line', report)
            self.assertLessEqual(len(report.splitlines()), 8)

    def test_missing_or_malformed_snapshot_is_explicit_and_fail_closed(self):
        for invalid in (None, [], {}, {'schema_version': 999}, {'schema_version': 1, 'mode': 'live'}):
            with self.subTest(snapshot=invalid):
                try:
                    report = brief.render(invalid)
                except Exception as exc:
                    report = f'uncaught missing-data error: {exc}'
                self.assertIn('資料缺失或格式錯誤', report)
                self.assertIn('實際錢包未知', report)
                self.assertIn('停用', report)
                self.assertLessEqual(len(report.splitlines()), 8)
        for key, wrong in [('cash_usdt', 0.1), ('equity_usdt', 'NaN'),
                           ('paper_trading_enabled', True), ('live_trading_enabled', True),
                           ('blockers', []), ('fills_count', 1)]:
            snapshot = handmade_snapshot()
            snapshot[key] = wrong
            self.assertIn('資料缺失或格式錯誤', brief.render(snapshot))
        snapshot = handmade_snapshot()
        snapshot['markets'] = []
        report = brief.render(snapshot)
        self.assertIn('資料缺失', report)
        self.assertNotIn('資料：新鮮', report)

    def test_read_time_freshness_rejects_old_or_disconnected_feed(self):
        snapshot = handmade_snapshot()
        now = datetime(2026, 10, 1, 0, 10, tzinfo=timezone.utc)
        report = brief.render(snapshot, now=now)
        self.assertIn('過期', report)
        self.assertNotIn('資料：新鮮', report)
        self.assertLessEqual(len(report.splitlines()), 8)
        now = datetime(2026, 10, 1, 0, 0, 10, tzinfo=timezone.utc)
        snapshot['feed']['connected'] = False
        self.assertIn('過期', brief.render(snapshot, now=now))
        snapshot['feed']['connected'] = True
        snapshot['markets'][0]['source_timestamps_ms']['bookTicker'] -= 180000
        self.assertIn('過期', brief.render(snapshot, now=now))

    def test_fresh_report_is_factual_eight_lines_with_decimal_precision(self):
        self.assertIsNotNone(brief, 'missing factual Traditional Chinese report')
        message = brief.render(handmade_snapshot(), now=datetime(2026, 10, 1, 0, 0, 10, tzinfo=timezone.utc))
        self.assertLessEqual(len(message.splitlines()), 8)
        self.assertIn('100 USDT 僅為 PAPER', message)
        self.assertIn('實際錢包未知', message)
        self.assertIn('新鮮', message)
        self.assertIn('REST polling', message)
        self.assertIn('100.000000000000000001', message)
        self.assertIn('風險限額未設定', message)
        self.assertIn('未實作', message)
        self.assertIn('實盤停用', message)
        self.assertNotIn('獲利', message)


if __name__ == '__main__':
    unittest.main()
