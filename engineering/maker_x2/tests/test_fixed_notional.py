"""Fictional tests for opt-in independent B episodes and H2 filtering."""
import unittest
from decimal import Decimal as D
from account_lifecycle import OfflineAccount
from account_ledger import replay_account
from market_replay import replay_cell
from maker_exit_lab import X2Error

def conf():
    return {"capital":"150","per_trade_margin":"10","leverage":"50",
            "daily_loss_limit":"1","loss_streak_limit":2,
            "halt_hours":"24","liq_pct":".0145","liq_fee_pct":".0005"}

def bar(i, high="100.27005", low="99", op="100", close="100"):
    return {"ts":i*60000,"open":op,"high":high,"low":low,
            "close":close,"closed":True}

def acct(mode="B"):
    return OfflineAccount(**{k:conf()[k] for k in
        ("capital","per_trade_margin","leverage","daily_loss_limit",
         "loss_streak_limit","halt_hours","liq_pct","liq_fee_pct")},
         mode=mode)

class Control(unittest.TestCase):
    def test_b_flat_reset(self):
        a=acct()
        self.assertEqual(a.submit("FICTION","one",0,"100","100","1",
                         ".25",2,"2","X2"),"PENDING")
        with self.assertRaisesRegex(X2Error,"cannot reset"):
            a.reset_b()
        a.bar(bar(0),entry_qty="2")
        with self.assertRaisesRegex(X2Error,"cannot reset"):
            a.reset_b()
        a.bar(bar(1,high="101.2",op="101",low="98"))
        self.assertEqual(a.ledger()["qty"],D(0))
        prior=a.verify_ledger()
        a.reset_b()
        self.assertEqual(a.ledger()["cash"],D(150))
        self.assertEqual(a.loss_streak,0)
        self.assertIsNone(a.halt_until)
        self.assertEqual(len(a.b_history),1)
        self.assertEqual(a.b_history[0]["ledger"],prior)
        self.assertEqual(replay_account("150",a.b_history[0]["events"]),
                         prior)
        self.assertEqual(a.submit("FICTION","two",120000,"100","100",
                         "1",".25",2,"2","X2"),"PENDING")

    def test_default_a_bytes(self):
        a=acct("A")
        b=acct()
        self.assertEqual(a.mode,"A")
        self.assertEqual(a.submit("FICTION","one",0,"100","100","1",
                         ".25",2,"2","X2"),"PENDING")
        self.assertEqual(b.submit("FICTION","one",0,"100","100","1",
                         ".25",2,"2","X2"),"PENDING")
        self.assertEqual(a.events,b.events)

    def test_tail_keeps_slot(self):
        a=acct()
        a.submit("FICTION","tail",0,"100","100","1",".25",2,"2","X2")
        a.bar(bar(0),entry_qty="2")
        state=a.tail(gap=True)
        self.assertTrue(state["slot"])
        self.assertTrue(state["stale"])
        with self.assertRaises(X2Error):
            a.reset_b()

    def test_filter_matched(self):
        class Filter:
            def skip(self, ts):
                return True,"H2_SKIP"
        data=[bar(i,high="100.1") for i in range(300)]
        cfg=conf()
        for matched in (None,[290*60000]):
            result=replay_cell("FICTION",data,[],1,".25",2,"X2",
                 cfg,290*60000,300*60000,signal_times=matched,
                 eligible=[{"signal_time":290*60000,"triggered":True,
                    "signal_high":D(100),"signal_close":D(100),
                    "atr14":D(1),"vwap60":D(99),"signal_id":"one",
                    "signal_type":"S1"}], entry_filter=Filter())
            self.assertEqual(result["counts"]["signals"],1)
            self.assertEqual(result["funnel"]["H2_SKIP"],1)
            self.assertEqual(result["counts"].get("attempts",0),0)
