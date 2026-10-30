"""Synthetic data-adapter, actual-account and CLI unit controls."""
import csv
import gzip
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from decimal import Decimal as D, localcontext
from pathlib import Path

sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_adapter import read_bars, read_funding
from market_replay import replay_cell
from backtest_controls import self_test
from backtest_reports import persist_run, verify_run
from backtest_entry import main
import grid_runner
from scenario_rates import BASE, SENSITIVITY
from account_lifecycle import OfflineAccount
from account_ledger import replay_account
from maker_exit_lab import X2Error


def minutes(count=300):
    bars=[]
    for i in range(count):
        close=D("100")+D(i%7)/100
        bars.append({"ts":i*60000,"open":str(close),
                     "high":str(close+D(".30")),"low":str(close-D(".30")),
                     "close":str(close),"base_volume":"2",
                     "quote_volume":"200","closed":True})
    return bars


def cfg():
    return {"capital":"1000","per_trade_margin":"50","leverage":"5",
            "daily_loss_limit":"5","loss_streak_limit":2,"halt_hours":"24",
            "liq_pct":".0145","liq_fee_pct":".0005","entry_qty":"2"}


class Control(unittest.TestCase):
    def test_self_test_real_account_cash_and_mtm(self):
        # Do not recursively invoke CLI --self-test from inside its own
        # full discovery gate; CLI independently wraps this suite.
        self.assertTrue(self_test())

    def test_binance_csv_and_funding_mark_proxy_input(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            rows=[[60000*i,"100","101","99","100","2",
                   60000*i+59999,"200",0,0,0,"0"] for i in range(3)]
            (root/"k.json").write_text(json.dumps(rows),encoding="utf-8")
            result=read_bars(root/"k.json")
            self.assertEqual(result["count"],3)
            self.assertEqual(result["gaps"],[])
            self.assertEqual(result["bars"][-1]["ts"],120000)
            with (root/"k.csv").open("w",newline="",encoding="utf-8") as f:
                writer=csv.DictWriter(f,fieldnames=[
                    "open_time","open","high","low","close",
                    "volume","quote_volume"])
                writer.writeheader()
                for row in rows:
                    writer.writerow(dict(zip(writer.fieldnames,
                        [row[0],*row[1:6],row[7]])))
            self.assertEqual(read_bars(root/"k.csv")["bars"],
                             result["bars"])
            with (root/"f.csv").open("w",newline="",encoding="utf-8") as f:
                writer=csv.DictWriter(f,fieldnames=["funding_time_ms","rate"])
                writer.writeheader()
                writer.writerow({"funding_time_ms":60000,"rate":".01"})
            funding=read_funding(root/"f.csv")
            self.assertEqual(funding["events"][0]["mark"],None)
            self.assertEqual(D(funding["events"][0]["rate"]),D(".01"))
            with gzip.open(root/"f.json.gz","wt",encoding="utf-8") as f:
                json.dump([{"fundingTime":60000,
                            "fundingRate":".01","markPrice":"100"}],f)
            self.assertEqual(read_funding(root/"f.json.gz")["events"][0]
                             ["mark"],"100")

    def test_full_account_replay_and_matched_nontrigger_path(self):
        data=minutes()
        expected=[290*60000,291*60000]
        a=replay_cell("FICTION",data,[],1,".25",2,"X2",cfg(),
                      290*60000,300*60000,signal_times=expected)
        self.assertTrue(a["matched"])
        self.assertEqual(a["signal_count"],2)
        self.assertEqual(a["ledger"]["cash"],"1000")
        self.assertEqual(a["ledger"]["qty"],"0")
        self.assertEqual(a["counts"].get("attempts"),1)
        self.assertEqual(len(a["events"]),2)
        self.assertEqual(len(a["equity_path"]),1)
        self.assertFalse(a["equity_path"][0]["stale"])

    def test_delay2_moves_real_pending_arrival_not_feature_decision(self):
        data=minutes()
        selected=[290*60000]
        ordinary=replay_cell("FICTION",data,[],1,".25",2,"X2",cfg(),
                             290*60000,300*60000,
                             signal_times=selected,delay_minutes=0)
        delayed=replay_cell("FICTION",data,[],1,".25",2,"X2",cfg(),
                            290*60000,300*60000,
                            signal_times=selected,delay_minutes=2)
        one=next(e for e in ordinary["events"] if e["type"]=="reserve")
        two=next(e for e in delayed["events"] if e["type"]=="reserve")
        self.assertEqual(two["ts"]-one["ts"],2*60000)
        self.assertEqual(ordinary["signal_count"],1)
        self.assertEqual(delayed["signal_count"],1)

    def test_grid_has_48_isolated_cells_and_shared_sampler(self):
        # Fictional flat account: 1000 cash, zero encumbered margin,
        # zero position/costs, and equity = cash + unrealized.
        # A mocked replay must still satisfy the full ledger contract.
        fake={"signal_count":0,"counts":{},
              "ledger":{"cash":"1000","reserved":"0","available":"1000",
                        "qty":"0","basis":"0","gross":"0","fees":"0",
                        "funding_cost":"0","writeoff":"0","episodes":[],
                        "open_episode":None,"ids":set(),"settlements":set()},
              "account":{"equity":"1000","unrealized":"0"},
              "episodes":[]}
        ledger=fake["ledger"]
        self.assertEqual(D(ledger["available"]),
                         D(ledger["cash"])-D(ledger["reserved"]))
        self.assertEqual(D(fake["account"]["equity"]),
                         D(ledger["cash"])+D(fake["account"]["unrealized"]))
        self.assertEqual(D(ledger["qty"]),D("0"))
        self.assertEqual(D(ledger["reserved"]),D("0"))
        source={"ETHUSDT":{"bars":[],"funding":[]},
                "BTCUSDT":{"bars":[],"funding":[]}}
        cached={}
        def eligible(_,tf):
            return [{"framework_minutes":tf,"signal_time":j*tf*60000}
                    for j in range(3)]
        with patch.object(grid_runner,"eligible_decisions",eligible),patch.object(
                grid_runner,"replay_cell",return_value=fake) as replay:
            result=grid_runner.run_grid(source,cfg(),0,10**9,
                                         sample_count=3,eligible_cache=cached)
        self.assertEqual(result["cell_count"],48)
        self.assertEqual(set(cached),{("ETHUSDT",1),("ETHUSDT",15),
                                      ("BTCUSDT",1),("BTCUSDT",15)})
        self.assertEqual(replay.call_count,96)
        self.assertEqual(len(result["samplers"]),4)
        self.assertTrue(all(s["status"]=="READY"
                            for s in result["samplers"].values()))
        self.assertEqual(len({c["cell_id"] for c in result["cells"]}),48)

    def test_handderived_full_x1_flat_episode_reconciles(self):
        account=OfflineAccount("1000","50","5","5",2,"24",".0145",".0005")
        self.assertEqual(account.submit("FICTION","one",0,"100","100",
                          "1",".25",2,"2","X1",signal_vwap="99"),
                         "PENDING")
        first={"ts":0,"open":"100","high":"100.27005",
               "low":"99","close":"100","closed":True}
        second={"ts":60000,"open":"100","high":"100.1",
                "low":"98.9","close":"99","closed":True}
        account.bar(first,entry_qty="2")
        self.assertEqual(account.bar(second,maker_fill=True),"CLOSED")
        independent=replay_account("1000",account.events)
        self.assertEqual(independent["gross"],D("2.5"))
        self.assertEqual(independent["fees"],D(".0797"))
        self.assertEqual(independent["cash"],D("1002.4203"))
        self.assertEqual(independent["reserved"],D("0"))
        self.assertEqual(independent["qty"],D("0"))
        self.assertEqual(independent["episodes"][-1]["net"],D("2.4203"))
        self.assertFalse(account.snapshot()["slot"])
        self.assertEqual([e["type"] for e in account.events].count("fill"),2)

    def test_baseline_differential_and_1_5x_real_account_cash(self):
        def execute(rates=None):
            a=OfflineAccount("1000","50","5","5",2,"24",".0145",".0005",
                             rates=rates)
            self.assertEqual(a.submit("FICTION","case",0,"100","100","1",
                             ".25",2,"2","X1",signal_vwap="99"),"PENDING")
            a.bar({"ts":0,"open":"100","high":"100.27005",
                   "low":"99","close":"100","closed":True},entry_qty="2")
            a.bar({"ts":60000,"open":"100","high":"100.1",
                   "low":"98.9","close":"99","closed":True},
                  maker_fill=True)
            return a
        default=execute()
        explicit=execute(BASE)
        stressed=execute(SENSITIVITY)
        self.assertEqual(default.events,explicit.events)
        self.assertEqual(default.ledger(),explicit.ledger())
        self.assertEqual(default.ledger()["cash"],D("1002.4203"))
        self.assertEqual(stressed.ledger()["gross"],D("2.5"))
        self.assertEqual(stressed.ledger()["fees"],D(".11955"))
        self.assertEqual(stressed.ledger()["cash"],D("1002.38045"))
        self.assertEqual(stressed.ledger()["qty"],D("0"))

    def test_cost_sensitivity_changes_actual_sl_slippage_and_fees(self):
        def run(rates=None):
            a=OfflineAccount("1000","50","5","5",2,"24",
                             ".0145",".0005",rates=rates)
            a.submit("FICTION","risk",0,"100","100","1",
                     ".25",2,"2","X2")
            a.bar({"ts":0,"open":"100","high":"100.27005",
                   "low":"99","close":"100","closed":True},entry_qty="2")
            self.assertEqual(a.bar({"ts":60000,"open":"101","high":"101.2",
                         "low":"98","close":"100","closed":True}),
                             "CLOSED_SL")
            return a.ledger()
        base=run()
        stress=run(SENSITIVITY)
        self.assertEqual(base["cash"],D("998.3184798"))
        self.assertEqual(stress["cash"],D("998.22770455"))
        self.assertEqual(stress["gross"],D("-1.5606"))
        self.assertEqual(stress["fees"],D(".21169545"))
        self.assertEqual(stress["reserved"],D("0"))
        self.assertEqual(stress["qty"],D("0"))

    def test_explicit_margin_leverage_sets_actual_candidate_notional(self):
        history=minutes(300)
        record={"framework_minutes":1,"signal_time":290*60000,
                "triggered":True,"signal_high":D("100"),
                "signal_close":D("100"),"atr14":D("1"),
                "vwap60":D("99"),"signal_id":"sized",
                "signal_type":"S1"}
        out=replay_cell("FICTION",history,[],1,".25",2,"X2",
                        cfg(),290*60000,294*60000,eligible=[record])
        sells=[e for e in out["events"]
               if e["type"]=="fill" and e["side"]=="sell"]
        self.assertEqual(len(sells),1)
        self.assertEqual(D(sells[0]["price"]),D("100.25"))
        with localcontext() as ctx:
            ctx.prec=50
            expected=D("250")/D("100.25")
        self.assertEqual(D(sells[0]["qty"]),expected)
        self.assertEqual(D(out["ledger"]["fees"])>=D("0"),True)

    def test_samebar_counterfactual_does_not_overwrite_primary_liq(self):
        observed=minutes(300)
        observed[290]={"ts":290*60000,"open":"100","high":"100.55",
                       "low":"99.9","close":"100",
                       "base_volume":"2","quote_volume":"200","closed":True}
        observed[291]={"ts":291*60000,"open":"101","high":"102",
                       "low":"98","close":"100",
                       "base_volume":"2","quote_volume":"200","closed":True}
        out=replay_cell("FICTION",observed,[],1,".25",2,"X1",
                        cfg(),290*60000,295*60000,
                        signal_times=[290*60000])
        self.assertEqual(out["counts"].get("samebar_risk_profit_conflict"),1)
        self.assertGreater(D(out["counts"]["favorable_order_upper_bound_usd"]),
                           D("0"))
        self.assertEqual(D(out["ledger"]["qty"]),D("0"))
        self.assertGreater(D(out["ledger"]["writeoff"]),D("0"))
        self.assertEqual(out["episodes"][0]["exit_type"],"LIQ")

    def test_real_48_grid_baseline_and_full_placebo(self):
        # No patched signal, grid, sampler or account: 290 15m closed
        # buckets provide one eligible 15m matched time per symbol.
        data=minutes(15*290)
        dataset={symbol:{"bars":data,"funding":[]}
                 for symbol in ("ETHUSDT","BTCUSDT")}
        report=grid_runner.run_grid(dataset,cfg(),289*15*60000,
                  291*15*60000,sample_count=1)
        self.assertEqual(report["cell_count"],48)
        self.assertEqual(len(report["samplers"]),4)
        self.assertTrue(all(source["status"]=="READY"
                            for source in report["samplers"].values()))
        self.assertTrue(all(cell["matched"] is not None
                            for cell in report["cells"]))
        self.assertTrue(all("ledger" in cell["strategy"] and
                            "ledger" in cell["matched"]
                            for cell in report["cells"]))

    def test_reports_verify_raw_artifact_tampering(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/"output"
            fake={"cells":[{"cell_id":"fiction","strategy":{"events":[]},
                            "matched":None,"metrics":{},
                            "matched_status":"INSUFFICIENT_UNIVERSE"}],
                  "cell_count":1,"samplers":{},
                  "classification":"EXPLORATORY_NOT_ACCEPTED"}
            persist_run(root,{"fictional":True},fake)
            self.assertEqual(verify_run(root)["status"],"CHECKSUMS_VERIFIED")
            (root/"results.json").write_text("{}",encoding="utf-8")
            with self.assertRaises(X2Error):
                verify_run(root)


if __name__=="__main__":
    unittest.main()
