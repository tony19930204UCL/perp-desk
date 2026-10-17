import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from engineering.progress_semantics.watcher_adapter import (
    assess_item, assess_profile, output_record, schedule_from_job)

ZONE = "Europe/Berlin"
NOW = datetime(2026, 10, 17, 13, 20, tzinfo=ZoneInfo(ZONE))

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

    def test_wall_timestamp_requires_explicit_zone(self):
        with self.assertRaises(ValueError):
            output_record("job1","cron/output/job1/2026-10-17_01-19-29.md","x","")

    def test_body_filename_mismatch_fails(self):
        text="**Run Time:** 2026-10-17 01:20:29\n**Mode:** no_agent (script)\n**Status:** silent (empty output)"
        with self.assertRaises(ValueError):
            output_record("job1","cron/output/job1/2026-10-17_01-19-29.md",text,ZONE)

    def test_weekly_not_due(self):
        weekly=cron("0 20 * * 0")
        old=out("2026-10-11_20-00-00")
        r=assess_item(item(),NOW,weekly,[old],self.req())
        self.assertEqual(r["state"],"OK")

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
