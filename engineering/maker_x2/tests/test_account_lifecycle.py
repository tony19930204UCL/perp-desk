"""Fictional single-account controls; mutations fail named assertions."""
import os
import sys
import unittest
from decimal import Decimal as D
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from account_lifecycle import OfflineAccount
from liquidation import liquidation_proxy
from maker_exit_lab import X2Error

T = 60000


def account():
    return OfflineAccount("1000", "50", "5", "5", 2, "24", ".0145", ".0005")


def bar(j, op="100", high="100.27005", low="99", close="100"):
    return dict(ts=j*T, open=op, high=high, low=low,
                close=close, closed=True)


def submit(a, signal="s", symbol="AAA", style="X2", **kw):
    options = dict(symbol=symbol, signal_id=signal, signal_time=0,
                   high="100", close="100", atr="1", k=".25",
                   ttl=2, qty="2", style=style)
    options.update(kw)
    return a.submit(**options)


class Control(unittest.TestCase):
    def test_slot_cash_margin_pending_cross_symbol_and_dedup(self):
        a = account()
        self.assertEqual(submit(a), "PENDING")
        self.assertEqual(a.ledger()["reserved"], D("50"))
        self.assertEqual(a.ledger()["cash"], D("1000"))
        self.assertEqual(a.ledger()["available"], D("950"))
        with self.assertRaises(X2Error):
            submit(a, "s2", "BBB")
        with self.assertRaises(X2Error):
            submit(a, "s", "AAA")
        a.cancel_entry(0)
        self.assertEqual(a.ledger()["reserved"], D("0"))
        with self.assertRaises(X2Error):
            submit(a, "s", "BBB")
        low = OfflineAccount("50", "50", "5", "5", 2, "24",
                             ".0145", ".0005")
        self.assertEqual(submit(low), "INSUFFICIENT_CASH_OR_MARGIN")
        self.assertEqual(low.ledger()["cash"], D("50"))

    def test_x1_missing_target_cannot_pay_or_reserve(self):
        a = account()
        with self.assertRaises(X2Error):
            submit(a, style="X1", signal_vwap=None)
        self.assertEqual(a.events, [])
        self.assertEqual(a.ledger()["cash"], D("1000"))
        self.assertEqual(a.ledger()["reserved"], D("0"))

    def test_open_position_actual_fee_mtm_no_future_exit_fee(self):
        a = account()
        submit(a)
        a.bar(bar(0), entry_qty="2")
        s = a.snapshot()
        self.assertTrue(s["slot"])
        self.assertEqual(s["ledger"]["qty"], D("2"))
        self.assertEqual(s["ledger"]["fees"], D(".04010"))
        self.assertEqual(s["ledger"]["cash"], D("999.95990"))
        self.assertEqual(s["unrealized"], D(".50"))
        self.assertEqual(s["equity"], D("1000.45990"))
        self.assertGreater(s["estimated_close_fee_not_paid"], D("0"))
        self.assertEqual(s["ledger"]["reserved"], D("50"))

    def test_full_and_partial_liq_proxy_remaining_margin(self):
        full = liquidation_proxy("100", "102", "2", "2", "50",
                                 ".0145", ".0005")
        self.assertEqual(full["margin_writeoff"], D("50"))
        self.assertEqual(full["fee"], D(".10"))
        half = liquidation_proxy("100", "102", "1", "2", "50",
                                 ".0145", ".0005")
        self.assertEqual(half["margin_writeoff"], D("25"))
        self.assertEqual(half["fee"], D(".05"))
        self.assertIsNone(liquidation_proxy("100", "101", "1", "2", "50",
                                            ".0145", ".0005"))

    def test_liq_does_not_double_charge_original_margin(self):
        a = account()
        submit(a)
        a.bar(bar(0), entry_qty="2")
        a.bar(bar(1, "100", "100.1", "98", "99"), maker_fill=True)
        self.assertEqual(a.ledger()["qty"], D("1"))
        self.assertEqual(a.bar(bar(2, "102", "103", "98", "100"),
                               maker_fill=True), "LIQ_PROXY")
        s = a.ledger()
        self.assertEqual(s["writeoff"], D("25"))
        self.assertEqual(s["qty"], D("0"))
        self.assertFalse(a.snapshot()["slot"])
        self.assertTrue(any(e["type"] == "liquidation" for e in a.events))
        self.assertFalse(any(e["type"] == "fill" and e["id"].startswith("time")
                             for e in a.events))

    def test_daily_open_mtm_halt_does_not_block_risk(self):
        a = OfflineAccount("1000", "50", "5", ".5", 2, "24",
                           ".0145", ".0005")
        submit(a)
        a.bar(bar(0), entry_qty="2")
        a.bar(bar(1, "100.6", "100.7", "99", "100.6"))
        self.assertIsNotNone(a.halt_until)
        with self.assertRaises(X2Error):
            submit(a, "next", "BBB", signal_time=2*T)
        self.assertEqual(a.bar(bar(2, "101", "101.2", "98", "100")),
                         "CLOSED_SL")
        self.assertEqual(a.ledger()["qty"], D("0"))
        self.assertEqual(a.ledger()["reserved"], D("0"))

    def test_two_complete_losses_halt_exact_24h_and_no_extension(self):
        a = account()
        for i, sid in ((0, "loss-one"), (2, "loss-two")):
            self.assertEqual(submit(a, sid, signal_time=i*T), "PENDING")
            a.bar(bar(i), entry_qty="2")
            self.assertEqual(a.bar(bar(i+1, "101", "101.2", "98", "100")),
                             "CLOSED_SL")
        self.assertEqual(a.loss_streak, 2)
        expected = 4*T + 24*3600000
        self.assertEqual(a.halt_until, expected)
        a.bar(bar(4, "100", "100.1", "99", "100"))
        self.assertEqual(a.halt_until, expected)
        self.assertEqual(a.ledger()["reserved"], D("0"))
        self.assertEqual(len(a.ledger()["episodes"]), 2)

    def test_pending_expiry_releases_without_fake_fill(self):
        a = account()
        submit(a)
        a.bar(bar(0, high="100.1"))
        a.bar(bar(1, high="100.1"))
        self.assertEqual(a.bar(bar(2, high="100.1")), "EXPIRED")
        self.assertEqual(a.ledger()["reserved"], D("0"))
        self.assertEqual(a.ledger()["qty"], D("0"))
        self.assertEqual(a.ledger()["fees"], D("0"))
        self.assertFalse(a.snapshot()["slot"])

    def test_tail_gap_preserves_original_account_events(self):
        a = account()
        submit(a)
        a.bar(bar(0), entry_qty="2")
        before = list(a.events)
        a.stale = True
        with self.assertRaises(X2Error):
            submit(a, "different", "BBB", signal_time=2*T)
        self.assertEqual(a.events, before)
        self.assertEqual(a.ledger()["qty"], D("2"))


