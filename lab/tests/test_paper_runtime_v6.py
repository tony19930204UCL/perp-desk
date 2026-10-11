"""Offline W1 configuration and runtime regression contracts."""
import hashlib
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))

from paper_runtime_v6 import PaperRuntime
from paper_runtime_v5 import PaperRuntime as F1HRuntime
from paper_sizing import size_long
from signals_v6 import Detector


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        raise AssertionError("No network access permitted")


class W1RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = json.loads((LAB / "paper_config_f1h_btcusdt.json").read_text())
        self.symbols = ("ethusdt", "btcusdt", "solusdt", "xrpusdt")

    def config(self, symbol):
        return LAB / ("paper_config_w1_" + symbol + ".json")

    def runtime(self, symbol="btcusdt", root=None):
        return PaperRuntime(root or Path(self.tmp.name) / symbol,
                            self.config(symbol), client=FakeClient(),
                            clock_ms=lambda: 1700000000000,
                            sleep=lambda _: None, monotonic=lambda: 0,
                            fixture=True)

    def test_four_hashes_only(self):
        self.assertEqual(len(PaperRuntime.ALLOWED_CONFIG_HASHES), 4)

    def test_hashes_are_bytes(self):
        for symbol in self.symbols:
            with self.subTest(symbol=symbol):
                digest = hashlib.sha256(self.config(symbol).read_bytes()).hexdigest()
                self.assertIn(digest, PaperRuntime.ALLOWED_CONFIG_HASHES)

    def test_inherits_f1h(self):
        self.assertTrue(issubclass(PaperRuntime, F1HRuntime))

    def test_v6_detector(self):
        r = self.runtime()
        self.addCleanup(r.close)
        self.assertIsInstance(r.detector, Detector)

    def test_thresholds_reach_detector(self):
        r = self.runtime()
        self.addCleanup(r.close)
        self.assertEqual(r.detector.downside_sigma, Decimal("0.75"))
        self.assertEqual(r.detector.volume_multiple, Decimal("1.0"))

    def test_symbol_isolation(self):
        for symbol in self.symbols:
            with self.subTest(symbol=symbol):
                r = self.runtime(symbol)
                self.assertEqual(r.symbol, symbol.upper())
                r.close()

    def test_modified_config_rejected(self):
        data = self.config("btcusdt").read_bytes().replace(b'"0.75"', b'"0.76"')
        path = Path(self.tmp.name) / "modified.json"
        path.write_bytes(data)
        with self.assertRaises(ValueError):
            PaperRuntime(Path(self.tmp.name) / "modified", path, fixture=True)

    def test_f1h_config_rejected(self):
        with self.assertRaises(ValueError):
            PaperRuntime(Path(self.tmp.name) / "f1h",
                         LAB / "paper_config_f1h_btcusdt.json", fixture=True)

    def test_v4_config_rejected(self):
        with self.assertRaises(ValueError):
            PaperRuntime(Path(self.tmp.name) / "v4",
                         LAB / "paper_config_v4.json", fixture=True)

    def test_allowed_config_differences(self):
        allowed = {"version_id", "risk_version", "max_loss_per_trade_usdt",
                   "max_daily_loss_usdt", "total_loss_limit_usdt",
                   "max_effective_exposure_x", "paper_leverage"}
        strategy_allowed = {"symbol", "downside_sigma", "volume_multiple",
                            "stop_distance_fraction"}
        for symbol in self.symbols:
            with self.subTest(symbol=symbol):
                data = json.loads(self.config(symbol).read_text())
                self.assertEqual(set(data), set(self.base))
                self.assertEqual({k for k in data if k != "strategy"
                                  and data[k] != self.base[k]}, allowed)
                self.assertEqual({k for k in data["strategy"]
                                  if data["strategy"][k] != self.base["strategy"][k]},
                                 strategy_allowed if symbol != "btcusdt"
                                 else strategy_allowed - {"symbol", "stop_distance_fraction"})

    def test_risk_values(self):
        for symbol in self.symbols:
            data = json.loads(self.config(symbol).read_text())
            self.assertEqual(data["max_loss_per_trade_usdt"], "2")
            self.assertEqual(data["max_daily_loss_usdt"], "6")
            self.assertEqual(data["total_loss_limit_usdt"], "30")

    def test_exposure_and_leverage(self):
        for symbol in self.symbols:
            data = json.loads(self.config(symbol).read_text())
            self.assertEqual(data["max_effective_exposure_x"], "5")
            self.assertEqual(data["paper_leverage"], "5")

    def test_stops(self):
        for symbol, expected in zip(self.symbols, ("0.0100", "0.0070", "0.0115", "0.0140")):
            data = json.loads(self.config(symbol).read_text())
            self.assertEqual(data["strategy"]["stop_distance_fraction"], expected)

    def test_holding_and_window(self):
        for symbol in self.symbols:
            data = json.loads(self.config(symbol).read_text())
            self.assertEqual(data["strategy"]["max_holding_ms"], 43200000)
            self.assertEqual(data["research"]["window_ms"], 1814400000)

    def test_deadline_restart(self):
        root = Path(self.tmp.name) / "restart"
        r = self.runtime(root=root)
        deadline = r.state["research_deadline_ms"]
        self.assertEqual(deadline, 1700000000000 + 1814400000)
        r.close()
        r2 = self.runtime(root=root)
        self.assertEqual(r2.state["research_deadline_ms"], deadline)
        r2.close()

    def test_cost_gate_and_samples(self):
        for symbol in self.symbols:
            data = json.loads(self.config(symbol).read_text())
            self.assertEqual(data["strategy"]["minimum_gross_reward_to_estimated_cost"], "2")
            self.assertEqual(data["strategy"]["prior_return_samples"], 60)
            self.assertEqual(data["indicator_bootstrap"]["bars"], 61)

    def test_frozen_sizing_reads_w1_limits(self):
        data = json.loads(self.config("btcusdt").read_text())
        spec = dict(tick_size="0.01", qty_step="0.001", market_qty_step="0.001",
                    min_qty="0.001", max_qty="100", min_notional="5", taker_fee="0.0005")
        result = size_long(data, spec, equity="100", bid="99.99", ask="100",
                           target="110")
        self.assertEqual(result["status"], "accepted")
        qty = Decimal(result["qty"])
        loss = qty * Decimal(result["planned_loss_per_unit"])
        self.assertLessEqual(loss, Decimal("2"))
        self.assertGreater(loss, Decimal("1"))


if __name__ == "__main__":
    unittest.main()
