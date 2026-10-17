import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "engineering" / "progress_semantics"

BASE = r'''
import json, os, time
from datetime import datetime
from pathlib import Path
OPEN,DISPATCHED,VERIFIED,BLOCKED,DROPPED="OPEN","DISPATCHED","VERIFIED","BLOCKED","DROPPED"
TERMINAL={VERIFIED,DROPPED}; OPEN_GRACE_H=24; STALE_H=72; REPEAT_H=6
def home(): return Path(os.environ["HERMES_HOME"])
def handle_status(h): return ("alive","cron")
def accept_ok(a,item=None): return (False,"no")
def touch(item,note): item["updated"]=time.time(); item.setdefault("history",[]).append([time.time(),note])
def blocker_state(led,bb): return (False,"waiting")
def fmt_t(ts): return "x"
def main(argv=None): return 0
'''

class Led:
    def __init__(self, item): self.data={"items":[item]}

class InstalledEntryTests(unittest.TestCase):
    def load(self, root):
        plugin=root/"plugins"/"commitment-gate"; plugin.mkdir(parents=True)
        (plugin/"commit.py.pa1-base").write_text(BASE,"utf-8")
        shutil.copy(SRC/"installed_commit.py",plugin/"commit.py")
        pa1=plugin/"pa1"/"progress_semantics"; pa1.mkdir(parents=True)
        for name in ("__init__.py","progress_semantics.py","watcher_adapter.py"):
            shutil.copy(SRC/name,pa1/name)
        spec=importlib.util.spec_from_file_location("candidate",plugin/"commit.py")
        mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod

    def item(self):
        return {"id":"C0026","text":"standing","state":"DISPATCHED","updated":1,
                "handle":{"kind":"cron","ref":"job1"},"accept":"manual","history":[]}

    def profile(self, root, req=True):
        (root/"commitments").mkdir(exist_ok=True)
        (root/"cron"/"output"/"job1").mkdir(parents=True,exist_ok=True)
        job={"id":"job1","enabled":True,"state":"scheduled","paused":None,"last_status":"ok",
             "schedule":{"kind":"cron","expr":"0 21 * * *"}}
        (root/"cron"/"jobs.json").write_text(json.dumps({"jobs":[job]}),"utf-8")
        if req:
            data={"schema_version":1,"items":{"C0026":{"timezone":"Europe/Berlin","coverage":"implemented"}}}
            (root/"commitments"/"progress_requirements.json").write_text(json.dumps(data),"utf-8")

    def test_mapped_healthy_recurring_suppresses_generic_stale_only(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.profile(root)
            it=self.item()
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[it]}),"utf-8")
            (root/"cron"/"output"/"job1"/"2026-10-16_21-00-00.md").write_text(
                "## Prompt\nquoted [CRON_FAILURE]\n## Response\n[SILENT]","utf-8")
            with mock.patch.dict("os.environ",{"HERMES_HOME":str(root)}):
                mod=self.load(root)
                now=mod.base.datetime.fromisoformat("2026-10-17T20:00:00+02:00").timestamp()
                self.assertEqual(mod.evaluate(Led(it),now),[])

    def test_unmapped_recurring_preserves_original_stale_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.profile(root,False)
            it=self.item()
            with mock.patch.dict("os.environ",{"HERMES_HOME":str(root)}):
                mod=self.load(root)
                alerts=mod.evaluate(Led(it),time_value:=400000)
                self.assertTrue(any("沒有任何進展" in x for x in alerts))

    def test_dedupe_fields_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.profile(root,False)
            it=self.item(); it["alert_sig"]="111 小時沒有任何進展紀錄"; it["alerted"]=399999
            with mock.patch.dict("os.environ",{"HERMES_HOME":str(root)}):
                mod=self.load(root)
                self.assertEqual(mod.evaluate(Led(it),400000),[])

    def test_deterministic_acceptance_still_auto_verifies(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.profile(root,False)
            it=self.item(); it["accept"]="file_exists:x"
            with mock.patch.dict("os.environ",{"HERMES_HOME":str(root)}):
                mod=self.load(root)
                mod.base.accept_ok=lambda a,item=None:(True,"proof")
                mod.evaluate(Led(it),400000)
                self.assertEqual((it["state"],it["evidence"]),("VERIFIED","proof"))

    def test_missing_base_refuses_install(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plugin = root / "plugins" / "commitment-gate"
            plugin.mkdir(parents=True)
            shutil.copy(SRC / "installed_commit.py", plugin / "commit.py")
            pa1 = plugin / "pa1" / "progress_semantics"
            pa1.mkdir(parents=True)
            for name in ("__init__.py", "progress_semantics.py", "watcher_adapter.py"):
                shutil.copy(SRC / name, pa1 / name)
            spec = importlib.util.spec_from_file_location("missing_base", plugin / "commit.py")
            mod = importlib.util.module_from_spec(spec)
            with self.assertRaisesRegex(SystemExit, "base checker backup unavailable"):
                spec.loader.exec_module(mod)

    def test_watch_wrapper_targets_installed_checker(self):
        source=(SRC/"installed_commit_watch.py").read_text("utf-8")
        self.assertIn('plugins/commitment-gate/commit.py',source)
        self.assertIn('"check"',source)

if __name__=="__main__":
    unittest.main()
