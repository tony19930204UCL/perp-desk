"""Synthetic data-adapter, actual-account and CLI unit controls."""
import csv
import gzip
import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_adapter import read_bars, read_funding
from market_replay import replay_cell
from backtest_controls import self_test
from backtest_reports import persist_run, verify_run
from backtest_entry import main
from maker_exit_lab import X2Error


def minutes(count=300):
    bars=[]
    for i in range(count):
        close=D("100")+D(i%7)/100
        bars.append({"ts":i*60000,"open":str(close),
                     "high":str(close+D("1")),"low":str(close-D("1")),
                     "close":str(close),"base_volume":"2",
                     "quote_volume":"200","closed":True})
    return bars


def cfg():
    return {"capital":"1000","per_trade_margin":"50","leverage":"5",
            "daily_loss_limit":"5","loss_streak_limit":2,"halt_hours":"24",
            "liq_pct":".0145","liq_fee_pct":".0005","entry_qty":"2"}


class Control(unittest.TestCase):
    def test_self_test_real_account_cash_and_mtm(self):
        self.assertTrue(self_test())
        self.assertEqual(main(["--self-test"]),0)

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
            self.assertEqual(funding["events"][0]["rate"],".01")
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
        self.assertEqual(a["counts"].get("attempts"),2)
        self.assertEqual(len(a["events"]),4)

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
