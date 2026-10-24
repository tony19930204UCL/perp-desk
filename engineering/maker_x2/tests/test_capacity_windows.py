"""r13 correction-2: frozen feature exclusion and causal recovery controls."""
import csv
import os
import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from bounded_history import prepare_bars, prepare_decisions, iter_eligible
from data_adapter import read_bars
from signal_features import eligible_decisions
from matched_signals import select_matched
from entry_lifecycle import vwap60
from maker_exit_lab import X2Error


def fictional_minutes(count=4650):
    """Variable valid OHLC; sixty fully closed zero-base minutes."""
    bars = []
    for i in range(count):
        close = D("100") + D((i * 13) % 19) / D("100")
        zero = 4410 <= i < 4470
        bars.append({
            "ts": i * 60000, "open": str(close),
            "high": str(close + D(".30")),
            "low": str(close - D(".30")), "close": str(close),
            "base_volume": "0" if zero else "2",
            "quote_volume": "0" if zero else "200",
            "closed": True})
    return bars


def write_minutes(path, bars):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=(
            "open_time", "open", "high", "low", "close",
            "volume", "quote_volume"))
        writer.writeheader()
        for bar in bars:
            writer.writerow({
                "open_time": bar["ts"], "open": bar["open"],
                "high": bar["high"], "low": bar["low"],
                "close": bar["close"], "volume": bar["base_volume"],
                "quote_volume": bar["quote_volume"]})


class ZeroVolumeWindowControl(unittest.TestCase):
    def test_reference_guard_and_both_framework_recovery(self):
        bars = fictional_minutes()
        self.assertEqual(len(bars), 4650)
        with self.assertRaisesRegex(X2Error, "VWAP60 invalid volumes"):
            vwap60(bars[4410:4470])
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "fictional.csv"
            write_minutes(source, bars)
            validated = read_bars(source)["bars"]
            prepared = prepare_bars(source, Path(folder) / "bars.db")
            self.assertEqual(len(prepared["bars"]), len(bars))
            self.assertEqual(prepared["gaps"], [])
            for tf in (1, 15):
                with self.subTest(framework=tf):
                    reference = eligible_decisions(validated, tf)
                    streamed = list(iter_eligible(prepared["bars"], tf))
                    self.assertEqual(streamed, reference)
                    disk = prepare_decisions(
                        prepared["bars"], tf, Path(folder) / f"tf{tf}.db")
                    self.assertEqual(list(disk), reference)
                    reasons = disk.exclusion_counts()
                    self.assertGreater(
                        reasons.get("VWAP60_INVALID_VOLUMES", 0), 0)
                    times = [row["signal_time"] for row in reference]
                    self.assertEqual(times, sorted(set(times)))
                    self.assertTrue(any(t < 4410 * 60000 for t in times))
                    self.assertTrue(any(t > 4530 * 60000 for t in times))
                    self.assertNotIn(4470 * 60000, times)
                    self.assertEqual(
                        select_matched(reference, tf, 0, 4650*60000, count=2),
                        select_matched(disk, tf, 0, 4650*60000, count=2))
                    self.assertEqual(
                        [r for r in reference if r["triggered"]],
                        [r for r in streamed if r["triggered"]])

    def test_causal_prefix_and_valid_recovery(self):
        bars = fictional_minutes()
        # No future minute can alter an already finalized decision.
        with tempfile.TemporaryDirectory() as folder:
            for tf in (1, 15):
                with self.subTest(framework=tf):
                    end = 4530 if tf == 1 else 4545
                    prefix = bars[:end]
                    path = Path(folder) / f"prefix{tf}.csv"
                    write_minutes(path, prefix)
                    prepared = prepare_bars(
                        path, Path(folder) / f"prefix{tf}.db")
                    earlier = eligible_decisions(prefix, tf)
                    entire = eligible_decisions(bars, tf)
                    self.assertEqual(
                        list(iter_eligible(prepared["bars"], tf)), earlier)
                    self.assertEqual(
                        earlier,
                        [r for r in entire if r["signal_time"] <= end*60000])
                    self.assertTrue(any(
                        r["signal_time"] >= 4530*60000
                        for r in entire))
