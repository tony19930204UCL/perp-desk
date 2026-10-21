"""Heartbeat and cron-managed PID contract tests; synthetic isolated profile."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from owner_metro_sources import load, processes_managed
from owner_metro_model import read_metro


class MetroBind(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/"owner_metro").mkdir()
        self.hb=self.root/"owner_metro/hb.json"
        self.fixture=json.loads((Path(__file__).parent/"fixtures/owner_metro_hb.json").read_text())
        self.now=self.fixture["ts_ms"]
        self.binding={"schema_version":1,
          "sources":{"heartbeat":"owner_metro/hb.json"}}
        self.save(self.fixture)

    def save(self, heartbeat):
        self.hb.write_text(json.dumps(heartbeat))
        (self.root/"owner_metro/bindings.json").write_text(json.dumps(self.binding))

    def test_heartbeat(self):
        m=read_metro(str(self.root),now_ms=self.now)
        self.assertEqual(m["kpis"]["liquidations"],2806)
        self.assertEqual(m["heartbeat"]["at"],self.fixture["ts_iso"])
        for mode in ("ms","iso"):
            v=dict(self.fixture)
            v.pop("ts_iso" if mode=="ms" else "ts_ms")
            self.save(v)
            self.assertEqual(read_metro(str(self.root),now_ms=self.now)
                             ["kpis"]["liquidations"],2806)
        for bad in (True,"1792625361639",float("nan"),1,4102444800000):
            v=dict(self.fixture)
            v.pop("ts_iso")
            v["ts_ms"]=bad
            self.save(v)
            self.assertEqual(load(str(self.root),self.now)[1]["heartbeat"]["state"],
                             "unverifiable")
            self.assertIsNone(read_metro(str(self.root),now_ms=self.now)
                              ["kpis"]["liquidations"])
        v=dict(self.fixture,ts_ms=self.now-300001)
        v.pop("ts_iso")
        self.save(v)
        self.assertIsNone(read_metro(str(self.root),now_ms=self.now)
                          ["kpis"]["liquidations"])
        for value,expected in ((None,None),(0,0)):
            v=dict(self.fixture)
            if value is None:v.pop("events_total")
            else:v["events_total"]=value
            self.save(v)
            self.assertEqual(read_metro(str(self.root),now_ms=self.now)
                             ["kpis"]["liquidations"],expected)

    def test_managed(self):
        proc=self.root/"proc"
        (proc/"1234").mkdir(parents=True)
        fields=["S"]+["0"]*50
        fields[19]="2000"
        fields[21]="5"
        (proc/"1234/stat").write_text("1234 (worker script) "+" ".join(fields))
        (proc/"1234/cmdline").write_bytes(b"python\x00/safe/liq_collector.py\x00")
        (proc/"uptime").write_text("1000.0 0.0")
        (proc/"stat").write_text("btime 1792624000\n")
        hz=os.sysconf("SC_CLK_TCK")
        v=dict(self.fixture,pid=1234,started_ms=int((1792624000+2000/hz)*1000))
        self.save(v)
        row={"id":"COLL_A","kind":"collector","product":"collectors",
             "source":"heartbeat","cmd_contains":"liq_collector.py"}
        self.binding["managed"]=[row,row]
        self.save(v)
        m=read_metro(str(self.root),now_ms=self.now,proc_root=str(proc))
        self.assertEqual(m["kpis"]["processes"],1)
        self.assertEqual(m["processes"][0]["identity"],"managed")
        for change,state in (({"cmd_contains":"different.py"},"mismatch"),
                             ({}, "verified")):
            r=dict(row,**change)
            found=processes_managed([r],{"heartbeat":v},
              {"heartbeat":{"state":"available"}},self.now,proc_root=str(proc))
            self.assertEqual(found[0]["state"],state)
        wrong=dict(v,started_ms=v["started_ms"]+20000)
        self.assertEqual(processes_managed([row],{"heartbeat":wrong},
            {"heartbeat":{"state":"available"}},self.now,proc_root=str(proc))[0]["state"],"mismatch")
        self.assertEqual(processes_managed([row],{"heartbeat":v},
            {"heartbeat":{"state":"stale"}},self.now,proc_root=str(proc)),[])
        self.assertEqual(processes_managed([row],{"heartbeat":{"pid":None}},
            {"heartbeat":{"state":"available"}},self.now,proc_root=str(proc)),[])
        (proc/"1234/stat").unlink()
        self.assertEqual(processes_managed([row],{"heartbeat":v},
            {"heartbeat":{"state":"available"}},self.now,proc_root=str(proc))[0]["state"],"gone")
