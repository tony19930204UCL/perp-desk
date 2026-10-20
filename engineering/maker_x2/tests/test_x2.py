"""Public synthetic X2 tests; mutation modes are test-only, never production flags."""
import os
import sys
import unittest
from decimal import Decimal as D, localcontext
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from accounting import replay
from maker_exit_lab import MakerX2, X2Error


def start(qty="4", price="100", cash="1000"):
    x = MakerX2(cash)
    x.entry_fill("en0", 10, 0, qty, price)
    x.arm()
    return x


def bar(x, index, ts, op="100", hi="100", lo="98", cl="99",
        settlements=(), fills=()):
    return x.on_bar(index, ts, op, hi, lo, cl, settlements, fills)


def tp(order, qty, price, event_id):
    return {"id": event_id, "order": order, "qty": qty, "price": price}


def settlement(event_id, sid, ts, rate, mark="100"):
    return {"id": event_id, "settlement_id": sid, "ts": ts,
            "rate": rate, "mark": mark}


class Control(unittest.TestCase):
    def test_weighted_partial_entry_and_tp1_economics(self):
        x = MakerX2("1000")
        x.entry_fill("e0", 10, 0, "2", "100")
        x.entry_fill("e1", 11, 0, "2", "110")
        x.arm()
        self.assertEqual(x.E, D("105"))
        self.assertEqual(x.original_stop, D("105.630"))
        self.assertEqual(x.orders, {"tp1": D("2"), "tp2": D("2")})
        self.assertEqual(x.targets["tp1"], D("104.5275"))
        bar(x, 1, 20, "104", "104.2", "103", "103.5",
            fills=[tp("tp1", "2", "104.5275", "tp1")])
        self.assertEqual(x.qty, D("2"))
        self.assertEqual(x.stop, D("105.630"))
        self.assertEqual(x.realized_gross, D("0.9450"))
        self.assertEqual(x.fees_paid, D("0.127811"))
        self.assertEqual(x.snapshot()["cash"], D("1000.817189"))
        independent = replay("1000", x.events)
        self.assertEqual(independent["cash"], D("1000.817189"))
        self.assertEqual(independent["entry_basis"], D("210"))
        self.assertEqual(independent["remaining_signed_qty"], D("-2"))
        self.assertEqual(independent["realized_net"], D("0.817189"))

    def test_half_exit_next_bar_economic_be_not_entry_be(self):
        x = start()
        bar(x, 1, 20, "99.6", "100", "99", "99.2",
            fills=[tp("tp1", "2", "99.55", "take1")])
        self.assertEqual(x.stop, D("100.600"))
        # gross 1; fees entry .08 + TP .0398 = .1198
        self.assertEqual(x.snapshot()["cash"], D("1000.78018"))
        bar(x, 2, 30, "99.7", "99.9", "99.3", "99.6")
        with localcontext() as ctx:
            ctx.prec = 50
            reference = D("200.78018") / D("2.0014002")
        self.assertEqual(x.stop, reference)
        self.assertNotEqual(x.stop, x.E)
        self.assertEqual(x.qty, D("2"))

    def test_same_bar_first_tp_never_retroactive_stop(self):
        x = start()
        bar(x, 1, 20, "99.6", "100", "99", "99.2",
            fills=[tp("tp1", "2", "99.55", "take1")])
        self.assertEqual(x.stop, D("100.600"))
        self.assertEqual(x.first_tp_bar, 1)

    def test_finalized_funding_remaining_signed_qty_and_replay(self):
        x = start()
        bar(x, 1, 20, "99.6", "100", "99", "99.2",
            fills=[tp("tp1", "2", "99.55", "take1")])
        bar(x, 2, 30, "99.7", "99.9", "99.3", "99.6",
            settlements=[settlement("fund1", "s1", 25, ".01")])
        r = replay("1000", x.events)
        self.assertEqual(r["funding_cost"], D("-2"))
        self.assertEqual(r["settlements"][0]["signed_qty"], D("-2"))
        self.assertEqual(r["settlements"][0]["ts"], 25)
        self.assertEqual(r["cash"], D("1002.78018"))
        self.assertEqual(r["fees_paid"], D("0.11982"))
        self.assertEqual(x.snapshot()["cash"], D("1002.78018"))
        self.assertEqual(x.funding_cost, D("-2"))
        self.assertEqual(x.qty, D("2"))
        with localcontext() as ctx:
            ctx.prec = 50
            reference = D("200.78018") / D("2.0014002")
        self.assertEqual(x.stop, reference)

    def test_funding_same_end_bar_uses_pre_exit_quantity(self):
        x = start()
        bar(x, 1, 20, "99.5", "99.8", "98", "99",
            settlements=[settlement("f1", "boundary", 20, ".01")],
            fills=[tp("tp1", "2", "99.55", "t1")])
        r = replay("1000", x.events)
        self.assertEqual([e["type"] for e in x.events],
                         ["fill", "funding", "fill"])
        self.assertEqual(r["settlements"][0]["signed_qty"], D("-4"))
        self.assertEqual(r["funding_cost"], D("-4"))
        self.assertEqual(r["cash"], D("1004.78018"))

    def test_tp2_flat_exact_and_no_old_order(self):
        x = start()
        bar(x, 1, 20, "99.5", "99.6", "99", "99.2",
            fills=[tp("tp1", "2", "99.55", "t1")])
        bar(x, 2, 30, "99", "99.2", "98", "98.5",
            fills=[tp("tp2", "2", "99.10", "t2")])
        r = replay("1000", x.events)
        self.assertEqual(x.phase, "CLOSED")
        self.assertEqual(x.qty, D("0"))
        self.assertEqual(r["realized_gross"], D("2.7"))
        self.assertEqual(r["fees_paid"], D("0.15946"))
        self.assertEqual(r["cash"], D("1002.54054"))
        self.assertTrue(r["is_flat"])
        with self.assertRaises(X2Error):
            bar(x, 3, 40, fills=[tp("tp2", "1", "99", "late")])

    def test_risk_wins_over_maker_and_gap_uses_actual_price(self):
        x = start()
        outcome = bar(x, 1, 20, "101", "102", "98", "99",
                      fills=[tp("tp1", "2", "99.55", "lost-maker")])
        self.assertEqual(outcome, "SL_GAP")
        self.assertEqual(x.phase, "CLOSED")
        self.assertEqual(x.events[-1]["price"], "101.0202")
        r = replay("1000", x.events)
        self.assertEqual(r["realized_gross"], D("-4.0808"))
        self.assertEqual(r["fees_paid"], D("0.2820404"))
        self.assertEqual(r["cash"], D("995.6371596"))
        self.assertEqual(len(r["fills"]), 2)
        self.assertEqual(x.orders, {"tp1": D("0"), "tp2": D("0")})

    def test_entry_bar_risk_not_maker_profit(self):
        x = start()
        with self.assertRaises(X2Error):
            bar(x, 0, 20, "99", "100", "98", "99",
                fills=[tp("tp1", "2", "99.55", "t1")])

    def test_duplicate_fill_and_cancel_races(self):
        x = start()
        x.cancel("tp1", "cancel-1", 15)
        with self.assertRaises(X2Error):
            x.cancel("tp1", "cancel-1", 16)
        with self.assertRaises(X2Error):
            bar(x, 1, 20, "99", "100", "98", "99",
                fills=[tp("tp1", "2", "99.55", "late")])
        y = start()
        bar(y, 1, 20, "99", "100", "98", "99",
            fills=[tp("tp1", "1", "99.55", "part")])
        with self.assertRaises(X2Error):
            bar(y, 2, 30, "99", "100", "98", "99",
                fills=[tp("tp1", "1", "99.55", "part")])

    def test_order_overfill_and_tp2_before_tp1(self):
        for order, amount in (("tp1", "3"), ("tp2", "2")):
            x = start()
            with self.subTest(order=order), self.assertRaises(X2Error):
                bar(x, 1, 20, "99", "100", "98", "99",
                    fills=[tp(order, amount, "99", "bad")])

    def test_no_future_funding_duplicate_settlement_or_backward_time(self):
        x = start()
        with self.assertRaises(X2Error):
            bar(x, 1, 20, "100", "100", "98", "99",
                settlements=[settlement("future", "s", 21, ".01")])
        y = start()
        bar(y, 1, 20, "100", "100", "98", "99",
            settlements=[settlement("first", "s", 19, ".01")])
        with self.assertRaises(X2Error):
            bar(y, 2, 30, "100", "100", "98", "99",
                settlements=[settlement("other", "s", 29, ".01")])

    def test_reject_sl_boundary_nonpositive_and_touch_only(self):
        for pct in (".0085", ".009", ".005", "-.01"):
            with self.subTest(pct=pct), self.assertRaises(X2Error):
                MakerX2(sl_pct=pct)
        x = start()
        with self.assertRaises(X2Error):
            bar(x, 1, 20, "100", "100", "99.55", "99.8",
                fills=[tp("tp1", "2", "99.55", "touch")])
        y = start()
        with self.assertRaises(X2Error):
            y.entry_fill("invalid", 11, 1, "-1", "100")

    def test_maker_penetration_exact_boundary_and_favorable_price_rejected(self):
        limit1 = D("99.55")
        boundary1 = limit1 * D("0.9998")
        for low in (limit1, D("99.54"), boundary1 + D("0.000001")):
            x = start()
            with self.subTest(low=low), self.assertRaises(X2Error):
                bar(x, 1, 20, "99.7", "100", str(low), "99.8",
                    fills=[tp("tp1", "2", str(limit1), "no-fill")])
        for low in (boundary1, boundary1 - D("0.01")):
            x = start()
            bar(x, 1, 20, "99.7", "100", str(low), "99.8",
                fills=[tp("tp1", "2", str(limit1), "fill1")])
            self.assertEqual(x.events[-1]["price"], str(limit1))
            self.assertEqual(x.qty, D("2"))
        x = start()
        with self.assertRaises(X2Error):
            bar(x, 1, 20, "99.7", "100", "99", "99.8",
                fills=[tp("tp1", "2", "99.0", "cheap")])
        x = start()
        bar(x, 1, 20, "99.7", "100", "99", "99.8",
            fills=[tp("tp1", "2", "99.55", "fill1")])
        limit2 = D("99.10")
        for low in (limit2, limit2 * D("0.9998") + D("0.000001")):
            y = start()
            bar(y, 1, 20, "99.7", "100", "99", "99.8",
                fills=[tp("tp1", "2", "99.55", "fill1")])
            with self.subTest(tp2_low=low), self.assertRaises(X2Error):
                bar(y, 2, 30, "99", "99.2", str(low), "99",
                    fills=[tp("tp2", "2", str(limit2), "no-fill2")])
        for low in (limit2 * D("0.9998"), D("98")):
            y = start()
            bar(y, 1, 20, "99.7", "100", "99", "99.8",
                fills=[tp("tp1", "2", "99.55", "fill1")])
            bar(y, 2, 30, "99", "99.2", str(low), "99",
                fills=[tp("tp2", "2", str(limit2), "fill2")])
            self.assertEqual(y.events[-1]["price"], str(limit2))
            self.assertEqual(y.phase, "CLOSED")
        y = start()
        bar(y, 1, 20, "99.7", "100", "99", "99.8",
            fills=[tp("tp1", "2", "99.55", "fill1")])
        with self.assertRaises(X2Error):
            bar(y, 2, 30, "99", "99.2", "98", "99",
                fills=[tp("tp2", "2", "98", "cheap2")])


