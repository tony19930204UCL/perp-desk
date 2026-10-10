"""Contract, invariant and runtime tests for F1H paper configurations."""
from contextlib import closing
import copy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

import paper_market
from paper_market import instrument
from paper_sizing import size_long

HOUR = 3600000
START = 1790812800000
V4_CONFIG_SHA256 = "e27b4b4b05ec66a2a88b9d4dd4eb502dd0bc329d87003346da55707ee0a7405c"

F1H_SPECS = {
    "BTCUSDT": {"filename": "paper_config_f1h_btcusdt.json", "version_id": "F1H-BTCUSDT-PAPER", "stop_distance_fraction": "0.0070"},
    "SOLUSDT": {"filename": "paper_config_f1h_solusdt.json", "version_id": "F1H-SOLUSDT-PAPER", "stop_distance_fraction": "0.0115"},
    "XRPUSDT": {"filename": "paper_config_f1h_xrpusdt.json", "version_id": "F1H-XRPUSDT-PAPER", "stop_distance_fraction": "0.0140"},
}


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def recursive_diff_paths(dict1, dict2, prefix=()):
    differences = []
    for key in sorted(set(dict1.keys()) | set(dict2.keys())):
        path = prefix + (key,)
        if key not in dict1 or key not in dict2:
            differences.append(path)
        elif isinstance(dict1[key], dict) and isinstance(dict2[key], dict):
            differences.extend(recursive_diff_paths(dict1[key], dict2[key], path))
        elif dict1[key] != dict2[key]:
            differences.append(path)
    return differences


