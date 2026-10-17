"""Read-only evidence adapter for the installed commitment checker."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import re

from .progress_semantics import evaluate

STAMP = re.compile(r"/([0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2})[.]md$")
CRON = re.compile(r"^(\d+)\s+(\d+)\s+\*\s+\*\s+(\*|[0-6])$")
JOB_LINE = re.compile(r"(?m)^\*\*Job ID:\*\*\s*([^\s]+)\s*$")
RUN_LINE = re.compile(r"(?m)^\*\*Run Time:\*\*\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$")
WRITER_SKEW_SECONDS = 1

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
        hold = job.get("quota_hold_until")
        if hold:
            held = aware(hold)
            if held >= nxt and job.get("last_status") == "error":
                return dict(base, frequency="anchored_interval", minutes=minutes,
                            last_run_at=last, next_run_at=nxt, quota_hold_until=held,
                            quota_held=True, last_error=job.get("last_error"))
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
        return dict(base, frequency="weekly", hour=int(hour), minute=int(minute),
                    weekday=(int(weekday) - 1) % 7)
    raise ValueError("unsupported cron schedule")

def due_slot(now, schedule):
    now = aware(now)
    if schedule["frequency"] != "anchored_interval":
        return None
    last = aware(schedule["last_run_at"])
    if schedule.get("quota_held"):
        return last
    nxt = aware(schedule["next_run_at"])
    step = timedelta(minutes=int(schedule["minutes"]))
    if now < nxt:
        return last
    count = int((now - nxt).total_seconds() // step.total_seconds())
    return nxt + count * step

def serialized_second(value):
    return aware(value).replace(microsecond=0)

def same_serialized_second(left, right):
    return serialized_second(left) == serialized_second(right)

def _wall_stamp(raw, timezone_name):
    zone = ZoneInfo(timezone_name)
    wall = datetime.strptime(raw, "%Y-%m-%d_%H-%M-%S")
    first = wall.replace(tzinfo=zone, fold=0)
    second = wall.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError("ambiguous wall timestamp")
    return first

def output_record(job_id, path, text, timezone_name, status=None):
    if not timezone_name:
        raise ValueError("timezone required")
    ref = str(path).replace("\\", "/")
    marker = STAMP.search("/" + ref.lstrip("/"))
    if not marker:
        raise ValueError("invalid cron output name")
    filename_at = _wall_stamp(marker.group(1), timezone_name)
    body_job = JOB_LINE.search(text)
    if body_job and body_job.group(1) != str(job_id):
        raise ValueError("output job mismatch")
    run = RUN_LINE.search(text)
    run_at = None
    if run:
        body_wall = datetime.strptime(run.group(1), "%Y-%m-%d %H:%M:%S")
        run_at = body_wall.replace(tzinfo=ZoneInfo(timezone_name))
        if abs((filename_at - run_at).total_seconds()) > WRITER_SKEW_SECONDS:
            raise ValueError("output timestamp mismatch")
    at = max(filename_at, run_at) if run_at else filename_at
    if "## Response" not in text and "**Mode:** no_agent" in text:
        body = "## Response\n[SILENT]" if "**Status:** silent" in text else "## Response\n" + text
    else:
        body = text
    return {"job_id": job_id, "at": at, "filename_at": filename_at,
            "run_at": run_at, "ref": ref, "content": body,
            "raw_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "status": status or "ok"}

def _safe_file(home, relative):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("unsafe evidence path")
    root = Path(home).resolve()
    target = root.joinpath(relative)
    resolved = target.resolve(strict=True)
    if root != resolved and root not in resolved.parents:
        raise ValueError("unsafe evidence path")
    if target.is_symlink() or not resolved.is_file():
        raise ValueError("unsafe evidence path")
    return resolved

def _field(row, selector):
    value = row
    for part in str(selector).split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value

def _journal_time(value):
    return datetime.strptime(str(value), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)

def resolve_artifacts(home, requirements, item_id, job_id, due, now):
    """Resolve configured files/JSONL on every profile assessment.

    A source is usable only when its configured selectors explicitly bind the
    evidence to this item/job/slot and its success/content requirements pass.
    """
    resolved = []
    specs = requirements.get("artifact_sources") or {}
    for name in requirements.get("required_artifacts") or []:
        spec = specs.get(name)
        if not isinstance(spec, dict):
            continue
        try:
            path = _safe_file(home, spec["path"])
            rows = []
            if spec.get("format") == "jsonl":
                for line in path.read_text("utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        if isinstance(row, dict):
                            rows.append(row)
            elif spec.get("format") == "json":
                row = json.loads(path.read_text("utf-8"))
                rows = [row] if isinstance(row, dict) else []
            else:
                continue
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            continue
        candidates = []
        for row in rows:
            try:
                stamp = _journal_time(_field(row, spec["time_field"])) if spec.get("time_format") == "journal_utc" else aware(_field(row, spec["time_field"]))
            except (KeyError, TypeError, ValueError):
                continue
            if stamp > aware(now) or stamp < aware(due):
                continue
            bindings = []
            for key, expected in (("item_field", item_id), ("job_field", job_id)):
                selector = spec.get(key)
                if selector:
                    bindings.append(str(_field(row, selector)) == str(expected))
            slot_field = spec.get("slot_field")
            if slot_field:
                try:
                    bindings.append(same_serialized_second(aware(_field(row, slot_field)), due))
                except (TypeError, ValueError):
                    bindings.append(False)
            if not bindings or not all(bindings):
                continue
            status_field = spec.get("status_field")
            if status_field and str(_field(row, status_field)) not in [str(x) for x in spec.get("success_values", [])]:
                continue
            content_field = spec.get("content_field")
            content = _field(row, content_field) if content_field else None
            required_text = spec.get("required_text")
            if required_text and required_text not in str(content or ""):
                continue
            candidates.append({"name": name, "at": stamp, "rc": 0,
                               "content": str(content or "bound"),
                               "ref": str(spec["path"])})
        if len(candidates) == 1:
            resolved.append(candidates[0])
    return resolved

def assess_item(item, now, job, outputs, requirements=None, artifacts=None):
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
        schedule = schedule_from_job(job, timezone_name, requirements.get("grace_minutes", 15))
    except (KeyError, TypeError, ValueError):
        return closed("SAFETY", "INVALID_SCHEDULE", job_id)
    required = requirements.get("required_artifacts")
    coverage = requirements.get("coverage")
    if required is None and coverage is None:
        return closed("GAP", "REQUIREMENTS_UNKNOWN", job_id)
    state = {"required_artifacts": list(required or [])}
    if coverage == "not_implemented":
        state["coverage"] = "not_implemented"
    elif coverage not in (None, "implemented"):
        return closed("GAP", "COVERAGE_UNKNOWN", job_id)
    evidence = {"runs": list(outputs), "files": list(artifacts or []), "journal": []}
    probe = dict(item); probe["kind"] = requirements.get("kind", "recurring")
    if schedule["frequency"] == "anchored_interval":
        if schedule.get("disabled"):
            return closed("SAFETY", "CRON_DISABLED", job_id)
        due = due_slot(now, schedule)
        current = [r for r in outputs if r.get("job_id") == job_id and
                   (aware(r["at"]) >= due or same_serialized_second(r["at"], due))]
        failed = [r for r in current if r.get("status") == "failed" or
                  "[CRON_FAILURE]" == str(r.get("content", "")).split("## Response")[-1].strip().splitlines()[0] if str(r.get("content", "")).split("## Response")[-1].strip().splitlines()]
        if failed:
            evidence["runs"] = current
        elif schedule.get("quota_held"):
            return closed("GAP", "QUOTA_HELD_UNKNOWN", job_id)
        grace = timedelta(minutes=float(schedule["grace_minutes"]))
        if aware(now) > due + grace and not current:
            return closed("OVERDUE", "CRON_OVERDUE", job_id)
        if not current:
            return closed("OK", "WITHIN_GRACE", job_id)
        selected = []
        for record in current:
            copy = dict(record)
            if same_serialized_second(record["at"], due):
                copy["source_at"] = record["at"]; copy["at"] = due
            selected.append(copy)
        evidence["runs"] = selected
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
    requirements = dict(requirements or {})
    timezone_name = requirements.get("timezone")
    if not timezone_name:
        return closed("SAFETY", "MISSING_TIMEZONE", job_id)
    outputs = []
    malformed = []
    out_dir = home / "cron" / "output" / str(job_id)
    if out_dir.is_dir():
        for path in sorted(out_dir.glob("*.md")):
            ref = "cron/output/" + str(job_id) + "/" + path.name
            try:
                outputs.append(output_record(job_id, ref, path.read_text("utf-8"), timezone_name))
            except (OSError, ValueError) as exc:
                marker = STAMP.search("/" + ref)
                stamp = None
                if marker:
                    try: stamp = _wall_stamp(marker.group(1), timezone_name)
                    except ValueError: pass
                malformed.append((stamp, ref, str(exc)))
    try:
        schedule = schedule_from_job(matches[0], timezone_name, requirements.get("grace_minutes", 15))
        due = due_slot(now, schedule) if schedule["frequency"] == "anchored_interval" else None
        if due is None:
            # Pure evaluator computes calendar due slots; malformed historical
            # records cannot abort profile assessment.
            from .progress_semantics import _slot
            due = _slot(aware(now), schedule)
    except (KeyError, TypeError, ValueError):
        return closed("SAFETY", "INVALID_SCHEDULE", job_id)
    current_bad = [x for x in malformed if x[0] is not None and aware(x[0]) >= aware(due)]
    if current_bad:
        return closed("SAFETY", "MALFORMED_CURRENT_OUTPUT", current_bad[-1][1])
    artifacts = resolve_artifacts(home, requirements, item_id, job_id, due, now)
    return assess_item(item, now, matches[0], outputs, requirements, artifacts)
