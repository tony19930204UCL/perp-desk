"""Causal handmade entry fixtures; no downloaded history or market runner."""
import os
import sys
import unittest
from decimal import Decimal as D, localcontext
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from accounting import replay
from entry_lifecycle import MINUTE, ShortEntry, atr14, vwap60
from maker_exit_lab import X2Error, MAKER_FEE

T = MINUTE


def bar(j, op="100", high="101", low="99", close="100", base="2", quote="200"):
    return {"ts": j * T, "open": op, "high": high, "low": low,
            "close": close, "closed": True,
            "base_volume": base, "quote_volume": quote}


def entry(ttl=2, **kw):
    opts = dict(signal_id="s", signal_time=0, high="100", close="100",
                atr="1", k=".25", ttl=ttl, qty="2")
    opts.update(kw)
    return ShortEntry(**opts)


class Control(unittest.TestCase):
    def test_a02_touch_insufficient_exact_boundary(self):
        x = entry()
        self.assertEqual(x.limit, D("100.25"))
        with self.assertRaises(X2Error):
            x.process(bar(0, "100", "100.25", "99", "100"), fill_qty="2")
        for high in ("100.25", "100.270049"):
            y = entry()
            with self.subTest(high=high), self.assertRaises(X2Error):
                y.process(bar(0, "100", high, "99", "100"), fill_qty="2")
        z = entry()
        z.process(bar(0, "100", "100.27005", "99", "100"), fill_qty="2")
        self.assertEqual(z.events[0]["price"], "100.25")
        self.assertEqual(z.snapshot()["ledger"]["fees_paid"], D("0.04010"))

    def test_a03_activation_cross_and_resting_gap(self):
        x = entry()
        self.assertEqual(x.process(bar(0, "101", "101.1", "99", "100")),
                         "POST_ONLY_REJECT")
        self.assertEqual(x.events, [])
        y = entry()
        y.process(bar(0, "100", "100.1", "99", "100"))
        self.assertEqual(y.process(bar(1, "101", "101.2", "99", "100"),
                                   fill_qty="2"), "FILLED")
        self.assertEqual(y.events[0]["price"], "100.25")

    def test_a04_ttl2_5_last_and_expiry_without_suffix(self):
        for ttl in (2, 5):
            with self.subTest(ttl=ttl):
                x = entry(ttl)
                for j in range(ttl - 1):
                    x.process(bar(j, "100", "100.1", "99", "100"))
                self.assertEqual(x.status, "PENDING")
                x.process(bar(ttl - 1, "100", "100.27005", "99", "100"),
                          fill_qty="2")
                self.assertEqual(x.status, "FILLED")
                y = entry(ttl)
                for j in range(ttl):
                    y.process(bar(j, "100", "100.1", "99", "100"))
                self.assertEqual(y.process(bar(ttl, "100", "102", "99", "100")),
                                 "EXPIRED")
                self.assertEqual(y.events, [])

    def test_a04_prefix_tail_gap_no_lookahead(self):
        x = entry(5)
        x.process(bar(0, "100", "100.1", "99", "100"))
        self.assertEqual(x.snapshot()["ledger"]["event_count"], 0)
        self.assertEqual(x.tail()["status"], "TRUNCATED")
        y = entry(5)
        y.process(bar(0, "100", "100.1", "99", "100"))
        self.assertEqual(y.process(bar(2, "100", "105", "99", "100")), "GAP")
        self.assertTrue(y.snapshot()["gap"])

    def test_pending_slot_dedup_missing_signal_indicators(self):
        x = entry()
        self.assertTrue(x.snapshot()["pending_slot"])
        with self.assertRaises(X2Error):
            x.submit_again("s")
        with self.assertRaises(X2Error):
            x.submit_again("other")
        for atr in ("0", "NaN", "-1"):
            with self.subTest(atr=atr), self.assertRaises(X2Error):
                entry(atr=atr)
        with self.assertRaises(X2Error):
            entry(high="101", close="100", atr="1", k=".50")
        with self.assertRaises(X2Error):
            atr14([bar(j) for j in range(14)])
        with self.assertRaises(X2Error):
            vwap60([bar(j) for j in range(59)])

    def test_known_atr14_true_range_and_quote_base_vwap60(self):
        series = [bar(j) for j in range(15)]
        self.assertEqual(atr14(series), D("2"))
        v = [bar(j, base="2", quote="210") for j in range(60)]
        self.assertEqual(vwap60(v), D("105"))
        v[-1]["quote_volume"] = "260"
        with localcontext() as ctx:
            ctx.prec = 50
            expected = D("12650") / D("120")
        self.assertEqual(vwap60(v), expected)

    def test_entry_partial_then_funding_open_replay(self):
        x = entry(ttl=5)
        self.assertEqual(x.process(bar(0, "100", "100.27005", "99", "100"),
                                   fill_qty="1"), "PARTIAL")
        self.assertEqual(x.snapshot()["ledger"]["remaining_qty"], D("1"))
        # Partial entry remains open; finalized funding credits a short.
        self.assertEqual(x.process(bar(1, "100", "100.1", "99", "100"),
                                   settlements=[{"id":"f1", "settlement_id":"s1",
                                                 "ts":2*T, "mark":"100",
                                                 "rate":".01"}]), "PARTIAL")
        result = replay("1000", x.events)
        self.assertEqual(result["funding_cost"], D("-1"))
        self.assertEqual(result["cash"], D("1000.97995"))
        self.assertEqual(x.snapshot()["open_qty"], D("1"))
        self.assertEqual(x.snapshot()["pending_slot"], True)

    def test_entry_fill_bar_hard_sl_without_maker_profit(self):
        x = entry()
        x.process(bar(0, "100", "100.27005", "99", "100"), fill_qty="2")
        self.assertEqual([e["type"] for e in x.events], ["fill"])
        self.assertEqual(x.snapshot()["open_qty"], D("2"))
        self.assertEqual(x.snapshot()["ledger"]["realized_gross"], D("0"))


class Mutation(unittest.TestCase):
    def mut_lookahead(self):
        def future(self, bar, *args, **kw):
            result = original(self, bar, *args, **kw)
            if self.status == "PENDING":
                self.status = "FILLED"
            return result
        original = ShortEntry.process
        with patch.object(ShortEntry, "process", future):
            Control("test_a04_prefix_tail_gap_no_lookahead").debug()

    def mut_touch(self):
        with patch("entry_lifecycle.MAKER_FEE", D("0")):
            Control("test_a02_touch_insufficient_exact_boundary").debug()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "control"
    if mode == "control":
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Control)
    else:
        mapping = {"mut-lookahead": "mut_lookahead",
                   "mut-touch": "mut_touch"}
        if mode not in mapping:
            raise SystemExit("unknown mode")
        suite = unittest.TestSuite([Mutation(mapping[mode])])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