class FakeClient:
    def __init__(self, start_ms=START):
        self.now = start_ms
        self.prices = {
            'BTCUSDT': {'price': '50000', 'bid': '49995', 'ask': '50005', 'tick': '0.10', 'qty_step': '0.001', 'min_qty': '0.001', 'notional': '50'},
            'SOLUSDT': {'price': '150', 'bid': '149.95', 'ask': '150.05', 'tick': '0.01', 'qty_step': '0.01', 'min_qty': '0.01', 'notional': '5'},
            'XRPUSDT': {'price': '0.50', 'bid': '0.4995', 'ask': '0.5005', 'tick': '0.0001', 'qty_step': '1', 'min_qty': '1', 'notional': '5'},
            'ETHUSDT': {'price': '100', 'bid': '99.99', 'ask': '100.01', 'tick': '0.01', 'qty_step': '0.001', 'min_qty': '0.001', 'notional': '20'},
            'XAUUSDT': {'price': '2600', 'bid': '2599.5', 'ask': '2600.5', 'tick': '0.01', 'qty_step': '0.01', 'min_qty': '0.01', 'notional': '20'},
        }
        self.calls, self.bars = [], {}
        self.funding_interval = 8

    def get(self, endpoint, params=None):
        params = params or {}
        self.calls.append((endpoint, dict(params)))
        sym = params.get('symbol', 'ETHUSDT')
        pinfo = self.prices.get(sym, self.prices['ETHUSDT'])
        src = self.now
        if endpoint.endswith('exchangeInfo'):
            symbols_meta = [
                dict(symbol=s, contractType='TRADIFI_PERPETUAL' if s == 'XAUUSDT' else 'PERPETUAL', status='TRADING', quoteAsset='USDT', marginAsset='USDT',
                     filters=[dict(filterType='PRICE_FILTER', tickSize=info['tick']),
                              dict(filterType='LOT_SIZE', stepSize=info['qty_step'], minQty=info['min_qty'], maxQty='1000000'),
                              dict(filterType='MARKET_LOT_SIZE', stepSize=info['qty_step'], minQty=info['min_qty'], maxQty='1000000'),
                              dict(filterType='MIN_NOTIONAL', notional=info['notional'])])
                for s, info in self.prices.items()
            ]
            payload = dict(symbols=symbols_meta)
        elif endpoint.endswith('fundingInfo'):
            payload = [dict(symbol=s, fundingIntervalHours=self.funding_interval) for s in self.prices]
        elif endpoint.endswith('premiumIndex'):
            next_f = (self.now // 28800000 + 1) * 28800000
            payload = dict(symbol=sym, markPrice=pinfo['price'], lastFundingRate='0.0001', time=src, nextFundingTime=next_f)
        elif endpoint.endswith('bookTicker'):
            payload = dict(symbol=sym, bidPrice=pinfo['bid'], askPrice=pinfo['ask'], time=src)
        elif endpoint.endswith('depth'):
            payload = dict(E=src, bids=[[pinfo['bid'], '100']], asks=[[pinfo['ask'], '100']])
        elif endpoint.endswith('fundingRate'):
            cur = self.now // 28800000 * 28800000
            payload = [dict(symbol=sym, fundingTime=t, fundingRate='0.0001', markPrice=pinfo['price'], rateType='Regular') for t in (cur - 28800000, cur)]
        elif endpoint.endswith('klines'):
            if params.get('limit') == 61:
                st = params['startTime']
                payload = [
                    [st + i * HOUR,
                     '100.3' if i % 2 else '100.0',
                     '100.3' if i % 2 else '100.0',
                     '100.3' if i % 2 else '100.0',
                     '100.3' if i % 2 else '100.0',
                     '10', st + (i + 1) * HOUR - 1, '0', 0, '0', '0', '0']
                    for i in range(61)
                ]
            else:
                st = params.get('startTime', 0)
                payload = [b for b in self.bars.get(sym, []) if b[0] >= st][:params.get('limit', 1000)]
        else:
            raise AssertionError(endpoint)
        return dict(endpoint=endpoint, params=params, received_at=iso(self.now), source_timestamp_ms=src, payload=payload)


def add_bar(client, symbol, i, price='100', volume='10'):
    t = START + i * HOUR
    client.bars.setdefault(symbol, []).append([t, price, price, price, price, volume, t + HOUR - 1, '0', 0, '0', '0', '0'])


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.v4_raw = (LAB / "paper_config_v4.json").read_text(encoding="utf-8")
        self.v4 = json.loads(self.v4_raw)
        self.configs = {s: json.loads((LAB / sp["filename"]).read_text(encoding="utf-8")) for s, sp in F1H_SPECS.items()}

    def test_configs_load_valid_json(self):
        self.assertEqual(len(self.configs), 3)
        for config in self.configs.values():
            self.assertEqual(config.get("schema_version"), 1)
            self.assertEqual(config.get("mode"), "paper")
            self.assertIs(config.get("approved"), True)

    def test_recursive_diff_against_v4_lists_exactly_three_changed_paths(self):
        expected = {("version_id",), ("strategy", "symbol"), ("strategy", "stop_distance_fraction")}
        for symbol, config in self.configs.items():
            diffs = recursive_diff_paths(config, self.v4)
            self.assertEqual(set(diffs), expected, f"diff mismatch for {symbol}")

    def test_version_ids_are_distinct(self):
        version_ids = [c["version_id"] for c in self.configs.values()]
        self.assertEqual(len(version_ids), len(set(version_ids)))
        for symbol, spec in F1H_SPECS.items():
            self.assertEqual(self.configs[symbol]["version_id"], spec["version_id"])

    def test_stop_distance_fractions_equal_specified_values(self):
        for symbol, spec in F1H_SPECS.items():
            self.assertEqual(self.configs[symbol]["strategy"]["stop_distance_fraction"], spec["stop_distance_fraction"])

    def test_full_dicts_equal_after_removing_three_changed_paths(self):
        for symbol, config in self.configs.items():
            c_copy, v4_copy = copy.deepcopy(config), copy.deepcopy(self.v4)
            for path in [("version_id",), ("strategy", "symbol"), ("strategy", "stop_distance_fraction")]:
                tc, tv = c_copy, v4_copy
                for segment in path[:-1]:
                    tc, tv = tc[segment], tv[segment]
                del tc[path[-1]], tv[path[-1]]
            self.assertEqual(c_copy, v4_copy, f"unallowed differences in {symbol}")

    def test_strategy_and_risk_contract_invariants(self):
        for config in self.configs.values():
            self.assertEqual(config["strategy"]["interval_ms"], 3600000)
            self.assertEqual(config["strategy"]["max_holding_ms"], 43200000)
            self.assertEqual(config["research"]["window_ms"], 1814400000)
            self.assertEqual(config["strategy"]["downside_sigma"], "1.5")
            self.assertEqual(config["strategy"]["volume_multiple"], "1.2")
            self.assertEqual(config["strategy"]["minimum_gross_reward_to_estimated_cost"], "2")
            self.assertEqual(config["initial_equity_usdt"], "100")
            self.assertEqual(config["max_loss_per_trade_usdt"], "1")
            self.assertEqual(config["max_daily_loss_usdt"], "3")
            self.assertEqual(config["max_effective_exposure_x"], "3")
            self.assertEqual(config["max_positions"], 1)
            self.assertEqual(config["total_loss_limit_usdt"], "10")

    def test_paper_config_v4_file_unchanged(self):
        self.assertEqual(hashlib.sha256((LAB / "paper_config_v4.json").read_bytes()).hexdigest(), V4_CONFIG_SHA256)

    def test_key_order_and_formatting_preserved(self):
        for symbol, config in self.configs.items():
            self.assertEqual(list(config.keys()), list(self.v4.keys()))
            self.assertEqual(list(config["strategy"].keys()), list(self.v4["strategy"].keys()))
            self.assertEqual(config["strategy"]["symbol"], symbol)


class RuntimeV5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_differential_v4_v5_bootstrap_and_polling_ethusdt(self):
        # Checks bootstrap and polling equivalence between the v4 and v5 runtimes;
        # the signal-to-order path is covered by test_signal_routing_submits_buy_for_each_symbol below.
        from paper_runtime_v4 import PaperRuntime as V4Runtime
        from paper_runtime_v5 import PaperRuntime as V5Runtime

        def run_scripted(runtime_cls, root_dir, observe_xau):
            client = FakeClient(start_ms=START + 62 * HOUR)
            kw = dict(observe_xau=observe_xau) if runtime_cls is V5Runtime else {}
            r = runtime_cls(root_dir, LAB / 'paper_config_v4.json', client=client, clock_ms=lambda: client.now, fixture=True, **kw)
            try:
                r.poll()
                add_bar(client, 'ETHUSDT', 61, '100.0', '10')
                client.now = START + 62 * HOUR + 2000
                r.poll()
                add_bar(client, 'ETHUSDT', 62, '98.0', '13')
                client.now = START + 63 * HOUR + 2000
                client.prices['ETHUSDT']['price'] = '98.0'
                client.prices['ETHUSDT']['bid'], client.prices['ETHUSDT']['ask'] = '97.99', '98.01'
                r.poll()
                client.now += 3000
                r.poll()
                client.now += 1000
                client.prices['ETHUSDT']['price'] = '100.0'
                client.prices['ETHUSDT']['bid'], client.prices['ETHUSDT']['ask'] = '99.99', '100.01'
                r.poll()
                client.now += 3000
                r.poll()
                with closing(sqlite3.connect(r.root / 'runtime.sqlite3')) as db:
                    event_types = [json.loads(row[0])['type'] for row in db.execute('SELECT payload FROM audit ORDER BY id').fetchall()]
                fills = [(f['symbol'], f['side'], f['qty'], f['price']) for f in r.broker.fills]
                return event_types, fills
            finally:
                r.close()

        events4, fills4 = run_scripted(V4Runtime, Path(self.temp.name) / 'v4', True)
        events5, fills5 = run_scripted(V5Runtime, Path(self.temp.name) / 'v5', True)
        self.assertNotIn('poll_error', events4)
        self.assertNotIn('poll_error', events5)
        self.assertEqual(events4, events5)
        self.assertEqual(fills4, fills5)

    def test_signal_routing_submits_buy_for_each_symbol(self):
        from paper_runtime_v5 import PaperRuntime
        for symbol, spec_info in F1H_SPECS.items():
            client = FakeClient(start_ms=START + 62 * HOUR)
            r = PaperRuntime(Path(self.temp.name) / f'route_{symbol}', LAB / spec_info['filename'], client=client, clock_ms=lambda: client.now, fixture=True)
            try:
                r.poll()
                market = next(m for m in r.state['markets'] if m['symbol'] == symbol)
                target = str(Decimal(market['ask']) * Decimal('1.03'))
                signal_id = f'sig_{symbol}'
                intent = {
                    'signal_id': signal_id,
                    'symbol': symbol,
                    'reversion_target': target,
                    'features': {'bar_close_ms': client.now - 1000},
                }
                with closing(sqlite3.connect(r.root / 'signals.sqlite3')) as db, db:
                    db.execute(
                        'INSERT INTO h1_signals (signal_id, version_id, intent) VALUES (?, ?, ?)',
                        (signal_id, r.config['version_id'], json.dumps(intent)),
                    )
                spec = paper_market.instrument(client.get('/fapi/v1/exchangeInfo'), symbol, 'crypto', r.config['fee_assumptions']['crypto'])
                r.route_signals(spec)
                self.assertEqual(r.state['handled_signals'][signal_id]['status'], 'submitted')
                self.assertEqual(len(r.broker.orders), 1)
                order = next(iter(r.broker.orders.values()))
                self.assertEqual(order['status'], 'PENDING')
                stop = float(r.state['handled_signals'][signal_id]['stop_price'])
                ask = float(market['ask'])
                fraction = float(r.config['strategy']['stop_distance_fraction'])
                self.assertTrue(abs(stop / ask - (1 - fraction)) < 0.0005)

                other_symbol = next(s for s in ('BTCUSDT', 'SOLUSDT', 'XRPUSDT', 'ETHUSDT') if s != symbol)
                other_id = f'sig_{symbol}_other'
                other_intent = {
                    'signal_id': other_id,
                    'symbol': other_symbol,
                    'reversion_target': target,
                    'features': {'bar_close_ms': client.now - 1000},
                }
                with closing(sqlite3.connect(r.root / 'signals.sqlite3')) as db, db:
                    db.execute(
                        'INSERT INTO h1_signals (signal_id, version_id, intent) VALUES (?, ?, ?)',
                        (other_id, r.config['version_id'], json.dumps(other_intent)),
                    )
                r.route_signals(spec)
                self.assertNotIn(other_id, r.state['handled_signals'])
            finally:
                r.close()

    def test_klines_requests_for_each_symbol(self):
        from paper_runtime_v5 import PaperRuntime
        for symbol, spec in F1H_SPECS.items():
            client = FakeClient(start_ms=START + 62 * HOUR)
            r = PaperRuntime(Path(self.temp.name) / f'klines_{symbol}', LAB / spec['filename'], client=client, clock_ms=lambda: client.now, fixture=True)
            try:
                r.poll()
                calls = [p for ep, p in client.calls if ep.endswith('klines') and p.get('limit') == 61]
                self.assertEqual(len(calls), 1)
                p = calls[0]
                self.assertEqual(p['symbol'], symbol)
                self.assertEqual(p['interval'], '1h')
                self.assertEqual(p['limit'], 61)
                self.assertEqual(p['startTime'] % HOUR, 0)
                self.assertEqual((p['endTime'] + 1) % HOUR, 0)
            finally:
                r.close()

    def test_broker_filters_come_from_configured_symbol(self):
        from paper_runtime_v5 import PaperRuntime
        for symbol, spec in F1H_SPECS.items():
            client = FakeClient(start_ms=START + 62 * HOUR)
            r = PaperRuntime(Path(self.temp.name) / f'broker_{symbol}', LAB / spec['filename'], client=client, clock_ms=lambda: client.now, fixture=True)
            try:
                r.poll()
                inst = r.broker.instruments.get(symbol)
                self.assertIsNotNone(inst)
                self.assertEqual(inst.symbol, symbol)
                pinfo = client.prices[symbol]
                self.assertEqual(str(inst.tick), pinfo['tick'])
                self.assertEqual(str(inst.quantity_step), pinfo['qty_step'])
                self.assertEqual(str(inst.min_notional), pinfo['notional'])
            finally:
                r.close()

    def test_late_closed_bar_signal_boundary(self):
        from paper_runtime_v5 import PaperRuntime
        client = FakeClient(start_ms=START + 62 * HOUR)
        r = PaperRuntime(Path(self.temp.name) / 'late_signal', LAB / 'paper_config_f1h_btcusdt.json', client=client, clock_ms=lambda: client.now, fixture=True)
        try:
            r.poll()
            spec = instrument(r.reference(), 'BTCUSDT', 'crypto', r.config['fee_assumptions']['crypto'])
            bar_close = client.now
            i1 = dict(signal_id='sig_14999', symbol='BTCUSDT', reversion_target='51000', features=dict(bar_close_ms=str(bar_close)))
            with closing(sqlite3.connect(r.root / 'signals.sqlite3')) as db, db:
                db.execute('INSERT INTO h1_signals (signal_id, version_id, intent) VALUES (?, ?, ?)',
                           ('sig_14999', r.config['version_id'], json.dumps(i1)))
            client.now = bar_close + 14999
            r.route_signals(spec)
            self.assertEqual(r.state['handled_signals']['sig_14999']['status'], 'submitted')

            r.broker.orders.clear()
            i2 = dict(signal_id='sig_15001', symbol='BTCUSDT', reversion_target='51000', features=dict(bar_close_ms=str(bar_close)))
            with closing(sqlite3.connect(r.root / 'signals.sqlite3')) as db, db:
                db.execute('INSERT INTO h1_signals (signal_id, version_id, intent) VALUES (?, ?, ?)',
                           ('sig_15001', r.config['version_id'], json.dumps(i2)))
            client.now = bar_close + 15001
            r.route_signals(spec)
            self.assertEqual(r.state['handled_signals']['sig_15001']['status'], 'rejected')
            self.assertEqual(r.state['handled_signals']['sig_15001']['reason'], 'late_closed_bar_signal')
        finally:
            r.close()

    def test_planned_loss_at_stop_at_most_1_usdt(self):
        for symbol, spec in F1H_SPECS.items():
            cfg = json.loads((LAB / spec['filename']).read_text(encoding='utf-8'))
            client = FakeClient()
            ref = client.get('/fapi/v1/exchangeInfo')
            inst = instrument(ref, symbol, 'crypto', cfg['fee_assumptions']['crypto'])
            pinfo = client.prices[symbol]
            target = str(Decimal(pinfo['ask']) * Decimal('1.03'))
            res = size_long(cfg, inst, equity='100', bid=pinfo['bid'], ask=pinfo['ask'], target=target)
            self.assertEqual(res['status'], 'accepted', f"sizing rejected for {symbol}: {res}")
            loss = Decimal(res['qty']) * Decimal(res['planned_loss_per_unit'])
            self.assertLessEqual(loss, Decimal('1.0000000001'), f"planned loss for {symbol} exceeds 1 USDT: {loss}")

    def test_stop_price_at_configured_fraction_below_entry(self):
        for symbol, spec in F1H_SPECS.items():
            cfg = json.loads((LAB / spec['filename']).read_text(encoding='utf-8'))
            client = FakeClient()
            ref = client.get('/fapi/v1/exchangeInfo')
            inst = instrument(ref, symbol, 'crypto', cfg['fee_assumptions']['crypto'])
            pinfo = client.prices[symbol]
            target = str(Decimal(pinfo['ask']) * Decimal('1.03'))
            res = size_long(cfg, inst, equity='100', bid=pinfo['bid'], ask=pinfo['ask'], target=target)
            self.assertEqual(res['status'], 'accepted')
            entry, stop = Decimal(res['entry_price_bound']), Decimal(res['stop_price'])
            fraction, tick = Decimal(spec['stop_distance_fraction']), Decimal(inst['tick_size'])
            expected_raw = entry * (1 - fraction)
            self.assertLessEqual(stop, expected_raw)
            self.assertGreater(stop, expected_raw - tick)

    def test_exit_triggers_max_hold_and_take_profit(self):
        from paper_runtime_v5 import PaperRuntime
        client = FakeClient(start_ms=START + 62 * HOUR)
        r = PaperRuntime(Path(self.temp.name) / 'exits', LAB / 'paper_config_f1h_btcusdt.json', client=client, clock_ms=lambda: client.now, fixture=True)
        try:
            r.poll()
            open_ts = client.now
            r.broker.positions['BTCUSDT'] = dict(symbol='BTCUSDT', qty='0.002', entry='50000', stop='49600', opened_ts=open_ts)
            r.broker.fills.append(dict(symbol='BTCUSDT', side='BUY', qty='0.002', price='50000', intent_id='intent_test'))
            r.state['handled_signals'] = {'intent_test': dict(target='51000')}
            r.exit_policy(open_ts + 43199999, '50000')
            self.assertEqual(len(r.broker.orders), 0)
            r.exit_policy(open_ts + 43200000, '50000')
            self.assertEqual(len(r.broker.orders), 1)
            self.assertIn('max_hold', next(iter(r.broker.orders.values()))['intent_id'])
            r.broker.orders.clear()
            r.exit_policy(open_ts + 1000, '50999')
            self.assertEqual(len(r.broker.orders), 0)
            r.exit_policy(open_ts + 1000, '51000')
            self.assertEqual(len(r.broker.orders), 1)
            self.assertIn('take_profit', next(iter(r.broker.orders.values()))['intent_id'])
        finally:
            r.close()

    def test_deadline_equals_start_plus_window_and_survives_restart(self):
        from paper_runtime_v5 import PaperRuntime
        sub = Path(self.temp.name) / 'deadline_restart'
        client = FakeClient(start_ms=START)
        r = PaperRuntime(sub, LAB / 'paper_config_f1h_btcusdt.json', client=client, clock_ms=lambda: client.now, fixture=True)
        start = r.state['strategy_start_ms']
        expected_deadline = start + 1814400000
        self.assertEqual(r.state['research_deadline_ms'], expected_deadline)
        r.close()
        client.now += 500000
        r2 = PaperRuntime(sub, LAB / 'paper_config_f1h_btcusdt.json', client=client, clock_ms=lambda: client.now, fixture=True)
        try:
            self.assertEqual(r2.state['strategy_start_ms'], start)
            self.assertEqual(r2.state['research_deadline_ms'], expected_deadline)
        finally:
            r2.close()

    def test_modified_config_refused(self):
        from paper_runtime_v5 import PaperRuntime
        cfg = json.loads((LAB / 'paper_config_f1h_btcusdt.json').read_text(encoding='utf-8'))
        cfg['strategy']['stop_distance_fraction'] = '0.02'
        mod_path = Path(self.temp.name) / 'modified.json'
        mod_path.write_text(json.dumps(cfg), encoding='utf-8')
        client = FakeClient(start_ms=START)
        with self.assertRaises(ValueError):
            PaperRuntime(Path(self.temp.name) / 'mod', mod_path, client=client, clock_ms=lambda: client.now, fixture=True)

    def test_other_symbol_bars_never_routed(self):
        from paper_runtime_v5 import PaperRuntime
        client = FakeClient(start_ms=START + 62 * HOUR)
        r = PaperRuntime(Path(self.temp.name) / 'foreign_symbol', LAB / 'paper_config_f1h_btcusdt.json', client=client, clock_ms=lambda: client.now, fixture=True)
        try:
            r.poll()
            spec = instrument(r.reference(), 'BTCUSDT', 'crypto', r.config['fee_assumptions']['crypto'])
            foreign = dict(signal_id='sig_sol', symbol='SOLUSDT', reversion_target='160', features=dict(bar_close_ms=str(client.now)))
            with closing(sqlite3.connect(r.root / 'signals.sqlite3')) as db, db:
                db.execute('INSERT INTO h1_signals (signal_id, version_id, intent) VALUES (?, ?, ?)',
                           ('sig_sol', r.config['version_id'], json.dumps(foreign)))
            r.route_signals(spec)
            self.assertNotIn('sig_sol', r.state['handled_signals'])
            self.assertEqual(len(r.broker.orders), 0)
            bar_sol = dict(symbol='SOLUSDT', open_time_ms=START, close_time_ms=START + HOUR, closed=True,
                           open=Decimal('150'), high=Decimal('150'), low=Decimal('150'), close=Decimal('150'), volume=Decimal('10'))
            res = r.detector.process(bar_sol, now=datetime.fromtimestamp(client.now / 1000, timezone.utc))
            self.assertTrue(res['diagnostic'].startswith('reject'))
            self.assertIsNone(res['intent'])
            self.assertFalse(any(o['intent']['symbol'] == 'SOLUSDT' for o in r.broker.orders.values()))
            self.assertFalse(any(f['symbol'] == 'SOLUSDT' for f in r.broker.fills))
            self.assertEqual(len(r.broker.orders), 0)
            self.assertEqual(len(r.broker.fills), 0)
        finally:
            r.close()

    def test_observe_xau_configurable_and_off_by_default(self):
        from paper_runtime_v5 import PaperRuntime
        client1 = FakeClient(start_ms=START + 62 * HOUR)
        r1 = PaperRuntime(Path(self.temp.name) / 'xau_off', LAB / 'paper_config_f1h_btcusdt.json', client=client1, clock_ms=lambda: client1.now, fixture=True)
        try:
            self.assertFalse(r1.observe_xau)
            r1.poll()
            self.assertEqual(len([p for ep, p in client1.calls if p.get('symbol') == 'XAUUSDT']), 0)
        finally:
            r1.close()
        client2 = FakeClient(start_ms=START + 62 * HOUR)
        r2 = PaperRuntime(Path(self.temp.name) / 'xau_on', LAB / 'paper_config_f1h_btcusdt.json', client=client2, clock_ms=lambda: client2.now, fixture=True, observe_xau=True)
        try:
            self.assertTrue(r2.observe_xau)
            r2.poll()
            self.assertGreater(len([p for ep, p in client2.calls if p.get('symbol') == 'XAUUSDT']), 0)
        finally:
            r2.close()

    def test_funding_interval_validation(self):
        from paper_runtime_v5 import PaperRuntime
        client = FakeClient(start_ms=START + 62 * HOUR)
        client.funding_interval = 4
        r = PaperRuntime(Path(self.temp.name) / 'funding_val', LAB / 'paper_config_f1h_btcusdt.json', client=client, clock_ms=lambda: client.now, fixture=True)
        try:
            spec = instrument(r.reference(), 'BTCUSDT', 'crypto', r.config['fee_assumptions']['crypto'])
            with self.assertRaises(ValueError):
                r.funding(spec)
        finally:
            r.close()

    def test_differential_v4_v5_seed_ethusdt(self):
        from signals_v4 import Detector as V4Detector
        from signals_v5 import Detector as V5Detector

        cutoff = START + 62 * HOUR
        end = (cutoff - 1) // HOUR * HOUR
        first = end - 61 * HOUR
        rows = []
        for i in range(61):
            t = first + i * HOUR
            rows.append([t, '100', '100', '100', '100', '10', t + HOUR - 1])
        now = cutoff + 1000
        receipt = dict(
            endpoint='/fapi/v1/klines',
            params=dict(symbol='ETHUSDT', interval='1h',
                        startTime=first, endTime=end - 1, limit=61),
            payload=rows,
            received_at=datetime.fromtimestamp((now - 1000) / 1000, timezone.utc).isoformat(),
        )

        d4_dir = Path(self.temp.name) / 'seed_v4'
        d4_dir.mkdir()
        d5_dir = Path(self.temp.name) / 'seed_v5'
        d5_dir.mkdir()

        forward_start = datetime.fromtimestamp(START / 1000, timezone.utc)
        d4 = V4Detector(d4_dir / 'signals.sqlite3', 'H2-TEST', forward_start, {'ETHUSDT': 'crypto'})
        d5 = V5Detector(d5_dir / 'signals.sqlite3', 'H2-TEST', forward_start, {'ETHUSDT': 'crypto'})

        manifest4 = d4.seed(receipt, cutoff_ms=cutoff, now_ms=now)
        manifest5 = d5.seed(receipt, cutoff_ms=cutoff, now_ms=now)
        self.assertEqual(manifest4, manifest5)

        next_bar = dict(
            symbol='ETHUSDT', open_time_ms=end, close_time_ms=end + HOUR, closed=True,
            open=Decimal('100'), high=Decimal('100'), low=Decimal('100'), close=Decimal('100'),
            volume=Decimal('10'),
        )
        feed_now = datetime.fromtimestamp((end + HOUR + 1000) / 1000, timezone.utc)
        res4 = d4.process(next_bar, now=feed_now)
        res5 = d5.process(next_bar, now=feed_now)
        self.assertEqual(res4['diagnostic'], res5['diagnostic'])
        self.assertEqual(res4, res5)


if __name__ == "__main__":
    unittest.main()