class Mutation(unittest.TestCase):
    def mut_double_entry(self):
        # Removing admission/reservation must violate the positive ledger
        # assertion, rather than escaping via an unrelated X2Error.
        with patch.object(OfflineAccount, "submit", return_value="PENDING"):
            Control("test_slot_cash_margin_pending_cross_symbol_and_dedup").debug()

    def mut_kill_bypass(self):
        def skip_halt(self, ts):
            self.halt_until = None
        with patch.object(OfflineAccount, "_risk_limits", skip_halt):
            Control("test_daily_open_mtm_halt_does_not_block_risk").debug()

    def mut_partial_liq_doublecharge(self):
        original = OfflineAccount.submit

        def double_allocated_margin(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            if result == "PENDING":
                self.events[-1]["amount"] = "100"
                self.allocation = D("100")
            return result

        with patch.object(OfflineAccount, "submit", double_allocated_margin):
            Control("test_liq_does_not_double_charge_original_margin").debug()

    def mut_hidden_mtm(self):
        with patch.object(OfflineAccount, "_valuation", lambda self, m, t: None):
            Control("test_daily_open_mtm_halt_does_not_block_risk").debug()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "control"
    mapping = {"mut-double-entry": "mut_double_entry",
               "mut-kill-bypass": "mut_kill_bypass",
               "mut-partial-liq": "mut_partial_liq_doublecharge",
               "mut-open-mtm": "mut_hidden_mtm"}
    suite = (unittest.defaultTestLoader.loadTestsFromTestCase(Control)
             if mode == "control" else
             unittest.TestSuite([Mutation(mapping[mode])]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
