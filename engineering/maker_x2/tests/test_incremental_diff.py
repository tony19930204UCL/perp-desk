"""Strict event-by-event parity with independent original replay."""
import random
import unittest
from decimal import Decimal as D, getcontext, localcontext
from account_lifecycle import OfflineAccount
from account_ledger import replay_account
from maker_exit_lab import X2Error

def new():
    return OfflineAccount("1000","50","5","5",2,"24",".0145",".0005")

def append(a,typ,**data):
    a._emit(typ,"e"+str(len(a.events)),len(a.events)*60000,**data)

def check(a):
    got=a.ledger()
    want=replay_account(a.capital,a.events,rates=a.rates)
    assert set(got)==set(want)
    assert dict(got.items())==want
    assert got==want and not(got!=want)
    assert want==got
    for key in want:
        assert type(got[key]) is type(want[key]),key
        assert got[key]==want[key],key
    return got

class Control(unittest.TestCase):
    def test_fixed_events(self):
        a=new()
        check(a)
        append(a,"reserve",amount="50",episode="one");check(a)
        append(a,"fill",side="sell",qty="2",price="100",
               liquidity="maker");check(a)
        append(a,"funding",settlement_id="f1",mark="101",rate=".001")
        check(a)
        append(a,"fill",side="buy",qty="1",price="99",
               liquidity="maker");check(a)
        append(a,"release",amount="25");check(a)
        append(a,"liquidation",qty="1",margin_writeoff="25",
               fee=".05",reason="LIQ_PROXY");check(a)
        self.assertEqual(a.ledger()["qty"],D(0))
        restored=OfflineAccount.recover(a.checkpoint())
        self.assertEqual(dict(restored.ledger().items()),check(a))

    def test_random_events(self):
        rng=random.Random(20261020)
        for case in range(12):
            a=new()
            append(a,"reserve",amount="50",episode=str(case));check(a)
            remaining=D(0)
            for i in range(20):
                if remaining==0 or rng.randrange(4)==0:
                    q=D(rng.randint(1,5))/D(10)
                    append(a,"fill",side="sell",qty=str(q),
                           price=str(100+rng.randrange(10)),
                           liquidity="maker");remaining+=q
                elif rng.randrange(3)==0:
                    append(a,"funding",settlement_id="f"+str(case)+":"+str(i),
                           mark="100",rate=".001")
                else:
                    q=remaining if rng.randrange(3)==0 else remaining/D(2)
                    append(a,"fill",side="buy",qty=str(q),
                           price="99",liquidity="taker");remaining-=q
                check(a)


    def test_available_prec50(self):
        from decimal import getcontext, localcontext
        qty="0.21922127435988758019877575879333720780837993677502"
        a=new()
        ambient=getcontext().prec
        self.assertEqual(ambient,28)
        append(a,"reserve",amount="50",episode="precision")
        check(a)
        append(a,"fill",side="sell",qty=qty,price="100.25",
               liquidity="maker")
        check(a)
        append(a,"fill",side="buy",qty=qty,price="100.8716703",
               liquidity="taker")
        check(a)
        append(a,"release",amount="50")
        final=check(a)
        self.assertGreater(len(str(final["available"])),40)
        self.assertEqual(final["qty"],D(0))
        self.assertEqual(getcontext().prec,ambient)

    def test_random_prec50(self):
        from decimal import getcontext, localcontext
        rng=random.Random(20261022)
        ambient=getcontext().prec
        self.assertEqual(ambient,28)
        for case in range(12):
            a=new()
            append(a,"reserve",amount="50",episode=str(case))
            check(a)
            with localcontext() as ctx:
                ctx.prec=50
                qty=D(rng.randrange(1,7))/D(7)
                price=D(700+rng.randrange(40))/D(7)
                part=qty/D(3)
                rest=qty-part
            append(a,"fill",side="sell",qty=str(qty),
                   price=str(price),liquidity="maker")
            check(a)
            append(a,"funding",settlement_id="s"+str(case),
                   mark=str(price),rate=".00012345678901234567890123456789")
            check(a)
            append(a,"fill",side="buy",qty=str(part),
                   price=str(price),liquidity="maker")
            check(a)
            if case%2:
                append(a,"fill",side="buy",qty=str(rest),
                       price=str(price),liquidity="taker")
                check(a)
                append(a,"release",amount="50")
            else:
                append(a,"release",amount="25")
                check(a)
                append(a,"liquidation",qty=str(rest),
                       margin_writeoff="25",fee=".03125",
                       reason="LIQ_PROXY")
            check(a)
        self.assertEqual(getcontext().prec,ambient)


    def test_x2_odd_half(self):
        qty="0.27404438520735813089201545257989788518969205534561"
        ambient=getcontext().prec
        self.assertEqual(ambient,28)
        a=new()
        self.assertEqual(a.submit("FICTION","odd-half",0,"100","100",
                                  "1",".25",2,qty,"X2"),"PENDING")
        first={"ts":0,"open":"100","high":"100.27005",
               "low":"99.9","close":"100","closed":True}
        self.assertEqual(a.bar(first,entry_qty=qty),"FILLED")
        check(a)
        self.assertIsNotNone(a.exit)
        tp1=a.exit.core.orders["tp1"]
        tp2=a.exit.core.orders["tp2"]
        with localcontext() as ctx:
            ctx.prec=80
            self.assertEqual(tp1+tp2,D(qty))
            self.assertLessEqual(len(tp1.as_tuple().digits),50)
        self.assertEqual(a.bar({"ts":60000,"open":"100","high":"100",
                   "low":"99.5","close":"99.8","closed":True},
                   maker_fill=True),"OPEN")
        check(a)
        self.assertEqual(a.bar({"ts":120000,"open":"99.4","high":"99.5",
                   "low":"99","close":"99.2","closed":True},
                   maker_fill=True),"CLOSED")
        final=check(a)
        self.assertEqual(final["qty"],D(0))
        self.assertEqual(final["reserved"],D(0))
        self.assertEqual(len(final["episodes"]),1)
        self.assertEqual(getcontext().prec,ambient)

    def test_liq_after_tp1(self):
        q="0.25056903781042036088205670645792476038217147602613"
        a=OfflineAccount("150","10","50","10",2,"24",".0145",".0005")
        bar=lambda ts,o,h,l,c:{"ts":ts,"open":o,"high":h,"low":l,
                               "close":c,"closed":True}
        self.assertEqual(a.submit("FICTION","liq",0,"100","100","1",
                                  ".25",2,q,"X2"),"PENDING")
        self.assertEqual(a.bar(bar(0,"100","100.27005","99.9","100"),
                               entry_qty=q),"FILLED")
        self.assertEqual(a.bar(bar(60000,"100","100","99.5","99.8"),
                               maker_fill=True),"OPEN")
        check(a)
        self.assertEqual(a.bar(bar(120000,"100","103","99.8","102")),
                         "LIQ_PROXY")
        done=check(a)
        self.assertEqual(done["qty"],D(0))
        self.assertEqual(done["reserved"],D(0))
        self.assertEqual(len(done["episodes"]),1)

    def test_invalid_sequences(self):
        base=new()
        append(base,"reserve",amount="50",episode="s");check(base)
        append(base,"fill",side="sell",qty="1",price="100",
               liquidity="maker");check(base)
        for change in (
            {"id":"e1"},
            {"seq":0},
            {"ts":-1},
            {"qty":"2","side":"buy","id":"excess"},
        ):
            events=[dict(x) for x in base.events]
            extra={"type":"fill","id":"test","seq":2,"ts":120000,
                   "side":"buy","qty":"1","price":"100","liquidity":"maker"}
            extra.update(change)
            events.append(extra)
            from copy import deepcopy
            a=deepcopy(base)
            a.events=events
            with self.assertRaises(X2Error) as expected:
                replay_account(a.capital,a.events,rates=a.rates)
            with self.assertRaisesRegex(X2Error,
                    "^"+__import__("re").escape(str(expected.exception))+"$"):
                a.ledger()
