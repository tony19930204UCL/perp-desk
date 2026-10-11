"""Offline contracts for the T1 daily, decision-only PAPER runtime."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
from paper_runtime_v5 import PaperRuntime as F1HRuntime
from paper_runtime_v7 import PaperRuntime, main
from trend_signals import TrendDetector
from trend_golden import GOLDEN

DAY = 86400000


class FakeClient:
    def get(self, endpoint, params=None):
        raise AssertionError("network disabled in T1 unit tests")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def rows(prices, first=0):
    return [[first + i * DAY, str(p), str(p), str(p), str(p), "1",
             first + (i + 1) * DAY - 1] for i, p in enumerate(prices)]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.clock = Clock(100 * DAY)

    def config(self, symbol="BTCUSDT"):
        return LAB / ("paper_config_t1_" + symbol.lower() + ".json")

    def runtime(self, symbol="BTCUSDT", root=None, now=None):
        if now is not None:
            self.clock.now = now
        return PaperRuntime(root or self.base / symbol, self.config(symbol),
                            client=FakeClient(), clock_ms=self.clock,
                            sleep=lambda _: None, monotonic=lambda: 0,
                            fixture=True)

    def offline(self, runtime, history, future):
        runtime.reference = lambda: {}
        runtime.funding = lambda spec: None
        runtime.collect_markets = lambda ref: ([], {})
        runtime.validate_sources = lambda *a, **kw: None
        runtime.fetch = lambda path, params=None: dict(
            payload=history if "endTime" in (params or {}) else
            [r for r in future if r[0] >= params["startTime"]])
        return runtime

    def test_hash_gate_four(self):
        self.assertEqual(len(PaperRuntime.ALLOWED_CONFIG_HASHES), 4)

    def test_hashes_use_bytes(self):
        for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"):
            with self.subTest(s=s):
                self.assertEqual(PaperRuntime.CONFIG_HASHES[self.config(s).name],
                                 hashlib.sha256(self.config(s).read_bytes()).hexdigest())

    def test_reject_modified_config(self):
        target = self.base / "bad.json"
        target.write_bytes(self.config().read_bytes() + b" ")
        with self.assertRaises(ValueError):
            self.runtime_from_config(target)

    def runtime_from_config(self, path):
        return PaperRuntime(self.base / "invalid", path, client=FakeClient(),
                            fixture=True)

    def test_inheritance(self):
        self.assertTrue(issubclass(PaperRuntime, F1HRuntime))

    def test_detector_parameters(self):
        r = self.runtime()
        try:
            self.assertIsInstance(r.detector, TrendDetector)
            self.assertEqual((r.detector.entry_lookback, r.detector.exit_lookback),
                             (20, 10))
        finally:
            r.close()

    def test_daily_interval(self):
        self.assertEqual((PaperRuntime.INTERVAL_NAME, PaperRuntime.INTERVAL_MS),
                         ("1d", DAY))

    def test_bootstrap_flat(self):
        r = self.runtime()
        try:
            self.offline(r, rows([100] * 60, 40 * DAY), [])
            r.bootstrap()
            self.assertTrue(r.state["t1_armed"])
            self.assertEqual(r.state["cursor"], 100 * DAY)
        finally:
            r.close()

    def test_bootstrap_running_trend(self):
        r = self.runtime()
        try:
            self.offline(r, rows([100] * 39 + [101] * 21, 40 * DAY), [])
            r.bootstrap()
            self.assertFalse(r.state["t1_armed"])
            self.assertEqual(r.detector.position, 1)
        finally:
            r.close()

    def test_bootstrap_is_idempotent(self):
        r = self.runtime()
        try:
            self.offline(r, rows([100] * 60, 40 * DAY), [])
            r.bootstrap()
            state = r.detector.export_state()
            r.bootstrap()
            self.assertEqual(r.detector.export_state(), state)
        finally:
            r.close()

    def test_unarmed_entry_then_exit_arms(self):
        r = self.runtime()
        try:
            past = rows([100] * 39 + [101] * 21, 40 * DAY)
            future = rows([110] + [50], 100 * DAY)
            self.offline(r, past, future)
            self.clock.now = 102 * DAY
            r.collect()
            self.assertTrue(r.state["t1_armed"])
            self.assertEqual(r.state["t1_decisions"], [])
        finally:
            r.close()

    def test_route_and_exit_never_submit(self):
        r = self.runtime()
        try:
            self.assertIsNone(r.route_signals({}))
            self.assertIsNone(r.exit_policy(0, "100"))
            self.assertIsNone(r.broker)
        finally:
            r.close()

    def test_snapshot_fields(self):
        r = self.runtime()
        try:
            snap = r.snapshot()
            self.assertEqual(snap["engine"]["candidate_implementation"],
                             "paper-engine-v7-candidate")
            self.assertIsNone(snap["research"]["t1_armed"])
            self.assertFalse(snap["research"]["window_void"])
            self.assertEqual(snap["research"]["pending_decisions"], 0)
            self.assertFalse(snap["paper_trading_enabled"])
        finally:
            r.close()

    def test_gap_void_stops(self):
        r = self.runtime()
        try:
            past = rows([100] * 60, 40 * DAY)
            future = [rows([110], 101 * DAY)[0]]
            self.offline(r, past, future)
            self.clock.now = 103 * DAY
            with self.assertRaisesRegex(ValueError, "gap"):
                r.collect()
            self.assertTrue(r.state["window_void"])
            self.assertIsNotNone(r.state["latest_error"])
        finally:
            r.close()

    def test_reject_void_stops(self):
        r = self.runtime()
        try:
            future = rows([110], 100 * DAY)
            future[0][6] += 1
            self.offline(r, rows([100] * 60, 40 * DAY), future)
            self.clock.now = 102 * DAY
            with self.assertRaisesRegex(ValueError, "invalid daily"):
                r.collect()
            self.assertTrue(r.state["window_void"])
        finally:
            r.close()

    def _golden(self, symbol, restart=False, limit=None):
        data = GOLDEN[symbol]
        count = min(len(data["ts"]), limit) if limit else len(data["ts"])
        history = rows(data["close"][:60], data["ts"][0] + 1 - DAY)
        future = rows(data["close"][60:count], data["ts"][60] + 1 - DAY)
        root = self.base / symbol
        r = self.runtime(symbol, root, now=data["ts"][60] + 1)
        try:
            self.offline(r, history, future)
            r.bootstrap()
            initial_armed = r.state["t1_armed"]
            for bar in future:
                self.clock.now = bar[6] + 1
                if restart:
                    r.close()
                    r = self.runtime(symbol, root)
                    self.offline(r, history, future)
                r.collect()
            observed = [(d["close_time_ms"], d["kind"])
                        for d in r.state["t1_decisions"]]
            transitions = [(data["ts"][i], "entry" if data["pos"][i] else "exit")
                           for i in range(60, count)
                           if data["pos"][i] != data["pos"][i - 1]]
            expected = transitions if initial_armed else [
                item for j, item in enumerate(transitions) if j > 0]
            self.assertEqual(observed, expected)
            self.assertEqual(len(observed), len(set(observed)))
            return observed
        finally:
            r.close()

    def test_btc_golden(self):
        self._golden("BTCUSDT")

    def test_xrp_golden(self):
        self._golden("XRPUSDT")

    def test_restart_each_bar_100(self):
        baseline = self._golden("BTCUSDT", limit=160)
        restarted = self._golden("XRPUSDT", restart=True, limit=160)
        self.assertIsInstance(baseline, list)
        self.assertIsInstance(restarted, list)

    def test_main_once(self):
        r = self.runtime()
        r.poll = r.snapshot
        status = self.base / "status.json"
        with patch("paper_runtime_v7.PaperRuntime", return_value=r):
            self.assertEqual(main(["--once", "--state-dir", str(self.base / "cli"),
                                   "--status", str(status)]), 0)
        self.assertTrue(status.exists())
        self.assertIsNone(json.loads(status.read_text())["latest_error"])


if __name__ == "__main__":
    unittest.main()
