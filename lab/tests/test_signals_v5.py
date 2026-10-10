"""Deterministic differential and boundary tests for symbol-bound hour detector."""
import hashlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
from signals_v2 import canonical
from signals_v4 import Detector as V4
from signals_v5 import Detector as V5

HOUR = 3600000
START = 1790812800000


def bar(i, symbol='ETHUSDT', price='100', volume='10', offset=0):
    t = START + i * HOUR + offset
    p = Decimal(price)
    return dict(symbol=symbol, open_time_ms=t, close_time_ms=t + HOUR,
                closed=True, open=p, high=p, low=p, close=p,
                volume=Decimal(volume))


def receipt(symbol='ETHUSDT', interval='1h', count=61, offset=0, age=1000):
    cutoff = START + 62 * HOUR
    end = (cutoff - 1) // HOUR * HOUR
    first = end - 61 * HOUR
    rows = []
    for i in range(count):
        t = first + i * HOUR + offset
        rows.append([t, '100', '100', '100', '100', '10', t + HOUR - 1])
    now = cutoff + 1000
    stamp = datetime.fromtimestamp((now - age) / 1000, timezone.utc).isoformat()
    return dict(endpoint='/fapi/v1/klines',
                params=dict(symbol=symbol, interval=interval,
                            startTime=first, endTime=end - 1, limit=61),
                payload=rows, received_at=stamp), cutoff, now


class SymbolHourTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def detector(self, symbol='ETHUSDT', cls=V5):
        path = Path(self.tmp.name) / (symbol + cls.__name__ + str(id(cls)) + '.sqlite3')
        return cls(path, 'H2-TEST',
                   datetime.fromtimestamp(START / 1000, timezone.utc),
                   {symbol: 'crypto'})

    def feed(self, detector, item):
        now = datetime.fromtimestamp((item['close_time_ms'] + 1000) / 1000, timezone.utc)
        return detector.process(item, now=now)

    def warm(self, detector, symbol='ETHUSDT'):
        return [self.feed(detector, bar(i, symbol)) for i in range(61)]

    def test_differential_eth_diagnostics_and_intents(self):
        old = self.detector(cls=V4)
        new = self.detector()
        for i in range(64):
            price = '90' if i == 61 else '100'
            volume = '13' if i == 61 else '10'
            item = bar(i, price=price, volume=volume)
            self.assertEqual(self.feed(old, item), self.feed(new, item))
        self.assertEqual(self.feed(old, item), self.feed(new, item))

    def test_three_symbols_have_distinct_signal_ids(self):
        ids = set()
        for symbol in ('BTCUSDT', 'SOLUSDT', 'XRPUSDT'):
            d = self.detector(symbol)
            self.warm(d, symbol)
            intent = self.feed(d, bar(61, symbol, '90', '13'))['intent']
            self.assertEqual(intent['symbol'], symbol)
            self.assertEqual(intent['side'], 'long')
            ids.add(intent['signal_id'])
        self.assertEqual(len(ids), 3)

    def test_warmup_61_bars(self):
        self.assertTrue(all(r['diagnostic'] == 'warmup'
                            and r['intent'] is None for r in self.warm(self.detector())))

    def test_drop_emits_once_and_target(self):
        d = self.detector()
        self.warm(d)
        item = bar(61, price='90', volume='13')
        result = self.feed(d, item)
        self.assertEqual(result['diagnostic'], 'research_intent')
        self.assertEqual(result['intent']['reversion_target'], '100')
        self.assertEqual(self.feed(d, item)['diagnostic'], 'duplicate')

    def test_volume_equal_threshold_no_signal(self):
        d = self.detector()
        self.warm(d)
        self.assertEqual(self.feed(d, bar(61, price='90', volume='12'))['diagnostic'],
                         'no_signal')

    def test_missing_hour_gap_reset(self):
        d = self.detector()
        self.feed(d, bar(0))
        result = self.feed(d, bar(2))
        self.assertEqual(result['diagnostic'], 'gap_reset')
        self.assertEqual(result['gap']['expected_open_ms'], str(START + HOUR))

    def test_seed_manifest_and_hash(self):
        d = self.detector('BTCUSDT')
        r, cutoff, now = receipt('BTCUSDT')
        manifest = d.seed(r, cutoff_ms=cutoff, now_ms=now)
        self.assertEqual(manifest['bars_count'], 61)
        self.assertEqual(manifest['receipt_sha256'],
                         hashlib.sha256(canonical(r).encode()).hexdigest())
        self.assertEqual(d.manifest(), manifest)

    def test_seed_rejects_wrong_symbol(self):
        d = self.detector('BTCUSDT')
        r, cutoff, now = receipt('SOLUSDT')
        with self.assertRaises(ValueError):
            d.seed(r, cutoff_ms=cutoff, now_ms=now)

    def test_seed_rejects_minute(self):
        d = self.detector()
        r, cutoff, now = receipt(interval='1m')
        with self.assertRaises(ValueError):
            d.seed(r, cutoff_ms=cutoff, now_ms=now)

    def test_seed_rejects_wrong_count(self):
        d = self.detector()
        r, cutoff, now = receipt(count=60)
        with self.assertRaises(ValueError):
            d.seed(r, cutoff_ms=cutoff, now_ms=now)

    def test_seed_rejects_misalignment(self):
        d = self.detector()
        r, cutoff, now = receipt(offset=1)
        with self.assertRaises(ValueError):
            d.seed(r, cutoff_ms=cutoff, now_ms=now)

    def test_seed_rejects_stale(self):
        d = self.detector()
        r, cutoff, now = receipt(age=15001)
        with self.assertRaises(ValueError):
            d.seed(r, cutoff_ms=cutoff, now_ms=now)

    def test_foreign_bar_rejected(self):
        d = self.detector('BTCUSDT')
        self.assertEqual(self.feed(d, bar(0, 'SOLUSDT'))['diagnostic'],
                         'reject_invalid_bar')

    def test_misaligned_bar_rejected(self):
        d = self.detector()
        self.assertEqual(self.feed(d, bar(0, offset=1))['diagnostic'],
                         'reject_invalid_bar')

    def test_registration_rejects_multiple_symbols(self):
        with self.assertRaises(ValueError):
            V5(Path(self.tmp.name) / 'invalid.sqlite3', 'H2-TEST',
               datetime.fromtimestamp(START / 1000, timezone.utc),
               {'BTCUSDT': 'crypto', 'SOLUSDT': 'crypto'})


if __name__ == '__main__':
    unittest.main()
