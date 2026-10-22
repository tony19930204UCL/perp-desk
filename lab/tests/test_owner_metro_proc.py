"""Synthetic Linux proc identity and Metro product isolation tests."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from owner_metro_sources import processes
from owner_metro_model import read_metro


class ProcProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        proc = self.root / "fakeproc"
        (proc / "1234").mkdir(parents=True)
        (proc / "sys/kernel/random").mkdir(parents=True)
        self.proc = str(proc)
        self.boot = "12345678-1234-1234-1234-123456789abc"
        (proc / "sys/kernel/random/boot_id").write_text(self.boot)
        (proc / "uptime").write_text("1000.0 0.0")
        fields = ["S"] + ["0"] * 50
        fields[19] = "2000"
        fields[21] = "7"
        (proc / "1234/stat").write_text("1234 (comm with ) spaces) " + " ".join(fields))
        self.argv = b"python\x00worker\x00"
        (proc / "1234/cmdline").write_bytes(self.argv)
        (proc / "1234/cwd").symlink_to("/trusted/work")
        self.row = dict(id="worker", pid=1234, start_ticks=2000,
            kind="compute", product="maker", boot_id=self.boot,
            argv_sha256=hashlib.sha256(self.argv).hexdigest(), cwd="/trusted/work")
        self.launch = {"processes": [self.row]}
        self.adopt = {"processes": [dict(id="worker", pid=1234, start_ticks=2000)]}

    def check(self, row=None):
        return processes({"processes": [row or self.row]}, self.adopt,
                         0, proc_root=self.proc)

    def test_identity(self):
        seen = self.check()
        self.assertEqual(seen[0]["state"], "verified")
        self.assertEqual(seen[0]["rss_bytes"], 7 * os.sysconf("SC_PAGE_SIZE"))
        self.assertGreaterEqual(seen[0]["elapsed_s"], 0)
        self.assertEqual(seen[0]["product"], "maker")

    def test_mismatch(self):
        for key, bad in (("start_ticks", 1999), ("boot_id", "0"*36),
                         ("argv_sha256", "f"*64), ("cwd", "/elsewhere")):
            with self.subTest(key=key):
                row = dict(self.row, **{key: bad})
                if key == "start_ticks":
                    adopted = {"processes": [dict(id="worker", pid=1234,
                                                 start_ticks=1999)]}
                    proof = processes({"processes":[row]}, adopted, 0,
                                      proc_root=self.proc)
                else:
                    proof = self.check(row)
                self.assertEqual(proof[0]["state"], "mismatch")

    def test_missing_expectation(self):
        for key in ("boot_id", "argv_sha256", "cwd"):
            row = dict(self.row)
            row.pop(key)
            self.assertEqual(self.check(row)[0]["state"], "unverified")

    def test_gone_and_dedup(self):
        self.assertEqual(len(processes({"processes":[self.row,self.row]},
            self.adopt,0,proc_root=self.proc)), 1)
        wrapper = dict(self.row, wrapper=True)
        self.assertEqual(self.check(wrapper), [])
        (Path(self.proc)/"1234/stat").unlink()
        self.assertEqual(self.check()[0]["state"], "gone")

    def test_route_scope(self):
        for folder in ("owner_metro", "commitments", "cron", "lab/journal", "lab/shared"):
            (self.root / folder).mkdir(exist_ok=True, parents=True)
        root = self.root / "owner_metro"
        names = ("launch", "adoption", "stage")
        (root / "bindings.json").write_text(json.dumps({"schema_version":1,
            "sources":{name:"owner_metro/"+name+".json" for name in names}}))
        for name, value in (("launch",self.launch),("adoption",self.adopt),
                            ("stage", {"products":{x:{"active_step":0}
                                for x in ("maker","collectors","relay")}})):
            (root/(name+".json")).write_text(json.dumps(
                {**value, "updated_at":1792065600}))
        model = read_metro(str(self.root), now_ms=1792065600000,
                           proc_root=self.proc)
        by_id = {route["id"]:route for route in model["routes"]}
        self.assertEqual(by_id["maker"]["steps"][0]["state"], "active")
        for name in ("collectors","relay"):
            self.assertNotEqual(by_id[name]["steps"][0]["state"], "active")

    def test_freshness(self):
        (self.root/"owner_metro").mkdir()
        (self.root/"owner_metro/bindings.json").write_text(json.dumps({
            "schema_version":1,"sources":{"directive":"owner_metro/directive.json"}}))
        doc = self.root/"owner_metro/directive.json"
        from owner_metro_sources import load
        for value in (None,1792060000,1792065600):
            source={"pending_decisions":7}
            if value is not None:source["updated_at"]=value
            doc.write_text(json.dumps(source))
            obj, health, _ = load(str(self.root),1792065600000)
            self.assertEqual(health["directive"]["state"],
                "unverifiable" if value is None else
                "stale" if value==1792060000 else "available")
            result=read_metro(str(self.root),now_ms=1792065600000)
            self.assertEqual(result["kpis"]["pending_decisions"],
                             7 if value==1792065600 else None)
