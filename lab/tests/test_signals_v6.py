"""Offline v6 threshold, differential, and bootstrap regression tests."""
import copy
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
from signals_v5 import Detector as V5
from signals_v6 import Detector as V6

HOUR = 3600000
START = 1790812800000


def bar(i, symbol='ETHUSDT', price='100', volume='10'):
    t = START + i * HOUR
    p = Decimal(price)
    return dict(symbol=symbol, open_time_ms=t, close_time_ms=t + HOUR,
                closed=True, open=p, high=p, low=p, close=p,
                volume=Decimal(volume))


def receipt():
    cutoff = START + 62 * HOUR
    end = (cutoff - 1) // HOUR * HOUR
    first = end - 61 * HOUR
    rows = [[first + i * HOUR, '100', '100', '100', '100', '10',
             first + (i + 1) * HOUR - 1] for i in range(61)]
    now = cutoff + 1000
    stamp = datetime.fromtimestamp((now - 1000) / 1000, timezone.utc).isoformat()
    return dict(endpoint='/fapi/v1/klines',
                params=dict(symbol='ETHUSDT', interval='1h', startTime=first,
                            endTime=end - 1, limit=61),
                payload=rows, received_at=stamp), cutoff, now


class ConfigurableDetectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.serial = 0

    def detector(self, cls=V6, **kwargs):
        self.serial += 1
        return cls(Path(self.tmp.name) / f'{self.serial}.sqlite3',
                   'V6-TEST', datetime.fromtimestamp(START / 1000, timezone.utc),
                   {'ETHUSDT': 'crypto'}, **kwargs)

    def feed(self, detector, item):
        now = datetime.fromtimestamp((item['close_time_ms'] + 1000) / 1000,
                                     timezone.utc)
        return detector.process(item, now=now)

    def warm(self, detector):
        return [self.feed(detector, bar(i)) for i in range(61)]

    def test_default_thresholds(self):
        d = self.detector()
        self.assertEqual(d.downside_sigma, Decimal('1.5'))
        self.assertEqual(d.volume_multiple, Decimal('1.2'))

    def test_decimal_and_string_inputs(self):
        d = self.detector(downside_sigma=Decimal('0.75'),
                          volume_multiple='1.0')
        self.assertEqual((d.downside_sigma, d.volume_multiple),
                         (Decimal('0.75'), Decimal('1.0')))

    def test_differential_defaults_with_seed_and_intent(self):
        old, new = self.detector(V5), self.detector()
        source, cutoff, now = receipt()
        self.assertEqual(old.seed(source, cutoff_ms=cutoff, now_ms=now),
                         new.seed(source, cutoff_ms=cutoff, now_ms=now))
        for i in range(61, 65):
            item = bar(i, price='90' if i == 61 else '100',
                       volume='13' if i == 61 else '10')
            self.assertEqual(self.feed(old, copy.deepcopy(item)),
                             self.feed(new, copy.deepcopy(item)))

    def test_loose_triggers_but_default_does_not(self):
        loose = self.detector(downside_sigma='0.75', volume_multiple='1.0')
        default = self.detector()
        self.warm(loose)
        self.warm(default)
        item = bar(61, price='90', volume='11')
        self.assertEqual(self.feed(loose, copy.deepcopy(item))['diagnostic'], 'research_intent')
        self.assertEqual(self.feed(default, copy.deepcopy(item))['diagnostic'], 'no_signal')

    def test_intent_threshold_features_configured(self):
        d = self.detector(downside_sigma='0.75', volume_multiple='1.0')
        self.warm(d)
        features = self.feed(d, bar(61, price='90', volume='11'))['intent']['features']
        self.assertEqual(Decimal(features['volume_threshold']), Decimal('10'))
        self.assertEqual(Decimal(features['return_threshold']), Decimal('0'))

    def test_invalid_sigma_zero(self):
        with self.assertRaisesRegex(ValueError, 'downside_sigma'):
            self.detector(downside_sigma='0')

    def test_invalid_sigma_negative(self):
        with self.assertRaisesRegex(ValueError, 'downside_sigma'):
            self.detector(downside_sigma='-1')

    def test_invalid_sigma_six(self):
        with self.assertRaisesRegex(ValueError, 'downside_sigma'):
            self.detector(downside_sigma='6')

    def test_invalid_volume_negative(self):
        with self.assertRaisesRegex(ValueError, 'volume_multiple'):
            self.detector(volume_multiple='-1')

    def test_invalid_volume_eleven(self):
        with self.assertRaisesRegex(ValueError, 'volume_multiple'):
            self.detector(volume_multiple='11')

    def test_nonfinite_rejected(self):
        for value in ('NaN', 'Infinity'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.detector(downside_sigma=value)

    def test_boundary_values(self):
        d = self.detector(downside_sigma='5', volume_multiple='0')
        self.assertEqual(d.downside_sigma, Decimal('5'))
        self.assertEqual(d.volume_multiple, Decimal('0'))

    def test_seed_earlier_than_forward_cutoff(self):
        d = self.detector()
        source, cutoff, now = receipt()
        self.assertEqual(d.seed(source, cutoff_ms=cutoff, now_ms=now)['bars_count'], 61)

    def test_foreign_symbol_rejected(self):
        d = self.detector()
        self.assertEqual(self.feed(d, bar(0, symbol='BTCUSDT'))['diagnostic'],
                         'reject_invalid_bar')

    def test_multiple_symbols_rejected(self):
        with self.assertRaises(ValueError):
            V6(Path(self.tmp.name) / 'invalid.sqlite3', 'V6-TEST',
               datetime.fromtimestamp(START / 1000, timezone.utc),
               {'ETHUSDT': 'crypto', 'BTCUSDT': 'crypto'})

    def test_volume_alone_blocks_signal(self):
        d = self.detector(downside_sigma='0.75', volume_multiple='1.0')
        self.warm(d)
        self.assertEqual(self.feed(d, bar(61, price='90', volume='10'))['diagnostic'],
                         'no_signal')


if __name__ == '__main__':
    unittest.main()
