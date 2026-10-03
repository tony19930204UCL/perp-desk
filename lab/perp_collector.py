"""Public REST observation only. No broker, keys, risk settings or trading."""

from decimal import Decimal
from pathlib import Path
import json
import os
import sqlite3
import hashlib
import time
import argparse
import fcntl
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

MAX_DB_BYTES = 256 * 1024 * 1024  # Engineering disk budget, not a financial risk setting.


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class PublicClient:
    BASE = 'https://fapi.binance.com'
    ALLOWED = {'/fapi/v1/exchangeInfo': set(), '/fapi/v1/ticker/bookTicker': {'symbol'},
               '/fapi/v1/premiumIndex': {'symbol'}, '/fapi/v1/depth': {'symbol', 'limit'},
               '/fapi/v1/klines': {'symbol', 'interval', 'limit', 'startTime', 'endTime'}}

    def __init__(self, opener=urlopen, sleep=time.sleep, clock=utc_now, on_error=None,
                 monotonic=time.monotonic, wall_ms=None):
        self.opener, self.sleep, self.clock = opener, sleep, clock
        self.on_error = on_error or (lambda event: None)
        self.monotonic = monotonic
        self.wall_ms = wall_ms or (lambda:int(time.time()*1000))

    def get_timed(self, endpoint, params=None, *, timing):
        # A fixture/subclass may intentionally override get() to inject exact source
        # evidence. Never bypass that override merely to manufacture transport timing.
        method=self.get
        if getattr(method,'__func__',None) is not PublicClient.get:
            timing({'kind':'instrumentation_unavailable',
                    'reason':'client_get_override_has_no_real_transport_attempt'})
            return method(endpoint,params)
        return method(endpoint,params,timing=timing)

    def get(self, endpoint, params=None, *, timing=None):
        params = params or {}
        timing = timing or (lambda event: None)
        if endpoint not in self.ALLOWED or not set(params).issubset(self.ALLOWED[endpoint]):
            raise ValueError('only allowlisted public market-data GETs permitted')
        if 'symbol' in params and params['symbol'] not in {'ETHUSDT', 'XAUUSDT'}:
            raise ValueError('unsupported observation symbol')
        url = self.BASE + endpoint + ('?' + urlencode(params) if params else '')
        request = Request(url, headers={'User-Agent': 'ShadowPublicObserver/1.0', 'Cache-Control': 'no-cache'}, method='GET')

        def measured_wait(seconds, reason, attempt):
            wall0=self.wall_ms(); mono0=self.monotonic()
            self.sleep(seconds)
            wall1=self.wall_ms(); mono1=self.monotonic()
            timing({'kind':'wait','reason':reason,'attempt':attempt,
                    'requested_ms':int(seconds*1000),
                    'start_wall_ms':wall0,'end_wall_ms':wall1,
                    'start_monotonic_ms':mono0*1000,'end_monotonic_ms':mono1*1000,
                    'wall_elapsed_ms':wall1-wall0,
                    'monotonic_elapsed_ms':(mono1-mono0)*1000,
                    'wall_minus_monotonic_ms':(wall1-wall0)-(mono1-mono0)*1000})

        for attempt in range(3):
            measured_wait(0.25,'pre_attempt_throttle',attempt+1)
            wall0=self.wall_ms(); mono0=self.monotonic()
            status=None; size=None
            try:
                with self.opener(request, timeout=10) as response:
                    status=getattr(response,'status',None)
                    if status is None and hasattr(response,'getcode'): status=response.getcode()
                    raw=response.read()
                    size=len(raw)
                    payload=json.loads(raw, parse_float=str)
                wall1=self.wall_ms(); mono1=self.monotonic()
                timing({'kind':'http_attempt','attempt':attempt+1,'outcome':'success',
                        'http_status':status,'response_bytes':size,
                        'start_wall_ms':wall0,'end_wall_ms':wall1,
                        'start_monotonic_ms':mono0*1000,'end_monotonic_ms':mono1*1000,
                        'wall_elapsed_ms':wall1-wall0,
                        'monotonic_elapsed_ms':(mono1-mono0)*1000,
                        'wall_minus_monotonic_ms':(wall1-wall0)-(mono1-mono0)*1000})
                received = self.clock()
                source = payload.get('time', payload.get('E', payload.get('serverTime'))) if isinstance(payload, dict) else None
                return {'endpoint': endpoint, 'params': params, 'source_timestamp_ms': source,
                        'received_at': received, 'payload': payload}
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                wall1=self.wall_ms(); mono1=self.monotonic()
                code=exc.code if isinstance(exc,HTTPError) else status
                timing({'kind':'http_attempt','attempt':attempt+1,'outcome':'error',
                        'error_type':type(exc).__name__,'http_status':code,
                        'response_bytes':size,
                        'start_wall_ms':wall0,'end_wall_ms':wall1,
                        'start_monotonic_ms':mono0*1000,'end_monotonic_ms':mono1*1000,
                        'wall_elapsed_ms':wall1-wall0,
                        'monotonic_elapsed_ms':(mono1-mono0)*1000,
                        'wall_minus_monotonic_ms':(wall1-wall0)-(mono1-mono0)*1000})
                self.on_error({'event': 'request_error', 'endpoint': endpoint, 'attempt': attempt + 1,
                               'received_at': self.clock(), 'error': str(exc)})
                if attempt == 2 or (isinstance(exc, HTTPError) and exc.code == 418):
                    raise
                delay = 2 ** (attempt + 1)
                if isinstance(exc, HTTPError) and exc.code == 429:
                    try:
                        delay = min(60, max(delay, int(exc.headers.get('Retry-After', '10'))))
                    except ValueError:
                        delay = 10
                measured_wait(delay,'retry_backoff',attempt+1)



