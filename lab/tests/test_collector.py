"""Hand-made test fixtures only; never written to runtime data."""
import importlib
import unittest
import json
import io
from contextlib import redirect_stdout, redirect_stderr
import tempfile
from unittest.mock import patch
from urllib.error import URLError
from pathlib import Path
from datetime import datetime, timezone


def handmade_info():
    return {'symbols': [
        {'symbol': symbol, 'contractType': contract, 'underlyingType': underlying,
         'status': 'TRADING', 'quoteAsset': 'USDT', 'marginAsset': 'USDT',
         'filters': [{'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},
                     {'filterType': 'LOT_SIZE', 'stepSize': '0.001'},
                     {'filterType': 'MIN_NOTIONAL', 'notional': '5'}]}
        for symbol, contract, underlying in [('ETHUSDT', 'PERPETUAL', 'COIN'), ('XAUUSDT', 'TRADIFI_PERPETUAL', 'COMMODITY')]]}


class HandmadeClient:
    def __init__(self, now='2026-10-01T00:10:00Z'):
        self.now = now
        self.ms = int(datetime.fromisoformat(now).timestamp() * 1000)
        self.calls = []

    def get(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        if endpoint.endswith('exchangeInfo'):
            payload = handmade_info()
        elif endpoint.endswith('bookTicker'):
            payload = {'symbol': params['symbol'], 'time': self.ms, 'bidPrice': '100.000000000001', 'askPrice': '100.000000000002'}
        elif endpoint.endswith('premiumIndex'):
            payload = {'symbol': params['symbol'], 'time': self.ms, 'markPrice': '100.000000000001', 'lastFundingRate': '0.0001', 'nextFundingTime': self.ms + 1000}
        elif endpoint.endswith('depth'):
            payload = {'E': self.ms, 'T': self.ms, 'bids': [['100', '1']], 'asks': [['101', '1']]}
        else:
            # before forward start, closed forward bar, still-open bar
            payload = [[self.ms - 900000, '100', '101', '99', '100', '1', self.ms - 600001],
                       [self.ms - 600000, '100', '101', '99', '100', '1', self.ms - 300001],
                       [self.ms, '100', '101', '99', '100', '1', self.ms + 299999]]
        return {'endpoint': endpoint, 'params': params or {}, 'payload': payload,
                'received_at': self.now, 'source_timestamp_ms': payload.get('time', payload.get('E')) if isinstance(payload, dict) else None}

SCRATCH = '/home/chihcheng/.hermes/profiles/perp-desk/cache/scratch'

try:
    collector = importlib.import_module('perp_collector')
except ModuleNotFoundError:
    collector = None


class CollectorTests(unittest.TestCase):
    def test_storage_budget_stops_public_collection_without_deleting_history(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            store = collector.Store(Path(tmp))
            client = HandmadeClient()
            observer = collector.Observer(store, client=client, clock=lambda: client.now)
            with patch.object(collector, 'MAX_DB_BYTES', 0, create=True):
                state = observer.poll()
            self.assertEqual(client.calls, [], 'storage budget must stop new raw data requests')
            self.assertFalse(state['feed']['connected'])
            self.assertIn('storage_budget_reached', state['blockers'])
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
            store.close()

    def test_exchange_reference_is_reused_until_daily_expiry(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            store = collector.Store(Path(tmp))
            client = HandmadeClient()
            observer = collector.Observer(store, client=client, clock=lambda: client.now)
            observer.poll()
            observer.poll()
            self.assertEqual(sum(endpoint.endswith('exchangeInfo') for endpoint, _ in client.calls), 1,
                             'daily reference must not be downloaded and stored every poll')
            later = HandmadeClient('2026-10-02T00:10:00Z')
            restarted = collector.Observer(store, client=later, clock=lambda: later.now)
            restarted.poll()
            self.assertEqual(sum(endpoint.endswith('exchangeInfo') for endpoint, _ in later.calls), 1)
            self.assertTrue(restarted.state['feed']['connected'])
            store.close()

    def test_polling_does_not_create_new_strategy_versions(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            store = collector.Store(Path(tmp))
            client = HandmadeClient()
            observer = collector.Observer(store, client=client, clock=lambda: client.now)
            observer.poll()
            first = json.loads(json.dumps(observer.state['versions']))
            observer.poll()
            self.assertEqual(observer.state['versions'], first, 'poll batches are not new strategy versions')
            self.assertEqual([v['id'] for v in first], ['OBS-001'])
            store.close()

    def test_observation_payload_digest_is_linked_to_audit(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            store = collector.Store(Path(tmp))
            store.record(HandmadeClient().get('/fapi/v1/ticker/bookTicker', {'symbol': 'ETHUSDT'}))
            rows = store.db.execute('SELECT payload FROM audit').fetchall()
            self.assertEqual(len(rows), 1, 'real observation needs a chained receipt event')
            event = json.loads(rows[0][0])
            payload = store.db.execute('SELECT payload FROM observations WHERE id=?', (event['observation_id'],)).fetchone()[0]
            import hashlib
            self.assertEqual(event['payload_sha256'], hashlib.sha256(payload.encode()).hexdigest())
            self.assertEqual(event['event'], 'public_receipt')
            self.assertEqual(event['endpoint'], '/fapi/v1/ticker/bookTicker')
            store.close()

    def test_continuous_loop_backs_off_and_recovers_without_fake_heartbeat(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            client = HandmadeClient(collector.utc_now())
            original = client.get
            calls = 0
            def first_fails(endpoint, params=None):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise URLError('hand-made initial outage')
                return original(endpoint, params)
            client.get = first_fails
            delays = []
            def stop_after_recovery(delay):
                delays.append(delay)
                if len(delays) == 2:
                    raise KeyboardInterrupt
            output = io.StringIO()
            with redirect_stdout(output):
                result = collector.main(['--interval', '15'], root=Path(tmp), client=client, sleep=stop_after_recovery)
            self.assertEqual(result, 0)
            self.assertEqual(delays, [30, 15], 'failed loop needs operational backoff')
            states = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(states), 2)
            self.assertFalse(states[0]['feed']['connected'])
            self.assertIsNone(states[0]['feed']['last_success_at'])
            self.assertTrue(states[1]['feed']['connected'])
            self.assertEqual(states[1]['feed']['errors_count'], 1)
            self.assertEqual(states[1]['feed']['gaps_count'], 1)
            self.assertEqual(states[1]['fills_count'], 0)

    def test_invalid_market_values_fail_closed_using_decimal(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            store = collector.Store(Path(tmp))
            client = HandmadeClient()
            original = client.get
            for endpoint_suffix, field, invalid in [('bookTicker', 'bidPrice', 'NaN'),
                                                      ('bookTicker', 'askPrice', '99'),
                                                      ('premiumIndex', 'markPrice', '0'),
                                                      ('premiumIndex', 'lastFundingRate', 'Infinity')]:
                def bad(endpoint, params=None):
                    response = original(endpoint, params)
                    if endpoint.endswith(endpoint_suffix):
                        response['payload'][field] = invalid
                    return response
                client.get = bad
                observer = collector.Observer(store, client=client, clock=lambda: client.now)
                state = observer.poll()
                self.assertFalse(state['feed']['connected'], f'invalid {field} accepted')
                self.assertIsNone(state['feed']['last_success_at'])
                self.assertEqual(state['versions'], [])
            store.close()

    def test_restart_records_missed_heartbeat_without_resetting_forward_start(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            store = collector.Store(Path(tmp))
            client = HandmadeClient()
            observer = collector.Observer(store, client=client, clock=lambda: client.now)
            first = observer.poll()
            started_at = first['collector_started_at']
            store.close()
            store = collector.Store(Path(tmp))
            client = HandmadeClient('2026-10-01T00:20:00Z')
            observer = collector.Observer(store, client=client, clock=lambda: client.now)
            state = observer.poll()
            self.assertEqual(state['feed']['gaps_count'], 1, 'restart falsely hid missed heartbeat')
            self.assertTrue(state['feed']['connected'])
            self.assertEqual(state['collector_started_at'], started_at)
            self.assertEqual([v['id'] for v in state['versions']], ['OBS-001'])
            audits = [json.loads(row[0]) for row in store.db.execute('SELECT payload FROM audit')]
            self.assertIn('heartbeat_gap', [a['event'] for a in audits])
            self.assertIn('gap_end', [a['event'] for a in audits])
            store.close()

    def test_once_cli_refuses_duplicate_collector_instance(self):
        self.assertTrue(hasattr(collector, 'main'), 'missing runnable CLI and single-instance lock')
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            root = Path(tmp)
            output, errors = io.StringIO(), io.StringIO()
            client = HandmadeClient(collector.utc_now())
            with collector.InstanceLock(root):
                with redirect_stdout(output), redirect_stderr(errors):
                    result = collector.main(['--once'], root=root, client=client)
                self.assertEqual(result, 2)
                self.assertEqual(client.calls, [])
                self.assertIn('already running', errors.getvalue())
            with redirect_stdout(output):
                self.assertEqual(collector.main(['--once'], root=root, client=client), 0)
            state = json.loads((root / 'shared/status.json').read_text())
            self.assertTrue(state['feed']['connected'])
            self.assertFalse(state['live_trading_enabled'])
            self.assertIn('"mode": "shadow"', output.getvalue())

    def test_stale_source_timestamps_cannot_make_success_heartbeat(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            store = collector.Store(Path(tmp))
            client = HandmadeClient()
            original = client.get
            def stale(endpoint, params=None):
                result = original(endpoint, params)
                if endpoint.endswith('bookTicker'):
                    result['source_timestamp_ms'] = client.ms - 180000
                return result
            client.get = stale
            observer = collector.Observer(store, client=client, clock=lambda: client.now)
            state = observer.poll()
            self.assertFalse(state['feed']['connected'], 'stale source incorrectly counted as live')
            self.assertIsNone(state['feed']['last_success_at'])
            self.assertEqual(state['versions'], [])
            self.assertIn('stale', state['latest_error'])
            store.close()

    def test_api_failure_exports_stale_without_success_heartbeat(self):
        self.assertTrue(hasattr(collector.Store, 'audit'), 'missing failure audit')
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            store = collector.Store(Path(tmp))
            client = HandmadeClient()
            observer = collector.Observer(store, client=client, clock=lambda: client.now)
            observer.poll()
            success_at = observer.state['feed']['last_success_at']
            versions = list(observer.state['versions'])
            client.now = '2026-10-01T00:11:00Z'
            with patch.object(client, 'get', side_effect=URLError('hand-made API failure')):
                state = observer.poll()
                self.assertFalse(state['feed']['connected'])
                self.assertEqual(state['feed']['last_success_at'], success_at)
                self.assertEqual(state['updated_at'], client.now)
                self.assertEqual(state['feed']['errors_count'], 1)
                self.assertEqual(state['feed']['gaps_count'], 1)
                self.assertEqual(state['versions'], versions)
                self.assertIn('hand-made API failure', state['latest_error'])
                observer.poll()
                self.assertEqual(state['feed']['errors_count'], 2)
                self.assertEqual(state['feed']['gaps_count'], 1)
            self.assertEqual(json.loads((Path(tmp) / 'shared/status.json').read_text()), state)
            audits = store.db.execute('SELECT payload, previous_hash, hash FROM audit ORDER BY id').fetchall()
            import hashlib
            previous = '0' * 64
            for payload, prev, digest in audits:
                self.assertEqual(prev, previous)
                self.assertEqual(digest, hashlib.sha256((prev + payload).encode()).hexdigest())
                previous = digest
            self.assertGreaterEqual(len(audits), 3)
            store.close()

    def test_fresh_forward_poll_persists_public_observations(self):
        self.assertTrue(hasattr(collector, 'Observer'), 'missing observation iteration')
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            store = collector.Store(Path(tmp))
            store.load('2026-10-01T00:00:00Z')
            client = HandmadeClient()
            observer = collector.Observer(store, client=client, clock=lambda: client.now)
            state = observer.poll()
            self.assertTrue(state['feed']['connected'])
            self.assertEqual(state['feed']['last_success_at'], client.now)
            self.assertEqual(state['markets'][0]['bid'], '100.000000000001')
            self.assertEqual(state['markets'][0]['closed_bars_count'], 1)
            self.assertEqual(state['markets'][0]['last_received_at'], client.now)
            self.assertEqual(state['versions'][0]['id'], 'OBS-001')
            self.assertEqual(state['versions'][0]['sample_start_at'], client.now)
            self.assertEqual(state['versions'][0]['fills_count'], 0)
            klines = [params for endpoint, params in client.calls if endpoint.endswith('klines')]
            start = int(datetime.fromisoformat('2026-10-01T00:00:00Z').timestamp() * 1000)
            self.assertTrue(all(p['startTime'] == start and p['interval'] == '5m' for p in klines))
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 9)
            bars = store.db.execute('SELECT COUNT(*) FROM closed_bars').fetchone()[0]
            self.assertEqual(bars, 2)
            observer.poll()
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM closed_bars').fetchone()[0], bars)
            self.assertEqual(observer.state['versions'][-1]['id'], 'OBS-001')
            self.assertFalse(observer.state['paper_trading_enabled'])
            self.assertEqual(observer.state['signals_count'], 0)
            store.close()

    def test_public_transport_retries_without_secret_endpoints(self):
        self.assertTrue(hasattr(collector, 'PublicClient'), 'missing restricted REST client')
        errors, sleeps, calls = [], [], []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"time":1790812800000,"symbol":"ETHUSDT"}'
        def opener(request, timeout):
            calls.append((request, timeout))
            if len(calls) == 1:
                raise URLError('hand-made network test failure')
            return Response()
        client = collector.PublicClient(opener=opener, sleep=sleeps.append,
                                        clock=lambda: '2026-10-01T00:00:00Z', on_error=errors.append)
        result = client.get('/fapi/v1/ticker/bookTicker', {'symbol': 'ETHUSDT'})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(errors), 1)
        self.assertIn(2, sleeps)
        self.assertEqual(calls[0][0].full_url, 'https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol=ETHUSDT')
        self.assertEqual(calls[0][1], 10)
        self.assertEqual(calls[0][0].get_method(), 'GET')
        self.assertEqual(result['source_timestamp_ms'], 1790812800000)
        self.assertEqual(result['received_at'], '2026-10-01T00:00:00Z')
        for endpoint, params in [('/fapi/v1/order', {}), ('https://bad.invalid', {}),
                                 ('/fapi/v1/ticker/bookTicker', {'signature': 'bad'}),
                                 ('/fapi/v1/ticker/bookTicker', {'symbol': 'BTCUSDT'})]:
            with self.assertRaises(ValueError):
                client.get(endpoint, params)
        self.assertEqual(len(calls), 2)

    def test_restart_preserves_canonical_state_and_atomic_snapshot(self):
        self.assertTrue(hasattr(collector, 'Store'), 'missing persistent canonical state')
        with tempfile.TemporaryDirectory(dir=SCRATCH, prefix='collector-unit-') as tmp:
            root = Path(tmp)
            store = collector.Store(root)
            state = store.load('2026-10-01T00:00:00Z')
            state['feed']['errors_count'] = 3
            state['versions'] = [{'id': 'OBS-001', 'started_at': state['collector_started_at']}]
            store.save(state)
            store.close()
            restarted = collector.Store(root)
            restored = restarted.load('2026-10-02T00:00:00Z')
            self.assertEqual(restored, state)
            self.assertEqual(json.loads((root / 'shared/status.json').read_text()), state)
            self.assertEqual(restored['initial_equity_usdt'], '100')
            restored['equity_usdt'] = '99'  # hand-made preservation check, NOT a runtime outcome
            restarted.save(restored)
            self.assertEqual(restarted.load('later')['equity_usdt'], '99')
            restarted.close()

    def test_live_metadata_filters_and_categories(self):
        info = {'symbols': []}
        for symbol, contract, underlying in [('ETHUSDT', 'PERPETUAL', 'COIN'), ('XAUUSDT', 'TRADIFI_PERPETUAL', 'COMMODITY')]:
            info['symbols'].append({'symbol': symbol, 'contractType': contract, 'underlyingType': underlying,
                                    'status': 'TRADING', 'quoteAsset': 'USDT', 'marginAsset': 'USDT',
                                    'filters': [{'filterType': 'PRICE_FILTER', 'tickSize': '0.00001000'},
                                                {'filterType': 'LOT_SIZE', 'stepSize': '0.001000'},
                                                {'filterType': 'MIN_NOTIONAL', 'notional': '5.00000001'}]})
        self.assertTrue(hasattr(collector, 'market_metadata'), 'missing exchangeInfo verification')
        markets = collector.market_metadata(info)
        self.assertEqual([m['category'] for m in markets], ['crypto', 'TradFi'])
        self.assertEqual(markets[0]['tick_size'], '0.00001000')
        self.assertEqual(markets[1]['step_size'], '0.001000')
        self.assertEqual(markets[1]['min_notional'], '5.00000001')
        for field, wrong in [('status', 'HALT'), ('contractType', 'PERPETUAL'), ('quoteAsset', 'USD')]:
            original = info['symbols'][1][field]
            info['symbols'][1][field] = wrong
            with self.assertRaises(ValueError):
                collector.market_metadata(info)
            info['symbols'][1][field] = original
        info['symbols'][0]['filters'].pop()
        with self.assertRaises(ValueError):
            collector.market_metadata(info)

    def test_initial_shadow_is_fail_closed(self):
        self.assertIsNotNone(collector, 'missing shadow exporter implementation')
        state = collector.initial_state('2026-10-01T00:00:00Z')
        self.assertEqual(state['schema_version'], 1)
        self.assertEqual(state['mode'], 'shadow')
        for key in ('paper_start_equity_usdt', 'initial_equity_usdt', 'cash_usdt', 'equity_usdt'):
            self.assertEqual(state[key], '100')
        for key in ('realized_pnl_usdt', 'unrealized_pnl_usdt', 'total_pnl_usdt'):
            self.assertEqual(state[key], '0')
        self.assertEqual(state['positions'], [])
        self.assertEqual(state['fills_count'], 0)
        self.assertFalse(state['paper_trading_enabled'])
        self.assertFalse(state['live_trading_enabled'])
        self.assertEqual(state['blockers'], ['risk_limits_not_set', 'sim_broker_not_implemented', 'signals_not_implemented'])
        self.assertEqual(state['feed']['transport'], 'REST polling')
        self.assertFalse(state['feed']['connected'])
        self.assertEqual(state['versions'], [])


if __name__ == '__main__':
    unittest.main()
