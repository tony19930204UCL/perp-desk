import copy
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection, HTTPException
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
SCRATCH = Path(tempfile.gettempdir())


def fixture():
    now = datetime.now(timezone.utc).isoformat()
    return dict(schema_version=1, mode='shadow', initial_equity_usdt='100',
                cash_usdt='100', equity_usdt='100', realized_pnl_usdt='0',
                unrealized_pnl_usdt='0', total_pnl_usdt='0', positions=[],
                fills_count=0, signals_count=2, blocked_signals_count=2,
                paper_trading_enabled=False, live_trading_enabled=False,
                blockers=['risk_limits_not_set', 'sim_broker_not_implemented'],
                updated_at=now, collector_started_at=now,
                feed=dict(transport='REST polling', connected=True,
                          last_success_at=now, errors_count=0, gaps_count=0),
                markets=[], versions=[dict(id='OBS-001', status='observing',
                    started_at=now, sample_start_at=now, signals_count=2, fills_count=0)],
                equity_history=[dict(ts=now, equity_usdt='100')], latest_error=None)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((LAB / 'dashboard.py').exists(), 'dashboard server must exist')
        spec = importlib.util.spec_from_file_location('paper_dashboard', LAB / 'dashboard.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix='dashboard-test-', dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'status.json'
        self.data = fixture()
        self.save()
        self.html = Path(self.tmp.name) / 'dashboard.html'
        self.html.write_text('<!doctype html><title>fixture</title>', encoding='utf-8')
        self.server = self.module.make_server(0, self.path, self.html)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def save(self):
        self.path.write_text(json.dumps(self.data), encoding='utf-8')

    def request(self, path='/api/status', method='GET', headers=None):
        client = HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        client.request(method, path, headers=headers or {})
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    def test_get_preserves_snapshot_and_reads_each_request(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        result = json.loads(body)
        for key, value in self.data.items():
            self.assertEqual(result[key], value)
        self.data['signals_count'] = 9
        self.save()
        self.assertEqual(json.loads(self.request()[2])['signals_count'], 9)
        self.assertEqual(self.request('/')[2], self.html.read_bytes())
        self.assertEqual(self.server.server_address[0], '127.0.0.1')

    def test_unavailable_snapshot_has_explicit_503_and_never_fakes_balance(self):
        for payload in (None, '{malformed', '{}', '[]'):
            if payload is None:
                self.path.unlink(missing_ok=True)
            else:
                self.path.write_text(payload, encoding='utf-8')
            try:
                status, headers, body = self.request()
            except HTTPException:
                self.fail('missing snapshot must receive an honest 503, not a dropped connection')
            self.assertEqual(status, 503)
            result = json.loads(body)
            self.assertEqual(result['available'], False)
            self.assertIn('error', result)
            self.assertNotIn('equity_usdt', result)
            self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_stale_snapshot_is_labeled_at_request_time(self):
        status, _, body = self.request()
        self.assertIn('feed_stale', json.loads(body), 'freshness must be derived on read')
        self.assertFalse(json.loads(body)['feed_stale'])
        self.data['feed']['last_success_at'] = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        self.save()
        result = json.loads(self.request()[2])
        self.assertTrue(result['feed_stale'])
        self.assertTrue(result['feed']['connected'], 'raw source must remain distinguishable from derived state')
        self.assertGreaterEqual(result['feed_age_seconds'], 89)

    def test_rejects_untrusted_host_or_origin(self):
        for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request(headers=headers)[0], 403)
        self.assertEqual(self.request(headers={'Origin': f'http://127.0.0.1:{self.server.server_port}'})[0], 200)

    def test_read_only_methods_and_paths_never_mutate_snapshot(self):
        before = self.path.read_bytes()
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            self.assertEqual(self.request(method=method)[0], 405)
        for path in ('/../../status.json', '/data/observations.sqlite3', '/api/order'):
            self.assertEqual(self.request(path=path)[0], 404)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("default-src 'self'", self.request('/')[1]['Content-Security-Policy'])

    def test_malformed_fields_do_not_render_as_valid_money(self):
        for key, invalid in [('equity_usdt', 'NaN'), ('cash_usdt', 100), ('feed', []), ('positions', {}), ('live_trading_enabled', True)]:
            data = copy.deepcopy(self.data)
            data[key] = invalid
            self.path.write_text(json.dumps(data))
            try:
                status, _, body = self.request()
            except HTTPException:
                self.fail('malformed fields must fail closed with 503')
            self.assertEqual(status, 503, f'invalid {key} accepted')
            self.assertFalse(json.loads(body)['available'])

    def test_dashboard_asset_is_self_contained_and_labels_paper(self):
        asset = LAB / 'dashboard.html'
        self.assertTrue(asset.exists(), 'working UI asset is missing')
        html = asset.read_text(encoding='utf-8')
        # Observer dashboard: capital belongs to the ledger, not a UI constant.
        self.assertIn('s.initial_equity_usdt', html)
        self.assertNotIn('100 USDT', html)
        self.assertIn('PAPER', html)
        self.assertIn('/api/status', html)
        self.assertIn('textContent', html)
        self.assertNotIn('innerHTML', html)
        self.assertNotIn('https://', html)
        self.assertIn('未交易', html)

    def test_cli_exposes_read_only_local_server_options(self):
        self.assertTrue(callable(getattr(self.module, 'main', None)), 'runnable dashboard CLI missing')
        with self.assertRaises(SystemExit) as result:
            self.module.main(['--help'])
        self.assertEqual(result.exception.code, 0)

    def test_source_quotes_expire_even_if_heartbeat_is_new(self):
        now = datetime.now(timezone.utc)
        ms = int(now.timestamp() * 1000)
        self.data['markets'] = [{'symbol': 'ETHUSDT', 'last_received_at': now.isoformat(),
                                 'source_timestamps_ms': {'bookTicker': ms - 90000, 'premiumIndex': ms, 'depth5': ms}}]
        self.save()
        self.assertTrue(json.loads(self.request()[2])['feed_stale'], 'old quote cannot be called live by a new heartbeat')


if __name__ == '__main__':
    unittest.main()