class Store:
    """SQLite is authoritative; status.json is an atomic read-only export."""
    def __init__(self, root):
        self.root = Path(root)
        (self.root / 'data').mkdir(parents=True, exist_ok=True)
        (self.root / 'shared').mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / 'data/observations.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, endpoint TEXT NOT NULL, symbol TEXT, source_timestamp_ms INTEGER, payload TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS closed_bars (symbol TEXT NOT NULL, open_time_ms INTEGER NOT NULL, close_time_ms INTEGER NOT NULL, received_at TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(symbol,open_time_ms))')
        self.db.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL)')
        self.db.commit()

    def audit(self, event):
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        row = self.db.execute('SELECT hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
        previous = row[0] if row else '0' * 64
        digest = hashlib.sha256((previous + payload).encode()).hexdigest()
        with self.db:
            self.db.execute('INSERT INTO audit(payload,previous_hash,hash) VALUES(?,?,?)', (payload, previous, digest))

    def load(self, now):
        row = self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if row:
            return json.loads(row[0])
        state = initial_state(now)
        self.save(state)
        return state

    def save(self, state):
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        with self.db:
            self.db.execute('INSERT INTO state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (payload,))
        target = self.root / 'shared/status.json'
        temporary = target.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            stream.write(payload + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def record(self, observation):
        payload = json.dumps(observation, sort_keys=True)
        with self.db:
            inserted = self.db.execute('INSERT INTO observations(received_at,endpoint,symbol,source_timestamp_ms,payload) VALUES(?,?,?,?,?)',
                                       (observation['received_at'], observation['endpoint'], observation['params'].get('symbol'),
                                        observation['source_timestamp_ms'], payload))
            self.audit({'event': 'public_receipt', 'received_at': observation['received_at'],
                        'endpoint': observation['endpoint'], 'observation_id': inserted.lastrowid,
                        'payload_sha256': hashlib.sha256(payload.encode()).hexdigest()})

    def closed_bars(self, symbol, bars, received_at):
        with self.db:
            for bar in bars:
                self.db.execute('INSERT OR IGNORE INTO closed_bars VALUES(?,?,?,?,?)',
                                (symbol, int(bar[0]), int(bar[6]), received_at, json.dumps(bar)))

    def close(self):
        self.db.close()


class Observer:
    def __init__(self, store, client=None, clock=utc_now):
        self.store, self.clock = store, clock
        self.state = store.load(clock())
        self.client = client or PublicClient(clock=clock, on_error=self.request_error)

    def request_error(self, event):
        self.state['feed']['errors_count'] += 1
        self.store.audit(event)

    def fetch(self, endpoint, params=None):
        observation = self.client.get(endpoint, params)
        self.store.record(observation)
        return observation

    def poll(self):
        errors_before = self.state['feed']['errors_count']
        connected_before = self.state['feed']['connected']
        try:
            if (self.store.root / 'data/observations.sqlite3').stat().st_size >= MAX_DB_BYTES:
                if 'storage_budget_reached' not in self.state['blockers']:
                    self.state['blockers'].append('storage_budget_reached')
                raise RuntimeError('storage_budget_reached: collection paused; no history deleted')
            last = self.state['feed']['last_success_at']
            if last and not self.state.get('gap_open'):
                age = (datetime.fromisoformat(self.clock()) - datetime.fromisoformat(last)).total_seconds()
                if not -5 <= age <= 120:
                    self.state['feed']['gaps_count'] += 1
                    self.state['gap_open'] = True
                    self.state['feed']['connected'] = False
                    self.store.audit({'event': 'heartbeat_gap', 'received_at': self.clock(),
                                      'last_success_at': last, 'seconds': age})
            self._collect()
            if self.state.get('gap_open'):
                self.store.audit({'event': 'gap_end', 'received_at': self.clock()})
                self.state['gap_open'] = False
        except Exception as exc:
            if self.state['feed']['errors_count'] == errors_before:
                self.state['feed']['errors_count'] += 1
            if not self.state.get('gap_open'):
                self.state['feed']['gaps_count'] += 1
                self.state['gap_open'] = True
                self.store.audit({'event': 'gap_start', 'received_at': self.clock(),
                                  'previously_connected': connected_before})
            self.state['feed']['connected'] = False
            self.state['latest_error'] = f'{type(exc).__name__}: {exc}'
            self.store.audit({'event': 'iteration_error', 'received_at': self.clock(),
                              'error': self.state['latest_error']})
        finally:
            self.state['updated_at'] = self.clock()
            self.state['paper_trading_enabled'] = False
            self.state['live_trading_enabled'] = False
            self.store.save(self.state)
        return self.state

    def _collect(self):
        cached = self.store.db.execute(
            "SELECT received_at, payload FROM observations WHERE endpoint='/fapi/v1/exchangeInfo' ORDER BY id DESC LIMIT 1").fetchone()
        metadata = None
        if cached:
            age = (datetime.fromisoformat(self.clock()) - datetime.fromisoformat(cached[0])).total_seconds()
            if 0 <= age < 86400:
                metadata = json.loads(cached[1])
        if metadata is None:
            metadata = self.fetch('/fapi/v1/exchangeInfo')
        markets = market_metadata(metadata['payload'])
        self.state['reference_received_at'] = metadata['received_at']
        start_ms = int(datetime.fromisoformat(self.state['collector_started_at']).timestamp() * 1000)
        cursor = dict(self.state.get('kline_cursor', {}))
        for market in markets:
            symbol = market['symbol']
            book = self.fetch('/fapi/v1/ticker/bookTicker', {'symbol': symbol})
            mark = self.fetch('/fapi/v1/premiumIndex', {'symbol': symbol})
            depth = self.fetch('/fapi/v1/depth', {'symbol': symbol, 'limit': 5})
            for observation, fields in ((book, ('bidPrice', 'askPrice')), (mark, ('markPrice', 'lastFundingRate'))):
                if observation['payload']['symbol'] != symbol:
                    raise ValueError(f'{symbol}: response symbol mismatch')
                for field in fields:
                    value = observation['payload'][field]
                    if not isinstance(value, str) or not Decimal(value).is_finite():
                        raise ValueError(f'{symbol}: invalid decimal {field}')
                    if field != 'lastFundingRate' and Decimal(value) <= 0:
                        raise ValueError(f'{symbol}: nonpositive {field}')
            if Decimal(book['payload']['askPrice']) < Decimal(book['payload']['bidPrice']):
                raise ValueError(f'{symbol}: crossed book')
            for observation in (book, mark, depth):
                source = observation['source_timestamp_ms']
                received_ms = int(datetime.fromisoformat(observation['received_at']).timestamp() * 1000)
                if source is None or not -5000 <= received_ms - int(source) <= 120000:
                    raise ValueError(f"{symbol}: stale/missing/future source timestamp at {observation['endpoint']}")
            end_ms = int(datetime.fromisoformat(self.clock()).timestamp() * 1000) - 1
            bar_start = max(start_ms, cursor.get(symbol, start_ms - 1) + 1)
            klines = self.fetch('/fapi/v1/klines', {'symbol': symbol, 'interval': '5m',
                                                  'limit': 1000, 'startTime': bar_start, 'endTime': end_ms})
            bars = [bar for bar in klines['payload'] if int(bar[0]) >= bar_start and int(bar[6]) <= end_ms]
            self.store.closed_bars(symbol, bars, klines['received_at'])
            if bars:
                cursor[symbol] = max(int(bar[6]) for bar in bars)
            market.update({'bid': book['payload']['bidPrice'], 'ask': book['payload']['askPrice'],
                           'mark_price': mark['payload']['markPrice'], 'funding_rate': mark['payload']['lastFundingRate'],
                           'next_funding_time': mark['payload']['nextFundingTime'],
                           'last_received_at': klines['received_at'], 'closed_bars_count': len(bars),
                           'source_timestamps_ms': {'bookTicker': book['source_timestamp_ms'],
                                                    'premiumIndex': mark['source_timestamp_ms'],
                                                    'depth5': depth['source_timestamp_ms'],
                                                    'last_closed_5m': max((int(b[6]) for b in bars), default=cursor.get(symbol))}})
        now = self.clock()
        self.state['markets'] = markets
        self.state['kline_cursor'] = cursor
        self.state['updated_at'] = now
        self.state['feed']['connected'] = True
        self.state['feed']['last_success_at'] = now
        self.state['latest_error'] = None
        if not self.state['versions']:
            self.state['versions'].append({'id': 'OBS-001',
                                           'status': 'observing', 'started_at': now, 'sample_start_at': now,
                                           'signals_count': 0, 'fills_count': 0,
                                           'description': 'Engineering observation; not H1 activation or performance confirmation'})
        self.state['equity_history'].append({'ts': now, 'equity_usdt': self.state['equity_usdt']})


class InstanceLock:
    def __init__(self, root):
        self.root = Path(root)
        self.stream = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.stream = (self.root / 'collector.lock').open('a+')
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            raise BlockingIOError('collector already running') from None
        return self

    def __exit__(self, *args):
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def main(argv=None, root=None, client=None, sleep=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=15)
    args = parser.parse_args(argv)
    if args.interval < 15 or not args.interval < float('inf'):
        parser.error('--interval must be finite and at least 15 seconds (public REST pacing)')
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    try:
        with InstanceLock(root):
            store = Store(root)
            try:
                observer = Observer(store, client=client)
                consecutive_failures = 0
                while True:
                    state = observer.poll()
                    print(json.dumps(state, ensure_ascii=False), flush=True)
                    if args.once:
                        return 0 if state['feed']['connected'] else 1
                    consecutive_failures = 0 if state['feed']['connected'] else min(5, consecutive_failures + 1)
                    sleep(min(300, args.interval * 2 ** consecutive_failures))
            finally:
                store.close()
    except BlockingIOError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


def market_metadata(info):
    result = []
    for symbol, contract, underlying, category in (
            ('ETHUSDT', 'PERPETUAL', 'COIN', 'crypto'),
            ('XAUUSDT', 'TRADIFI_PERPETUAL', 'COMMODITY', 'TradFi')):
        entry = next((x for x in info['symbols'] if x['symbol'] == symbol), None)
        if entry is None or any(entry.get(k) != v for k, v in {
                'status': 'TRADING', 'contractType': contract, 'underlyingType': underlying,
                'quoteAsset': 'USDT', 'marginAsset': 'USDT'}.items()):
            raise ValueError(f'{symbol}: missing or unsupported market metadata / not TRADING')
        filters = {x['filterType']: x for x in entry['filters']}
        try:
            values = {'tick_size': filters['PRICE_FILTER']['tickSize'],
                      'step_size': filters['LOT_SIZE']['stepSize'],
                      'min_notional': filters['MIN_NOTIONAL']['notional']}
            if any(not Decimal(v).is_finite() or Decimal(v) <= 0 for v in values.values()):
                raise ValueError('nonpositive filter')
        except (KeyError, ArithmeticError) as exc:
            raise ValueError(f'{symbol}: required filters missing or invalid') from exc
        result.append({'symbol': symbol, 'category': category, 'status': 'TRADING', **values})
    return result


def initial_state(now):
    return {
        'schema_version': 1, 'mode': 'shadow',
        'paper_start_equity_usdt': '100', 'initial_equity_usdt': '100',
        'cash_usdt': '100', 'equity_usdt': '100',
        'realized_pnl_usdt': '0', 'unrealized_pnl_usdt': '0', 'total_pnl_usdt': '0',
        'positions': [], 'fills_count': 0, 'signals_count': 0, 'blocked_signals_count': 0,
        'paper_trading_enabled': False, 'live_trading_enabled': False,
        'blockers': ['risk_limits_not_set', 'sim_broker_not_implemented', 'signals_not_implemented'],
        'updated_at': now, 'collector_started_at': now,
        'feed': {'transport': 'REST polling', 'connected': False, 'last_success_at': None,
                 'errors_count': 0, 'gaps_count': 0},
        'markets': [], 'versions': [],
        'equity_history': [{'ts': now, 'equity_usdt': '100'}], 'latest_error': None,
    }


if __name__ == '__main__':
    raise SystemExit(main())
