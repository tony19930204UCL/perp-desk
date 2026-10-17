import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from engineering.progress_semantics.watcher_adapter import (
    assess_item, assess_profile, output_record, schedule_from_job)

NOW = datetime(2026, 10, 17, 2, 30, tzinfo=timezone.utc)

def item(ident="C0026", job="job1"):
    return {"id": ident, "state": "DISPATCHED", "updated": 1,
            "handle": {"kind": "cron", "ref": job}, "history": []}

def job(schedule=None, **extra):
    data = {"id": "job1", "enabled": True, "state": "scheduled",
            "paused": None, "last_status": "ok",
            "schedule": schedule or {"kind": "interval", "minutes": 60}}
    data.update(extra)
    return data

def out(at="2026-10-17_02-00-00", text="## Prompt\nquoted [CRON_FAILURE]\n## Response\n[SILENT]"):
    return output_record("job1", "cron/output/job1/" + at + ".md", text)

class WatcherAdapterTests(unittest.TestCase):
    def test_prompt_failure_is_quoted_only(self):
        r = assess_item(item(), NOW, job(), [out()],
                        {"coverage": "implemented"})
        self.assertEqual(r["state"], "OK")

    def test_current_response_failure(self):
        r = assess_item(item(), NOW, job(),
                        [out(text="## Prompt\nold [CRON_FAILURE]\n## Response\n[CRON_FAILURE]\nboom")],
                        {"coverage": "implemented"})
        self.assertEqual((r["state"], r["reason_code"]), ("SAFETY", "CRON_FAILED"))

    def test_no_agent_silent_is_execution_only(self):
        text = "# Cron Job: x\n\n**Mode:** no_agent (script)\n**Status:** silent (empty output)"
        r = assess_item(item(), NOW, job(), [out(text=text)])
        self.assertEqual(r["reason_code"], "MISSING_ARTIFACT")

    def test_missing_coverage_fails_closed(self):
        r = assess_item(item(), NOW, job(), [out()])
        self.assertEqual(r["reason_code"], "MISSING_ARTIFACT")

    def test_known_missing_coverage_is_gap(self):
        r = assess_item(item("C0003"), NOW, job(), [out()],
                        {"coverage": "not_implemented"})
        self.assertEqual((r["state"], r["reason_code"]),
                         ("GAP", "COVERAGE_NOT_IMPLEMENTED"))

    def test_disabled_is_safety(self):
        r = assess_item(item(), NOW, job(enabled=False), [out()],
                        {"coverage": "implemented"})
        self.assertEqual(r["reason_code"], "CRON_DISABLED")

    def test_overdue_missing_output(self):
        r = assess_item(item(), NOW, job(), [],
                        {"coverage": "implemented"})
        self.assertEqual(r["state"], "OVERDUE")

    def test_weekly_not_due_uses_previous_slot(self):
        weekly = job({"kind": "weekly", "weekday": 6, "hour": 20})
        old = out("2026-10-11_20-00-00")
        r = assess_item(item(), NOW, weekly, [old],
                        {"coverage": "implemented"})
        self.assertEqual(r["state"], "OK")

    def test_profile_reads_supplied_paths_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "commitments").mkdir()
            (root / "cron" / "output" / "job1").mkdir(parents=True)
            (root / "commitments" / "ledger.json").write_text(
                json.dumps({"items": [item()]}), "utf-8")
            (root / "cron" / "jobs.json").write_text(
                json.dumps({"jobs": [job()]}), "utf-8")
            (root / "cron" / "output" / "job1" / "2026-10-17_02-00-00.md").write_text(
                "## Prompt\nold [CRON_FAILURE]\n## Response\n[SILENT]", "utf-8")
            r = assess_profile(root, "C0026", NOW, {"coverage": "implemented"})
            self.assertEqual(r["state"], "OK")

    def test_non_cron_is_not_auto_closed(self):
        x = item(); x["handle"] = {"kind": "manual", "ref": "m"}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"commitments").mkdir(); (root/"cron").mkdir()
            (root/"commitments"/"ledger.json").write_text(json.dumps({"items":[x]}),"utf-8")
            (root/"cron"/"jobs.json").write_text(json.dumps({"jobs":[]}),"utf-8")
            r=assess_profile(root,"C0026",NOW)
            self.assertEqual(r["state"],"NEEDS_RECLASSIFY")

if __name__ == "__main__":
    unittest.main()
