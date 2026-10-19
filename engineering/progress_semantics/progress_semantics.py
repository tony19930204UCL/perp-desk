"""Pure progress evidence evaluation; no I/O."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re

def parse_output(text):
    """Return (success, reason) from the Response section only."""
    if not isinstance(text, str):
        return False, "MISSING_OUTPUT"
    parts = re.split(r"(?m)^## Response\s*$", text, maxsplit=1)
    if len(parts) != 2:
        return False, "MISSING_RESPONSE"
    response = parts[1].split("\n## ", 1)[0].strip()
    if not response:
        return False, "EMPTY_RESPONSE"
    if response.splitlines()[0].strip() == "[CRON_FAILURE]":
        return False, "CRON_FAILED"
    return True, "SILENT" if response == "[SILENT]" else "SUCCESS"

def _time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timezone required")
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("timezone required")
        return result
    raise ValueError("invalid timestamp")

def _slot(now, schedule):
    zone = ZoneInfo(schedule["timezone"])
    local = now.astimezone(zone)
    frequency = schedule["frequency"]
    if frequency == "hourly":
        return local.replace(minute=int(schedule.get("minute", 0)), second=0, microsecond=0) - (timedelta(hours=1) if local.minute < int(schedule.get("minute", 0)) else timedelta())
    if frequency == "every_n_minutes":
        step = int(schedule["minutes"])
        if not 1 <= step <= 1440 or 1440 % step:
            raise ValueError("invalid interval")
        minute = (local.hour * 60 + local.minute) // step * step
        return local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    hour, minute = int(schedule.get("hour", 0)), int(schedule.get("minute", 0))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if frequency == "daily":
        return candidate - (timedelta(days=1) if candidate > local else timedelta())
    if frequency == "weekly":
        weekday = int(schedule["weekday"])
        candidate -= timedelta(days=(candidate.weekday() - weekday) % 7)
        if candidate > local:
            candidate -= timedelta(days=7)
        return candidate
    raise ValueError("unsupported frequency")

def evaluate(item, now, schedule, evidence, state, cooldown_hours=6):
    """Evaluate without mutating any argument.

    evidence: dict with runs, journal, files lists. state: coverage,
    required_artifacts, last_alerts, window_invalidated, state_stopped.
    """
    now = _time(now)
    ident = str(item.get("id", ""))
    def result(status, reason, ref=None, stamp=None):
        age = (now - stamp).total_seconds() / 3600 if stamp else None
        safety = status == "SAFETY" or reason in {"CRON_FAILED", "CRON_DISABLED", "CRON_OVERDUE", "STATE_STOPPED", "WINDOW_INVALIDATED"}
        prior = state.get("last_alerts", {}).get(ident + ":" + reason)
        quiet = False
        if prior is not None and not safety and status != "OK":
            try:
                delta = (now - _time(prior)).total_seconds() / 3600
                quiet = 0 <= delta < cooldown_hours
            except (ValueError, TypeError, OverflowError):
                pass
        return {"state": status, "reason_code": reason, "evidence_ref": ref,
                "evidence_age_hours": age, "alert": status != "OK" and not quiet,
                "suppressed_by_cooldown": quiet,
                "suggested_kind": ("one_shot_delivered" if item.get("delivered") else "one_shot_open") if status == "NEEDS_RECLASSIFY" else None}
    if state.get("window_invalidated"):
        return result("SAFETY", "WINDOW_INVALIDATED")
    if state.get("state_stopped"):
        return result("SAFETY", "STATE_STOPPED")
    if item.get("one_shot") or item.get("deliverable_kind") == "one_shot":
        return result("NEEDS_RECLASSIFY", "ONE_SHOT_MISLABELLED", item.get("delivery_ref"))
    if item.get("kind") not in ("standing", "recurring"):
        stamp = _time(item["updated"]) if item.get("updated") else None
        return result("STALE", "NO_PROGRESS", None, stamp) if stamp and (now-stamp).total_seconds()>72*3600 else result("OK", "NOT_DUE")
    if not schedule or not schedule.get("timezone"):
        return result("SAFETY", "INVALID_SCHEDULE")
    if state.get("disabled") or schedule.get("disabled"):
        return result("SAFETY", "CRON_DISABLED")
    due = _slot(now, schedule).astimezone(timezone.utc)
    grace = timedelta(minutes=float(schedule.get("grace_minutes", 15)))
    job = schedule.get("job_id")
    runs = [r for r in evidence.get("runs", []) if r.get("job_id") == job]
    for r in runs:
        if r.get("at") and _time(r["at"]) > now:
            return result("SAFETY", "FUTURE_EVIDENCE", r.get("ref"), _time(r["at"]))
    runs.sort(key=lambda r: _time(r["at"]), reverse=True)
    latest = runs[0] if runs else None
    if latest:
        stamp = _time(latest["at"])
        if latest.get("status") in ("failed", "disabled"):
            return result("SAFETY", "CRON_FAILED" if latest["status"] == "failed" else "CRON_DISABLED", latest.get("ref"), stamp)
        good, why = parse_output(latest.get("content"))
        if not good:
            return result("SAFETY", why, latest.get("ref"), stamp)
        if not latest.get("ref", "").startswith("cron/output/" + str(job) + "/"):
            return result("SAFETY", "INVALID_OUTPUT_PATH", latest.get("ref"), stamp)
        if state.get("expected_hash") and latest.get("script_hash") != state["expected_hash"]:
            return result("SAFETY", "SCRIPT_MISMATCH", latest.get("ref"), stamp)
    else:
        stamp = None
    if now.astimezone(timezone.utc) > due + grace and (stamp is None or stamp < due):
        return result("OVERDUE", "CRON_OVERDUE", latest.get("ref") if latest else None, stamp)
    if state.get("coverage") == "not_implemented":
        return result("GAP", "COVERAGE_NOT_IMPLEMENTED", latest.get("ref") if latest else None, stamp)
    for name in state.get("required_artifacts", []):
        candidates = [f for f in evidence.get("files", []) if f.get("name") == name]
        if any(f.get("at") and _time(f["at"]) > now for f in candidates):
            return result("SAFETY", "FUTURE_EVIDENCE", name)
        matches = [f for f in candidates
                   if f.get("content") and f.get("rc") == 0
                   and f.get("binding") == "explicit"
                   and f.get("at") and _time(f["at"]) >= due]
        if len(matches) != 1:
            return result("STALE", "MISSING_ARTIFACT", name)
    if latest and stamp >= due:
        return result("OK", "EVIDENCE_CURRENT", latest.get("ref"), stamp)
    if now.astimezone(timezone.utc) <= due + grace:
        return result("OK", "WITHIN_GRACE", latest.get("ref") if latest else None, stamp)
    return result("OVERDUE", "CRON_OVERDUE")
