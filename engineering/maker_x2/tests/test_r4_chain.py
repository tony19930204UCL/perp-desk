"""Synthetic CLI injection tests; these are never historical market evidence."""
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stdout, redirect_stderr
from decimal import Decimal as D, localcontext
from pathlib import Path
from unittest.mock import patch
import r4_entry as entry
import r4_reflection as reflection
from backtest_reports import atomic_json, sha256_file
from r4_report import SYMS, money, audit_result
from test_fixed_notional import conf, bar


class Chain(unittest.TestCase):
    @contextmanager
    def fixture(self, folder):
        root=Path(folder)
        start=1698796800000
        cfg=dict(conf(),evaluation_start=start,evaluation_end=1792368000000)
        atomic_json(root/"cfg.json",cfg)
        (root/"bars").write_text("synthetic bars only")
        (root/"fund").write_text("synthetic funding only")
        atomic_json(root/"input.json",{s:{"bars":str(root/"bars"),
                    "funding":str(root/"fund")} for s in SYMS})
        stages=[{"id":s,"source_head":entry.git_head(),
                 "input_sha256":sha256_file(root/"input.json"),"mode":"exec",
                 "argv":["python3"],"cwd":str(root),
                 "reports":[{"path":"reports/"+name,"rows":n}]}
                for s,name,n in (("r4-run","r4-matrix.json",144),
                    ("r4-verify","r4-verify.json",1),
                    ("r4-reflection","r4-reflection.json",1))]
        atomic_json(root/"graph.json",{"schema_version":1,"kind":"stage_relay",
                    "evidence_root":str(root),"bootstrap":{},"stages":stages})
        bars=[dict(bar(i,high="100.1"),ts=start+i*60000) for i in range(10)]
        bars[1].update(high="100.55")
        bars[2].update(open="101",high="102",low="98")
        eligible=[{"signal_time":start+60000,"triggered":True,
            "signal_high":D(100),"signal_close":D(100),"atr14":D(1),
            "vwap60":D(99),"signal_id":"synthetic","signal_type":"S1"}]
        base=["--config",str(root/"cfg.json"),"--input",str(root/"input.json"),
              "--output",str(root/"out"),"--stage-config",str(root/"graph.json")]
        class Pass:
            def skip(self, ts):
                return False,"H2_PASS"
        with ExitStack() as stack:
            mocks={"cap_memory":lambda:None,
                "prepare_bars":lambda p,d:{"count":1604160,
                    "start_open_ms":1696118400000,"end_open_ms":cfg["evaluation_end"]-60000,
                    "bars":bars,"sha256":sha256_file(p),"gaps":[]},
                "read_funding":lambda p:{"events":[],"count":0,"sha256":sha256_file(p)},
                "prepare_decisions":lambda *a:eligible,
                "select_matched":lambda *a:{"status":"READY",
                    "selected_timestamps":[start+60000]},
                "classify":lambda *a:{start//86400000:{"regime":"RANGE"}},
                "SpeedFilter":lambda *a:Pass()}
            for name,value in mocks.items():
                stack.enter_context(patch.object(entry,name,side_effect=value))
            stack.enter_context(patch.object(reflection,"cap_memory",return_value=None))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            yield root,base

    def accept(self, root, stage, report, count):
        graph=json.loads((root/"graph.json").read_text())
        identity={"stage":stage,"source_head":graph["stages"][0]["source_head"],
            "input_sha256":graph["stages"][0]["input_sha256"],
            "graph_sha256":sha256_file(root/"graph.json")}
        rec=root/(stage+".receipt")
        acc=root/(stage+".acceptance")
        atomic_json(rec,dict(identity,owner={"pid":1,"start_ticks":1,
            "boot_id":"synthetic","token":"synthetic"},exit_code=0,
            outcome="SUCCEEDED",reason="synthetic fixture",output_count=1,
            reports=[{"path":"reports/"+report,"sha256":sha256_file(
                root/"out/reports"/report),"rows":count}]))
        atomic_json(acc,dict(identity,receipt_sha256=sha256_file(rec),
                            decision="ACCEPTED",reviewer="synthetic fixture"))
        return ["--receipt",str(rec),"--acceptance",str(acc)]

    def test_cli_chain(self):
        with tempfile.TemporaryDirectory() as d, self.fixture(d) as (root,base):
            self.assertEqual(entry.main(["--run",*base]),0)
            prior=self.accept(root,"r4-run","r4-matrix.json",144)
            self.assertEqual(entry.main(["--verify",*base,*prior]),0)
            audit=json.loads((root/"out/reports/r4-verify.json").read_text())
            self.assertEqual(audit["acceptance_sha256"],sha256_file(prior[-1]))
            prior=self.accept(root,"r4-verify","r4-verify.json",1)
            report=root/"out/reports/r4-reflection.json"
            argv=[*base,*prior,"--report",str(report)]
            self.assertEqual(reflection.main(argv),0)
            decision=json.loads(report.read_text())["rows"][0]["decision"]
            self.assertEqual(decision["candidates"],[])
            self.assertTrue(decision["h2"])
            self.assertTrue(all(x["judgement"]=="INSUFFICIENT"
                for x in decision["h1"]["measures"]))
            self.assertEqual(reflection.main(argv),1)
            diag=list(base)
            diag[diag.index("--output")+1]=str(root/"diag")
            self.assertEqual(entry.main(["--diag15",*diag]),0)
            rows=json.loads((root/"diag/reports/r4-diag15.json").read_text())["rows"]
            self.assertEqual(len(rows),12)
            for row in rows:
                f=row["metrics"]["funnel"]
                self.assertIsNotNone(f)
                self.assertEqual(sum(f["terminal_counts"].values())+f["unprocessed"],1)

    def test_corruption_nonzero(self):
        for kind in ("acceptance","input","metrics","episodes","family","escape"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d, self.fixture(d) as (root,base):
                self.assertEqual(entry.main(["--run",*base]),0)
                prior=self.accept(root,"r4-run","r4-matrix.json",144)
                manifest=root/"out/manifest.json"
                doc=json.loads(manifest.read_text())
                matrix=root/"out/reports/r4-matrix.json"
                rows=json.loads(matrix.read_text())
                if kind=="acceptance":
                    atomic_json(Path(prior[-1]),{"decision":"ACCEPTED"})
                elif kind=="input":
                    (root/"bars").write_text("changed actual bytes")
                else:
                    if kind=="metrics":
                        rows["rows"][0]["metrics"]["net_bp"]="999"
                        doc["artifacts"]["reports/r4-matrix.json"]=atomic_json(matrix,rows)
                    elif kind=="episodes":
                        row=rows["rows"][0]
                        path=root/"out"/row["strategy_path"]
                        raw=json.loads(path.read_text())
                        raw["episodes"][0]["net"]="999"
                        digest=atomic_json(path,raw)
                        row["strategy_sha256"]=digest
                        doc["artifacts"][row["strategy_path"]]=digest
                        doc["artifacts"]["reports/r4-matrix.json"]=atomic_json(matrix,rows)
                    elif kind=="family":
                        path=root/"out/reports/r4-regime.json"
                        raw=json.loads(path.read_text())
                        next(iter(raw["family_m96"].values()))["bh96"]=True
                        doc["artifacts"]["reports/r4-regime.json"]=atomic_json(path,raw)
                    else:
                        doc["artifacts"]["reports/../../bars"]=sha256_file(root/"bars")
                    atomic_json(manifest,doc)
                    prior=self.accept(root,"r4-run","r4-matrix.json",144)
                self.assertEqual(entry.main(["--verify",*base,*prior]),1)

    def test_decimal_boundary(self):
        raw={"episodes":[{"entry_notional":"3","gross":"1","net":"1",
                           "exit_type":"TP"}],"signal_count":1,"counts":{}}
        expected=money(raw,raw)
        with localcontext() as ctx:
            ctx.prec=6
            self.assertEqual(money(raw,raw),expected)
            self.assertEqual(ctx.prec,6)

class Revision(unittest.TestCase):
    def candidates(self, counts):
        # Inject only orchestration/measurement boundaries; run real gates.
        from copy import deepcopy
        from r4_report import PREREG
        measures=[{"cell_id":f"ETHUSDT:B:F0:{s}:.25:2:X1",
            "regime":"RANGE", "strategy":{"n":n,"net_bp":"20","pf":"2"},
            "net_alpha_bp":"10"} for s,n in zip(
                ("baseline","delay2","cost1_5x"),counts)]
        head="0"*40
        key="ETHUSDT:F0:.25:X1:RANGE"
        with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
            root=Path(d)
            cfg=root/"config.json"
            inp=root/"input.json"
            atomic_json(cfg,{})
            atomic_json(inp,{})
            atomic_json(root/"manifest.json",{"source_head":head,
                "input_sha256":sha256_file(inp),"config_sha256":sha256_file(cfg),
                "graph_sha256":"graph","artifacts":{}})
            atomic_json(root/"reports/r4-matrix.json",{"rows":[]})
            family={str(i):{} for i in range(95)}
            family[key]={"bh96":True,"bonf96":True}
            atomic_json(root/"reports/r4-regime.json",{"prereg_sha256":PREREG,
                "family_m96":family,"daily":{}})
            audit={"stage":"r4-verify","source_head":head,
                "input_sha256":sha256_file(inp),"graph_sha256":"graph",
                "rows":[{"verified":144}]}
            atomic_json(root/"reports/r4-verify.json",audit)
            mocks={"git_head":head,"stage_args":"graph","audit_matrix":True,
                "receipt":{},"verify":audit,"filter_effects":[],
                "regime_measures":deepcopy(measures)}
            for name,value in mocks.items():
                stack.enter_context(patch.object(reflection,name,return_value=value))
            report=reflection.reflect(root,cfg,inp,"graph","receipt",
                                      root/"reflection.json","acceptance")
            return report["rows"][0]["decision"]["candidates"]

    def check_boundary(self, arm):
        self.assertEqual(self.candidates([100,100,100]),
                         ["ETHUSDT:F0:.25:X1:RANGE"])
        counts=[100,100,100]
        counts[arm]=99
        self.assertEqual(self.candidates(counts),[],
                         f"n=99 {('baseline','delay2','cost1_5x')[arm]} must not qualify")

    def test_base_n100(self):
        self.check_boundary(0)

    def test_delay_n100(self):
        self.check_boundary(1)

    def test_cost_n100(self):
        self.check_boundary(2)

    def test_episode_forgery(self):
        from copy import deepcopy
        from maker_exit_lab import X2Error
        from market_replay import replay_cell
        from r4_report import entry_notionals
        bars=[bar(0,high="100.1"),bar(1,high="100.55"),
              dict(bar(2,high="102",low="98"),open="101")]
        eligible=[{"signal_time":60000,"triggered":True,
            "signal_high":D(100),"signal_close":D(100),"atr14":D(1),
            "vwap60":D(99),"signal_id":"synthetic","signal_type":"S1"}]
        raw=entry_notionals(replay_cell("FICTION",bars,[],1,".25",2,"X2",
            conf(),0,180000,eligible=eligible))
        self.assertEqual(len(raw["episodes"]),1)
        audit_result(raw,"150",None)
        for field,value in (("entry_notional","999"),("exit_type","FORGED"),
                            ("net","999")):
            with self.subTest(field=field):
                forged=deepcopy(raw)
                forged["episodes"][0][field]=value
                with self.assertRaisesRegex(X2Error,
                        "financial episode differs from ledger events"):
                    audit_result(forged,"150",None)

    def test_receipt_report_hash(self):
        from maker_exit_lab import X2Error
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            report=root/"report.json"
            atomic_json(report,{"synthetic":True})
            identity={"stage":"r4-verify","source_head":"0"*40,
                      "input_sha256":"a"*64,"graph_sha256":"b"*64}
            rec=root/"receipt.json"
            acc=root/"acceptance.json"
            value=dict(identity,owner={"pid":1,"start_ticks":1,
                "boot_id":"synthetic","token":"synthetic"},exit_code=0,
                outcome="SUCCEEDED",reports=[{"path":report.name,
                    "sha256":sha256_file(report),"rows":1}])
            def write_chain():
                atomic_json(rec,value)
                atomic_json(acc,dict(identity,receipt_sha256=sha256_file(rec),
                    decision="ACCEPTED",reviewer="synthetic"))
            def validate():
                return entry.receipt(rec,acc,"r4-verify","0"*40,
                                     "a"*64,"b"*64,report)
            write_chain()
            self.assertIn("receipt_sha256",validate())
            value["reports"][0]["sha256"]="f"*64
            write_chain()  # Acceptance still binds actual receipt bytes.
            with self.assertRaisesRegex(X2Error,"receipt report identity mismatch"):
                validate()

    def test_command_graph(self):
        from maker_exit_lab import X2Error
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            stages=[{"id":s,"source_head":"0"*40,"input_sha256":"a"*64,
                "mode":"exec","command":["python3","r4_entry.py"],
                "lease_seconds":300,"reports":[{"path":"reports/file.json"}]}
                for s in entry.STAGES]
            graph={"schema_version":1,"kind":"stage_relay",
                   "evidence_root":str(root),"bootstrap":{},"stages":stages}
            path=root/"graph.json"
            for cwd in (None,str(root),"relative",42):
                with self.subTest(cwd=cwd):
                    if cwd is not None:
                        stages[0]["cwd"]=cwd
                    stages[0]["argv"]="ignored fallback"
                    atomic_json(path,graph)
                    if cwd is None or cwd==str(root):
                        self.assertEqual(entry.stage_args(path,"r4-run","0"*40,
                            "a"*64),sha256_file(path))
                    else:
                        with self.assertRaisesRegex(X2Error,"mismatch"):
                            entry.stage_args(path,"r4-run","0"*40,"a"*64)
            # A command-only installed graph, without argv or cwd.
            stages[0].pop("argv")
            stages[0].pop("cwd")
            atomic_json(path,graph)
            self.assertEqual(entry.stage_args(path,"r4-run","0"*40,"a"*64),
                             sha256_file(path))
