"""Offline deterministic contract tests for the one-hour detector."""
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
from signals_v3 import Detector as MinuteDetector
from signals_v4 import Detector
from signals_v2 import canonical

HOUR = 3600000
START = 1790812800000  # 2026-10-01 00:00 UTC


def bar(i, price="100", volume="10", *, offset=0):
    t = START + i * HOUR + offset
    p = Decimal(price)
    return dict(symbol="ETHUSDT", open_time_ms=t, close_time_ms=t + HOUR,
                closed=True, open=p, high=p, low=p, close=p,
                volume=Decimal(volume))


def receipt(*, interval="1h", count=61, offset=0, age=1000):
    cutoff = START + 62 * HOUR
    end = (cutoff - 1) // HOUR * HOUR
    first = end - 61 * HOUR
    rows = []
    for i in range(count):
        t = first + i * HOUR + offset
        rows.append([t, "100", "100", "100", "100", "10",
                     t + HOUR - 1])
    now = cutoff + 1000
    stamp = datetime.fromtimestamp((now - age) / 1000, timezone.utc).isoformat()
    return dict(endpoint="/fapi/v1/klines",
                params=dict(symbol="ETHUSDT", interval=interval,
                            startTime=first, endTime=end - 1, limit=61),
                payload=rows, received_at=stamp), cutoff, now


class HourDetectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.detector = Detector(Path(self.tmp.name) / "bars.sqlite3",
                                 "H2-TEST", datetime.fromtimestamp(START / 1000, timezone.utc),
                                 {"ETHUSDT": "crypto"})

    def feed(self, item):
        now = datetime.fromtimestamp((item["close_time_ms"] + 1000) / 1000, timezone.utc)
        return self.detector.process(item, now=now)

    def warm(self):
        return [self.feed(bar(i)) for i in range(61)]

    def test_class_contract_independent(self):
        self.assertEqual((Detector.interval_ms, Detector.interval_name), (HOUR, "1h"))
        self.assertEqual((MinuteDetector.interval_ms, MinuteDetector.interval_name), (60000, "1m"))
        self.assertEqual(Detector.downside_sigma, MinuteDetector.downside_sigma)
        self.assertEqual(Detector.volume_multiple, MinuteDetector.volume_multiple)

    def test_warmup_has_no_intents(self):
        results = self.warm()
        self.assertTrue(all(x["diagnostic"] == "warmup" and x["intent"] is None for x in results))

    def test_drop_with_volume_emits_once(self):
        self.warm()
        result = self.feed(bar(61, "90", "13"))
        self.assertEqual(result["diagnostic"], "research_intent")
        self.assertEqual(result["intent"]["reversion_target"], "100")
        self.assertEqual(result["intent"]["side"], "long")
        self.assertEqual(self.feed(bar(61, "90", "13"))["diagnostic"], "duplicate")

    def test_volume_equal_threshold_does_not_trigger(self):
        self.warm()
        self.assertEqual(self.feed(bar(61, "90", "12"))["diagnostic"], "no_signal")

    def test_volume_below_threshold_does_not_trigger(self):
        self.warm()
        self.assertEqual(self.feed(bar(61, "90", "11"))["diagnostic"], "no_signal")

    def test_return_equal_threshold_does_not_trigger(self):
        self.warm()
        self.assertEqual(self.feed(bar(61, "100", "20"))["diagnostic"], "no_signal")

    def test_missing_hour_gap_resets(self):
        self.feed(bar(0))
        result = self.feed(bar(2))
        self.assertEqual(result["diagnostic"], "gap_reset")
        self.assertEqual(result["gap"]["expected_open_ms"], str(START + HOUR))

    def test_misaligned_forward_bar_rejected(self):
        self.assertEqual(self.feed(bar(0, offset=1))["diagnostic"], "reject_invalid_bar")

    def test_seed_accepts_hour_receipt_and_hash(self):
        r, cutoff, now = receipt()
        manifest = self.detector.seed(r, cutoff_ms=cutoff, now_ms=now)
        self.assertEqual(manifest["bars_count"], 61)
        self.assertEqual(manifest["receipt_sha256"], hashlib.sha256(canonical(r).encode()).hexdigest())
        self.assertEqual(manifest["context_start_ms"], START)
        self.assertEqual(self.detector.manifest(), manifest)

    def test_seed_refuses_minute_receipt(self):
        r, cutoff, now = receipt(interval="1m")
        with self.assertRaises(ValueError):
            self.detector.seed(r, cutoff_ms=cutoff, now_ms=now)

    def test_seed_refuses_wrong_count(self):
        r, cutoff, now = receipt(count=60)
        with self.assertRaises(ValueError):
            self.detector.seed(r, cutoff_ms=cutoff, now_ms=now)

    def test_seed_refuses_misaligned_open(self):
        r, cutoff, now = receipt(offset=1)
        with self.assertRaises(ValueError):
            self.detector.seed(r, cutoff_ms=cutoff, now_ms=now)

    def test_seed_refuses_stale_receipt(self):
        r, cutoff, now = receipt(age=15001)
        with self.assertRaises(ValueError):
            self.detector.seed(r, cutoff_ms=cutoff, now_ms=now)

    def test_seed_refuses_future_receipt(self):
        r, cutoff, now = receipt(age=-1)
        with self.assertRaises(ValueError):
            self.detector.seed(r, cutoff_ms=cutoff, now_ms=now)


if __name__ == "__main__":
    unittest.main()
