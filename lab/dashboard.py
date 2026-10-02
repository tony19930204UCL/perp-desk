"""Local read-only paper snapshot dashboard; no exchange or broker access."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import re


def decimal_value(value, positive=False):
    if not isinstance(value, str):
        raise ValueError('financial values must be decimal strings')
    try:
        number = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError('invalid decimal') from exc
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError('invalid financial value')
    return number


def _counter(value):
    if type(value) is not int or value < 0:
        raise ValueError('invalid nonnegative counter')


def _nonempty(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('missing version identifier')


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError('invalid proof hash')


def validate_paper(data):
    """Validate evidence, never grant permissions or select risk limits."""
    engine, risk = data.get('engine'), data.get('risk')
    if not isinstance(engine, dict) or not isinstance(risk, dict):
        raise ValueError('missing paper engine/risk proof')
    if engine.get('implementation') != 'paper-engine-v1':
        raise ValueError('unsupported paper engine')
    _nonempty(engine.get('version_id'))
    _hash(engine.get('model_sha256'))
    try:
        start = datetime.fromisoformat(engine['forward_start_at'])
        if start.utcoffset() is None or start.utcoffset().total_seconds() != 0:
            raise ValueError('forward start must be UTC')
    except (KeyError, TypeError) as exc:
        raise ValueError('missing UTC forward start') from exc
    if engine.get('status') not in ('warmup', 'ready', 'running', 'halted', 'error'):
        raise ValueError('invalid engine status')
    if risk.get('approved') is not True:
        raise ValueError('unapproved paper risk')
    _nonempty(risk.get('risk_version'))
    _hash(risk.get('risk_config_sha256'))
    if decimal_value(data['initial_equity_usdt']) != Decimal('100'):
        raise ValueError('unexpected initial paper equity')
    initial, cash, equity, realized, unrealized, total = (
        decimal_value(data[name]) for name in ('initial_equity_usdt', 'cash_usdt',
            'equity_usdt', 'realized_pnl_usdt', 'unrealized_pnl_usdt', 'total_pnl_usdt'))
    values = (initial, cash, equity, realized, unrealized, total)
    # Enough digits for exact alignment and addition, independent of caller context.
    with localcontext() as context:
        context.prec = max(v.adjusted() for v in values) - min(v.as_tuple().exponent for v in values) + 3
        if cash != initial + realized or equity != cash + unrealized or total != realized + unrealized:
            raise ValueError('paper currency invariant failed (realized is net of fees/funding)')
    for name in ('gross_realized_pnl_usdt', 'fees_usdt', 'funding_pnl_usdt'):
        if name in data:
            decimal_value(data[name])
    for name in ('signals_count', 'fills_count', 'blocked_signals_count'):
        _counter(data.get(name))
    for name in ('warmup_received', 'warmup_required'):
        if name in engine:
            _counter(engine[name])
    if data['feed'].get('transport') != 'REST polling' or type(data['feed'].get('connected')) is not bool:
        raise ValueError('invalid REST feed metadata')
    for name in ('errors_count', 'gaps_count'):
        _counter(data['feed'].get(name))
    categories = {'ETHUSDT': 'crypto', 'XAUUSDT': 'TradFi'}
    for market in data['markets']:
        if not isinstance(market, dict) or market.get('symbol') not in categories or categories[market['symbol']] != market.get('category'):
            raise ValueError('unsupported market category')
        for name in ('bid', 'ask', 'mark_price'):
            decimal_value(market.get(name), positive=True)
        for name in ('funding_rate', 'min_notional'):
            if name in market:
                decimal_value(market[name])
        if not isinstance(market.get('source_timestamps_ms'), dict):
            raise ValueError('invalid source timestamps')
    for point in data['equity_history']:
        if not isinstance(point, dict):
            raise ValueError('invalid equity history point')
        decimal_value(point.get('equity_usdt'))
    for position in data['positions']:
        if not isinstance(position, dict) or position.get('side') not in ('BUY', 'SELL'):
            raise ValueError('invalid position side')
        for name in ('qty', 'entry_price', 'mark_price', 'stop_price'):
            decimal_value(position.get(name), positive=True)
        decimal_value(position.get('unrealized_pnl_usdt'))


def validate_snapshot(data):
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data.get('schema_version') != 1 or data.get('mode') not in ('shadow', 'paper'):
        raise ValueError('unsupported snapshot schema')
    if data.get('live_trading_enabled') is not False or type(data.get('paper_trading_enabled')) is not bool:
        raise ValueError('invalid trading enablement')
    if data['mode'] == 'shadow' and data['paper_trading_enabled'] is not False:
        raise ValueError('observation-only dashboard cannot accept trading enablement')
    if not isinstance(data.get('feed'), dict):
        raise ValueError('invalid feed')
    for name in ('positions', 'markets', 'versions', 'equity_history', 'blockers'):
        if not isinstance(data.get(name), list):
            raise ValueError('invalid collection')
    for name in ('cash_usdt', 'equity_usdt', 'initial_equity_usdt', 'realized_pnl_usdt', 'unrealized_pnl_usdt', 'total_pnl_usdt'):
        decimal_value(data.get(name))
    if data['mode'] == 'paper':
        validate_paper(data)


def age_seconds(stamp, now):
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.utcoffset() is None:
            return None
        return (now - parsed).total_seconds()
    except (ValueError, TypeError):
        return None


def make_server(port, status_path, html_path):
    status_path, html_path = Path(status_path), Path(html_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send(405, b'Read-only', 'text/plain; charset=utf-8')

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_POST

        def do_GET(self):
            port = self.server.server_port
            hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
            origins = {f'http://{host}' for host in hosts}
            if (self.headers.get('Host') not in hosts
                    or (self.headers.get('Origin') and self.headers['Origin'] not in origins)
                    or self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                self.send(403, b'Forbidden', 'text/plain; charset=utf-8')
                return
            if self.path == '/api/work':
                from work_status import load_status
                try:
                    data = load_status(status_path.parent / 'work_status.json')
                except (OSError, ValueError, UnicodeError, TypeError, KeyError, OverflowError):
                    self.send(503, json.dumps({'available': False, 'error': 'Work status unavailable or invalid'}).encode(), 'application/json; charset=utf-8')
                    return
                self.send(200, json.dumps(data).encode(), 'application/json; charset=utf-8')
            elif self.path == '/api/status':
                try:
                    data = json.loads(status_path.read_text(encoding='utf-8'))
                    validate_snapshot(data)
                except (OSError, ValueError, UnicodeError, ArithmeticError, TypeError, AttributeError, KeyError):
                    self.send(503, json.dumps({'available': False, 'error': 'Snapshot unavailable or invalid'}).encode(), 'application/json; charset=utf-8')
                    return
                now = datetime.now(timezone.utc)
                snapshot_age = age_seconds(data.get('updated_at'), now)
                feed_age = age_seconds(data.get('feed', {}).get('last_success_at'), now)
                data['snapshot_age_seconds'] = snapshot_age
                data['feed_age_seconds'] = feed_age
                data['feed_stale'] = not data.get('feed', {}).get('connected') or any(
                    age is None or not -5 <= age <= 60 for age in (snapshot_age, feed_age))
                for market in data['markets']:
                    received_age = age_seconds(market.get('last_received_at'), now)
                    if received_age is None or not -5 <= received_age <= 60:
                        data['feed_stale'] = True
                    sources = market.get('source_timestamps_ms', {})
                    for endpoint in ('bookTicker', 'premiumIndex', 'depth5'):
                        stamp = sources.get(endpoint)
                        if not isinstance(stamp, (int, float)) or not -5 <= now.timestamp() - stamp / 1000 <= 60:
                            data['feed_stale'] = True
                self.send(200, json.dumps(data).encode(), 'application/json; charset=utf-8')
            elif self.path == '/':
                self.send(200, html_path.read_bytes(), 'text/html; charset=utf-8')
            else:
                self.send(404, b'Not found', 'text/plain')

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    import argparse
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--status', type=Path, default=root / 'shared/status.json')
    parser.add_argument('--html', type=Path, default=root / 'dashboard.html')
    args = parser.parse_args(argv)
    with make_server(args.port, args.status, args.html) as server:
        print(f'Read-only PAPER dashboard http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
