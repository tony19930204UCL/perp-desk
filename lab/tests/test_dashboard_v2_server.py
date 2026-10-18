import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard_v2_reader as reader
import dashboard_v2_server as server

AT = '2026-10-12T11:33:20+00:00'
NOW = int(datetime.fromisoformat(AT).timestamp() * 1000)


def arm():
    return dict(arm='A', days_in=1.07, days_left=19.93, signals=4,
                blocked=1, fills=6, round_trips=3,
                pnl='123.123456789012345678901234567890', fees='0.00000001',
                max_dd='0.92', loss_limit='10', open_positions=0,
                feed_errors=9, feed_gaps=0, stale_min=0.2)


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = tuple(Path(tmp.name) / x for x in ('score', 'journal', 'ledger'))
        self.paths[0].write_bytes((json.dumps({'at': AT, 'arms': [arm()]}) + '\n').encode())
        self.paths[1].write_bytes((json.dumps({'id': 'j', 'body': '中文研究'}) + '\n').encode())
        self.paths[2].write_bytes(json.dumps({'items': [
            {'id': 'c', 'text': '待辦', 'state': 'OPEN', 'due': None}]}).encode())
        self.start()

    def start(self, **kw):
        config = dict(zip(('scorecard_path', 'journal_path', 'ledger_path'), self.paths))
        config.update(dict(clock_ms=lambda: NOW, max_age_ms=1000))
        config.update(kw)
        httpd = server.create_server(**config)
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        self.addCleanup(self.stop, httpd, thread)
        self.httpd = httpd
        self.port = httpd.server_address[1]

    def stop(self, httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def request(self, method='GET', path='/api/dashboard', *, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def expected(self, **kw):
        return reader.read_dashboard(*self.paths, now_ms=kw.get('now_ms', NOW),
                                     max_age_ms=kw.get('max_age_ms', 1000),
                                     max_bytes=kw.get('max_bytes', 1048576))

    def test_precise_reader_equality(self):
        status, _, data = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), self.expected())

    def test_fractional_days_and_money_exact(self):
        _, _, data = self.request()
        row = json.loads(data)['arms'][0]
        self.assertEqual((row['days_in'], row['days_left']), (1.07, 19.93))
        self.assertEqual(row['pnl']['exact'], arm()['pnl'])
        self.assertEqual(row['fees']['exact'], arm()['fees'])

    def test_compact_utf8_and_headers(self):
        status, headers, data = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(int(headers['Content-Length']), len(data))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('中文研究'.encode('utf-8'), data)
        self.assertNotIn(b'\\u4e2d', data)
        self.assertNotIn(b': ', data)
        self.assertNotIn(b', ', data)

    def test_head_matches_get_without_body(self):
        gstatus, gheaders, gbody = self.request()
        hstatus, hheaders, hbody = self.request('HEAD')
        self.assertEqual(hstatus, gstatus)
        self.assertEqual(hheaders, gheaders)
        self.assertEqual(hbody, b'')
        self.assertEqual(int(hheaders['Content-Length']), len(gbody))

    def test_each_get_reads_once(self):
        with patch.object(server, 'read_dashboard', wraps=reader.read_dashboard) as call:
            self.request()
            self.request()
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.kwargs['max_age_ms'], 1000)

    def test_missing_source_unknown_not_zero(self):
        self.paths[0].unlink()
        status, _, data = self.request()
        value = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual(value, self.expected())
        self.assertEqual(value['scorecard_state'], 'unknown')
        self.assertEqual(value['arms'], [])

    def test_corrupt_latest_scorecard_unknown(self):
        with self.paths[0].open('ab') as f:
            f.write(b'{bad}\n')
        value = json.loads(self.request()[2])
        self.assertEqual(value, self.expected())
        self.assertEqual(value['scorecard_state'], 'unknown')

    def test_stale_scorecard_unknown(self):
        self.start(clock_ms=lambda: NOW + 1001)
        value = json.loads(self.request()[2])
        self.assertEqual(value, self.expected(now_ms=NOW + 1001))
        self.assertEqual(value['scorecard_state'], 'unknown')

    def test_missing_journal_and_corrupt_ledger(self):
        self.paths[1].unlink()
        self.paths[2].write_bytes(b'not json')
        value = json.loads(self.request()[2])
        self.assertEqual(value, self.expected())
        self.assertEqual(value['journal'], [])
        self.assertEqual(value['commitments'], [])

    def test_query_encoded_absolute_traversal_routes_are_404(self):
        routes = ('/api/dashboard?x=1', '/api/%64ashboard',
                  '/api/dashboard%3Fx=1', '/api/../api/dashboard',
                  '//api/dashboard', '/api/dashboard/', '/%2e%2e/api/dashboard',
                  'http://localhost/api/dashboard', '/score')
        with patch.object(server, 'read_dashboard', side_effect=AssertionError('read')) as call:
            for route in routes:
                with self.subTest(route=route):
                    status, _, data = self.request(path=route)
                    self.assertEqual(status, 404)
                    self.assertEqual(json.loads(data), {'error': '找不到資源'})
            with self.subTest(route='/'):
                status, headers, body = self.request(path='/')
                self.assertEqual(status, 200)
                self.assertEqual(headers['Content-Type'], 'text/html; charset=utf-8')
                self.assertEqual(headers['Cache-Control'], 'no-store')
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertEqual(
                    body, (Path(server.__file__).resolve().parent /
                           'dashboard_v2.html').read_bytes())
            call.assert_not_called()

    def test_head_invalid_route_sanitized(self):
        status, headers, body = self.request('HEAD', '/api/dashboard?debug=true')
        self.assertEqual(status, 404)
        self.assertEqual(body, b'')
        self.assertEqual(int(headers['Content-Length']),
                         len('{"error":"找不到資源"}'.encode()))

    def test_other_methods_405_and_allow(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT', 'BREW'):
            with self.subTest(method=method):
                status, headers, data = self.request(method, body=b'ignore this body')
                self.assertEqual(status, 405)
                self.assertEqual(headers['Allow'], 'GET, HEAD')
                self.assertEqual(json.loads(data), {'error': '不支援此方法'})

    def test_no_cors_headers(self):
        for method in ('GET', 'HEAD', 'OPTIONS'):
            _, headers, _ = self.request(method)
            self.assertFalse(any(key.lower().startswith('access-control-')
                                 for key in headers))

    def test_reader_exception_sanitized_and_recovery(self):
        with patch.object(server, 'read_dashboard', side_effect=RuntimeError(
                'private /sensitive/path traceback')):
            status, _, body = self.request()
            self.assertEqual(status, 500)
            self.assertEqual(json.loads(body), {'error': '讀取失敗'})
            self.assertNotIn(b'sensitive', body)
            self.assertEqual(self.request('HEAD')[:1], (500,))
            self.assertEqual(self.request('HEAD')[2], b'')
        self.assertEqual(json.loads(self.request()[2]), self.expected())

    def test_invalid_clock_return_sanitized(self):
        self.start(clock_ms=lambda: True)
        self.assertEqual(self.request()[0], 500)

    def test_clock_and_reader_configuration_are_frozen(self):
        ticks = [NOW, NOW + 1001]
        self.start(clock_ms=lambda: ticks.pop(0), max_bytes=500)
        self.assertEqual(json.loads(self.request()[2]), self.expected(max_bytes=500))
        self.assertEqual(json.loads(self.request()[2])['scorecard_state'], 'unknown')

    def test_validation_precedes_bind(self):
        config = dict(zip(('scorecard_path', 'journal_path', 'ledger_path'), self.paths))
        bad = ({'port': True}, {'port': -1}, {'port': 65536}, {'port': 0.5},
               {'max_age_ms': -1}, {'max_age_ms': True}, {'max_bytes': 0},
               {'max_bytes': True}, {'clock_ms': 42},
               {'scorecard_path': 'relative'}, {'journal_path': ''},
               {'ledger_path': b'/tmp/byte-path'},
               {'scorecard_path': '/tmp/zero\x00byte'})
        with patch.object(server, '_LoopbackHTTPServer', side_effect=AssertionError('bind')) as bind:
            for options in bad:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    server.create_server(**(config | options))
            bind.assert_not_called()

    def test_bind_literal_loopback_and_sync_server(self):
        self.assertEqual(self.httpd.server_address[0], '127.0.0.1')
        self.assertEqual(self.httpd.socket.getsockname()[0], '127.0.0.1')
        self.assertIsInstance(self.httpd, server.HTTPServer)
        self.assertEqual(self.httpd.address_family, server.HTTPServer.address_family)

    def test_sources_byte_identical_after_100_gets(self):
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in self.paths]
        for _ in range(100):
            self.assertEqual(self.request()[0], 200)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in self.paths])

    def test_methods_do_not_change_sources(self):
        before = [p.read_bytes() for p in self.paths]
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            self.assertEqual(self.request(method, body=b'alter')[0], 405)
        self.assertEqual(before, [p.read_bytes() for p in self.paths])

    def test_cli_invalid_configuration_exits_two_before_bind(self):
        arguments = ['--scorecard', 'relative', '--journal', str(self.paths[1]),
                     '--ledger', str(self.paths[2])]
        with patch.object(server, '_LoopbackHTTPServer',
                          side_effect=AssertionError('bound')) as bind, \
             patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as caught:
                server.main(arguments)
        self.assertEqual(caught.exception.code, 2)
        bind.assert_not_called()

    def test_cli_keyboardinterrupt_closes(self):
        instance = Mock()
        instance.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(server, 'create_server', return_value=instance) as create:
            self.assertEqual(server.main(['--scorecard', str(self.paths[0]),
                                          '--journal', str(self.paths[1]),
                                          '--ledger', str(self.paths[2])]), 0)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs['port'], 8766)
        instance.server_close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
