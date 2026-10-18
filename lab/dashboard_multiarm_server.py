"""Local read-only multiarm dashboard: four exact targets, GET/HEAD only."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import time
from dashboard_multiarm_reader import read_all

ASSETS = Path(__file__).resolve().parent
ROUTES = {'/': ('dashboard_multiarm.html', 'text/html; charset=utf-8'),
          '/dashboard_multiarm.html': ('dashboard_multiarm.html', 'text/html; charset=utf-8'),
          '/dashboard_multiarm_ui.js': ('dashboard_multiarm_ui.js', 'application/javascript; charset=utf-8')}
ERRORS = {400:b'{"error":"請求無效"}',404:b'{"error":"找不到資源"}',
          405:b'{"error":"不支援此方法"}',500:b'{"error":"讀取失敗"}'}
# Literals are encoded once, not parsed/unquoted from request content.
TARGETS = {k.encode('ascii'):v for k,v in ROUTES.items()}


class LocalServer(HTTPServer):
    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(2)
        return sock, address
    def handle_error(self, request, client_address):
        pass


def create_server(*, data_root, port=0, stale_after_s=90, clock_ms=None):
    root = Path(data_root)
    if not root.is_absolute() or not root.is_dir():
        raise ValueError('absolute existing data root required')
    if type(port) is not int or port not in range(65536):
        raise ValueError('invalid port')
    if type(stale_after_s) is not int or stale_after_s < 0:
        raise ValueError('invalid stale threshold')
    clock = clock_ms if clock_ms is not None else lambda: time.time_ns() // 1000000
    if not callable(clock):
        raise ValueError('invalid clock')

    class Handler(BaseHTTPRequestHandler):
        server_version = 'MultiarmPaper'
        sys_version = ''
        def log_message(self, *args):
            pass
        def reply(self, status, body, *, mime='application/json; charset=utf-8', head=False):
            self.close_connection = True
            self.send_response(status)
            for key, val in (('Content-Type',mime),('Content-Length',str(len(body))),
                             ('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')):
                self.send_header(key,val)
            if status == 405:
                self.send_header('Allow','GET, HEAD')
            self.end_headers()
            if not head:
                self.wfile.write(body)
        def send_error(self, code, message=None, explain=None):
            status = 405 if code == 501 else 400
            self.reply(status, ERRORS[status], head=self.command == 'HEAD')
        def serve(self, head=False):
            parts = self.raw_requestline.split()
            target = parts[1] if len(parts) >= 2 else b''
            if target == b'/api/multiarm' and self.path == '/api/multiarm':
                try:
                    model = read_all(root, now_ms=clock(), stale_after_s=stale_after_s)
                    body = json.dumps(model, ensure_ascii=False, allow_nan=False,
                                      separators=(',',':')).encode('utf-8')
                    if len(body) > 16 * 1024 * 1024:
                        raise ValueError('response too large')
                    self.reply(200, body, head=head)
                except Exception:
                    self.reply(500, ERRORS[500], head=head)
            elif target in TARGETS and self.path == target.decode('ascii'):
                filename, mime = TARGETS[target]
                try:
                    self.reply(200, (ASSETS / filename).read_bytes(), mime=mime, head=head)
                except OSError:
                    self.reply(500, ERRORS[500], head=head)
            else:
                self.reply(404, ERRORS[404], head=head)
        def do_GET(self):
            self.serve()
        def do_HEAD(self):
            self.serve(head=True)
        def __getattr__(self, name):
            if name.startswith('do_'):
                return lambda: self.reply(405, ERRORS[405])
            raise AttributeError(name)

    return LocalServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18771)
    parser.add_argument('--stale-after-s', type=int, default=90)
    args = parser.parse_args(argv)
    if args.port in (18767,18769,8766):
        parser.error('reserved port')
    try:
        app = create_server(data_root=args.data_root, port=args.port,
                            stale_after_s=args.stale_after_s)
    except (ValueError, OSError):
        parser.error('invalid server configuration')
    try:
        app.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
