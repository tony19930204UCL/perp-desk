"""Handcrafted unit fixtures only; never market observations or performance."""
import unittest
import tempfile
import json
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone, timedelta
from decimal import Decimal, localcontext
try:
    import signals
except ImportError:
    signals = None

SCRATCH = Path(tempfile.gettempdir())
START = datetime(2026, 10, 1, tzinfo=timezone.utc)
STEP = 300000
BASE = int(START.timestamp()) * 1000

class SignalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='signals-test-', dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'signals.sqlite3'

    def engine(self, version='H1-TEST', start=START, category='crypto', symbol='TEST'):
        self.assertIsNotNone(signals, 'signal library must exist')
        return signals.Detector(self.path, version, start, {'TEST': category} if symbol == 'TEST' else {symbol: category})

    def bar(self, i, close='100', volume='10', **changes):
        result = dict(symbol='TEST', open_time_ms=BASE+i*STEP,
                      close_time_ms=BASE+(i+1)*STEP, closed=True,
                      open=Decimal(close), high=Decimal(close), low=Decimal(close),
                      close=Decimal(close), volume=Decimal(volume))
        result.update(changes)
        return result

    def push(self, engine, bar, **kw):
        return engine.process(bar, now=START+timedelta(days=2), **kw)

    def warm(self, engine, count=61):
        for i in range(count):
            self.push(engine, self.bar(i))

    def test_registration_is_immutable(self):
        engine = self.engine()
        self.assertEqual(engine.registration['forward_start'], START.isoformat())
        self.assertEqual(engine.registration['version_id'], 'H1-TEST')
        with self.assertRaises(ValueError):
            self.engine(start=START+timedelta(minutes=5))
        with self.assertRaises(ValueError):
            self.engine(category='TradFi')

    def test_early_insufficient_60_prior_returns(self):
        engine = self.engine()
        self.assertTrue(callable(getattr(engine, 'process', None)), 'closed-bar processing required')
        self.warm(engine, 60)
        result = self.push(engine, self.bar(60, '90', '100'))
        self.assertEqual(result['diagnostic'], 'warmup')
        self.assertIsNone(result['intent'])

    def test_h1_long_research_intent(self):
        engine = self.engine()
        self.warm(engine)
        result = self.push(engine, self.bar(61, '90', '21'))
        self.assertEqual(result['diagnostic'], 'research_intent')
        intent = result['intent']
        self.assertEqual(intent['side'], 'long')
        self.assertEqual(intent['entry_reference'], '100')
        self.assertEqual(intent['reversion_target'], '100')
        self.assertEqual(intent['version_id'], 'H1-TEST')
        self.assertTrue(intent['signal_id'])
        for forbidden in ('quantity', 'leverage', 'stop', 'risk', 'fill'):
            self.assertNotIn(forbidden, intent)

    def test_current_return_equal_threshold_does_not_trigger(self):
        engine = self.engine()
        self.warm(engine)
        result = self.push(engine, self.bar(61, '100', '21'))
        self.assertEqual(result['diagnostic'], 'no_signal')
        self.assertIsNone(result['intent'])

    def test_current_volume_equal_threshold_does_not_trigger(self):
        engine = self.engine()
        self.warm(engine)
        result = self.push(engine, self.bar(61, '90', '20'))
        self.assertEqual(result['diagnostic'], 'no_signal')
        self.assertIsNone(result['intent'])

    def test_tradfi_requires_valid_open_session(self):
        engine = self.engine(category='TradFi')
        bar = self.bar(0)
        contexts = [None, {}, {'is_open': False}, {'is_open': True},
                    {'is_open': True, 'symbol': 'WRONG', 'valid_from_ms': BASE, 'valid_until_ms': BASE+STEP, 'source': 'fixture'},
                    {'is_open': True, 'symbol': 'TEST', 'valid_from_ms': BASE+1, 'valid_until_ms': BASE+STEP, 'source': 'fixture'}]
        for context in contexts:
            with self.subTest(context=context):
                result = self.push(engine, bar, session=context)
                self.assertEqual(result['diagnostic'], 'reject_session')
                self.assertIsNone(result['intent'])
        valid = {'is_open': True, 'symbol': 'TEST', 'valid_from_ms': BASE,
                 'valid_until_ms': BASE+100*STEP, 'source': 'handcrafted-test-not-calendar'}
        for i in range(61):
            self.assertEqual(self.push(engine, self.bar(i), session=valid)['diagnostic'], 'warmup')
        self.assertEqual(self.push(engine, self.bar(61, '90', '21'), session=valid)['diagnostic'], 'research_intent')

    def test_gap_resets_warmup_without_interpolation(self):
        engine = self.engine()
        self.warm(engine)
        result = self.push(engine, self.bar(62, '90', '21'))
        self.assertEqual(result['diagnostic'], 'gap_reset')
        self.assertIsNone(result['intent'])
        self.assertEqual(result['gap']['expected_open_ms'], str(BASE+61*STEP))
        self.assertEqual(result['gap']['actual_open_ms'], str(BASE+62*STEP))
        for i in range(63, 123):
            self.assertEqual(self.push(engine, self.bar(i, '90'))['diagnostic'], 'warmup')
        self.assertEqual(self.push(engine, self.bar(123, '80', '21'))['diagnostic'], 'research_intent')

    def test_duplicate_closed_bar_never_repeats_signal(self):
        engine = self.engine()
        self.warm(engine)
        bar = self.bar(61, '90', '21')
        first = self.push(engine, bar)
        repeated = self.push(engine, dict(bar))
        self.assertEqual(first['diagnostic'], 'research_intent')
        self.assertEqual(repeated['diagnostic'], 'duplicate')
        self.assertIsNone(repeated['intent'])
        self.assertEqual(self.push(engine, self.bar(62, '80', '21'))['diagnostic'], 'research_intent')

    def test_out_of_order_and_revised_bars_rejected(self):
        engine = self.engine()
        self.warm(engine)
        original = self.push(engine, self.bar(61, '90', '21'))
        for rejected in (self.bar(60), self.bar(61, '89', '21')):
            result = self.push(engine, rejected)
            self.assertEqual(result['diagnostic'], 'reject_out_of_order')
            self.assertIsNone(result['intent'])
        self.assertEqual(self.push(engine, self.bar(62, '80', '21'))['diagnostic'], 'research_intent')
        self.assertEqual(original['intent']['entry_reference'], '100')

    def test_invalid_decimal_ohlcv_rejected_without_state_change(self):
        engine = self.engine()
        self.warm(engine)
        bad = [dict(close=Decimal('NaN')), dict(volume=Decimal('Infinity')),
               dict(high=Decimal('-Infinity')), dict(close=90.0), dict(close='90'),
               dict(volume=Decimal('-1')), dict(low=Decimal('101')),
               dict(open=Decimal('0')), dict(close=Decimal('0'))]
        for changes in bad:
            with self.subTest(changes=changes):
                candidate = self.bar(61)
                candidate.update(changes)
                try:
                    result = self.push(engine, candidate)
                except Exception as exc:
                    self.fail(f'invalid bars must reject without throwing: {exc!r}')
                self.assertEqual(result['diagnostic'], 'reject_invalid_bar')
                self.assertIsNone(result['intent'])
        self.assertEqual(self.push(engine, self.bar(61, '90', '21'))['diagnostic'], 'research_intent')

    def test_bar_timestamps_closed_symbol_and_future_validation(self):
        engine = self.engine()
        variants = [dict(symbol='WRONG'), dict(closed=False), dict(closed=1),
                    dict(open_time_ms=True), dict(open_time_ms=str(BASE)),
                    dict(open_time_ms=-1), dict(open_time_ms=BASE+1),
                    dict(close_time_ms=BASE+STEP-1), dict(close_time_ms=BASE+STEP+1)]
        for changes in variants:
            with self.subTest(changes=changes):
                try:
                    result = self.push(engine, self.bar(0, **changes))
                except Exception as exc:
                    self.fail(f'bad metadata must reject without throwing: {exc!r}')
                self.assertEqual(result['diagnostic'], 'reject_invalid_bar')
        self.assertEqual(engine.process(self.bar(0), now=START)['diagnostic'], 'reject_future')
        self.assertEqual(engine.process(self.bar(0), now=START.replace(tzinfo=None))['diagnostic'], 'reject_clock')
        self.assertEqual(engine.process(self.bar(0), now=START.astimezone(timezone(timedelta(hours=1))))['diagnostic'], 'reject_clock')
        self.assertEqual(engine.process(self.bar(0), now=START+timedelta(minutes=5))['diagnostic'], 'warmup')

    def test_registration_requires_explicit_category_and_utc(self):
        for category in ('tradfi', 'unknown', '', None):
            with self.subTest(category=category), self.assertRaises(ValueError):
                self.engine(version='INVALID-'+repr(category), category=category)
        for start in (START.replace(tzinfo=None), START.astimezone(timezone(timedelta(hours=1)))):
            with self.subTest(start=start), self.assertRaises(ValueError):
                self.engine(version='INVALID-UTC-'+start.isoformat(), start=start)
        for version in ('', None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.engine(version=version)
        with self.assertRaises(ValueError):
            signals.Detector(self.path, 'EMPTY', START, {})

    def test_new_version_needs_new_forward_warmup(self):
        old = self.engine()
        self.warm(old)
        self.assertEqual(self.push(old, self.bar(61, '90', '21'))['diagnostic'], 'research_intent')
        new = self.engine(version='H1-NEW', start=START+timedelta(minutes=5*62))
        for i in range(62):
            self.assertEqual(self.push(new, self.bar(i))['diagnostic'], 'reject_before_forward_start')
        self.assertEqual(self.push(new, self.bar(62, '90', '21'))['diagnostic'], 'warmup')
        for i in range(63, 123):
            self.assertEqual(self.push(new, self.bar(i, '90'))['diagnostic'], 'warmup')
        self.assertEqual(self.push(new, self.bar(123, '80', '21'))['diagnostic'], 'research_intent')

    def test_restart_restores_state_and_seen_signal_ids(self):
        engine = self.engine()
        self.warm(engine, 40)
        restarted = self.engine()
        for i in range(40, 61):
            self.assertEqual(self.push(restarted, self.bar(i))['diagnostic'], 'warmup')
        signal = self.push(restarted, self.bar(61, '90', '21'))
        self.assertEqual(signal['diagnostic'], 'research_intent')
        resumed = self.engine()
        self.assertEqual(self.push(resumed, self.bar(61, '90', '21'))['diagnostic'], 'duplicate')
        self.assertEqual(self.push(resumed, self.bar(62, '80', '21'))['diagnostic'], 'research_intent')
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db:
            saved = db.execute('SELECT intent FROM h1_signals WHERE signal_id=?', (signal['intent']['signal_id'],)).fetchone()
        self.assertEqual(json.loads(saved[0]), signal['intent'])

    def test_features_reproduce_decision_from_frozen_decimal_snapshot(self):
        engine = self.engine()
        with localcontext() as ctx:
            ctx.prec = 50
            closes = [Decimal(100)]
            for i in range(60):
                closes.append(closes[-1] * (Decimal('1.01') if i % 2 == 0 else Decimal('0.99')))
            for i, close in enumerate(closes):
                self.push(engine, self.bar(i, str(close), str(i+1)))
            result = self.push(engine, self.bar(61, str(closes[-1]*Decimal('0.5')), '1000'))
        intent = result['intent']
        self.assertIn('features', intent, 'signal must carry a reproducible snapshot')
        f = intent['features']
        self.assertEqual(len(f['prior_closes']), 61)
        self.assertEqual(len(f['prior_volumes']), 60)
        self.assertEqual(f['prior_volumes'], [str(i+1) for i in range(1, 61)])
        for key in ('mean', 'sample_stdev', 'median_volume', 'current_return', 'reference_close',
                    'current_volume', 'current_close', 'return_threshold', 'volume_threshold',
                    'bar_open_ms', 'bar_close_ms', 'cutoff_ms', 'prior_start_ms', 'prior_end_ms'):
            self.assertIsInstance(f[key], str)
        with localcontext() as ctx:
            ctx.prec = int(f['decimal_precision'])
            closes = list(map(Decimal, f['prior_closes']))
            returns = [b/a-1 for a,b in zip(closes, closes[1:])]
            mean = sum(returns, Decimal(0))/60
            sd = (sum((r-mean)**2 for r in returns)/59).sqrt()
            volumes = sorted(map(Decimal, f['prior_volumes']))
            median = (volumes[29]+volumes[30])/2
            current = Decimal(f['current_close'])/closes[-1]-1
            self.assertEqual(Decimal(f['mean']), mean)
            self.assertEqual(Decimal(f['sample_stdev']), sd)
            self.assertEqual(Decimal(f['median_volume']), median)
            self.assertEqual(Decimal(f['current_return']), current)
            self.assertLess(current, mean-2*sd)
            self.assertGreater(Decimal(f['current_volume']), 2*median)
        frozen = json.dumps(intent, sort_keys=True)
        self.push(engine, self.bar(62, '1', '1000'))
        self.assertEqual(json.dumps(intent, sort_keys=True), frozen)
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db:
            stored = json.loads(db.execute('SELECT intent FROM h1_signals WHERE signal_id=?', (intent['signal_id'],)).fetchone()[0])
        self.assertEqual(stored, intent)

    def test_ambient_decimal_context_cannot_change_signal_features(self):
        first = self.engine(version='ROUND-A')
        second = self.engine(version='ROUND-B')
        closes = [Decimal('100') + Decimal(i)/Decimal('7') for i in range(61)]
        for engine in (first, second):
            for i, price in enumerate(closes):
                self.push(engine, self.bar(i, str(price)))
        candidate = self.bar(61, '30', '21')
        a = self.push(first, candidate)['intent']['features']
        with localcontext() as ctx:
            ctx.prec = 6
            ctx.rounding = 'ROUND_UP'
            b = self.push(second, candidate)['intent']['features']
        self.assertEqual(a, b)

    def test_ingest_integration_api_requires_matching_category(self):
        engine = self.engine()
        self.assertTrue(callable(getattr(engine, 'ingest', None)), 'public integration ingest API required')
        now_ms = BASE+200*STEP
        fields = self.bar(0)
        fields.pop('symbol')
        self.assertEqual(engine.ingest('TEST', 'TradFi', now_ms=now_ms, **fields)['diagnostic'], 'reject_category')
        self.assertEqual(engine.ingest('TEST', 'crypto', now_ms=now_ms, **fields)['diagnostic'], 'warmup')
        for i in range(1, 61):
            fields = self.bar(i)
            fields.pop('symbol')
            engine.ingest('TEST', 'crypto', now_ms=now_ms, **fields)
        fields = self.bar(61, '90', '21')
        fields.pop('symbol')
        result = engine.ingest('TEST', 'crypto', now_ms=now_ms, **fields)
        self.assertEqual(result['diagnostic'], 'research_intent')
        self.assertEqual(result['signal']['side'], 'long')
        self.assertEqual(engine.ingest('TEST', 'crypto', now_ms=True, **fields)['diagnostic'], 'reject_clock')
        self.assertIsNone(engine.ingest('TEST', 'crypto', now_ms=now_ms, **fields)['signal'])

    def test_registration_cannot_be_mutated_by_callers(self):
        symbols = {'TEST': 'crypto'}
        engine = signals.Detector(self.path, 'IMMUTABLE', START, symbols)
        symbols['TEST'] = 'TradFi'
        external = engine.registration
        external['symbols']['TEST'] = 'TradFi'
        self.assertEqual(engine.registration['symbols'], {'TEST': 'crypto'})
        self.assertEqual(self.push(engine, self.bar(0))['diagnostic'], 'warmup')

    def test_self_check_cli_is_labeled_fixture_not_fills(self):
        import subprocess
        import sys
        completed = subprocess.run([sys.executable, '-B', str(Path(signals.__file__)), '--self-check'],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.strip(), 'self-check must emit labeled diagnostics')
        rows = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([r['kind'] for r in rows], ['diagnostic', 'diagnostic', 'self_check'])
        self.assertEqual(rows[0]['fixture'], 'handcrafted-unit-only')
        self.assertEqual(rows[0]['diagnostic'], 'research_intent')
        self.assertEqual(rows[1]['diagnostic'], 'duplicate')
        self.assertEqual(rows[2]['fills'], 0)
        self.assertTrue(rows[2]['passed'])
        self.assertEqual(completed.stderr, '')

    def test_foreign_sqlite_namespace_is_rejected(self):
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE observations (x TEXT)')
        with self.assertRaises(ValueError):
            self.engine()
        with closing(sqlite3.connect(self.path)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {'observations'})

    def test_bar_metadata_is_validated_and_not_coerced_or_persisted(self):
        engine = self.engine()
        candidate = self.bar(0, exchange_payload={'fake': 'not-OHLCV'})
        self.assertEqual(self.push(engine, candidate)['diagnostic'], 'reject_invalid_bar')
        self.assertEqual(self.push(engine, self.bar(0))['diagnostic'], 'warmup')
        self.assertEqual(self.push(engine, {})['diagnostic'], 'reject_invalid_bar')
        self.assertEqual(engine.process(None, now=START)['diagnostic'], 'reject_invalid_bar')

    def test_decimal_overflow_rejects_without_consuming_bar(self):
        engine = self.engine()
        self.warm(engine)
        candidate = self.bar(61, '1E+999999999', '21')
        try:
            result = self.push(engine, candidate)
        except Exception as exc:
            self.fail(f'arithmetic outside fixed Decimal context must reject: {exc!r}')
        self.assertEqual(result['diagnostic'], 'reject_arithmetic')
        self.assertEqual(self.push(engine, self.bar(61, '90', '21'))['diagnostic'], 'research_intent')

if __name__ == '__main__':
    unittest.main()
