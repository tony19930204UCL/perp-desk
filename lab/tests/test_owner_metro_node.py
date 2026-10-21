"""Synthetic canonical ledger gate/retest ordering and live handle checks."""
import json
import tempfile
import unittest
from pathlib import Path
from owner_metro_nodes import derive
from owner_metro_model import read_metro


class NodeProof(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.proc=self.root/"proc"
        (self.proc/"1234").mkdir(parents=True)
        fields=["S"]+["0"]*50
        fields[19]="2000"
        (self.proc/"1234/stat").write_text("1234 (node owner) "+" ".join(fields))
        self.old={"id":"C-1","state":"DROPPED","reason":"prior attempt failed"}
        self.now={"id":"C-2","state":"DISPATCHED",
                  "handle":{"kind":"pid","pid":1234,"start":2000}}
        self.gate={"id":"C-3","state":"BLOCKED","blocked_by":"user:decision",
                   "text":"approval required"}
        self.bind=[{"route":"collectors","step":1,"items":["C-1","C-2"]}]
        self.rows=[self.old,self.now,self.gate]

    def state(self, rows=None, binding=None):
        return derive(str(self.root),self.rows if rows is None else rows,
                      self.bind if binding is None else binding,0,
                      proc_root=str(self.proc))

    def test_retest(self):
        overrides,errors=self.state()
        step=overrides[("collectors",1)]
        self.assertEqual(step["state"],"retest")
        self.assertIn("prior attempt failed",step["note"])
        self.assertFalse(errors)

    def test_active_failed(self):
        self.old["state"]="VERIFIED"
        self.assertEqual(self.state()[0][("collectors",1)]["state"],"active")
        for kind in ("manual","pid"):
            self.now["handle"]["kind"]=kind
            if kind=="pid":self.now["handle"]["start"]=1999
            self.assertEqual(self.state()[0][("collectors",1)]["state"],"failed")

    def test_gate(self):
        self.bind[0]["gate"]="C-3"
        self.assertEqual(self.state()[0][("collectors",1)]["state"],"user")
        self.assertEqual(self.state()[0][("collectors",1)]["item"],"C-3")

    def test_cron(self):
        (self.root/"cron").mkdir()
        file=self.root/"cron/jobs.json"
        self.now["handle"]={"kind":"cron","ref":"JOB_A"}
        self.old["state"]="VERIFIED"
        for enabled,expected in ((True,"active"),(False,"failed")):
            file.write_text(json.dumps({"jobs":[{"id":"JOB_A","enabled":enabled}]}))
            self.assertEqual(self.state()[0][("collectors",1)]["state"],expected)
        file.unlink()
        self.assertEqual(self.state()[0][("collectors",1)]["state"],"failed")

    def test_missing_invalid(self):
        overrides,warnings=self.state(None,[{"route":"maker","step":True,"items":["C-2"]},
                                            {"route":"maker","step":0,"items":["ITEM_A"]},
                                            {"route":"maker","step":0,"items":["C-9"]}])
        self.assertEqual(overrides,{})
        self.assertTrue(warnings)
        self.assertFalse(self.state(rows=None,binding=[])[0])
        self.assertTrue(derive(str(self.root),None,self.bind,0)[1])

    def test_verified_not_done(self):
        self.now["state"]="VERIFIED"
        self.assertFalse(self.state()[0])

    def test_pending_fallback(self):
        (self.root/"commitments").mkdir()
        (self.root/"commitments/ledger.json").write_text(json.dumps(
            {"next":4,"items":self.rows}))
        m=read_metro(str(self.root),now_ms=1792625361639,
                     proc_root=str(self.proc))
        self.assertEqual(m["kpis"]["pending_decisions"],1)
        self.assertEqual(m["kpis"]["pending_source"],"ledger")
