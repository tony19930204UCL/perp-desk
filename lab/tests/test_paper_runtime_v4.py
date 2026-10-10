"""One-hour migration checks.

Bar-size dependent locations: runtime INTERVAL_MS/INTERVAL_NAME (bootstrap,
forward cursor, closed-row validation, gap reset), detector interval and
contiguity, 61-bar bootstrap range, 12-hour max hold, research deadline.
The 15-second source gate and 2x cost gate intentionally remain unchanged.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
from paper_runtime_v4 import PaperRuntime
from paper_runtime_v3 import PaperRuntime as MinuteRuntime
from paper_sizing import size_long

CONFIG = LAB / "paper_config_v4.json"
HOUR = 3600000
START = 1790812800000


class Clock:
    def __init__(self, now=START + 62 * HOUR + 1000):
        self.now = now

    def __call__(self):
        return self.now


class FakeClient:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def get(self, endpoint, params=None):
        self.calls.append((endpoint, dict(params or {})))
        if endpoint != "/fapi/v1/klines":
            raise AssertionError("unexpected public request")
        p = params
        first = p["startTime"]
        count = p["limit"] if "endTime" in p else 0
        rows = [[t, "100", "100", "100", "100", "10", t + HOUR - 1]
                for t in (first + i * HOUR for i in range(count))]
        return dict(endpoint=endpoint, params=p, payload=rows,
                    received_at=datetime.fromtimestamp(self.clock() / 1000, timezone.utc).isoformat())


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.client = FakeClient(self.clock)
        self.root = Path(self.tmp.name) / "isolated"
        self.runtime = PaperRuntime(self.root, CONFIG, client=self.client,
                                    clock_ms=self.clock, fixture=True)
        self.addCleanup(self.runtime.close)

    def test_hour_constants(self):
        self.assertEqual((self.runtime.INTERVAL_MS, self.runtime.INTERVAL_NAME), (HOUR, "1h"))

    def test_minute_runtime_unchanged(self):
        self.assertEqual((MinuteRuntime.INTERVAL_MS, MinuteRuntime.INTERVAL_NAME), (60000, "1m"))
        self.assertEqual(172800000, 48 * HOUR)

    def test_config_hash_matches_bytes(self):
        digest = hashlib.sha256(CONFIG.read_bytes()).hexdigest()
        self.assertEqual(PaperRuntime.CONFIG_HASH, digest)
        self.assertEqual(self.runtime.config_hash, digest)

    def test_report_hash(self):
        self.assertEqual(self.runtime.snapshot()["risk"]["risk_config_sha256"], PaperRuntime.CONFIG_HASH)

    def test_modified_config_refused(self):
        changed = self.root.parent / "changed.json"
        obj = json.loads(CONFIG.read_text())
        obj["strategy"]["volume_multiple"] = "1.1"
        changed.write_text(json.dumps(obj))
        with self.assertRaises(ValueError):
            PaperRuntime(self.root.parent / "other", changed, client=self.client,
                         clock_ms=self.clock, fixture=True)

    def test_bootstrap_hour_aligned(self):
        self.runtime.bootstrap()
        endpoint, p = self.client.calls[-1]
        self.assertEqual(endpoint, "/fapi/v1/klines")
        self.assertEqual((p["interval"], p["limit"]), ("1h", 61))
        self.assertEqual(p["startTime"] % HOUR, 0)
        self.assertEqual((p["endTime"] + 1) % HOUR, 0)
        self.assertEqual(p["endTime"] + 1 - p["startTime"], 61 * HOUR)

    def test_bootstrap_warmup(self):
        self.runtime.bootstrap()
        self.assertEqual(self.runtime.state["warmup"], 61)

    def test_research_deadline(self):
        self.assertEqual(self.runtime.state["research_deadline_ms"],
                         self.runtime.state["strategy_start_ms"] + 1814400000)

    def test_research_restart(self):
        deadline = self.runtime.state["research_deadline_ms"]
        self.runtime.close()
        restarted = PaperRuntime(self.root, CONFIG, client=self.client,
                                 clock_ms=self.clock, fixture=True)
        try:
            self.assertEqual(restarted.state["research_deadline_ms"], deadline)
        finally:
            restarted.close()

    def test_research_target(self):
        report = self.runtime.snapshot()["research"]
        self.assertEqual(report["target_complete_round_trips"], 30)
        self.assertTrue(report["target_not_guarantee"])

    def test_research_gate_after_deadline(self):
        self.clock.now = self.runtime.state["research_deadline_ms"]
        self.assertIn("research_window_closed", self.runtime.risk_blockers())

    def test_stop_and_max_hold_config(self):
        self.assertEqual(self.runtime.config["strategy"]["stop_distance_fraction"], "0.01")
        self.assertEqual(self.runtime.config["strategy"]["max_holding_ms"], 43200000)

    def test_cost_gate_unchanged(self):
        self.assertEqual(self.runtime.config["strategy"]["minimum_gross_reward_to_estimated_cost"], "2")
        self.assertEqual(self.runtime.config["execution_model"]["max_source_age_ms"], 15000)

    def test_risk_unchanged(self):
        c = self.runtime.config
        self.assertEqual((c["max_loss_per_trade_usdt"], c["max_daily_loss_usdt"],
                          c["total_loss_limit_usdt"], c["max_effective_exposure_x"]),
                         ("1", "3", "10", "3"))

    def test_sizing_cost_rejection(self):
        spec = dict(tick_size="0.01", qty_step="0.001", min_qty="0.001",
                    max_qty="100", min_notional="5", taker_fee="0.0005")
        result = size_long(self.runtime.config, spec, equity="100",
                           bid="99.99", ask="100", target="100.10")
        self.assertEqual(result["reason"], "insufficient_reward_after_costs")

    def test_sizing_risk_bound(self):
        spec = dict(tick_size="0.01", qty_step="0.001", min_qty="0.001",
                    max_qty="100", min_notional="5", taker_fee="0.0005")
        result = size_long(self.runtime.config, spec, equity="100",
                           bid="99.99", ask="100", target="103")
        self.assertEqual(result["status"], "accepted")
        self.assertLessEqual(D(result["qty"]) * D(result["planned_loss_per_unit"]), D("1"))
        self.assertLess(D(result["stop_price"]), D("99.1"))


if __name__ == "__main__":
    unittest.main()
