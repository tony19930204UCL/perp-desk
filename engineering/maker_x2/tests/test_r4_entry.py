"""R4 structural synthetic controls. Never represent market performance."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from maker_exit_lab import X2Error
from r4_report import (required, audit_matrix, report_family, money,
                       audit_result, PREREG)
from r4_entry import main, stage_args
from r4_reflection import reflect


class Control(unittest.TestCase):
    def test_exact_matrix(self):
        # Detect missing or duplicated 144 matrix members.
        self.assertEqual(len(required()),144)
        items={name:"deadbeef" for name in ("cells/x","cells/y")}
        rows=[{"cell_id":cid,"symbol":cid.split(":")[0],
               "strategy_path":"cells/x","matched_path":"cells/y",
               "strategy_sha256":"deadbeef","matched_sha256":"deadbeef",
               "metrics":{"n":0}} for cid in sorted(required())]
        self.assertTrue(audit_matrix(rows,items))
        with self.assertRaisesRegex(X2Error,"incomplete"):
            audit_matrix(rows[:-1],items)
        with self.assertRaisesRegex(X2Error,"duplicate"):
            audit_matrix(rows[:-1]+[rows[0]],items)
        rows[0]["strategy_sha256"]="corrupt"
        with self.assertRaisesRegex(X2Error,"artifact"):
            audit_matrix(rows,items)

    def test_fixed_96(self):
        # Detect dropping insufficient hypotheses from multiple testing.
        rows=[]
        from itertools import product
        for s,f,k,exit_style in product(("ETHUSDT","BTCUSDT"),
                                       ("F0","F1"),(".25",".50"),
                                       ("X1","X2","X3")):
            rows.append({"symbol":s,"mode":"B","filter":f,
                         "scenario":"baseline","k":k,"exit":exit_style,
                         "regime_episodes":[]})
        outcome=report_family(rows,0,86400000*40)
        self.assertEqual(len(outcome),96)
        self.assertTrue(all(x["p"]==1 and x["placeholder"]
                            and not x["bh96"] and not x["bonf96"]
                            for x in outcome.values()))

    def test_money_no_fake_profit(self):
        # Detect zero-trade outcomes represented as positive alpha.
        blank={"episodes":[],"signal_count":1,"counts":{}}
        row=money(blank,blank)
        self.assertEqual(row["n"],0)
        self.assertIsNone(row["net_bp"])
        self.assertIsNone(row["net_alpha_bp"])

    def test_replay_strict(self):
        # Detect accepting absent B archives as valid fully audited trade.
        with self.assertRaises(X2Error):
            audit_result({"research_mode":"B","ledger":None,
                          "cash_curve_not_investable":True,
                          "independent_episodes":[],"events":[]}, "150",None)

    def test_stage_bad_graph(self):
        # Detect arbitrary graph stage/source/input substitutions.
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"graph.json"
            p.write_text(json.dumps({"stage":"r4-run",
                "source_head":"0"*40,"input_sha256":"a"*64}))
            self.assertEqual(len(stage_args(p,"r4-run","0"*40,"a"*64)),64)
            with self.assertRaisesRegex(X2Error,"mismatch"):
                stage_args(p,"r4-verify","0"*40,"a"*64)

    def test_cli_nonzero(self):
        # Missing original data cannot produce fake complete 144 rows.
        with tempfile.TemporaryDirectory() as d:
            self.assertNotEqual(main(["--verify","--config","missing.json",
                "--input","missing.json","--output",d]),0)
            self.assertNotEqual(main(["--diag15","--config","missing.json",
                "--input","missing.json","--output",d]),0)
            with self.assertRaises((OSError,X2Error,KeyError)):
                reflect(d,"missing.json","missing.json","missing.json",
                        "missing.json",str(Path(d)/"reflect.json"))
