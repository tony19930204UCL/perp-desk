"""Offline contracts for the T1 daily, decision-only PAPER runtime."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from trend_sizing import size_trend_long
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
        runtime.reference = lambda: {
            "payload": {
                "symbols": [{
                    "symbol": runtime.symbol,
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001",
                         "minQty": "0.001", "maxQty": "100000"},
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001",
                         "minQty": "0.001", "maxQty": "100000"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                }],
            },
        }
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

    def test_snapshot_bootstrap_ready(self):
        r = self.runtime()
        try:
            self.offline(r, rows([100] * 60, 40 * DAY), [])
            r.bootstrap()
            snap = r.snapshot()
            self.assertNotIn("fresh_forward_warmup", snap["blockers"])
            self.assertEqual(snap["engine"]["status"], "running")
            self.assertEqual(snap["engine"]["warmup_required"], 60)
            self.assertEqual(snap["versions"][0]["status"], "observing")
            self.assertEqual(snap["runtime_state"], "observing")
            self.assertTrue(snap["paper_trading_enabled"])
        finally:
            r.close()

    def test_snapshot_before_bootstrap_is_warmup(self):
        r = self.runtime()
        try:
            self.offline(r, rows([100] * 60, 40 * DAY), [])
            snap = r.snapshot()
            self.assertIn("fresh_forward_warmup", snap["blockers"])
            self.assertEqual(snap["engine"]["status"], "warmup")
            self.assertFalse(snap["paper_trading_enabled"])
        finally:
            r.close()

    def test_snapshot_window_void_halts(self):
        r, spec = self.prepared()
        try:
            r.state["window_void"] = True
            snap = r.snapshot()
            self.assertEqual(snap["engine"]["status"], "halted")
            self.assertIn("window_void", snap["blockers"])
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
        r = self.runtime(symbol, root, now=data["ts"][59] + 1)
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


    def prepared(self, symbol="BTCUSDT", price="100"):
        """Use the existing exchange-info helper, with a real offline SimBroker."""
        r = self.runtime(symbol)
        self.offline(r, rows([Decimal(price)] * 60, 40 * DAY), [])
        from paper_market import instrument
        spec = instrument(r.reference(), symbol, "crypto",
                          r.config["fee_assumptions"]["crypto"])
        r.ensure_broker(spec)
        r.state["t1_armed"] = True
        r.state["warmup"] = 60
        # The book source remains the existing validated-market contract.
        r.state["markets"] = [dict(symbol=symbol, bid=price,
                                    ask=str(Decimal(price) + Decimal("0.01")))]
        r.broker.marks[symbol] = price
        r.broker.funding_status[symbol] = dict(complete=True,
                                              valid_until_ts=self.clock.now + 10 * DAY)
        r.broker._save()
        return r, spec

    def decision(self, r, kind="entry", age=5000):
        stamp = self.clock.now - age
        item = dict(id="t1:%s:%s:%s" % (r.symbol, stamp, kind),
                    kind=kind, close_time_ms=stamp, close="100",
                    status="new")
        r.state["t1_decisions"].append(item)
        r.save()
        return item

    def test_entry_submits_taker_with_stop(self):
        r, spec = self.prepared()
        try:
            d = self.decision(r)
            expected = size_trend_long(r.config, spec,
                                       equity=str(r.broker.equity),
                                       bid="100", ask="100.01")
            self.assertEqual(expected["status"], "accepted")
            r.route_signals(spec)
            order = r.broker.orders["order:" + d["id"]]
            self.assertEqual(d["status"], "submitted")
            self.assertEqual(order["intent"]["side"], "BUY")
            self.assertEqual(order["intent"]["kind"], "TAKER")
            self.assertTrue(order["intent"]["risk_limited"])
            self.assertEqual(Decimal(order["intent"]["stop"]),
                             Decimal(expected["stop_price"]))
            self.assertEqual(order["intent"]["expires_ts"],
                             self.clock.now + r.ENTRY_WINDOW_MS)
        finally:
            r.close()

    def test_entry_is_idempotent_in_same_process(self):
        r, spec = self.prepared()
        try:
            self.decision(r)
            r.route_signals(spec)
            r.route_signals(spec)
            self.assertEqual(len(r.broker.orders), 1)
        finally:
            r.close()

    def test_restart_after_submit_before_fill(self):
        r, spec = self.prepared()
        try:
            d = self.decision(r)
            r.route_signals(spec)
            ident = d["id"]
        finally:
            r.close()
        reopened = self.runtime()
        try:
            self.offline(reopened, rows([100] * 60, 40 * DAY), [])
            reopened.state["markets"] = [dict(symbol="BTCUSDT", bid="100", ask="100.01")]
            reopened.state["t1_decisions"][0]["status"] = "new"
            reopened.route_signals(spec)
            self.assertEqual(list(reopened.broker.orders), ["order:" + ident])
            self.assertEqual(reopened.state["t1_decisions"][0]["status"], "submitted")
        finally:
            reopened.close()

    def test_late_entry_does_not_catch_up(self):
        r, spec = self.prepared()
        try:
            d = self.decision(r, age=r.ENTRY_WINDOW_MS + 1)
            r.route_signals(spec)
            self.assertEqual((d["status"], d["reason"]),
                             ("rejected", "late_closed_bar_signal"))
            self.clock.now += 300000
            r.route_signals(spec)
            self.assertFalse(r.broker.orders)
        finally:
            r.close()

    def test_unarmed_entry_rejected(self):
        r, spec = self.prepared()
        try:
            d = self.decision(r)
            r.state["t1_armed"] = False
            r.route_signals(spec)
            self.assertEqual(d["reason"], "t1_unarmed")
            self.assertFalse(r.broker.orders)
        finally:
            r.close()

    def test_window_void_blocks_entry(self):
        r, spec = self.prepared()
        try:
            d = self.decision(r)
            r.state["window_void"] = True
            r.route_signals(spec)
            self.assertEqual(d["reason"], "window_void")
            self.assertFalse(r.broker.orders)
        finally:
            r.close()

    def test_risk_blocker_rejects_entry(self):
        r, spec = self.prepared()
        try:
            d = self.decision(r)
            r.risk_blockers = lambda: ["risk_daily_loss"]
            r.route_signals(spec)
            self.assertEqual(d["reason"], "risk_daily_loss")
            self.assertFalse(r.broker.orders)
        finally:
            r.close()

    def test_sizing_rejection_recorded(self):
        r, spec = self.prepared()
        try:
            d = self.decision(r)
            r.state["markets"][0]["ask"] = "100"
            r.route_signals(spec)
            self.assertEqual(d["reason"], "crossed_or_locked_book")
            self.assertEqual(d["status"], "rejected")
        finally:
            r.close()

    def test_pending_entry_blocks_another_entry(self):
        r, spec = self.prepared()
        try:
            self.decision(r)
            r.route_signals(spec)
            second = self.decision(r, age=4000)
            r.route_signals(spec)
            self.assertEqual(second["reason"], "single_position_or_pending")
            self.assertEqual(len(r.broker.orders), 1)
        finally:
            r.close()

    def test_exit_without_position_is_void(self):
        r, spec = self.prepared()
        try:
            d = self.decision(r, "exit")
            r.exit_policy(self.clock.now, "100")
            self.assertEqual(d["status"], "void_no_position")
            self.assertFalse(r.broker.orders)
        finally:
            r.close()

    def test_exit_reduce_only_and_not_duplicated(self):
        r, spec = self.prepared()
        try:
            r.broker.positions[r.symbol] = dict(qty="0.1", entry="100",
                                                stop="85", opened_ts=self.clock.now)
            r.broker._save()
            d = self.decision(r, "exit")
            r.exit_policy(self.clock.now, "100")
            r.exit_policy(self.clock.now + 1, "100")
            order = r.broker.orders["order:" + d["id"]]
            self.assertEqual(d["status"], "submitted")
            self.assertEqual(order["intent"]["side"], "SELL")
            self.assertTrue(order["intent"]["reduce_only"])
            self.assertEqual(Decimal(order["intent"]["qty"]), Decimal("0.1"))
            self.assertEqual(len(r.broker.orders), 1)
        finally:
            r.close()

    def test_exit_retries_after_rejected_order(self):
        r, spec = self.prepared()
        try:
            r.broker.positions[r.symbol] = dict(qty="0.1", entry="100",
                                                stop="85", opened_ts=self.clock.now)
            r.broker._save()
            d = self.decision(r, "exit")
            r.exit_policy(self.clock.now, "100")
            original = r.broker.orders["order:" + d["id"]]
            original["status"] = "REJECTED"
            r.broker._save()
            r.exit_policy(self.clock.now + 1, "100")
            self.assertEqual(len(r.broker.orders), 2)
            self.assertIn("order:" + d["id"] + ":retry:1", r.broker.orders)
        finally:
            r.close()

    def test_holding_400_days_never_take_profit_or_max_hold(self):
        r, spec = self.prepared()
        try:
            r.broker.positions[r.symbol] = dict(qty="0.1", entry="100",
                                                stop="85", opened_ts=self.clock.now)
            r.broker._save()
            r.exit_policy(self.clock.now + 400 * DAY, "100000")
            self.assertIn(r.symbol, r.broker.positions)
            self.assertFalse(r.broker.orders)
        finally:
            r.close()

    def test_void_window_keeps_position_and_stop(self):
        r, spec = self.prepared()
        try:
            r.broker.positions[r.symbol] = dict(qty="0.1", entry="100",
                                                stop="85", opened_ts=self.clock.now)
            r.broker._save()
            d = self.decision(r)
            r.state["window_void"] = True
            r.route_signals(spec)
            self.assertEqual(d["status"], "rejected")
            self.assertEqual(r.broker.positions[r.symbol]["stop"], "85")
            self.assertFalse(r.broker.orders)
        finally:
            r.close()

    def test_entry_while_position_exists_rejected(self):
        r, spec = self.prepared()
        try:
            r.broker.positions[r.symbol] = dict(qty="0.1", entry="100",
                                                stop="85", opened_ts=self.clock.now)
            r.broker._save()
            d = self.decision(r)
            r.route_signals(spec)
            self.assertEqual(d["reason"], "single_position_or_pending")
        finally:
            r.close()

    def test_main_once(self):
        r = self.runtime()
        r.poll = r.snapshot
        status = self.base / "t1_status_test.json"
        with patch("paper_runtime_v7.PaperRuntime", return_value=r):
            self.assertEqual(main(["--once", "--state-dir", str(self.base / "cli"),
                                   "--status", str(status)]), 0)
        self.assertTrue(status.exists())
        self.assertIsNone(json.loads(status.read_text())["latest_error"])


if __name__ == "__main__":
    unittest.main()
