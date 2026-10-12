"""Fixed-route serving integration tests using only ephemeral loopback HTTP."""
import http.client
import io
import json
import socket
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
ROOT = Path(server.__file__).resolve().parent
MIME_JSON = 'application/json; charset=utf-8'


class DashboardV2ServingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.paths = [Path(temp.name) / name for name in ('s', 'j', 'l')]
        arm = dict(arm='A', days_in=1.07, days_left=19.93, signals=4,
                   blocked=1, fills=6, round_trips=3, pnl='123.123456789',
                   fees='0.00001', max_dd='0.92', loss_limit='10',
                   open_positions=0, feed_errors=0, feed_gaps=0, stale_min=0)
        self.paths[0].write_text(json.dumps({'at': AT, 'arms': [arm]}) + '\n')
        self.paths[1].write_text(json.dumps({'id': 'j', 'body': '研究'}) + '\n')
        self.paths[2].write_text(json.dumps({'items': [
            {'id': 'c', 'state': 'OPEN', 'text': '待辦'}]}))
        self.start()

    def start(self, **overrides):
        settings = dict(scorecard_path=self.paths[0], journal_path=self.paths[1],
                        ledger_path=self.paths[2], port=0, max_age_ms=1000,
                        clock_ms=lambda: NOW)
        settings.update(overrides)
        httpd = server.create_server(**settings)
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        self.addCleanup(self.stop, httpd, thread)
        self.httpd = httpd
        self.port = httpd.server_address[1]

    def stop(self, httpd, thread):
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())

    def request(self, method='GET', target='/', body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request(method, target, body=body)
            reply = connection.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read()
        finally:
            connection.close()

    def raw(self, target, method='GET'):
        # send an exact HTTP request line; http.client normalizes leading //.
        with socket.create_connection(('127.0.0.1', self.port), timeout=3) as sock:
            sock.settimeout(3)
            sock.sendall((method + ' ' + target + ' HTTP/1.1\r\n'
                          'Host: localhost\r\nConnection: close\r\n\r\n').encode('ascii'))
            chunks = []
            while True:
                chunk = sock.recv(16384)
                if not chunk:
                    break
                chunks.append(chunk)
        wire = b''.join(chunks)
        heading, _, body = wire.partition(b'\r\n\r\n')
        parts = heading.split(b'\r\n')
        status = int(parts[0].split()[1])
        headers = dict(line.decode('iso-8859-1').split(': ', 1)
                       for line in parts[1:] if b': ' in line)
        return status, headers, body

    def check_headers(self, headers, body, mime):
        self.assertEqual(headers['Content-Type'], mime)
        self.assertEqual(int(headers['Content-Length']), len(body))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_root_html_exact_bytes(self):
        status, headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body, (ROOT / 'dashboard_v2.html').read_bytes())
        self.check_headers(headers, body, 'text/html; charset=utf-8')

    def test_named_html_exact_bytes(self):
        status, headers, body = self.request(target='/dashboard_v2.html')
        self.assertEqual(status, 200)
        self.assertEqual(body, (ROOT / 'dashboard_v2.html').read_bytes())
        self.check_headers(headers, body, 'text/html; charset=utf-8')

    def test_js_exact_bytes(self):
        status, headers, body = self.request(target='/dashboard_v2_ui.js')
        self.assertEqual(status, 200)
        self.assertEqual(body, (ROOT / 'dashboard_v2_ui.js').read_bytes())
        self.check_headers(headers, body, 'application/javascript; charset=utf-8')

    def test_head_matches_get_each_asset(self):
        for target in ('/', '/dashboard_v2.html', '/dashboard_v2_ui.js'):
            with self.subTest(target=target):
                get_status, get_headers, get_body = self.request(target=target)
                status, headers, body = self.request('HEAD', target)
                self.assertEqual((status, headers, body),
                                 (get_status, get_headers, b''))
                self.assertEqual(int(headers['Content-Length']), len(get_body))

    def test_api_source_model_exact(self):
        status, headers, body = self.request(target='/api/dashboard')
        self.assertEqual(status, 200)
        self.check_headers(headers, body, MIME_JSON)
        actual = json.loads(body)
        expected = reader.read_dashboard(*self.paths, now_ms=NOW, max_age_ms=1000)
        self.assertEqual(actual, expected)
        self.assertEqual(actual['arms'][0]['pnl']['exact'], '123.123456789')
        self.assertEqual(actual['arms'][0]['days_left'], 19.93)

    def test_api_head_and_stale_unknown(self):
        self.start(clock_ms=lambda: NOW + 1001)
        get_status, get_headers, get_body = self.request(target='/api/dashboard')
        head_status, head_headers, head_body = self.request('HEAD', '/api/dashboard')
        self.assertEqual((head_status, head_headers, head_body),
                         (get_status, get_headers, b''))
        self.assertEqual(json.loads(get_body)['scorecard_state'], 'unknown')
        self.assertEqual(json.loads(get_body)['arms'], [])

    def test_unknown_json_404(self):
        for target in ('/missing', '/dashboard_v2.css', '/dashboard_v2.html/',
                       '/api', '/favicon.ico'):
            with self.subTest(target=target):
                status, headers, body = self.request(target=target)
                self.assertEqual(status, 404)
                self.check_headers(headers, body, MIME_JSON)
                self.assertEqual(json.loads(body), {'error': '找不到資源'})

    def test_raw_double_slash_rejected(self):
        for target in ('//', '//dashboard_v2.html', '//dashboard_v2_ui.js',
                       '//api/dashboard'):
            with self.subTest(target=target):
                status, headers, body = self.raw(target)
                self.assertEqual(status, 404)
                self.check_headers(headers, body, MIME_JSON)

    def test_encoded_and_query_targets_rejected(self):
        targets = ('/?x=1', '/dashboard_v2.html?x=1',
                   '/dashboard_v2_ui.js?x=1', '/api/dashboard?x=1',
                   '/%64ashboard_v2.html', '/dashboard_v2%2ehtml',
                   '/dashboard_v2_ui%2ejs', '/%2f', '/%2F',
                   '/dashboard_v2.html%3Fdebug')
        for target in targets:
            with self.subTest(target=target):
                self.assertEqual(self.raw(target)[0], 404)

    def test_traversal_and_absolute_targets_rejected(self):
        for target in ('/../dashboard_v2.html', '/a/../dashboard_v2_ui.js',
                       '/%2e%2e/dashboard_v2.html', '/dashboard_v2.html/..',
                       'http://localhost/dashboard_v2.html', '/./',
                       '/dashboard_v2_ui.js%00'):
            with self.subTest(target=target):
                self.assertEqual(self.raw(target)[0], 404)

    def test_invalid_head_no_body(self):
        for target in ('/nope', '//dashboard_v2.html', '/dashboard_v2.html?x=1'):
            with self.subTest(target=target):
                status, headers, body = self.raw(target, 'HEAD')
                self.assertEqual(status, 404)
                self.assertEqual(body, b'')
                self.assertEqual(headers['Content-Type'], MIME_JSON)
                self.assertEqual(int(headers['Content-Length']),
                                 len(server._ERRORS[404]))

    def test_all_mutating_and_unknown_methods_405(self):
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in self.paths]
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE',
                       'CONNECT', 'BREW'):
            for target in ('/', '/dashboard_v2_ui.js', '/api/dashboard', '/bad'):
                with self.subTest(method=method, target=target):
                    status, headers, body = self.raw(target, method)
                    self.assertEqual(status, 405)
                    self.check_headers(headers, body, MIME_JSON)
                    self.assertEqual(headers['Allow'], 'GET, HEAD')
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in self.paths])

    def test_missing_asset_generic_500(self):
        with patch.object(server, '_read_asset',
                          side_effect=FileNotFoundError('/private/asset')):
            for target in ('/', '/dashboard_v2.html', '/dashboard_v2_ui.js'):
                with self.subTest(target=target):
                    status, headers, body = self.request(target=target)
                    self.assertEqual(status, 500)
                    self.check_headers(headers, body, MIME_JSON)
                    self.assertEqual(body, server._ERRORS[500])
                    self.assertNotIn(b'/private/', body)
                    head = self.request('HEAD', target)
                    self.assertEqual(head, (status, headers, b''))

    def test_unreadable_asset_generic_500(self):
        with patch.object(server, '_read_asset',
                          side_effect=PermissionError('/private/unreadable')):
            status, headers, body = self.request(target='/dashboard_v2_ui.js')
            self.assertEqual(status, 500)
            self.check_headers(headers, body, MIME_JSON)
            self.assertNotIn(b'/private/', body)

    def test_fixed_routing_does_not_read_api_for_assets(self):
        with patch.object(server, 'read_dashboard',
                          side_effect=AssertionError('api unexpectedly read')):
            for target in ('/', '/dashboard_v2.html', '/dashboard_v2_ui.js'):
                self.assertEqual(self.request(target=target)[0], 200)

    def test_single_thread_loopback_and_signature(self):
        self.assertEqual(self.httpd.server_address[0], '127.0.0.1')
        self.assertIsInstance(self.httpd, server.HTTPServer)
        self.assertNotIn('Threading', type(self.httpd).__name__)
        import inspect
        parameters = inspect.signature(server.create_server).parameters
        self.assertEqual(tuple(parameters), ('scorecard_path', 'journal_path',
                         'ledger_path', 'port', 'max_age_ms', 'max_bytes', 'clock_ms'))

    def test_cli_compatibility(self):
        instance = Mock()
        instance.serve_forever.side_effect = KeyboardInterrupt
        args = ['--scorecard', str(self.paths[0]), '--journal', str(self.paths[1]),
                '--ledger', str(self.paths[2])]
        with patch.object(server, 'create_server', return_value=instance) as call:
            self.assertEqual(server.main(args), 0)
        self.assertEqual(call.call_args.kwargs['port'], 8766)
        instance.server_close.assert_called_once()

    def test_sources_and_static_files_unchanged(self):
        assets = [ROOT / 'dashboard_v2.html', ROOT / 'dashboard_v2_ui.js']
        files = self.paths + assets
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in files]
        for _ in range(100):
            self.assertEqual(self.request(target='/api/dashboard')[0], 200)
        for target in ('/', '/dashboard_v2.html', '/dashboard_v2_ui.js'):
            self.assertEqual(self.request(target=target)[0], 200)
        self.assertEqual(before, [(p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in files])


if __name__ == '__main__':
    unittest.main()
