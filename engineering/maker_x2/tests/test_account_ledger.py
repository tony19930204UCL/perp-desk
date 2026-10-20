"""Fictional account replay controls and independent explicit Decimal values."""
import os
import sys
import unittest
from decimal import Decimal as D
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from account_ledger import replay_account
from maker_exit_lab import X2Error


def e(kind, i, ts, **kwargs):
    return dict(type=kind, id="a"+str(i), seq=i, ts=ts, **kwargs)


class Control(unittest.TestCase):
    def test_independent_real_fees_funding_and_episode_net(self):
        events = [
            e("reserve", 0, 0, amount="50", episode="fiction-1"),
            e("fill", 1, 60000, side="sell", qty="2", price="100",
              liquidity="maker"),
            e("funding", 2, 120000, settlement_id="settle1",
              mark="100", rate=".01"),
            e("fill", 3, 180000, side="buy", qty="2", price="99",
              liquidity="maker"),
            e("release", 4, 180000, amount="50")]
        s = replay_account("1000", events)
        self.assertEqual(s["gross"], D("2"))
        self.assertEqual(s["funding_cost"], D("-2"))
        self.assertEqual(s["fees"], D(".0796"))
        self.assertEqual(s["cash"], D("1003.9204"))
        self.assertEqual(s["reserved"], D("0"))
        self.assertEqual(s["qty"], D("0"))
        self.assertEqual(s["episodes"][0]["net"], D("3.9204"))

    def test_remaining_only_liq_writeoff_not_fake_fill(self):
        events = [
            e("reserve", 0, 0, amount="50", episode="fiction-2"),
            e("fill", 1, 60000, side="sell", qty="2", price="100",
              liquidity="maker"),
            e("fill", 2, 120000, side="buy", qty="1", price="99",
              liquidity="maker"),
            e("liquidation", 3, 180000, qty="1", margin_writeoff="25",
              fee=".05", reason="LIQ_PROXY"),
            e("release", 4, 180000, amount="25")]
        s = replay_account("1000", events)
        self.assertEqual(s["gross"], D("1"))
        self.assertEqual(s["fees"], D(".1098"))
        self.assertEqual(s["writeoff"], D("25"))
        self.assertEqual(s["cash"], D("975.8902"))
        self.assertEqual(s["episodes"][0]["net"], D("-24.1098"))
        self.assertEqual(s["qty"], D("0"))
        self.assertEqual(s["reserved"], D("0"))
        self.assertFalse(any(x["type"] == "fill" for x in events[3:4]))

    def test_replay_duplicate_sequence_and_reserve_guard(self):
        events = [e("reserve", 0, 0, amount="50", episode="fiction-3")]
        for bad in (
            [dict(events[0]), dict(events[0])],
            [dict(events[0]), e("reserve", 1, 0, amount="50",
                                episode="other")],
            [dict(events[0]), e("release", 2, 1, amount="10")]):
            with self.subTest(bad=bad), self.assertRaises(X2Error):
                replay_account("1000", bad)
        with self.assertRaises(X2Error):
            replay_account("40", events)


if __name__ == "__main__":
    unittest.main()
