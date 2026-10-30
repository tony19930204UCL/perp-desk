"""R4 bounded speed regression: causal lookback and finite memory."""
import unittest
from decimal import Decimal
from unittest.mock import patch

from path_speed import SpeedFilter, WIN_BARS, MINUTE, MIN_HIST


class SpeedMemory(unittest.TestCase):
    def bars(self, n, jump=-1):
        for i in range(n):
            ts = i * MINUTE + (MINUTE if i >= jump >= 0 else 0)
            yield {"ts": ts, "close": str(100 + (i % 13) / 100)}

    def test_stream_input(self):
        class Once:
            def __init__(self, rows):
                self.rows = rows
                self.used = False

            def __iter__(self):
                if self.used:
                    raise AssertionError("bars traversed twice")
                self.used = True
                yield from self.rows
        src = Once(self.bars(40))
        filt = SpeedFilter(src)
        self.assertTrue(src.used)
        self.assertFalse(hasattr(filt, "bars"))
        self.assertEqual(len(filt.ends), 40)
        self.assertEqual(len(filt.speeds), 40)

    def test_exact_speed(self):
        from path_speed import speed5
        rows = list(self.bars(24))
        filt = SpeedFilter(iter(rows))
        for i in range(WIN_BARS, len(rows)):
            self.assertEqual(filt.speeds[i], speed5(rows, i))
        self.assertEqual(filt.speeds[:WIN_BARS], [None] * WIN_BARS)

    def test_causal_warmup(self):
        filt = SpeedFilter(self.bars(MIN_HIST + WIN_BARS + 2))
        t = (MIN_HIST + WIN_BARS) * MINUTE
        self.assertEqual(filt.skip(t)[1], "H2_WARMUP")
        self.assertIn(filt.skip(t + MINUTE)[1], ("H2_SKIP", "H2_PASS"))
        self.assertEqual(filt.skip(t)[1], "H2_WARMUP")

    def test_gap_invalidates(self):
        filt = SpeedFilter(self.bars(MIN_HIST + WIN_BARS + 12, jump=12))
        t = filt.ends[-1]
        self.assertEqual(filt.skip(t)[1], "H2_WARMUP")

    def test_bad_price_rejected(self):
        for value in ("0", "-1", "NaN"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    SpeedFilter([{"ts": 0, "close": value}])

    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            SpeedFilter([{"ts": 0, "close": "1"},
                         {"ts": 0, "close": "1"}])


if __name__ == "__main__":
    unittest.main()
