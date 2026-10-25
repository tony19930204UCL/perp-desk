"""Negative integration controls; never run until positive controls pass.

Each mode fails with a named AssertionError from a genuinely changed data
or accounting behavior, not an ImportError or a fake mutation status.
"""
import os
import sys
import unittest
from decimal import Decimal as D
from unittest.mock import patch

sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import market_replay
from account_lifecycle import OfflineAccount
from maker_exit_lab import X2Error
from backtest_controls import synthetic_account, fictional_bar, self_test


class Control(unittest.TestCase):
    def test_actual_fee_not_omitted(self):
        self.assertTrue(self_test())

    def test_finalized_funding_changes_account_cash(self):
        a=synthetic_account()
        a.submit("FICTION","fund-episode",0,"100","100","1",
                 ".25",2,"2","X2")
        a.bar(fictional_bar(0),entry_qty="2")
        funded={"id":"fund0","settlement_id":"s0","ts":120000,
                "mark":"100","rate":".01"}
        a.bar(fictional_bar(1,high="100.1"),settlements=[funded])
        self.assertEqual(a.ledger()["funding_cost"],D("-2"))
        self.assertEqual(a.ledger()["cash"],D("1001.95990"))
        self.assertEqual(a.ledger()["qty"],D("2"))

    def test_time_is_never_a_synthetic_exit(self):
        a=synthetic_account()
        a.submit("FICTION","time-open",0,"100","100","1",
                 ".25",2,"2","X2")
        a.bar(fictional_bar(0),entry_qty="2")
        a.tail()
        self.assertEqual(a.ledger()["qty"],D("2"))
        self.assertEqual([e["type"] for e in a.events],["reserve","fill"])
        self.assertEqual(a.ledger()["reserved"],D("50"))
        self.assertFalse(any(e["type"]=="fill" and e["side"]=="buy"
                             for e in a.events))

    def test_future_bar_must_not_change_predecision_features(self):
        from signal_features import eligible_decisions
        from test_signal_features import bars
        known=bars(300)
        later=[dict(b,ts=(300+i)*60000,quote_volume="88888")
               for i,b in enumerate(bars(2))]
        baseline=eligible_decisions(known,1)
        suffix=eligible_decisions(known+later,1)
        self.assertEqual(baseline,suffix[:len(baseline)])

    def test_order_overfill_is_rejected(self):
        a=synthetic_account()
        a.submit("FICTION","overfill",0,"100","100","1",
                 ".25",2,"2","X2")
        with self.assertRaises(X2Error):
            a.bar(fictional_bar(0),entry_qty="3")
        self.assertEqual(a.ledger()["qty"],D("0"))


class Mutation(unittest.TestCase):
    def mut_fee(self):
        from maker_exit_lab import MakerX2
        with patch.object(MakerX2,"_fee",return_value=D("0")):
            # Use the existing independently accepted positive fee control
            # to ensure this never silently reports a success.
            from test_x2 import Control as X2Control
            X2Control("test_weighted_partial_entry_and_tp1_economics").debug()

    def mut_funding(self):
        from account_ledger import replay_account
        original=replay_account
        def wrong(*args,**kwargs):
            result=original(*args,**kwargs)
            result["funding_cost"]=D("0")
            return result
        with patch("account_lifecycle.replay_account",wrong):
            Control("test_finalized_funding_changes_account_cash").debug()

    def mut_fake_time(self):
        original=OfflineAccount.tail
        def wrong(self,*args,**kwargs):
            if self.events:
                self.events.append({"id":"fake-tail-close",
                                    "type":"fill","seq":len(self.events),
                                    "ts":self.events[-1]["ts"],
                                    "side":"buy","qty":"2","price":"100",
                                    "liquidity":"taker"})
            return original(self,*args,**kwargs)
        with patch.object(OfflineAccount,"tail",wrong):
            Control("test_time_is_never_a_synthetic_exit").debug()

    def mut_future(self):
        from signal_features import eligible_decisions
        original=eligible_decisions
        def wrong(bars,tf):
            records=original(bars,tf)
            for record in records:
                record["vwap60"]+=D(bars[-1]["quote_volume"])
            return records
        with patch("signal_features.eligible_decisions",wrong):
            Control("test_future_bar_must_not_change_predecision_features").debug()

    def mut_overfill(self):
        from entry_lifecycle import ShortEntry
        original=ShortEntry.process
        def wrong(self,bar,fill_qty=None,settlements=()):
            if fill_qty is not None:
                self.remaining=D(fill_qty)
            return original(self,bar,fill_qty=fill_qty,settlements=settlements)
        with patch.object(ShortEntry,"process",wrong):
            Control("test_order_overfill_is_rejected").debug()


if __name__=="__main__":
    names={"mut-fee":"mut_fee","mut-funding":"mut_funding",
           "mut-fake-time":"mut_fake_time","mut-future":"mut_future",
           "mut-overfill":"mut_overfill"}
    parser=sys.argv[1] if len(sys.argv)>1 else "control"
    suite=(unittest.defaultTestLoader.loadTestsFromTestCase(Control)
           if parser=="control" else
           unittest.TestSuite([Mutation(names[parser])]))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
