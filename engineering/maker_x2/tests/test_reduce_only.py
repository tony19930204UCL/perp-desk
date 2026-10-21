"""Exact witnessed entry-risk reduction, strict replay, and partial exit."""
import unittest
from decimal import Decimal as D, localcontext
from account_lifecycle import OfflineAccount
from account_ledger import replay_account
from entry_lifecycle import reduce_execution
from maker_exit_lab import X2Error

Q="0.21922127435988758019877575879333720780837993677502"
OLD_BUY="0.2192212743598875801987757588"

def account():
    return OfflineAccount("1000","50","5","5",2,"24",".0145",".0005")

def bar(ts, op, hi, lo, close):
    return {"ts":ts,"open":op,"high":hi,"low":lo,
            "close":close,"closed":True}

class ReduceOnlyControl(unittest.TestCase):
    def test_witness_entry_risk(self):
        # Derived from public forensic witness quantities; no market-bar claim.
        a=account()
        self.assertEqual(a.submit("FICTION","witness",0,"100","100","1",
                         ".25",2,Q,"X1",signal_vwap="99"),"PENDING")
        result=a.bar(bar(0,"100","100.9","99.9","100"),entry_qty=Q)
        self.assertEqual(result,"CLOSED_SL")
        events=[e for e in a.events if e["type"]=="fill"]
        self.assertEqual([e["side"] for e in events],["sell","buy"])
        self.assertEqual([D(e["qty"]) for e in events],[D(Q),D(Q)])
        self.assertEqual(a.ledger()["qty"],D(0))
        self.assertEqual(a.ledger()["reserved"],D(0))
        self.assertEqual(a.ledger(),replay_account("1000",a.events))
        self.assertTrue(any(v["type"]=="REDUCE_ONLY" and
                            v["requested"]==Q and v["executed"]==Q and
                            v["cancelled"]=="0" for v in a.entry.audit)
                        if a.entry else True)

    def test_true_external_excess(self):
        events=[{"type":"reserve","id":"reserve","seq":0,"ts":0,
                 "amount":"50","episode":"witness"},
                {"type":"fill","id":"sell","seq":1,"ts":60000,
                 "side":"sell","qty":Q,"price":"100","liquidity":"maker"},
                {"type":"fill","id":"buy","seq":2,"ts":60000,
                 "side":"buy","qty":str(D(Q)+D("0.0000000000000000000000000000001")),
                 "price":"101","liquidity":"taker"}]
        # Build actual excess in explicit Decimal50, never ambient Decimal28.
        with localcontext() as ctx:
            ctx.prec=50
            events[-1]["qty"]=str(D(Q)+D("0.0000000000000000000000000000001"))
        with self.assertRaisesRegex(X2Error,"over-close"):
            replay_account("1000",events)

    def test_requested_execution_cancel(self):
        audit=[]
        with localcontext() as ctx:
            ctx.prec=50
            req=D(Q)+D(".0000000000000000000000000001")
        executed,cancelled=reduce_execution(req,D(Q),audit,"TEST",60000)
        self.assertEqual(executed,D(Q))
        self.assertEqual(executed+cancelled,req)
        self.assertEqual(audit[-1]["outcome"],"REDUCED")
        executed,cancelled=reduce_execution(Q,D(0),audit,"TEST",60001)
        self.assertEqual(executed,D(0))
        self.assertEqual(cancelled,D(Q))
        self.assertEqual(audit[-1]["outcome"],"NO_POSITION")

if __name__=="__main__":
    unittest.main(verbosity=2)
