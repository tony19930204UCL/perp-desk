"""R4 structural synthetic controls. Never represent market performance."""
import json
import tempfile
import unittest
import io
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from maker_exit_lab import X2Error
from r4_report import (required, audit_matrix, report_family, money,
                       audit_result, PREREG)
from r4_entry import main, stage_args, run, generate_diag15
from r4_report import STRUCT, SYMS
from backtest_reports import atomic_json, sha256_file, canon
from account_ledger import replay_account
from r4_reflection import reflect


class Control(unittest.TestCase):
    @contextmanager
    def injected(self, root):
        # Tiny synthetic orchestration controls, never market evidence.
        cfg={"capital":"150", "per_trade_margin":"10", "leverage":"3",
             "daily_loss_limit":"10", "loss_streak_limit":3,
             "halt_hours":24, "liq_pct":".1", "liq_fee_pct":".01",
             "evaluation_start":1698796800000, "evaluation_end":1792368000000}
        atomic_json(root/"config.json",cfg)
        (root/"bars.json").write_text("[]")
        (root/"funding.json").write_text("[]")
        sources={s:{"bars":str(root/"bars.json"),
                    "funding":str(root/"funding.json")} for s in SYMS}
        atomic_json(root/"input.json",sources)
        blank={"episodes":[], "signal_count":7, "counts":{}, "events":[],
               "ledger":canon(replay_account("150",[]))}
        args=SimpleNamespace(config=str(root/"config.json"),
            input=str(root/"input.json"),output=str(root/"run"),stage_config=None)
        def prepared(path, dest):
            return {"count":1604160,"start_open_ms":1696118400000,
                    "end_open_ms":cfg["evaluation_end"]-60000,"bars":[],
                    "sha256":sha256_file(path),"gaps":[]}
        with ExitStack() as stack:
            mocks={"prepare_bars":prepared,
                   "read_funding":lambda p:{"events":[],"count":0,
                                             "sha256":sha256_file(p)},
                   "prepare_decisions":lambda *a:[],
                   "select_matched":lambda *a:{"selected_timestamps":[42]},
                   "classify":lambda *a:[], "SpeedFilter":lambda *a:None,
                   "replay":lambda *a:(blank,blank),
                   "replay_cell":lambda *a,**kw:dict(blank,signal_count=
                       3 if "signal_times" in kw else 7)}
            for name,value in mocks.items():
                stack.enter_context(patch("r4_entry."+name,side_effect=value))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            yield args,cfg,blank

    def test_diag_equivalence(self):
        with tempfile.TemporaryDirectory() as d, self.injected(Path(d)) as data:
            args,cfg,blank=data
            # Independent pre-extraction loop, with real audit and money.
            expected=[]
            for symbol in SYMS:
                for k,exit_style in STRUCT:
                    a=dict(blank,signal_count=7)
                    b=dict(blank,signal_count=3)
                    audit_result(a,cfg["capital"],None)
                    audit_result(b,cfg["capital"],None)
                    expected.append({"symbol":symbol,"cell_id":
                        f"{symbol}:15m:A:F0:baseline:{k}:2:{exit_style}",
                        "metrics":money(a,b),"diagnostic_only":True})
            direct=[]
            for symbol in SYMS:
                direct.extend(generate_diag15(symbol,[],[],cfg,
                    cfg["evaluation_start"],cfg["evaluation_end"],d))
            self.assertEqual(direct,expected)
            result=run(args)
            self.assertEqual(len(result["rows"]),144)
            path=Path(args.output)/"reports/r4-diag15.json"
            original=path.read_bytes()
            self.assertEqual(json.loads(original)["rows"],expected)
            argv=["--diag15","--config",args.config,"--input",args.input,
                  "--output",str(Path(d)/"standalone")]
            with patch("r4_entry.replay",side_effect=AssertionError("1m replay")):
                self.assertEqual(main(argv),0)
            new=Path(d)/"standalone/reports/r4-diag15.json"
            self.assertEqual(new.read_bytes(),original)
            self.assertIs(json.loads(original)["family_member"],False)
            self.assertNotEqual(main(argv),0)
            self.assertEqual(new.read_bytes(),original)
            argv[2]=str(Path(d)/"missing.json")
            argv[-1]=str(Path(d)/"missing-output")
            self.assertNotEqual(main(argv),0)
            self.assertFalse(Path(argv[-1]).exists())

    def test_diag_input_change(self):
        with tempfile.TemporaryDirectory() as d, self.injected(Path(d)) as data:
            args,cfg,blank=data
            argv=["--diag15","--config",args.config,"--input",args.input,
                  "--output",args.output]
            for name in ("input.json","bars.json","funding.json"):
                with self.subTest(name=name):
                    target=Path(d)/name
                    before=target.read_bytes()
                    def mutate(*a):
                        target.write_bytes(before+b" ")
                        return []
                    with patch("r4_entry.generate_diag15",side_effect=mutate):
                        self.assertNotEqual(main(argv),0)
                    self.assertFalse((Path(args.output)/
                                      "reports/r4-diag15.json").exists())
                    target.write_bytes(before)

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
            stages=[{"id":name,"source_head":"0"*40,
                     "input_sha256":"a"*64,"mode":"exec","argv":["python3"],
                     "cwd":d,"reports":[{"path":"reports/file.json","rows":1}]}
                    for name in ("r4-run","r4-verify","r4-reflection")]
            graph={"schema_version":1,"kind":"stage_relay",
                   "evidence_root":d,"bootstrap":{},"stages":stages}
            p.write_text(json.dumps(graph))
            digest=stage_args(p,"r4-run","0"*40,"a"*64)
            self.assertEqual(len(digest),64)
            self.assertEqual(stage_args(p,"r4-verify","0"*40,"a"*64),
                             digest)
            with self.assertRaisesRegex(X2Error,"mismatch"):
                stage_args(p,"r4-verify","1"*40,"a"*64)

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
