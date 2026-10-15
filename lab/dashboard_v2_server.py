"""Synchronous loopback-only transport for the read-only dashboard v2."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import time

from dashboard_v2_reader import read_dashboard
from owner_activity import read_activity


_ROUTE = '/api/dashboard'
_ASSET_DIR = Path(__file__).resolve().parent
_STATIC_ROUTES = {
    '/': ('dashboard_v2.html', 'text/html; charset=utf-8'),
    '/dashboard_v2.html': ('dashboard_v2.html', 'text/html; charset=utf-8'),
    '/owner_activity.html': ('owner_activity.html', 'text/html; charset=utf-8'),
    '/owner_activity.js': ('owner_activity.js', 'application/javascript; charset=utf-8'),
    '/dashboard_v2_ui.js': ('dashboard_v2_ui.js', 'application/javascript; charset=utf-8'),
}
_ERRORS = {
    403: '{"error":"禁止跨來源存取"}'.encode('utf-8'),
    400: '{"error":"請求無效"}'.encode('utf-8'),
    404: '{"error":"找不到資源"}'.encode('utf-8'),
    405: '{"error":"不支援此方法"}'.encode('utf-8'),
    500: '{"error":"讀取失敗"}'.encode('utf-8'),
}


def _absolute_path(value):
    try:
        path = os.fspath(value)
    except TypeError as exc:
        raise ValueError('來源路徑必須是絕對路徑') from exc
    if not isinstance(path, str) or not os.path.isabs(path) or '\x00' in path:
        raise ValueError('來源路徑必須是絕對路徑')
    return path


def _read_asset(filename):
    # The caller chooses only a literal, module-relative allowlisted filename.
    return (_ASSET_DIR / filename).read_bytes()


class _LoopbackHTTPServer(HTTPServer):
    """One connection at a time, with bounded idle socket lifetime."""

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(2.0)
        return connection, address

    def handle_error(self, request, client_address):
        # A disconnected client must not cause a traceback on stderr.
        pass


def create_server(*, scorecard_path, journal_path, ledger_path, port=0,
                  max_age_ms=300000, max_bytes=1048576, clock_ms=None):
    """Validate inputs before binding and return an unstarted local HTTPServer."""
    paths = tuple(_absolute_path(path) for path in
                  (scorecard_path, journal_path, ledger_path))
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('port 必須介於 0 至 65535')
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('max_bytes 必須是正整數')
    if type(max_age_ms) is not int or max_age_ms < 0:
        raise ValueError('max_age_ms 必須是非負整數')
    if clock_ms is None:
        clock_ms = lambda: time.time_ns() // 1000000
    if not callable(clock_ms):
        raise ValueError('clock_ms 必須是可呼叫物件')

    scorecard, journal, ledger = paths
    monitor_root = None
    def configure_monitor(*, profile_root):
        nonlocal monitor_root
        root = _absolute_path(profile_root)
        if os.path.realpath(root) != os.path.normpath(root) or not os.path.isdir(root):
            raise ValueError('invalid trusted root')
        monitor_root = root
    age, limit, clock = max_age_ms, max_bytes, clock_ms

    class Handler(BaseHTTPRequestHandler):
        server_version = 'DashboardV2'
        sys_version = ''

        def log_message(self, format, *args):
            pass

        def _respond(self, status, payload, *, head=False, allow=False,
                     content_type='application/json; charset=utf-8'):
            self.close_connection = True
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if allow:
                self.send_header('Allow', 'GET, HEAD')
            self.end_headers()
            if not head:
                self.wfile.write(payload)

        def send_error(self, code, message=None, explain=None):
            # Invalid request lines must never trigger the stdlib HTML page.
            status = 405 if code == 501 else 400
            self._respond(status, _ERRORS[status],
                          head=self.command == 'HEAD', allow=status == 405)

        def _trusted(self):
            host = self.headers.get('Host', '')
            allowed = {'127.0.0.1', 'localhost', '[::1]'}
            try:
                name, port_text = host.rsplit(':', 1)
                port_valid = int(port_text) == self.server.server_port
            except (ValueError, TypeError):
                return False
            if name not in allowed or not port_valid:
                return False
            origin = self.headers.get('Origin')
            if origin is not None and origin not in ('http://' + host,):
                return False
            if self.headers.get('Sec-Fetch-Site', '') in ('cross-site', 'same-site'):
                return False
            return True

        def _read(self, *, head):
            # BaseHTTPRequestHandler normalizes leading //; require exact raw
            # request target as well as the parsed path for every route.
            if not self._trusted():
                self._respond(403, _ERRORS[403], head=head)
                return
            target = self.raw_requestline.split()[1]
            if self.path == '/api/owner-activity' and target == b'/api/owner-activity':
                if monitor_root is None:
                    self._respond(404, _ERRORS[404], head=head)
                    return
                try:
                    result = read_activity(monitor_root, now_ms=clock(), max_age_ms=age,
                                           max_bytes=limit)
                    payload = json.dumps(result, ensure_ascii=False, allow_nan=False,
                                         separators=(',', ':')).encode('utf-8')
                except Exception:
                    self._respond(500, _ERRORS[500], head=head)
                    return
                self._respond(200, payload, head=head)
                return
            if self.path == _ROUTE and target == b'/api/dashboard':
                try:
                    result = read_dashboard(scorecard, journal, ledger,
                                            now_ms=clock(), max_age_ms=age,
                                            max_bytes=limit)
                    payload = json.dumps(result, ensure_ascii=False,
                                         allow_nan=False, separators=(',', ':')).encode('utf-8')
                except Exception:
                    self._respond(500, _ERRORS[500], head=head)
                    return
                self._respond(200, payload, head=head)
                return

            asset = _STATIC_ROUTES.get(self.path)
            if asset is None or target != self.path.encode('ascii'):
                self._respond(404, _ERRORS[404], head=head)
                return
            filename, content_type = asset
            try:
                payload = _read_asset(filename)
            except OSError:
                self._respond(500, _ERRORS[500], head=head)
                return
            self._respond(200, payload, head=head, content_type=content_type)

        def do_GET(self):
            self._read(head=False)

        def do_HEAD(self):
            self._read(head=True)

        def _reject_method(self):
            self._respond(405, _ERRORS[405], allow=True)

        def __getattr__(self, name):
            # BaseHTTPRequestHandler dispatches any valid method to do_NAME.
            # Unknown verbs are also read-only 405 rather than a 501 HTML page.
            if name.startswith('do_'):
                return self._reject_method
            raise AttributeError(name)

    service = _LoopbackHTTPServer(('127.0.0.1', port), Handler)
    service.configure_monitor = configure_monitor
    return service


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only loopback dashboard v2 JSON')
    parser.add_argument('--scorecard', required=True)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--ledger', required=True)
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--max-age-ms', type=int, default=300000)
    parser.add_argument('--max-bytes', type=int, default=1048576)
    parser.add_argument('--profile-root', help='Trusted absolute profile root for Monitor')
    args = parser.parse_args(argv)
    try:
        httpd = create_server(scorecard_path=args.scorecard,
                              journal_path=args.journal, ledger_path=args.ledger,
                              port=args.port, max_age_ms=args.max_age_ms,
                              max_bytes=args.max_bytes)
        if args.profile_root:
            httpd.configure_monitor(profile_root=args.profile_root)
    except (ValueError, OSError):
        parser.error('設定無效或無法繫結本機埠')
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
