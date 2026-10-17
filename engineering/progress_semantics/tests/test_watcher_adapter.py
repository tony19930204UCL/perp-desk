import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from engineering.progress_semantics.watcher_adapter import (
    assess_item, assess_profile, output_record, schedule_from_job)

ZONE = "Europe/Berlin"
NOW = datetime(2026, 10, 17, 13, 26, tzinfo=ZoneInfo(ZONE))

def item(ident="C0026", job_id="job1"):
    return {"id": ident, "state": "DISPATCHED", "updated": 1,
            "handle": {"kind": "cron", "ref": job_id}, "history": []}

def interval(**extra):
    data={"id":"job1","enabled":True,"state":"scheduled","paused":None,
          "last_status":"ok","schedule":{"kind":"interval","minutes":60},
          "last_run_at":"2026-10-17T12:10:33.375004+02:00",
          "next_run_at":"2026-10-17T13:10:33.375004+02:00"}
    data.update(extra); return data

def cron(expr):
    return {"id":"job1","enabled":True,"state":"scheduled","paused":None,
            "last_status":"ok","schedule":{"kind":"cron","expr":expr}}

def out(name="2026-10-17_13-10-33", text=None):
    text=text or "## Prompt\nquoted [CRON_FAILURE]\n## Response\n[SILENT]"
    return output_record("job1","cron/output/job1/"+name+".md",text,ZONE)

