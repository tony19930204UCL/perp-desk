"""Closed-bar lifecycle fixture tests and negative mutations."""
import os
import sys
import unittest
from decimal import Decimal as D
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from accounting import replay
from entry_lifecycle import ShortEntry, MINUTE
from exit_lifecycle import ShortExit
from maker_exit_lab import X2Error

T = MINUTE


def bar(j, op="100", high="100.1", low="99", close="100"):
    return {"ts": j*T, "open": op, "high": high, "low": low,
            "close": close, "closed": True}


def seed():
    x = ShortEntry("signal", 0, "100", "100", "1", ".25", 2, "2")
    x.process(bar(0, "100", "100.27005", "99", "100"), fill_qty="2")
    return x


class Control(unittest.TestCase):
    def test_a06_x1_signal_frozen_vwap_profit_side_net_and_unknown(self):
        x = seed()
        y = ShortExit(x, "X1", vwap="99")
        self.assertEqual(y.target, D("99"))
        self.assertTrue(y.unknown_future_funding)
        with self.assertRaises(X2Error):
            ShortExit(x, "X1", vwap="101")
        with self.assertRaises(X2Error):
            ShortExit(x, "X1", vwap="100.24")
        z = ShortExit(x, "X1", vwap="99", known_funding_cost="1")
        self.assertFalse(z.unknown_future_funding)
        with self.assertRaises(X2Error):
            ShortExit(x, "X1", vwap="99", known_funding_cost="5")
        # Once X1 is installed, future/rolling VWAP is never consulted.
        self.assertEqual(y.on_bar(bar(1, "100", "100.1", "98.9", "99"),
                                  maker_fill=True), "CLOSED")
        r = replay("1000", y.core.events)
        self.assertEqual(r["realized_gross"], D("2.5"))
        self.assertEqual(r["fees_paid"], D("0.0897"))
        self.assertEqual(r["cash"], D("1002.4103"))
        self.assertTrue(r["is_flat"])

    def test_a08_x3_held_three_adverse_closes_next_bar_only(self):
        x = ShortExit(seed(), "X3")
        x.on_bar(bar(1, "100", "100.2", "99", "100.1"))
        x.on_bar(bar(2, "100.1", "100.3", "99", "100.2"))
        self.assertFalse(x.withdraw)
        x.on_bar(bar(3, "100.2", "100.4", "99", "100.3"))
        self.assertTrue(x.withdraw)
        self.assertEqual(x.pending["start"], 4*T)
        self.assertEqual(x.core.events[-1]["side"], "sell")
        self.assertEqual(x.snapshot()["qty"], D("2"))
        self.assertEqual(x.pending["limit"], D("100.27994"))

    def test_a08_zero_resets_and_delivery_cross_then_retry(self):
        x = ShortExit(seed(), "X3")
        for j, cl in ((1, "100.1"), (2, "100.1"), (3, "100.2"),
                      (4, "100.3"), (5, "100.4")):
            x.on_bar(bar(j, cl, str(D(cl)+D(".1")), "99", cl))
        self.assertEqual(x.pending["start"], 6*T)
        self.assertEqual(x.on_bar(bar(6, "100", "100.5", "99", "100.2"),
                                  maker_fill=True), "OPEN")
        self.assertIn(("POST_ONLY_REJECT", 6*T), x.history)
        self.assertTrue(x.snapshot()["pending_slot"])
        self.assertEqual(x.snapshot()["ledger"]["remaining_qty"], D("2"))
        self.assertIsNotNone(x.pending)
        self.assertEqual(x.pending["start"], 7*T)

    def test_a08_ttl2_expiry_reprice_without_three_new_bars(self):
        x = ShortExit(seed(), "X3")
        for j, close in ((1, "100.1"), (2, "100.2"), (3, "100.3")):
            x.on_bar(bar(j, close, str(D(close)+D(".1")), "99", close))
        first = x.pending["id"]
        x.on_bar(bar(4, "100.4", "100.5", "99", "100.4"))
        x.on_bar(bar(5, "100.5", "100.6", "99", "100.5"))
        x.on_bar(bar(6, "100.6", "100.7", "99", "100.6"))
        self.assertTrue(x.withdraw)
        self.assertNotEqual(x.pending["id"], first)
        self.assertEqual(x.snapshot()["qty"], D("2"))
        self.assertEqual(x.snapshot()["ledger"]["realized_gross"], D("0"))

    def test_a10_four_hour_unfilled_stays_open_real_cost_funding_mtm(self):
        x = ShortExit(seed(), "X1", vwap="99")
        x.on_bar(bar(1, "100", "100.1", "99.5", "100"),
                 settlements=[{"id":"fund", "settlement_id":"s1",
                               "ts":2*T, "mark":"100", "rate":".01"}])
        for j in range(2, 241):
            x.on_bar(bar(j, "100", "100.1", "99.5", "100"))
        s = x.snapshot()
        self.assertTrue(s["withdraw"])
        self.assertEqual(s["status"], "OPEN")
        self.assertEqual(s["qty"], D("2"))
        self.assertEqual(s["ledger"]["fees_paid"], D(".04010"))
        self.assertEqual(s["ledger"]["funding_cost"], D("-2"))
        self.assertEqual(s["ledger"]["cash"], D("1001.95990"))
        self.assertEqual(s["unrealized"], D(".50"))
        self.assertEqual(s["equity"], D("1002.45990"))
        self.assertIsNotNone(s["estimated_close_fee_not_paid"])
        self.assertTrue(s["pending_slot"])
        self.assertFalse(any(e["id"].startswith("time") for e in s["events"]))

    def test_a11_tail_gap_preserves_open_and_closed_immutable(self):
        x = ShortExit(seed(), "X3")
        x.on_bar(bar(1, "100", "100.1", "99", "100"))
        before = list(x.core.events)
        x.tail(gap=True)
        self.assertTrue(x.snapshot()["stale"])
        self.assertTrue(x.snapshot()["truncated"])
        self.assertEqual(x.core.events, before)
        closed = ShortExit(seed(), "X1", vwap="99")
        closed.on_bar(bar(1, "100", "100.1", "98", "99"), maker_fill=True)
        closed_events = list(closed.core.events)
        closed.tail(gap=True)
        self.assertEqual(closed.core.events, closed_events)
        self.assertEqual(closed.snapshot()["status"], "CLOSED")

    def test_entry_fill_bar_maker_exit_forbidden_sl_always_active(self):
        x = ShortExit(seed(), "X1", vwap="99")
        # Entry was filled at minute-0 end: no retroactive minute-0 X1.
        with self.assertRaises(X2Error):
            x.on_bar(bar(0, "100", "100.1", "98", "99"), maker_fill=True)
        y = ShortExit(seed(), "X1", vwap="99")
        y.on_bar(bar(1, "101", "102", "98", "99"), maker_fill=True)
        self.assertEqual(y.snapshot()["status"], "CLOSED_SL")
        self.assertEqual(y.core.events[-1]["liquidity"], "taker")
        self.assertEqual(y.snapshot()["ledger"]["remaining_qty"], D("0"))

    def test_x2_reuses_existing_half_targets_and_remaining_risk(self):
        x = ShortExit(seed(), "X2")
        self.assertEqual(x.core.targets["tp1"], D("99.798875"))
        self.assertEqual(x.core.targets["tp2"], D("99.34775"))
        x.on_bar(bar(1, "100", "100.1", "99", "99.5"), maker_fill=True)
        self.assertEqual(x.core.qty, D("1"))
        self.assertEqual(x.snapshot()["ledger"]["fees_paid"],
                         D("0.060059775"))
        x.on_bar(bar(2, "101", "101.1", "98", "99"), maker_fill=True)
        self.assertEqual(x.status, "CLOSED_SL")
        self.assertEqual(x.snapshot()["ledger"]["remaining_qty"], D("0"))


class Mutation(unittest.TestCase):
    def mut_fake_time(self):
        original = ShortExit.on_bar
        def fake(self, bar, *args, **kwargs):
            result = original(self, bar, *args, **kwargs)
            if self.withdraw and self.core.phase == "OPEN":
                self.status = "CLOSED_TIME"
            return result
        with patch.object(ShortExit, "on_bar", fake):
            Control("test_a10_four_hour_unfilled_stays_open_real_cost_funding_mtm").debug()

    def mut_reverse(self):
        original = ShortExit.on_bar
        def fake(self, bar, *args, **kwargs):
            result = original(self, bar, *args, **kwargs)
            if self.withdraw and self.pending:
                self.pending["start"] = bar["ts"]
            return result
        with patch.object(ShortExit, "on_bar", fake):
            Control("test_a08_x3_held_three_adverse_closes_next_bar_only").debug()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "control"
    if mode == "control":
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Control)
    else:
        names = {"mut-time": "mut_fake_time", "mut-reverse": "mut_reverse"}
        if mode not in names:
            raise SystemExit("unknown mode")
        suite = unittest.TestSuite([Mutation(names[mode])])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
