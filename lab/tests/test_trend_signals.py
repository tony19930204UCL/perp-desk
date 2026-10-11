"""T1 daily Donchian regression tests."""
import json
import sys
import unittest
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trend_signals import TrendDetector, DAY
from trend_golden import GOLDEN


def bar(i, price="100", symbol="BTCUSDT", base=0):
    start = base + i * DAY
    return dict(symbol=symbol, open_time_ms=start, close_time_ms=start + DAY - 1,
                open=str(price), high=str(price), low=str(price),
                close=str(price), volume="1")


class TrendTests(unittest.TestCase):
    def test_golden_replay(self):
        for symbol, rows in GOLDEN.items():
            with self.subTest(symbol=symbol):
                d = TrendDetector(symbol)
                for i, (ts, close, pos) in enumerate(zip(rows["ts"], rows["close"], rows["pos"])):
                    item = bar(0, close, symbol, ts - DAY + 1)
                    result = d.process(item)
                    if i >= 20:
                        self.assertEqual(result["target_position"], pos, (symbol, i, result))

    def test_golden_transition_counts(self):
        for symbol, rows in GOLDEN.items():
            d = TrendDetector(symbol)
            actual = []
            for ts, close in zip(rows["ts"], rows["close"]):
                actual.append(d.process(bar(0, close, symbol, ts - DAY + 1))["diagnostic"])
            expected = rows["pos"]
            self.assertEqual(actual.count("entry"), sum(a == 0 and b == 1 for a, b in zip(expected, expected[1:])))
            self.assertEqual(actual.count("exit"), sum(a == 1 and b == 0 for a, b in zip(expected, expected[1:])))

    def test_restore_golden_cutpoints(self):
        for symbol, rows in GOLDEN.items():
            for cut in (1, 19, 20, 35, 100):
                with self.subTest(symbol=symbol, cut=cut):
                    d = TrendDetector(symbol)
                    items = [bar(0, close, symbol, ts - DAY + 1) for ts, close in zip(rows["ts"], rows["close"])]
                    for item in items[:cut]:
                        d.process(item)
                    resumed = TrendDetector(symbol)
                    resumed.restore_state(json.loads(json.dumps(d.export_state())))
                    for item in items[cut:cut + 25]:
                        self.assertEqual(d.process(item), resumed.process(item))

    def test_warmup_length(self):
        d = TrendDetector("BTCUSDT")
        self.assertEqual([d.process(bar(i))["diagnostic"] for i in range(20)], ["warmup"] * 20)
        self.assertEqual(d.process(bar(20))["diagnostic"], "no_signal")

    def test_entry(self):
        d = TrendDetector("BTCUSDT")
        for i in range(20): d.process(bar(i))
        self.assertEqual(d.process(bar(20, "101"))["diagnostic"], "entry")

    def test_exit(self):
        d = TrendDetector("BTCUSDT")
        for i in range(20): d.process(bar(i))
        d.process(bar(20, "101"))
        self.assertEqual(d.process(bar(21, "99"))["diagnostic"], "exit")

    def test_equal_entry(self):
        d = TrendDetector("BTCUSDT")
        for i in range(20): d.process(bar(i))
        self.assertEqual(d.process(bar(20))["target_position"], 0)

    def test_equal_exit(self):
        d = TrendDetector("BTCUSDT", 2, 2)
        d.process(bar(0, "100"))
        d.process(bar(1, "100"))
        d.process(bar(2, "110"))
        self.assertEqual(d.process(bar(3, "100"))["target_position"], 1)

    def test_duplicate_idempotent(self):
        d = TrendDetector("BTCUSDT")
        item = bar(0)
        self.assertEqual(d.process(item), d.process(item))
        self.assertEqual(len(d.closes), 1)

    def test_conflicting_duplicate(self):
        d = TrendDetector("BTCUSDT")
        d.process(bar(0))
        self.assertEqual(d.process(bar(0, "101"))["diagnostic"], "reject_duplicate_conflict")

    def test_out_of_order(self):
        d = TrendDetector("BTCUSDT")
        d.process(bar(1))
        self.assertEqual(d.process(bar(0))["diagnostic"], "reject_out_of_order")

    def test_wrong_symbol(self):
        self.assertEqual(TrendDetector("BTCUSDT").process(bar(0, symbol="ETHUSDT"))["diagnostic"], "reject_symbol")

    def test_wrong_length(self):
        item = bar(0)
        item["close_time_ms"] -= 1
        self.assertEqual(TrendDetector("BTCUSDT").process(item)["diagnostic"], "reject_time")

    def test_gap_latches(self):
        d = TrendDetector("BTCUSDT")
        d.process(bar(0))
        self.assertEqual(d.process(bar(2))["diagnostic"], "reject_gap")
        self.assertEqual(d.process(bar(1))["diagnostic"], "reject_gap")

    def test_resync(self):
        d = TrendDetector("BTCUSDT")
        d.process(bar(0))
        d.process(bar(2))
        d.resync()
        self.assertEqual(d.process(bar(2))["diagnostic"], "warmup")

    def test_decimal(self):
        d = TrendDetector("BTCUSDT", 1, 1)
        d.process(bar(0, Decimal("0.1")))
        self.assertEqual(d.process(bar(1, Decimal("0.10000000000000000001")))["diagnostic"], "entry")

    def test_invalid_decimal(self):
        self.assertEqual(TrendDetector("BTCUSDT").process(bar(0, "NaN"))["diagnostic"], "reject_invalid_bar")

    def test_invalid_configuration(self):
        with self.assertRaises(ValueError): TrendDetector("BTCUSDT", 0)

    def test_restore_configuration_mismatch(self):
        state = TrendDetector("BTCUSDT").export_state()
        with self.assertRaises(ValueError): TrendDetector("ETHUSDT").restore_state(state)

    def test_restart_duplicate(self):
        d = TrendDetector("BTCUSDT")
        item = bar(0)
        expected = d.process(item)
        other = TrendDetector("BTCUSDT")
        other.restore_state(json.loads(json.dumps(d.export_state())))
        self.assertEqual(other.process(item), expected)


if __name__ == "__main__":
    unittest.main()
