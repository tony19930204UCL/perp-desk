"""Real offline account fractional X2 conservation and strict over-close guards."""
import os
import sys
import unittest
from decimal import Decimal as D, localcontext

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from account_lifecycle import OfflineAccount
from account_ledger import replay_account
from maker_exit_lab import X2Error

Q = "1.9999999999999999999999999999999999999999999999999"

def bar(ts, op, hi, lo, cl):
    return {"ts":ts, "open":op,"high":hi,"low":lo,
            "close":cl,"closed":True}

def account():
    return OfflineAccount("1000","50","5","5",2,"24",".0145",".0005")

class OvercloseControl(unittest.TestCase):
    def test_fractional_partial_tp_funding_exact_conservation(self):
        a=account()
        self.assertEqual(a.submit("FICTION","frac",0,"100","100","1",
                                  ".25",2,Q,"X2"),"PENDING")
        self.assertEqual(a.bar(bar(0,"100","100.27005","99.9","100"),
                               entry_qty=Q),"FILLED")
        initial=a.ledger()
        self.assertEqual(initial["qty"],D(Q))
        self.assertEqual(initial["reserved"],D("50"))
        self.assertEqual(a.bar(bar(60000,"100","100","99.5","99.8"),
                       maker_fill=True,settlements=[{
                           "id":"fund-1","settlement_id":"settlement-1",
                           "ts":120000,"mark":"100","rate":".001"}]),"OPEN")
        remaining=a.ledger()
        self.assertGreater(remaining["qty"],D("0"))
        self.assertLess(remaining["qty"],D(Q))
        self.assertEqual(remaining["funding_cost"],-D(Q)*D("100")*D(".001"))
        self.assertEqual(a.bar(bar(120000,"99.4","99.5","99","99.2"),
                               maker_fill=True),"CLOSED")
        finished=a.ledger()
        self.assertEqual(finished["qty"],D("0"))
        self.assertEqual(finished["reserved"],D("0"))
        sells=[e for e in a.events if e["type"]=="fill" and e["side"]=="sell"]
        buys=[e for e in a.events if e["type"]=="fill" and e["side"]=="buy"]
        self.assertEqual(len(sells),1)
        self.assertEqual(len(buys),2)
        with localcontext() as ctx:
            ctx.prec=80
            self.assertEqual(sum((D(e["qty"]) for e in buys),D("0")),D(Q))
            gross=sum(((D("100.25")-D(e["price"]))*D(e["qty"])
                       for e in buys),D("0"))
        self.assertEqual(finished["gross"],gross)
        self.assertEqual(finished["cash"],D("1000")+finished["gross"]
                         -finished["fees"]-finished["funding_cost"])
        self.assertEqual(finished,replay_account("1000",a.events))

    def test_true_overclose_remains_rejected(self):
        a=account()
        a.submit("FICTION","negative",0,"100","100","1",".25",2,"2","X2")
        a.bar(bar(0,"100","100.27005","99.9","100"),entry_qty="2")
        original=list(a.events)
        forged=list(original)+[{"id":"bad-buy","seq":len(original),
            "ts":60000,"type":"fill","side":"buy",
            "qty":"2.000000000000000000000000000000000001",
            "price":"99","liquidity":"maker"}]
        with self.assertRaisesRegex(X2Error,"over-close"):
            replay_account("1000",forged)
        self.assertEqual(a.events,original)

if __name__=="__main__":
    unittest.main(verbosity=2)