class WatcherAdapterTests(unittest.TestCase):
    def req(self, **extra):
        data={"timezone":ZONE,"coverage":"implemented"}; data.update(extra); return data

    def test_interval_uses_real_anchor_not_round_hour(self):
        r=assess_item(item(),NOW,interval(),[out()],self.req())
        self.assertEqual(r["state"],"OK")

    def test_exact_second_anchor_accepts_exact_second_output(self):
        job = interval(
            last_run_at="2026-10-17T12:10:33+02:00",
            next_run_at="2026-10-17T13:10:33+02:00")
        r = assess_item(item(), NOW, job, [out()], self.req())
        self.assertEqual(r["state"], "OK")

    def test_previous_second_is_not_current_slot(self):
        previous = out("2026-10-17_13-10-32")
        r = assess_item(item(), NOW, interval(), [previous], self.req())
        self.assertEqual((r["state"], r["reason_code"]), ("OVERDUE", "CRON_OVERDUE"))

    def test_next_second_is_current_without_slack(self):
        later = out("2026-10-17_13-10-34")
        r = assess_item(item(), NOW, interval(), [later], self.req())
        self.assertEqual(r["state"], "OK")

    def test_wrong_job_record_cannot_satisfy_slot(self):
        wrong = dict(out())
        wrong["job_id"] = "other"
        r = assess_item(item(), NOW, interval(), [wrong], self.req())
        self.assertEqual((r["state"], r["reason_code"]), ("OVERDUE", "CRON_OVERDUE"))

    def test_long_script_completion_uses_completion_anchor_bucket(self):
        job = interval(
            last_run_at="2026-10-17T13:10:59.900000+02:00",
            next_run_at="2026-10-17T14:10:59.900000+02:00")
        record = out("2026-10-17_13-10-59")
        now = datetime(2026, 10, 17, 13, 27, tzinfo=ZoneInfo(ZONE))
        r = assess_item(item(), now, job, [record], self.req())
        self.assertEqual(r["state"], "OK")

    def test_cron_daily_schema(self):
        s=schedule_from_job(cron("0 21 * * *"),ZONE)
        self.assertEqual((s["frequency"],s["hour"],s["minute"]),("daily",21,0))

    def test_cron_weekly_schema(self):
        s=schedule_from_job(cron("0 20 * * 0"),ZONE)
        self.assertEqual((s["frequency"],s["weekday"]),("weekly",6))

    def test_cron_daily_half_hour(self):
        s=schedule_from_job(cron("30 20 * * *"),ZONE)
        self.assertEqual((s["hour"],s["minute"]),(20,30))

    def test_prompt_failure_is_quoted_only(self):
        self.assertEqual(assess_item(item(),NOW,interval(),[out()],self.req())["state"],"OK")

    def test_current_response_failure(self):
        rec=out(text="## Prompt\nold [CRON_FAILURE]\n## Response\n[CRON_FAILURE]\nboom")
        self.assertEqual(assess_item(item(),NOW,interval(),[rec],self.req())["reason_code"],"CRON_FAILED")

    def test_no_agent_silent_does_not_prove_artifact(self):
        text="# Cron Job: x\n\n**Run Time:** 2026-10-17 13:10:33\n**Mode:** no_agent (script)\n**Status:** silent (empty output)"
        r=assess_item(item(),NOW,interval(),[out(text=text)],self.req(required_artifacts=["report"]))
        self.assertEqual(r["reason_code"],"MISSING_ARTIFACT")

    def test_missing_requirements_truthful_unknown(self):
        r=assess_item(item(),NOW,interval(),[out()],{"timezone":ZONE})
        self.assertEqual((r["state"],r["reason_code"]),("GAP","REQUIREMENTS_UNKNOWN"))

    def test_missing_timezone_fails_closed(self):
        r=assess_item(item(),NOW,interval(),[out()],{"coverage":"implemented"})
        self.assertEqual(r["reason_code"],"MISSING_TIMEZONE")

    def test_known_missing_coverage_is_gap(self):
        r=assess_item(item("C0003"),NOW,interval(),[out()],
                      {"timezone":ZONE,"coverage":"not_implemented"})
        self.assertEqual((r["state"],r["reason_code"]),("GAP","COVERAGE_NOT_IMPLEMENTED"))

    def test_disabled_is_safety(self):
        r=assess_item(item(),NOW,interval(enabled=False),[out()],self.req())
        self.assertEqual(r["reason_code"],"CRON_DISABLED")

    def test_overdue_missing_output(self):
        r=assess_item(item(),NOW,interval(),[],self.req())
        self.assertEqual(r["state"],"OVERDUE")

    def test_missing_output_within_grace_is_not_overdue(self):
        now = datetime(2026, 10, 17, 13, 20, tzinfo=ZoneInfo(ZONE))
        r = assess_item(item(), now, interval(), [], self.req())
        self.assertEqual((r["state"], r["reason_code"]), ("OK", "WITHIN_GRACE"))

    def test_wall_timestamp_requires_explicit_zone(self):
        with self.assertRaises(ValueError):
            output_record("job1","cron/output/job1/2026-10-17_01-19-29.md","x","")

    def test_ambiguous_dst_wall_time_fails_closed(self):
        with self.assertRaises(ValueError):
            output_record("job1", "cron/output/job1/2026-10-25_02-30-00.md",
                          "## Response\n[SILENT]", ZONE)

    def test_wrong_output_path_job_is_rejected(self):
        record = out()
        record["ref"] = "cron/output/other/2026-10-17_13-10-33.md"
        r = assess_item(item(), NOW, interval(), [record], self.req())
        self.assertEqual(r["reason_code"], "INVALID_OUTPUT_PATH")

    def test_body_filename_one_second_writer_skew_is_bounded(self):
        text="**Job ID:** job1\n**Run Time:** 2026-10-17 01:19:28\n**Mode:** no_agent (script)\n**Status:** silent (empty output)"
        rec=output_record("job1","cron/output/job1/2026-10-17_01-19-29.md",text,ZONE)
        self.assertEqual(rec["filename_at"].second,29)
        self.assertEqual(rec["run_at"].second,28)
        self.assertTrue(rec["raw_sha256"])

    def test_real_shape_211342_filename_211341_body_is_one_run(self):
        text="**Job ID:** fa25edbf3834\n**Run Time:** 2026-10-16 21:13:41\n## Response\n[SILENT]"
        rec=output_record("fa25edbf3834",
                          "cron/output/fa25edbf3834/2026-10-16_21-13-42.md",text,ZONE)
        self.assertEqual(rec["at"].isoformat(),"2026-10-16T21:13:42+02:00")
        self.assertEqual(rec["run_at"].second,41)

    def test_body_filename_larger_unexplained_skew_fails(self):
        text="**Job ID:** job1\n**Run Time:** 2026-10-17 01:19:27\n**Mode:** no_agent (script)\n**Status:** silent (empty output)"
        with self.assertRaises(ValueError):
            output_record("job1","cron/output/job1/2026-10-17_01-19-29.md",text,ZONE)

    def test_body_job_mismatch_fails(self):
        with self.assertRaises(ValueError):
            output_record("job1","cron/output/job1/2026-10-17_01-19-29.md",
                          "**Job ID:** other\n## Response\n[SILENT]",ZONE)

    def test_weekly_not_due(self):
        weekly=cron("0 20 * * 0")
        old=out("2026-10-11_20-00-00")
        r=assess_item(item(),NOW,weekly,[old],self.req())
        self.assertEqual(r["state"],"OK")

    def test_profile_ignores_old_malformed_when_current_output_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"commitments").mkdir()
            outdir=root/"cron"/"output"/"job1"; outdir.mkdir(parents=True)
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[item()]}),"utf-8")
            (root/"cron"/"jobs.json").write_text(json.dumps({"jobs":[interval()]}),"utf-8")
            (outdir/"2026-10-16_01-19-29.md").write_text(
                "**Job ID:** job1\n**Run Time:** 2026-10-16 01:19:20\n## Response\n[SILENT]","utf-8")
            (outdir/"2026-10-17_13-10-33.md").write_text(
                "**Job ID:** job1\n**Run Time:** 2026-10-17 13:10:32\n## Response\n[SILENT]","utf-8")
            self.assertEqual(assess_profile(root,"C0026",NOW,self.req())["state"],"OK")

    def test_latest_scheduler_error_applies_only_to_matching_run(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"commitments").mkdir()
            outdir=root/"cron"/"output"/"job1"; outdir.mkdir(parents=True)
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[item()]}),"utf-8")
            job=interval(last_status="error",last_run_at="2026-10-17T13:10:33.375004+02:00")
            (root/"cron"/"jobs.json").write_text(json.dumps({"jobs":[job]}),"utf-8")
            (outdir/"2026-10-17_12-10-33.md").write_text("## Response\n[SILENT]","utf-8")
            (outdir/"2026-10-17_13-10-33.md").write_text("## Response\n[SILENT]","utf-8")
            r=assess_profile(root,"C0026",NOW,self.req())
            self.assertEqual((r["state"],r["reason_code"]),("SAFETY","CRON_FAILED"))

    def test_profile_current_malformed_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"commitments").mkdir()
            outdir=root/"cron"/"output"/"job1"; outdir.mkdir(parents=True)
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[item()]}),"utf-8")
            (root/"cron"/"jobs.json").write_text(json.dumps({"jobs":[interval()]}),"utf-8")
            (outdir/"2026-10-17_13-10-34.md").write_text(
                "**Job ID:** other\n## Response\n[SILENT]","utf-8")
            r=assess_profile(root,"C0026",NOW,self.req())
            self.assertEqual((r["state"],r["reason_code"]),("SAFETY","MALFORMED_CURRENT_OUTPUT"))

    def test_dynamic_jsonl_artifact_requires_explicit_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"commitments").mkdir()
            outdir=root/"cron"/"output"/"job1"; outdir.mkdir(parents=True)
            journal=root/"lab"/"journal"; journal.mkdir(parents=True)
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[item()]}),"utf-8")
            (root/"cron"/"jobs.json").write_text(json.dumps({"jobs":[interval()]}),"utf-8")
            (outdir/"2026-10-17_13-10-33.md").write_text("## Response\n[SILENT]","utf-8")
            spec={"timezone":ZONE,"coverage":"implemented",
                  "required_artifacts":["checked"],
                  "artifact_sources":{"checked":{"path":"lab/journal/entries.jsonl","format":"jsonl",
                    "time_field":"at","time_format":"journal_utc","item_field":"item_id",
                    "job_field":"job_id","status_field":"result","success_values":["ok"],
                    "content_field":"body","required_text":"coverage complete"}}}
            mention={"id":"J-0437","at":"2026-10-17 11:11 UTC","refs":"C0026,job1",
                     "title":"coverage complete"}
            (journal/"entries.jsonl").write_text(json.dumps(mention)+"\n","utf-8")
            self.assertEqual(assess_profile(root,"C0026",NOW,spec)["reason_code"],"MISSING_ARTIFACT")
            bound=dict(mention,item_id="C0026",job_id="job1",result="ok",body="coverage complete")
            (journal/"entries.jsonl").write_text(json.dumps(bound)+"\n","utf-8")
            self.assertEqual(assess_profile(root,"C0026",NOW,spec)["state"],"OK")

    def test_conflicting_current_candidates_fail_closed(self):
        good=out()
        bad=dict(good,ref="cron/output/job1/duplicate.md",
                 raw_sha256="different",content="## Response\n[CRON_FAILURE]")
        r=assess_item(item(),NOW,interval(),[good,bad],self.req())
        self.assertEqual((r["state"],r["reason_code"]),("SAFETY","CONFLICTING_CURRENT_OUTPUT"))

    def test_multiple_artifact_candidates_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"commitments").mkdir()
            outdir=root/"cron"/"output"/"job1"; outdir.mkdir(parents=True)
            journal=root/"lab"/"journal"; journal.mkdir(parents=True)
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[item()]}),"utf-8")
            (root/"cron"/"jobs.json").write_text(json.dumps({"jobs":[interval()]}),"utf-8")
            (outdir/"2026-10-17_13-10-33.md").write_text("## Response\n[SILENT]","utf-8")
            spec={"timezone":ZONE,"coverage":"implemented","required_artifacts":["checked"],
                  "artifact_sources":{"checked":{"path":"lab/journal/entries.jsonl","format":"jsonl",
                    "time_field":"at","time_format":"journal_utc","item_field":"item_id",
                    "job_field":"job_id","status_field":"result","success_values":["ok"],
                    "content_field":"body","required_text":"complete"}}}
            row={"at":"2026-10-17 11:11 UTC","item_id":"C0026","job_id":"job1",
                 "result":"ok","body":"complete"}
            (journal/"entries.jsonl").write_text(json.dumps(dict(row,id="J-1"))+"\n"+
                                                  json.dumps(dict(row,id="J-2"))+"\n","utf-8")
            self.assertEqual(assess_profile(root,"C0026",NOW,spec)["reason_code"],"MISSING_ARTIFACT")

    def test_artifact_path_traversal_does_not_satisfy_requirement(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"profile"; root.mkdir(); (root/"commitments").mkdir()
            outdir=root/"cron"/"output"/"job1"; outdir.mkdir(parents=True)
            outside=root.parent/"outside.json"; outside.write_text(json.dumps({"at":"2026-10-17T11:11:00+00:00",
                "item_id":"C0026","job_id":"job1","result":"ok","body":"complete"}),"utf-8")
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[item()]}),"utf-8")
            (root/"cron"/"jobs.json").write_text(json.dumps({"jobs":[interval()]}),"utf-8")
            (outdir/"2026-10-17_13-10-33.md").write_text("## Response\n[SILENT]","utf-8")
            spec={"timezone":ZONE,"coverage":"implemented","required_artifacts":["checked"],
                  "artifact_sources":{"checked":{"path":"../outside.json","format":"json",
                    "time_field":"at","item_field":"item_id","job_field":"job_id",
                    "status_field":"result","success_values":["ok"],"content_field":"body",
                    "required_text":"complete"}}}
            self.assertEqual(assess_profile(root,"C0026",NOW,spec)["reason_code"],"MISSING_ARTIFACT")

    def test_quota_held_interval_is_unknown_not_invalid_schedule(self):
        job=interval(last_status="error",last_error="Codex quota429",
                     next_run_at="2026-10-22T08:36:43.515815+02:00",
                     quota_hold_until="2026-10-22T08:36:43.515815+02:00")
        r=assess_item(item(),NOW,job,[],self.req())
        self.assertEqual((r["state"],r["reason_code"]),("GAP","QUOTA_HELD_UNKNOWN"))

    def test_profile_reads_only_supplied_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"commitments").mkdir()
            (root/"cron"/"output"/"job1").mkdir(parents=True)
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[item()]}),"utf-8")
            (root/"cron"/"jobs.json").write_text(json.dumps({"jobs":[interval()]}),"utf-8")
            (root/"cron"/"output"/"job1"/"2026-10-17_13-10-33.md").write_text(
                "## Prompt\nold [CRON_FAILURE]\n## Response\n[SILENT]","utf-8")
            self.assertEqual(assess_profile(root,"C0026",NOW,self.req())["state"],"OK")

if __name__=="__main__":
    unittest.main()
