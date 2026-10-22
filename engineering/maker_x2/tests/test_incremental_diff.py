"""Strict event-by-event parity with independent original replay."""
import random
import unittest
from decimal import Decimal as D
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
