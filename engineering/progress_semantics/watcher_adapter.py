"""Read-only profile evidence adapter for the installed commitment watcher chain."""
from datetime import datetime, timezone
from pathlib import Path
import json
import re

from .progress_semantics import evaluate

STAMP = re.compile(r"/([0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2})[.]md$")

def load_json(path):
    return json.loads(Path(path).read_text("utf-8"))

def schedule_from_job(job, timezone_name="UTC", grace_minutes=15):
    if not job or not job.get("enabled") or job.get("state") != "scheduled" or job.get("paused"):
        return {"timezone": timezone_name, "frequency": "hourly",
                "job_id": (job or {}).get("id"), "disabled": True,
                "grace_minutes": grace_minutes}
    raw = job.get("schedule") or {}
    kind = raw.get("kind")
    base = {"timezone": timezone_name, "job_id": job.get("id"),
            "grace_minutes": grace_minutes}
    if kind == "interval":
        minutes = int(raw["minutes"])
        if minutes == 60:
            base.update(frequency="hourly", minute=0)
        else:
            base.update(frequency="every_n_minutes", minutes=minutes)
        return base
    if kind in ("daily", "weekly"):
        base.update(frequency=kind, hour=int(raw["hour"]),
                    minute=int(raw.get("minute", 0)))
        if kind == "weekly":
            base["weekday"] = int(raw["weekday"])
        return base
    raise ValueError("unsupported cron schedule")

def output_record(job_id, path, text, status="ok"):
    ref = str(path).replace("\\", "/")
    marker = STAMP.search("/" + ref.lstrip("/"))
    if not marker:
        raise ValueError("invalid cron output name")
    at = datetime.strptime(marker.group(1), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)
    # no_agent output has no Response section. A silent status is successful
    # execution evidence only; it never proves required artifacts or coverage.
    if "## Response" not in text and "**Mode:** no_agent" in text:
        body = "## Response\n[SILENT]" if "**Status:** silent" in text else "## Response\n" + text
    else:
        body = text
    return {"job_id": job_id, "at": at, "ref": ref, "content": body,
            "status": "failed" if status not in ("ok", "silent") else "ok"}

def assess_item(item, now, job, outputs, requirements=None):
    """Assess a recurring cron item without changing ledger fields."""
    requirements = dict(requirements or {})
    if not item.get("handle") or item["handle"].get("kind") != "cron":
        raise ValueError("cron handle required")
    job_id = item["handle"].get("ref")
    if not job or job.get("id") != job_id:
        return _closed("SAFETY", "MISSING_CRON_JOB", job_id)
    try:
        schedule = schedule_from_job(job, requirements.get("timezone", "UTC"),
                                     requirements.get("grace_minutes", 15))
    except (KeyError, TypeError, ValueError):
        return _closed("SAFETY", "INVALID_SCHEDULE", job_id)
    state = {}
    required = requirements.get("required_artifacts")
    coverage = requirements.get("coverage")
    if required is None and coverage is None:
        # Supplied contract has no journal/coverage reader. Unknown proof fails
        # closed rather than turning rc0 into product progress.
        state["required_artifacts"] = ["coverage-contract"]
    else:
        state["required_artifacts"] = list(required or [])
        if coverage == "not_implemented":
            state["coverage"] = "not_implemented"
        elif coverage not in (None, "implemented"):
            return _closed("GAP", "COVERAGE_UNKNOWN", job_id)
    evidence = {"runs": list(outputs), "files": list(requirements.get("artifacts", [])),
                "journal": []}
    probe = dict(item)
    probe["kind"] = requirements.get("kind", "recurring")
    return evaluate(probe, now, schedule, evidence, state)

def _closed(state, reason, ref):
    return {"state": state, "reason_code": reason, "evidence_ref": ref,
            "evidence_age_hours": None, "alert": True,
            "suppressed_by_cooldown": False, "suggested_kind": None}

def assess_profile(home, item_id, now, requirements=None):
    """Read only the supplied profile contract; caller owns existing mutations."""
    home = Path(home)
    ledger = load_json(home / "commitments" / "ledger.json")
    jobs = load_json(home / "cron" / "jobs.json")
    items = [x for x in ledger.get("items", []) if x.get("id") == item_id]
    if len(items) != 1:
        return _closed("SAFETY", "MISSING_LEDGER_ITEM", item_id)
    item = items[0]
    if item.get("state") in ("VERIFIED", "DROPPED"):
        return _closed("OK", "TERMINAL_UNCHANGED", item_id)
    handle = item.get("handle") or {}
    if handle.get("kind") != "cron":
        return _closed("NEEDS_RECLASSIFY", "NON_CRON_UNCHANGED", item_id)
    job_id = handle.get("ref")
    matches = [x for x in jobs.get("jobs", []) if x.get("id") == job_id]
    if len(matches) != 1:
        return _closed("SAFETY", "MISSING_CRON_JOB", job_id)
    out_dir = home / "cron" / "output" / str(job_id)
    outputs = []
    if out_dir.is_dir():
        for path in sorted(out_dir.glob("*.md")):
            outputs.append(output_record(job_id, "cron/output/" + str(job_id) + "/" + path.name,
                                         path.read_text("utf-8"), matches[0].get("last_status", "ok")))
    return assess_item(item, now, matches[0], outputs, requirements)