# Mutations deliberately patch only the TEST's view of the production method.
# Each targeted CONTROL assertion must fail, yielding unittest failure exit 1.
class Mutation(unittest.TestCase):
    def mut_fee_omission(self):
        with patch.object(MakerX2, "_fee", return_value=D("0")):
            Control("test_weighted_partial_entry_and_tp1_economics").debug()

    def mut_funding_remaining_qty(self):
        with patch.object(MakerX2, "_funding_qty", return_value=D("-4")):
            Control("test_finalized_funding_remaining_signed_qty_and_replay").debug()

    def mut_same_bar_be(self):
        original_bar = MakerX2.on_bar

        def premature_be(state, bar_index, *args, **kwargs):
            outcome = original_bar(state, bar_index, *args, **kwargs)
            if state.phase == "OPEN" and state.first_tp_bar == bar_index:
                state._promote_be(bar_index)
            return outcome

        with patch.object(MakerX2, "on_bar", premature_be), patch.object(
            MakerX2, "_be_eligible", return_value=True
        ):
            Control("test_same_bar_first_tp_never_retroactive_stop").debug()

    def mut_duplicate_fill(self):
        def allow_duplicate(self, event_id, ts):
            if event_id in self.seen:
                self.seen.remove(event_id)
            original(self, event_id, ts)
        original = MakerX2._claim
        with patch.object(MakerX2, "_claim", allow_duplicate):
            Control("test_duplicate_fill_and_cancel_races").debug()


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "control"
    tests = unittest.TestLoader().loadTestsFromTestCase(
        Mutation if mode.startswith("mut-") else Control)
    if mode.startswith("mut-"):
        wanted = {"mut-fee": "mut_fee_omission",
                  "mut-funding": "mut_funding_remaining_qty",
                  "mut-be": "mut_same_bar_be",
                  "mut-duplicate": "mut_duplicate_fill"}.get(mode)
        if wanted is None:
            raise SystemExit("unknown mutation")
        tests = unittest.TestSuite([Mutation(wanted)])
    outcome = unittest.TextTestRunner(verbosity=2).run(tests)
    sys.exit(0 if outcome.wasSuccessful() else 1)
