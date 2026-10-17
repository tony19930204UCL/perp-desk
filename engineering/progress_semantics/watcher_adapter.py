"""Read-only evidence adapter for the installed commitment checker."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import re

from .progress_semantics import evaluate

STAMP = re.compile(r"/([0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2})[.]md$")
CRON = re.compile(r"^(\d+)\s+(\d+)\s+\*\s+\*\s+(\*|[0-6])$")

def load_json(path):
    return json.loads(Path(path).read_text("utf-8"))

def aware(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value
    out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if out.tzinfo is None:
        raise ValueError("timezone required")
    return out

def schedule_from_job(job, timezone_name, grace_minutes=15):
    if not timezone_name:
        raise ValueError("timezone required")
    base = {"timezone": timezone_name, "job_id": (job or {}).get("id"),
            "grace_minutes": grace_minutes}
    if not job or not job.get("enabled", True) or job.get("state") == "paused" or job.get("paused"):
        return dict(base, frequency="anchored_interval", disabled=True, minutes=60)
    raw = job.get("schedule") or {}
    if raw.get("kind") == "interval":
        minutes = int(raw["minutes"])
        if minutes not in (1, 2, 5, 10, 15, 60, 1440):
            raise ValueError("unsupported interval")
        last = aware(job.get("last_run_at"))
        nxt = aware(job.get("next_run_at"))
        if nxt <= last or abs((nxt - last).total_seconds() - minutes * 60) > 2:
            raise ValueError("invalid interval anchors")
        return dict(base, frequency="anchored_interval", minutes=minutes,
                    last_run_at=last, next_run_at=nxt)
    if raw.get("kind") == "cron":
        match = CRON.fullmatch(str(raw.get("expr", "")).strip())
        if not match:
            raise ValueError("unsupported cron expression")
        minute, hour, weekday = match.groups()
        if weekday == "*":
            return dict(base, frequency="daily", hour=int(hour), minute=int(minute))
        # cron Sunday=0; Python Monday=0.
        return dict(base, frequency="weekly", hour=int(hour), minute=int(minute),
                    weekday=(int(weekday) - 1) % 7)
    raise ValueError("unsupported cron schedule")

def due_slot(now, schedule):
    now = aware(now)
    if schedule["frequency"] != "anchored_interval":
        return None
    last = aware(schedule["last_run_at"])
    nxt = aware(schedule["next_run_at"])
    step = timedelta(minutes=int(schedule["minutes"]))
    if now < nxt:
        return last
    count = int((now - nxt).total_seconds() // step.total_seconds())
    return nxt + count * step

def output_record(job_id, path, text, timezone_name, status="ok"):
    if not timezone_name:
        raise ValueError("timezone required")
    ref = str(path).replace("\\", "/")
    marker = STAMP.search("/" + ref.lstrip("/"))
    if not marker:
        raise ValueError("invalid cron output name")
    zone = ZoneInfo(timezone_name)
    wall = datetime.strptime(marker.group(1), "%Y-%m-%d_%H-%M-%S")
    first = wall.replace(tzinfo=zone, fold=0)
    second = wall.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("ambiguous wall timestamp")
    at = first
    run = re.search(r"(?m)^\*\*Run Time:\*\*\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$", text)
    if run:
        body_wall = datetime.strptime(run.group(1), "%Y-%m-%d %H:%M:%S")
        if body_wall != wall:
            raise ValueError("output timestamp mismatch")
    if "## Response" not in text and "**Mode:** no_agent" in text:
        body = "## Response\n[SILENT]" if "**Status:** silent" in text else "## Response\n" + text
    else:
        body = text
    return {"job_id": job_id, "at": at, "ref": ref, "content": body,
            "status": "failed" if status not in ("ok", "silent") else "ok"}

def assess_item(item, now, job, outputs, requirements=None):
    requirements = dict(requirements or {})
    handle = item.get("handle") or {}
    if handle.get("kind") != "cron":
        raise ValueError("cron handle required")
    job_id = handle.get("ref")
    if not job or job.get("id") != job_id:
        return closed("SAFETY", "MISSING_CRON_JOB", job_id)
    timezone_name = requirements.get("timezone")
    if not timezone_name:
        return closed("SAFETY", "MISSING_TIMEZONE", job_id)
    try:
        schedule = schedule_from_job(job, timezone_name,
                                     requirements.get("grace_minutes", 15))
    except (KeyError, TypeError, ValueError):
        return closed("SAFETY", "INVALID_SCHEDULE", job_id)
    state = {}
    required = requirements.get("required_artifacts")
    coverage = requirements.get("coverage")
    if required is None and coverage is None:
        return closed("GAP", "REQUIREMENTS_UNKNOWN", job_id)
    state["required_artifacts"] = list(required or [])
    if coverage == "not_implemented":
        state["coverage"] = "not_implemented"
    elif coverage not in (None, "implemented"):
        return closed("GAP", "COVERAGE_UNKNOWN", job_id)
    evidence = {"runs": list(outputs), "files": list(requirements.get("artifacts", [])),
                "journal": []}
    probe = dict(item)
    probe["kind"] = requirements.get("kind", "recurring")
    if schedule["frequency"] == "anchored_interval":
        if schedule.get("disabled"):
            return closed("SAFETY", "CRON_DISABLED", job_id)
        due = due_slot(now, schedule)
        grace = timedelta(minutes=float(schedule["grace_minutes"]))
        current = [r for r in outputs if aware(r["at"]) >= due]
        # The slot is overdue only after its own bounded grace. Before that,
        # absence is not yet a missed run even if the previous slot is old.
        if aware(now) > due + grace and not current:
            return closed("OVERDUE", "CRON_OVERDUE", job_id)
        if not current:
            return closed("OK", "WITHIN_GRACE", job_id)
        # evaluate against a synthetic one-off slot without changing evidence.
        local = due.astimezone(ZoneInfo(timezone_name))
        schedule = {"timezone": timezone_name, "frequency": "daily",
                    "hour": local.hour, "minute": local.minute, "job_id": job_id,
                    "grace_minutes": schedule["grace_minutes"]}
    return evaluate(probe, now, schedule, evidence, state)

def closed(state, reason, ref):
    return {"state": state, "reason_code": reason, "evidence_ref": ref,
            "evidence_age_hours": None, "alert": state != "OK",
            "suppressed_by_cooldown": False, "suggested_kind": None}

def assess_profile(home, item_id, now, requirements=None):
    home = Path(home)
    ledger = load_json(home / "commitments" / "ledger.json")
    jobs = load_json(home / "cron" / "jobs.json")
    items = [x for x in ledger.get("items", []) if x.get("id") == item_id]
    if len(items) != 1:
        return closed("SAFETY", "MISSING_LEDGER_ITEM", item_id)
    item = items[0]
    if item.get("state") in ("VERIFIED", "DROPPED"):
        return closed("OK", "TERMINAL_UNCHANGED", item_id)
    handle = item.get("handle") or {}
    if handle.get("kind") != "cron":
        return closed("NEEDS_RECLASSIFY", "NON_CRON_UNCHANGED", item_id)
    job_id = handle.get("ref")
    matches = [x for x in jobs.get("jobs", []) if x.get("id") == job_id]
    if len(matches) != 1:
        return closed("SAFETY", "MISSING_CRON_JOB", job_id)
    timezone_name = (requirements or {}).get("timezone")
    if not timezone_name:
        return closed("SAFETY", "MISSING_TIMEZONE", job_id)
    outputs = []
    out_dir = home / "cron" / "output" / str(job_id)
    if out_dir.is_dir():
        for path in sorted(out_dir.glob("*.md")):
            outputs.append(output_record(job_id, "cron/output/" + str(job_id) + "/" + path.name,
                          path.read_text("utf-8"), timezone_name,
                          matches[0].get("last_status", "ok")))
    return assess_item(item, now, matches[0], outputs, requirements)
