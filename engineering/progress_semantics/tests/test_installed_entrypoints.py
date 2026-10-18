import importlib.util
import json
import shutil
import tempfile
import unittest
import os
import subprocess
import sys
import time
import hashlib
import sqlite3
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
class Ledger:
    pass
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

    def test_import_preserves_legacy_commit_public_api_and_evaluator(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.profile(root,False)
            with mock.patch.dict("os.environ",{"HERMES_HOME":str(root)}):
                mod=self.load(root)
                for name in ("Ledger","DISPATCHED","handle_status","OPEN","VERIFIED","main","touch","home"):
                    self.assertTrue(hasattr(mod,name), name)
                    self.assertIs(getattr(mod,name), getattr(mod.base,name))
                self.assertIs(mod.base.evaluate,mod.evaluate)
                self.assertEqual(mod.DISPATCHED,"DISPATCHED")
                self.assertEqual(mod.handle_status({"kind":"cron"})[0],"alive")
                self.assertEqual(mod.main([]),0)

    def test_installed_evaluate_reloads_bound_artifact_through_profile_seam(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.profile(root)
            it=self.item()
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[it]}),"utf-8")
            (root/"cron"/"output"/"job1"/"2026-10-16_21-00-00.md").write_text(
                "**Job ID:** job1\n## Response\n[SILENT]","utf-8")
            journal=root/"lab"/"journal"; journal.mkdir(parents=True)
            row={"at":"2026-10-17 11:11 UTC","item_id":"C0026","job_id":"job1",
                 "result":"ok","body":"checked complete"}
            (journal/"entries.jsonl").write_text(json.dumps(row)+"\n","utf-8")
            req={"schema_version":1,"items":{"C0026":{"timezone":"Europe/Berlin",
                 "coverage":"implemented","required_artifacts":["checked"],
                 "artifact_sources":{"checked":{"path":"lab/journal/entries.jsonl","format":"jsonl",
                 "time_field":"at","time_format":"journal_utc","item_field":"item_id",
                 "job_field":"job_id","status_field":"result","success_values":["ok"],
                 "content_field":"body","required_text":"checked complete"}}}}}
            (root/"commitments"/"progress_requirements.json").write_text(json.dumps(req),"utf-8")
            with mock.patch.dict("os.environ",{"HERMES_HOME":str(root)}):
                mod=self.load(root)
                now=mod.base.datetime.fromisoformat("2026-10-17T20:00:00+02:00").timestamp()
                self.assertEqual(mod.evaluate(Led(it),now),[])

    def test_watch_wrapper_targets_installed_checker(self):
        source=(SRC/"installed_commit_watch.py").read_text("utf-8")
        self.assertIn('plugins/commitment-gate/commit.py',source)
        self.assertIn('"check"',source)

    def real_layout(self, root):
        plugin=root/"plugins"/"commitment-gate"; plugin.mkdir(parents=True)
        shutil.copy(SRC/"installed_commit_base.py",plugin/"commit.py.pa1-base")
        shutil.copy(SRC/"installed_commit.py",plugin/"commit.py")
        pa1=plugin/"pa1"/"progress_semantics"; pa1.mkdir(parents=True)
        for name in ("__init__.py","progress_semantics.py","watcher_adapter.py"):
            shutil.copy(SRC/name,pa1/name)
        return plugin/"commit.py"

    def ledger_item(self, state="DISPATCHED", handle=None):
        now=time.time()
        return {"id":"C-0001","text":"synthetic","created":now-100,"updated":now-100,
                "state":state,"verifier":"script","source":"agent","accept":"manual","due":None,
                "handle":handle,"reason":None,"blocked_by":None,"defer":None,
                "history":[[now-100,"registered "+state]]}

    def test_cas_wrong_expected_state_is_byte_preserving(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); ledger=root/"ledger.json"
            ledger.write_text(json.dumps({"next":2,"items":[self.ledger_item("OPEN")]}),"utf-8")
            before=ledger.read_bytes()
            r=subprocess.run([sys.executable,str(cli),"--ledger",str(ledger),"done","C-0001",
                              "--expect-state","DISPATCHED","--evidence","proof"],capture_output=True,text=True,
                              env=dict(os.environ,HERMES_HOME=str(root)))
            self.assertEqual(r.returncode,3); self.assertIn("state=OPEN",r.stdout)
            self.assertEqual(ledger.read_bytes(),before)

    def test_block_verified_refuses_without_expect_state(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); ledger=root/"ledger.json"
            it=self.ledger_item("VERIFIED"); it["evidence"]="proof"
            ledger.write_text(json.dumps({"next":2,"items":[it]}),"utf-8"); before=ledger.read_bytes()
            r=subprocess.run([sys.executable,str(cli),"--ledger",str(ledger),"block","C-0001","--reason","late"],
                             capture_output=True,text=True,env=dict(os.environ,HERMES_HOME=str(root)))
            self.assertEqual(r.returncode,3); self.assertIn("state=VERIFIED",r.stdout)
            self.assertEqual(ledger.read_bytes(),before)

    def test_unknown_expect_state_is_refused_by_cli(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); ledger=root/"ledger.json"
            ledger.write_text(json.dumps({"next":2,"items":[self.ledger_item()]}),"utf-8")
            r=subprocess.run([sys.executable,str(cli),"--ledger",str(ledger),"done","C-0001",
                              "--expect-state","NOT_A_STATE","--evidence","proof"],capture_output=True,text=True,
                              env=dict(os.environ,HERMES_HOME=str(root)))
            self.assertNotEqual(r.returncode,0); self.assertIn("invalid choice",r.stderr)

    def test_cas_concurrent_done_block_exactly_one_wins_twenty_rounds(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); env=dict(os.environ,HERMES_HOME=str(root))
            runner=root/"runner.py"
            runner.write_text("import os,sys,time\nwhile not os.path.exists(sys.argv[1]): time.sleep(.001)\nos.execv(sys.executable,[sys.executable]+sys.argv[2:])\n","utf-8")
            for reverse in (False,True):
                for n in range(20):
                    ledger=root/("ledger-%s-%d.json"%(reverse,n))
                    ledger.write_text(json.dumps({"next":2,"items":[self.ledger_item()]}),"utf-8")
                    gate=root/("go-%s-%d"%(reverse,n))
                    done=[str(cli),"--ledger",str(ledger),"done","C-0001","--expect-state","DISPATCHED","--evidence","proof"]
                    block=[str(cli),"--ledger",str(ledger),"block","C-0001","--expect-state","DISPATCHED","--reason","blocked"]
                    cmds=[done,block] if not reverse else [block,done]
                    ps=[subprocess.Popen([sys.executable,str(runner),str(gate)]+x,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env) for x in cmds]
                    gate.write_text("go","utf-8"); rs=[p.communicate(timeout=10) for p in ps]; codes=[p.returncode for p in ps]
                    self.assertEqual(sorted(codes),[0,3])
                    loser=rs[codes.index(3)][0]; final=json.loads(ledger.read_text("utf-8"))["items"][0]
                    self.assertIn("state="+final["state"],loser)
                    self.assertEqual(final["history"][-1][1],"done" if final["state"]=="VERIFIED" else "block")

    def test_legacy_transitions_without_expect_state_remain_available(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); env=dict(os.environ,HERMES_HOME=str(root))
            for state,cmd,args in (("OPEN","block",["--reason","x"]),("DISPATCHED","done",["--evidence","x"]),("BLOCKED","drop",["--reason","x"])):
                ledger=root/(state+".json"); ledger.write_text(json.dumps({"next":2,"items":[self.ledger_item(state)]}),"utf-8")
                r=subprocess.run([sys.executable,str(cli),"--ledger",str(ledger),cmd,"C-0001"]+args,capture_output=True,text=True,env=env)
                self.assertEqual(r.returncode,0,(state,r.stderr))

    def test_successor_scan_classifies_only_lost_ownership_and_preserves_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); commitments=root/"commitments"; commitments.mkdir()
            now=time.time()
            dead=self.ledger_item(handle={"kind":"pid","pid":99999999,"start":1,"ref":"99999999"}); dead["id"]="C-0001"
            manual=self.ledger_item(handle={"kind":"manual","ref":"sa-test"}); manual.update(id="C-0002",created=now-50000)
            blocked=self.ledger_item("BLOCKED",None); blocked.update(id="C-0003",blocked_by=None,reason="free text")
            checked=[]
            for i, blocker in enumerate(("item:C-9999","file:/tmp/x","user:question","cmd:false"),4):
                row=self.ledger_item("BLOCKED",None); row.update(id="C-%04d"%i,blocked_by=blocker); checked.append(row)
            verified=self.ledger_item("VERIFIED",None); verified["id"]="C-0008"
            dropped=self.ledger_item("DROPPED",None); dropped["id"]="C-0009"
            ledger=commitments/"ledger.json"; ledger.write_text(json.dumps({"next":10,"items":[dead,manual,blocked]+checked+[verified,dropped]}),"utf-8")
            before=ledger.read_bytes(); mt=ledger.stat().st_mtime_ns
            env=dict(os.environ,HERMES_HOME=str(root),COMMIT_MANUAL_LEASE_S="3600")
            r=subprocess.run([sys.executable,str(cli),"attention"],capture_output=True,text=True,env=env)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertIn("NEEDS_SUCCESSOR C-0001 pid",r.stdout); self.assertIn("C-0002 manual",r.stdout); self.assertIn("C-0003 blocked",r.stdout)
            for cid in ("C-0004","C-0005","C-0006","C-0007","C-0008","C-0009"): self.assertNotIn("NEEDS_SUCCESSOR "+cid,r.stdout)
            self.assertEqual(ledger.read_bytes(),before); self.assertEqual(ledger.stat().st_mtime_ns,mt)

    def test_successor_manual_fresh_heartbeat_and_young_lease_are_not_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); commitments=root/"commitments"; (commitments/"heartbeats").mkdir(parents=True)
            now=time.time(); fresh=self.ledger_item(handle={"kind":"manual","ref":"fresh"}); fresh.update(id="C-0001",created=now-50000)
            young=self.ledger_item(handle={"kind":"manual","ref":"young"}); young.update(id="C-0002",created=now-10)
            (commitments/"heartbeats"/"fresh.json").write_text(json.dumps({"item_id":"C-0001","handle_ref":"fresh","at":now}),"utf-8")
            (commitments/"ledger.json").write_text(json.dumps({"next":3,"items":[fresh,young]}),"utf-8")
            r=subprocess.run([sys.executable,str(cli),"check"],capture_output=True,text=True,
                             env=dict(os.environ,HERMES_HOME=str(root),COMMIT_MANUAL_LEASE_S="3600"))
            self.assertEqual(r.returncode,0,r.stderr); self.assertNotIn("NEEDS_SUCCESSOR",r.stdout)

    def test_successor_finished_pid_not_flagged_and_recovery_removes_queue_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); commitments=root/"commitments"; runs=commitments/"runs"; runs.mkdir(parents=True)
            exitfile=runs/"C-0001.exit"; exitfile.write_text("0","utf-8")
            it=self.ledger_item(handle={"kind":"pid","pid":99999999,"start":1,"ref":"99999999","exitfile":str(exitfile)})
            ledger=commitments/"ledger.json"; ledger.write_text(json.dumps({"next":2,"items":[it]}),"utf-8")
            env=dict(os.environ,HERMES_HOME=str(root))
            r=subprocess.run([sys.executable,str(cli),"attention"],capture_output=True,text=True,env=env)
            self.assertNotIn("NEEDS_SUCCESSOR C-0001",r.stdout)
            q=json.loads((commitments/"needs_successor.json").read_text("utf-8")); self.assertEqual(q["items"],[])

    def test_successor_corrupt_ledger_keeps_existing_queue_and_fails_scan(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); commitments=root/"commitments"; commitments.mkdir()
            q=commitments/"needs_successor.json"; q.write_text('{"schema_version":1,"items":[{"id":"old"}]}',"utf-8"); before=q.read_bytes()
            (commitments/"ledger.json").write_text("{bad","utf-8")
            r=subprocess.run([sys.executable,str(cli),"attention"],capture_output=True,text=True,env=dict(os.environ,HERMES_HOME=str(root)))
            self.assertEqual(r.returncode,4); self.assertIn("NEEDS_SUCCESSOR_SCAN_ERROR",r.stdout); self.assertEqual(q.read_bytes(),before)

    def test_successor_first_seen_persists_across_scans(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); commitments=root/"commitments"; commitments.mkdir()
            it=self.ledger_item("BLOCKED"); it["blocked_by"]=None
            (commitments/"ledger.json").write_text(json.dumps({"next":2,"items":[it]}),"utf-8")
            env=dict(os.environ,HERMES_HOME=str(root))
            subprocess.run([sys.executable,str(cli),"attention"],capture_output=True,text=True,env=env,check=True)
            q1=json.loads((commitments/"needs_successor.json").read_text("utf-8")); time.sleep(.01)
            subprocess.run([sys.executable,str(cli),"attention"],capture_output=True,text=True,env=env,check=True)
            q2=json.loads((commitments/"needs_successor.json").read_text("utf-8"))
            self.assertEqual(q1["items"][0]["first_seen"],q2["items"][0]["first_seen"])
            self.assertGreaterEqual(q2["items"][0]["last_seen"],q1["items"][0]["last_seen"])

    def patrol_layout(self, root):
        scripts=root/"scripts"; scripts.mkdir(parents=True)
        shutil.copy(SRC/"installed_desk_patrol.py",scripts/"desk_patrol.py")
        hb=scripts/"heartbeat_gate.py"; hb.write_text("import sys\n","utf-8")
        bindir=root/"bin"; bindir.mkdir()
        systemctl=bindir/"systemctl"
        systemctl.write_text("#!/usr/bin/env python3\nimport os,sys\nu=' '.join(sys.argv)\nif 'is-active' in u: print(os.environ.get('FAKE_ACTIVE','inactive'))\nelif 'NRestarts' in u: print('0')\nelif 'MainPID' in u: print('0')\n","utf-8"); systemctl.chmod(0o755)
        (root/"lab"/"journal").mkdir(parents=True); (root/"lab"/"shared").mkdir(parents=True); (root/"lab"/"data").mkdir(parents=True)
        return scripts/"desk_patrol.py",bindir

    def run_patrol(self, root, active="inactive"):
        patrol,bindir=self.patrol_layout(root) if not (root/"scripts"/"desk_patrol.py").exists() else (root/"scripts"/"desk_patrol.py",root/"bin")
        env=dict(os.environ,HERMES_HOME=str(root),PATH=str(bindir)+os.pathsep+os.environ.get("PATH",""),FAKE_ACTIVE=active)
        return subprocess.run([sys.executable,str(patrol)],capture_output=True,text=True,env=env)

    def test_patrol_valid_pause_suppresses_only_inactive_and_stale_and_reports_unknown_exposure(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.patrol_layout(root); shared=root/"lab"/"shared"
            f=shared/"paper_t1_btcusdt_live.json"; f.write_text("{}","utf-8"); os.utime(f,(time.time()-1000,)*2)
            future="2099-01-01T00:00:00+00:00"; now="2026-01-01T00:00:00+00:00"
            pause={"schema_version":1,"units":{"t1-btcusdt":{"reason":"owner pause","authorized_by":"operator","authorized_at":now,"review_by":future}}}
            (root/"lab"/"journal"/"paused_units.json").write_text(json.dumps(pause),"utf-8")
            r=self.run_patrol(root)
            self.assertNotIn("SERVICE t1-btcusdt",r.stdout); self.assertNotIn("STALE_STATUS t1-btcusdt",r.stdout)
            self.assertIn("PAUSE_EXPOSURE_UNKNOWN t1-btcusdt",r.stdout)

    def test_patrol_pause_open_exposure_and_active_unit_keep_normal_checks(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.patrol_layout(root); data=root/"lab"/"data"/"paper-t1-btcusdt"; data.mkdir(parents=True)
            db=sqlite3.connect(data/"runtime.sqlite3"); db.execute("create table state(id integer primary key,payload text)")
            db.execute("insert into state values(1,?)",(json.dumps({"t1_detector":{"position":1},"open_orders":[]}),)); db.commit(); db.close()
            pause={"schema_version":1,"units":{"t1-btcusdt":{"reason":"owner pause","authorized_by":"operator","authorized_at":"2026-01-01T00:00:00+00:00","review_by":"2099-01-01T00:00:00+00:00"}}}
            (root/"lab"/"journal"/"paused_units.json").write_text(json.dumps(pause),"utf-8")
            r=self.run_patrol(root,"active")
            self.assertIn("PAUSE_OPEN_EXPOSURE t1-btcusdt",r.stdout); self.assertIn("PAUSE_UNIT_ACTIVE t1-btcusdt",r.stdout)

    def test_patrol_invalid_marker_suppresses_nothing_and_unknown_unit_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.patrol_layout(root); p=root/"lab"/"journal"/"paused_units.json"
            p.write_text('{"schema_version":9,"units":{}}',"utf-8"); r=self.run_patrol(root)
            self.assertIn("PAUSE_MARKER_INVALID",r.stdout); self.assertIn("SERVICE h2",r.stdout)
            p.write_text(json.dumps({"schema_version":1,"units":{"not-a-unit":{"reason":"x","authorized_by":"x","authorized_at":"2026-01-01T00:00:00+00:00","review_by":"2099-01-01T00:00:00+00:00"}}}),"utf-8")
            (root/"lab"/"journal"/"patrol_state.json").unlink(missing_ok=True); r=self.run_patrol(root)
            self.assertIn("PAUSE_UNKNOWN_UNIT not-a-unit",r.stdout); self.assertIn("SERVICE h2",r.stdout)

    def test_patrol_expired_pause_reports_expired_and_normal_service(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.patrol_layout(root)
            pause={"schema_version":1,"units":{"h2":{"reason":"x","authorized_by":"x","authorized_at":"2020-01-01T00:00:00+00:00","review_by":"2020-01-02T00:00:00+00:00"}}}
            (root/"lab"/"journal"/"paused_units.json").write_text(json.dumps(pause),"utf-8")
            r=self.run_patrol(root); self.assertIn("PAUSE_EXPIRED h2",r.stdout); self.assertIn("SERVICE h2",r.stdout)


    def test_successor_second_scan_changes_only_last_seen_and_closed_item_recovers(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.real_layout(root); commitments=root/"commitments"; commitments.mkdir()
            it=self.ledger_item("BLOCKED"); it["blocked_by"]=None
            ledger=commitments/"ledger.json"; ledger.write_text(json.dumps({"next":2,"items":[it]}),"utf-8")
            with mock.patch.dict("os.environ",{"HERMES_HOME":str(root)}):
                spec=importlib.util.spec_from_file_location("scan_candidate",root/"plugins"/"commitment-gate"/"commit.py")
                mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
                a=mod.scan_successors(now=1000); b=mod.scan_successors(now=1001)
                aa=dict(a[0]); bb=dict(b[0]); aa.pop("last_seen"); bb.pop("last_seen"); self.assertEqual(aa,bb)
                self.assertEqual((a[0]["last_seen"],b[0]["last_seen"]),(1000,1001))
                data=json.loads(ledger.read_text("utf-8")); data["items"][0]["state"]="VERIFIED"; ledger.write_text(json.dumps(data),"utf-8")
                self.assertEqual(mod.scan_successors(now=1002),[])

    def test_successor_scan_serializes_with_ledger_writer_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); commitments=root/"commitments"; commitments.mkdir()
            it=self.ledger_item("BLOCKED"); it["blocked_by"]=None
            ledger=commitments/"ledger.json"
            ledger.write_text(json.dumps({"next":2,"items":[it]}),"utf-8")
            ready=root/"writer.ready"; released=root/"writer.released"
            code = """
import json, sys, time, fcntl
from pathlib import Path
p, ready, released = map(Path, sys.argv[1:])
with open(str(p) + '.lock', 'a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    ready.write_text('locked')
    data = json.loads(p.read_text())
    data['items'][0]['reason'] = 'writer'
    time.sleep(0.5)
    p.write_text(json.dumps(data))
    fcntl.flock(lock, fcntl.LOCK_UN)
released.write_text('released')
"""
            writer=subprocess.Popen([sys.executable,"-c",code,str(ledger),str(ready),str(released)],
                                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                deadline=time.monotonic()+5
                while not ready.exists() and writer.poll() is None and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists(),"writer did not acquire lock")
                env=dict(os.environ,HERMES_HOME=str(root))
                started=time.monotonic()
                r=subprocess.run([sys.executable,str(cli),"attention"],
                                 capture_output=True,text=True,env=env,timeout=10)
                elapsed=time.monotonic()-started
                out,err=writer.communicate(timeout=5)
                self.assertEqual(writer.returncode,0,err)
                self.assertTrue(released.exists(),"writer did not release lock")
                self.assertGreaterEqual(elapsed,0.3,"scan did not wait for ledger writer")
                self.assertEqual(r.returncode,0,r.stderr)
                self.assertEqual(json.loads(ledger.read_text("utf-8"))["items"][0]["reason"],"writer")
                queue=json.loads((commitments/"needs_successor.json").read_text("utf-8"))
                self.assertEqual(queue["schema_version"],1)
                self.assertEqual(queue["items"][0]["id"],"C-0001")
            finally:
                if writer.poll() is None:
                    writer.terminate()
                    try:
                        writer.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        writer.kill()
                        writer.communicate(timeout=3)

    def test_successor_corrupt_ledger_check_reports_scan_error(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); commitments=root/"commitments"; commitments.mkdir()
            queue=commitments/"needs_successor.json"
            queue.write_text('{"schema_version":1,"items":[{"id":"old"}]}',"utf-8")
            before=queue.read_bytes()
            (commitments/"ledger.json").write_text("{bad","utf-8")
            r=subprocess.run([sys.executable,str(cli),"check"],capture_output=True,text=True,
                             env=dict(os.environ,HERMES_HOME=str(root)),timeout=10)
            self.assertEqual(r.returncode,4,r.stderr)
            self.assertIn("NEEDS_SUCCESSOR_SCAN_ERROR",r.stdout)
            self.assertEqual(queue.read_bytes(),before)

    def test_attention_indented_ledger_preserves_bytes_and_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); commitments=root/"commitments"; commitments.mkdir()
            ledger=commitments/"ledger.json"
            ledger.write_text(json.dumps({"next":2,"items":[self.ledger_item("BLOCKED")]},indent=1),"utf-8")
            before=ledger.read_bytes(); mtime=ledger.stat().st_mtime_ns
            r=subprocess.run([sys.executable,str(cli),"attention"],capture_output=True,text=True,
                             env=dict(os.environ,HERMES_HOME=str(root)),timeout=10)
            self.assertEqual(r.returncode,0,r.stderr)
            self.assertEqual(ledger.read_bytes(),before)
            self.assertEqual(ledger.stat().st_mtime_ns,mtime)

    def test_patrol_no_marker_preserves_baseline_service_and_state_contract(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); self.patrol_layout(root)
            r=self.run_patrol(root)
            for u in ("h2","f1h-btcusdt","f1h-solusdt","f1h-xrpusdt","w1-btcusdt","w1-ethusdt","w1-solusdt","w1-xrpusdt","t1-btcusdt","t1-ethusdt","t1-solusdt","t1-xrpusdt"):
                self.assertIn("SERVICE "+u+": unit is inactive",r.stdout)
            self.assertNotIn("PAUSE_",r.stdout)
            state=json.loads((root/"lab"/"journal"/"patrol_state.json").read_text("utf-8"))
            self.assertEqual(set(state),{"last_run","restarts","pidw","sent"})
            self.assertEqual(state["restarts"],{u:0 for u in ("h2","f1h-btcusdt","f1h-solusdt","f1h-xrpusdt","w1-btcusdt","w1-ethusdt","w1-solusdt","w1-xrpusdt","t1-btcusdt","t1-ethusdt","t1-solusdt","t1-xrpusdt")})


    def test_done_verified_identical_is_idempotent_but_different_evidence_refuses(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cli=self.real_layout(root); ledger=root/"ledger.json"; env=dict(os.environ,HERMES_HOME=str(root))
            it=self.ledger_item("VERIFIED"); it["evidence"]="proof"
            ledger.write_text(json.dumps({"next":2,"items":[it]}),"utf-8"); before=ledger.read_bytes()
            same=subprocess.run([sys.executable,str(cli),"--ledger",str(ledger),"done","C-0001","--evidence","proof"],capture_output=True,text=True,env=env)
            self.assertEqual(same.returncode,0); self.assertIn("state=VERIFIED",same.stdout); self.assertEqual(ledger.read_bytes(),before)
            diff=subprocess.run([sys.executable,str(cli),"--ledger",str(ledger),"done","C-0001","--evidence","other"],capture_output=True,text=True,env=env)
            self.assertEqual(diff.returncode,3); self.assertIn("state=VERIFIED",diff.stdout); self.assertEqual(ledger.read_bytes(),before)


if __name__=="__main__":
    unittest.main()
