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


    def test_archive_plain(self):
        # A retained owner reference breaks independent history memory bounds.
        from account_lifecycle import _LedgerView
        a=acct()
        for i in range(3):
            ts=i*180000
            self.assertEqual(a.submit("FICTION","s"+str(i),ts,"100","100",
                             "1",".25",2,"2","X2"),"PENDING")
            a.bar(bar(ts//60000),entry_qty="2")
            a.bar(bar(ts//60000+1,high="101.2",op="101",low="98"))
            a.reset_b()
        self.assertEqual(len(a.b_history),3)
        def walk(obj):
            self.assertNotIsInstance(obj,(OfflineAccount,_LedgerView))
            if isinstance(obj,dict):
                for item in obj.values():
                    walk(item)
            elif isinstance(obj,(list,tuple,set)):
                for item in obj:
                    walk(item)
        walk(a.b_history)
        self.assertEqual(a.loss_streak,0)
        self.assertEqual(a.ledger()["cash"],D(150))
        self.assertTrue(all(replay_account("150",ep["events"])==
                            ep["ledger"] for ep in a.b_history))

    def test_replay_default_same(self):
        # Default A must have byte-identical complete serialized output.
        import json
        data=[bar(i,high="100.1") for i in range(300)]
        base=dict(symbol="FICTION",bars=data,funding=[],framework=1,
                  k=".25",ttl=2,exit_style="X2",account_config=conf(),
                  evaluation_start=290*60000,evaluation_end=300*60000,
                  signal_times=[290*60000])
        one=replay_cell(**base)
        two=replay_cell(**base,mode="A",entry_filter=None)
        self.assertEqual(json.dumps(one,sort_keys=True),
                         json.dumps(two,sort_keys=True))
        self.assertNotIn("research_mode",one)

    def test_b_independent_replay(self):
        # Each new B episode starts from the same fictional 150 cash.
        from decimal import localcontext
        data=[bar(i,high="100.1") for i in range(300)]
        for i in (290,294):
            data[i]["high"]="100.55"
            data[i+1]["open"]="101"
            data[i+1]["high"]="101.2"
        records=[{"signal_time":i*60000,"triggered":True,
                  "signal_high":D(100),"signal_close":D(100),
                  "atr14":D(1),"vwap60":D(99),
                  "signal_id":"s"+str(i),"signal_type":"S1"}
                 for i in (290,294)]
        out=replay_cell("FICTION",data,[],1,".25",2,"X2",conf(),
                 290*60000,299*60000,eligible=records,mode="B")
        self.assertTrue(out["cash_curve_not_investable"])
        self.assertEqual(out["equity_path"],[])
        self.assertIsNone(out["ledger"])
        self.assertEqual(len(out["independent_episodes"]),2)
        self.assertEqual(len(out["episodes"]),2)
        for ep in out["independent_episodes"]:
            self.assertEqual(D(ep["ledger"]["qty"]),D(0))
            self.assertEqual(D(ep["ledger"]["reserved"]),D(0))
            self.assertEqual(D(ep["ledger"]["fees"])>0,True)
            self.assertEqual(replay_account("150",ep["events"])["cash"],
                             D(ep["ledger"]["cash"]))
        self.assertEqual(out["counts"]["complete_episodes"],2)

    def test_h2_funnel(self):
        # Skip and warmup belong to signal denominators, never hidden.
        class Filter:
            def skip(self,ts):
                return (True,"H2_SKIP") if ts==290*60000 else (
                    False,"H2_WARMUP")
        data=[bar(i,high="100.1") for i in range(300)]
        records=[{"signal_time":i*60000,"triggered":True,
                  "signal_high":D(100),"signal_close":D(100),
                  "atr14":D(1),"vwap60":D(99),
                  "signal_id":"f"+str(i),"signal_type":"S1"}
                 for i in (290,291)]
        for matched in (None,[290*60000,291*60000]):
            out=replay_cell("FICTION",data,[],1,".25",2,"X2",
                  conf(),290*60000,300*60000,eligible=records,
                  signal_times=matched,entry_filter=Filter())
            self.assertEqual(out["signal_count"],2)
            self.assertEqual(out["funnel"]["H2_SKIP"],1)
            self.assertEqual(out["funnel"]["H2_WARMUP"],1)
            self.assertEqual(out["funnel_denoms"]["signals"],2)
            self.assertEqual(out["funnel_denoms"]["intents"],1)
