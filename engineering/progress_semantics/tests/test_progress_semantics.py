import unittest
from datetime import datetime, timezone
from engineering.progress_semantics import evaluate, parse_output

NOW = datetime(2026, 10, 16, 22, tzinfo=timezone.utc)
def run(job, at="2026-10-16T21:00:00+00:00", content="## Prompt\n[CRON_FAILURE]\n## Response\n[SILENT]"):
    return {"job_id": job, "at": at, "content": content,
            "ref": "cron/output/" + job + "/2026-10-16_21-00-00.md"}
def check(ident, job, frequency="daily", **extra):
    item = {"id": ident, "kind": "standing", "updated": "2020-01-01T00:00:00+00:00"}
    schedule = dict(timezone="UTC", frequency=frequency, job_id=job, hour=21, grace_minutes=15)
    schedule.update(extra)
    return item, schedule
class ProgressTests(unittest.TestCase):
    def test_prompt_token_ignored(self):
        self.assertEqual(parse_output("## Prompt\n[CRON_FAILURE]\n## Response\n[SILENT]"), (True, "SILENT"))
    def test_response_failure(self):
        self.assertEqual(parse_output("## Prompt\nhello\n## Response\n[CRON_FAILURE]\nerror"), (False, "CRON_FAILED"))
    def test_empty_response(self):
        self.assertFalse(parse_output("## Prompt\nhello\n## Response\n")[0])
    def test_missing_response(self):
        self.assertFalse(parse_output("## Prompt\nhello")[0])
    def test_scorecard_c0026(self):
        item, sched = check("C0026", "9dc79f48761f", "hourly")
        self.assertEqual(evaluate(item, NOW, sched, {"runs":[run(sched["job_id"])]}, {})["state"], "OK")
    def test_gap_c0003(self):
        item, sched = check("C0003", "93f4f9882e43", "hourly")
        self.assertEqual(evaluate(item, NOW, sched, {"runs":[run(sched["job_id"])]}, {"coverage":"not_implemented"})["reason_code"], "COVERAGE_NOT_IMPLEMENTED")
    def test_install_c0004(self):
        item, sched = check("C0004", "unused")
        item.update(one_shot=True, delivered=True, delivery_ref="install-proof")
        r = evaluate(item, NOW, sched, {}, {})
        self.assertEqual((r["state"], r["suggested_kind"], r["evidence_ref"]), ("NEEDS_RECLASSIFY","one_shot_delivered","install-proof"))
    def test_daily_c0027(self):
        item, sched = check("C0027", "fa25edbf3834")
        self.assertEqual(evaluate(item, NOW, sched, {"runs":[run(sched["job_id"])]}, {})["state"], "OK")
    def test_weekly_c0028(self):
        item, sched = check("C0028", "213f30fc8f7c", "weekly", weekday=6, hour=20)
        self.assertEqual(evaluate(item, NOW, sched, {"runs":[run(sched["job_id"],"2026-10-11T20:00:00+00:00")]}, {})["state"], "OK")
    def test_supervisor_c0041(self):
        item, sched = check("C0041", "6997c01c6d9e", "every_n_minutes", minutes=30)
        self.assertEqual(evaluate(item, NOW, sched, {"runs":[run(sched["job_id"])]}, {})["state"], "OK")
    def test_daily_delegates(self):
        for ident in ("C0062", "C0069"):
            with self.subTest(ident=ident):
                item, sched = check(ident, "fa25edbf3834")
                self.assertEqual(evaluate(item, NOW, sched, {"runs":[run(sched["job_id"])], "journal":[{"item_id":ident}]}, {})["state"], "OK")
    def test_future(self):
        item, sched = check("C0027", "fa25edbf3834")
        self.assertEqual(evaluate(item, NOW, sched, {"runs":[run(sched["job_id"],"2026-10-17T00:00:00+00:00")]}, {})["reason_code"], "FUTURE_EVIDENCE")
    def test_missing_output(self):
        item, sched = check("C0027", "fa25edbf3834")
        self.assertEqual(evaluate(item, NOW, sched, {}, {})["state"], "OVERDUE")
    def test_failed(self):
        item, sched = check("C0027", "fa25edbf3834")
        r = run(sched["job_id"]); r["content"]="## Prompt\nhello\n## Response\n[CRON_FAILURE]"
        self.assertEqual(evaluate(item, NOW, sched, {"runs":[r]}, {})["state"], "SAFETY")
    def test_cooldown(self):
        item, sched = check("C0003", "93f4f9882e43")
        item["one_shot"]=True
        state={"last_alerts":{"C0003:ONE_SHOT_MISLABELLED":"2026-10-16T20:00:00+00:00"}}
        self.assertTrue(evaluate(item, NOW, sched, {}, state)["suppressed_by_cooldown"])
    def test_disabled_not_suppressed(self):
        item, sched = check("C0027", "fa25edbf3834", disabled=True)
        self.assertTrue(evaluate(item, NOW, sched, {}, {})["alert"])
if __name__ == "__main__":
    unittest.main()
