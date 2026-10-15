"""Read-only, bounded projection of owner activity from one trusted profile.

No process discovery, scheduler execution, private log export or write operations.
Execution and result observations must be independently injected and identity-matched.
"""
from datetime import datetime, timezone
import json
import math
import os
import re
import stat

SOURCES = {
    "ledger": ("commitments", "ledger.json"),
    "cron": ("cron", "jobs.json"),
    "roadmap": ("lab", "journal", "roadmap.json"),
    "work": ("lab", "shared", "work_status.json"),
    "journal": ("lab", "journal", "entries.jsonl"),
}
MAX_ITEMS = 500
ACTIVE = {"running", "testing", "verifying"}


def _stamp(value):
    """Declared epoch seconds or timezone-aware ISO/roadmap UTC; never bool/NaN."""
    if type(value) in (int, float):
        if not math.isfinite(value) or not 946684800 <= value < 4102444800:
            return None
        return int(value * 1000)
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", value):
            value = value[:-4] + "+00:00"
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.utcoffset() is None:
            return None
        ms = int(dt.timestamp() * 1000)
        return ms if 946684800000 <= ms < 4102444800000 else None
    except (ValueError, OverflowError, OSError):
        return None


def _time(value):
    ms = _stamp(value)
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms is not None else None


_PUBLIC_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_PRIVATE = re.compile(r"(?:[/\\]|://|@|[\x00-\x1f]|\b(?:token|secret|passwd|password|api.?key|credential|private.?key)\b)", re.I)


def _public_note(value, limit=240):
    """Conservative projection; uncertain history/defer contents are hidden."""
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) > limit or _PRIVATE.search(value):
        return "[private or unsupported note withheld]"
    return value


def _blocker(value):
    """Typed opaque blocker. Never echo command text, file paths or user questions."""
    if not isinstance(value, str) or ":" not in value:
        return None
    kind, raw = value.split(":", 1)
    if kind not in ("item", "cmd", "file", "user") or not raw.strip():
        return None
    if kind == "item":
        reference = raw if _PUBLIC_REF.fullmatch(raw) else None
        return {"type": kind, "ref": reference,
                "label": ("item:" + reference) if reference else "item:[reference withheld]"}
    return {"type": kind, "ref": None, "label": {
        "cmd": "cmd:[command withheld]", "file": "file:[path withheld]",
        "user": "user:[question withheld]"}[kind]}


def _safe(value, limit=240):
    return value[:limit] if isinstance(value, str) and value.strip() else None


def _name(value):
    return _safe(value, 100)


def _objects(value):
    return value[:MAX_ITEMS] if isinstance(value, list) else []


def _read_bytes(root, parts, limit, tail=False):
    """Open only literal relative components with O_NOFOLLOW, no path inputs."""
    if type(limit) is not int or not 128 <= limit <= 4 * 1024 * 1024:
        raise ValueError("invalid byte limit")
    if not isinstance(root, str) or not os.path.isabs(root):
        raise ValueError("absolute trusted profile root required")
    if os.path.realpath(root) != os.path.normpath(root):
        raise ValueError("symlinked profile root")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    directory = os.open(root, flags | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], flags, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not regular")
            if not tail and info.st_size > limit:
                raise ValueError("over limit")
            start = max(0, info.st_size - limit) if tail else 0
            os.lseek(fd, start, os.SEEK_SET)
            data = bytearray()
            while len(data) < limit + (0 if tail else 1):
                block = os.read(fd, min(65536, limit + (0 if tail else 1) - len(data)))
                if not block:
                    break
                data.extend(block)
            if not tail and len(data) > limit:
                raise ValueError("over limit")
            return bytes(data), start
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def _read_source(root, key, limit):
    try:
        raw, offset = _read_bytes(root, SOURCES[key], limit, tail=(key == "journal"))
        if key == "journal":
            # A truncated first line and a partial last line are never trusted.
            if offset:
                sep = raw.find(b"\n")
                raw = raw[sep + 1:] if sep >= 0 else b""
            if raw and not raw.endswith(b"\n"):
                raw = raw[:raw.rfind(b"\n") + 1]
            rows = []
            for line in raw.splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                    if isinstance(row, dict):
                        rows.append(row)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    continue
            return rows[-20:], "available"
        data = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data, dict):
            raise ValueError("not object")
        key_required = {"ledger": "items", "cron": "jobs", "roadmap": "items", "work": "tasks"}[key]
        if not isinstance(data.get(key_required), list) or len(data[key_required]) > MAX_ITEMS:
            raise ValueError("invalid collection")
        return data, "available"
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None, "invalid"


def _proof_for(proofs, key):
    return proofs.get(key) if isinstance(proofs, dict) and isinstance(proofs.get(key), dict) else None


def build_activity(ledger, cron, roadmap, work, journal, *, now_ms, max_age_ms=86400000,
                   execution_proofs=None, result_proofs=None, source_states=None):
    """Pure projection. No percentage, liveness, deployment or success inference."""
    if type(now_ms) is not int or type(max_age_ms) is not int or max_age_ms < 0:
        raise ValueError("invalid clock")
    inputs = {"ledger": ledger, "cron": cron, "roadmap": roadmap, "work": work, "journal": journal}
    states = source_states or {k: "available" if v is not None else "missing" for k, v in inputs.items()}
    sources, warnings = {}, []
    for key, value in inputs.items():
        status = states.get(key, "missing")
        raw = None
        basis = "source_timestamp"
        if isinstance(value, dict):
            if key == "cron":
                raw = value.get("updated_at")
            elif key == "roadmap":
                raw = value.get("updated")
            elif key in ("ledger", "work"):
                field = "updated" if key == "ledger" else "updated_at"
                records = value.get("items") if key == "ledger" else value.get("tasks")
                times = [record.get(field) for record in _objects(records)
                         if isinstance(record, dict) and record.get(field) is not None]
                valid = [_stamp(x) for x in times]
                basis = "latest_item_bookkeeping" if key == "ledger" else "latest_task_update"
                if times and any(x is None for x in valid):
                    status = "conflict" if status == "available" else status
                elif valid:
                    raw = max(valid) / 1000
            elif key == "journal":
                pass
        elif key == "journal" and isinstance(value, list):
            times = [row.get("at") for row in value if isinstance(row, dict) and row.get("at") is not None]
            valid = [_stamp(x) for x in times]
            basis = "latest_public_journal_event"
            if times and any(x is None for x in valid):
                status = "conflict" if status == "available" else status
            elif valid:
                raw = max(valid) / 1000
        at = _stamp(raw)
        if status == "available":
            if at is None:
                status = "unverifiable"
            elif at > now_ms + 5000:
                status = "conflict"
            elif now_ms - at > max_age_ms:
                status = "stale"
        if status != "available":
            warnings.append(key + ": " + status)
        sources[key] = {"state": status, "updated_at": _time(raw), "clock_basis": basis}
    items = {}
    def get(identifier):
        return items.setdefault(identifier, {
            "id": identifier, "title": None, "commitment": None, "state": "planned",
            "kind": "commitment", "stage": None, "stage_status": None, "why": None,
            "acceptance": None, "next": None, "owner": None, "step": None,
            "started_at": None, "progress_at": None, "due": None,
            "recheck_at": None, "defer_reason": None, "deferral_state": None,
            "dependencies": [], "blocker_details": [],
            "blockers": [], "evidence": [], "warnings": [], "updated_at": None,
            "engineering_accepted": False, "deployed": "unconfirmed",
            "unattended": "unconfirmed", "activity": "not_proven",
            "schedule": None, "history": [], "source_keys": [],
        })
    def touch(item, source):
        if source not in item["source_keys"]:
            item["source_keys"].append(source)
    ledger_rows = _objects(ledger.get("items")) if isinstance(ledger, dict) else []
    ledger_index = {}
    jobs_link = {}
    for row in ledger_rows:
        if not isinstance(row, dict) or not _name(row.get("id")):
            warnings.append("ledger: invalid id")
            continue
        identifier = _name(row["id"])
        if identifier in ledger_index:
            warnings.append("ledger: duplicate id")
            get(identifier)["warnings"].append("duplicate ledger id")
            continue
        ledger_index[identifier] = row
        item = get(identifier)
        touch(item, "ledger")
        item["title"] = _safe(row.get("text"))
        item["commitment"] = item["title"]
        item["why"] = _safe(row.get("reason"))
        item["acceptance"] = _safe(row.get("accept"), 600)
        item["due"] = _time(row.get("due"))
        item["created_at"] = _time(row.get("created"))
        item["updated_at"] = _time(row.get("updated"))
        for key in ("created", "updated", "due"):
            raw_value = row.get(key)
            if raw_value is not None and _stamp(raw_value) is None:
                item["warnings"].append("invalid ledger " + key + " time")
        if _stamp(row.get("updated")) is not None and _stamp(row["updated"]) > now_ms + 5000:
            item["warnings"].append("ledger bookkeeping time in future")
        raw_state = _name(row.get("state"))
        item["ledger_state"] = raw_state
        item["engineering_accepted"] = raw_state == "VERIFIED"
        if raw_state == "VERIFIED":
            item["state"] = "engineering_accepted"
        elif raw_state == "DROPPED":
            item["state"] = "dropped"
        elif raw_state == "BLOCKED":
            item["state"] = "blocked"
        elif raw_state is None:
            item["warnings"].append("ledger state unknown")
        raw_blockers = row.get("blocked_by")
        if isinstance(raw_blockers, str):
            raw_blockers = [raw_blockers]
        elif not isinstance(raw_blockers, list):
            raw_blockers = []
        for raw_blocker in raw_blockers[:20]:
            typed = _blocker(raw_blocker)
            if typed is None:
                item["warnings"].append("invalid blocker reference")
                item["blocker_details"].append({"type": "unknown", "ref": None,
                                                "label": "unsupported blocker", "resolution": "unknown"})
                continue
            typed["resolution"] = "unknown"
            item["blocker_details"].append(typed)
            if typed["type"] == "item" and typed["ref"]:
                item["dependencies"].append(typed["ref"])
        defer = row.get("defer")
        if isinstance(defer, str) and defer.strip():
            item["defer_reason"] = _public_note(defer)
            item["recheck_at"] = item["due"]
            item["deferral_state"] = "unverifiable" if item["recheck_at"] is None else "pending"
        elif defer is not None:
            item["warnings"].append("invalid defer value")
        handle = row.get("handle")
        if isinstance(handle, dict):
            kind = handle.get("kind")
            ref = handle.get("ref")
            if kind == "cron" and isinstance(ref, str) and _PUBLIC_REF.fullmatch(ref):
                if ref in jobs_link:
                    warnings.append("cron: duplicate ledger handle ref")
                    get(jobs_link[ref])["warnings"].append("duplicate cron ref")
                    item["warnings"].append("duplicate cron ref")
                else:
                    jobs_link[ref] = identifier
            elif kind in ("pid", "manual"):
                item["_handle"] = {k: handle.get(k) for k in ("kind", "pid", "start", "ref")}
            else:
                item["warnings"].append("unrecognized executor handle")
        for entry in _objects(row.get("history"))[-5:]:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                item["history"].append({
                    "at": _time(entry[0]), "state": None,
                    "note": _public_note(entry[1]),
                })
            elif isinstance(entry, dict):
                item["history"].append({"at": _time(entry.get("at")),
                    "state": _name(entry.get("state")),
                    "note": _public_note(entry.get("public_summary"))})

    roadmap_rows = _objects(roadmap.get("items")) if isinstance(roadmap, dict) else []
    goals = {}
    if isinstance(roadmap, dict):
        for row in _objects(roadmap.get("goals")):
            if isinstance(row, dict) and _name(row.get("id")):
                goals[row["id"]] = _safe(row.get("title"))
    stages = [{"id": key, "title": value, "exit_gate": "unsupported",
               "target_date": None} for key, value in goals.items()]
    for row in roadmap_rows:
        if not isinstance(row, dict):
            warnings.append("roadmap: invalid item")
            continue
        linked = _name(row.get("ledger")) or _name(row.get("id"))
        if not linked:
            warnings.append("roadmap: unlinked item without id")
            continue
        item = get(linked)
        touch(item, "roadmap")
        item["title"] = item["title"] or _safe(row.get("title"))
        item["stage"] = _name(row.get("goal"))
        item["stage_status"] = _name(row.get("status"))
        item["next"] = _safe(row.get("next"))
        item["updated_at"] = item["updated_at"] or _safe(row.get("touched"), 64)
        if isinstance(row.get("after"), list):
            item["dependencies"] = sorted(set(item["dependencies"] + [
                _name(x) for x in row["after"] if _name(x)]))[:20]
        if isinstance(row.get("evidence"), list):
            item["evidence"] += [_safe(e) for e in row["evidence"] if _safe(e)][:6]
        if item["stage"] not in goals:
            item["warnings"].append("stage unclassified")

    work_rows = _objects(work.get("tasks")) if isinstance(work, dict) else []
    for row in work_rows:
        if not isinstance(row, dict) or not _name(row.get("id")):
            warnings.append("work: invalid id")
            continue
        item = get(_name(row["id"]))
        touch(item, "work")
        item["title"] = item["title"] or _safe(row.get("title"))
        item["step"] = _safe(row.get("current_step"))
        item["next"] = _safe(row.get("next_step")) or item["next"]
        item["started_at"] = _safe(row.get("started_at"), 64)
        item["progress_at"] = _safe(row.get("updated_at"), 64)
        work_state = _name(row.get("state"))
        item["work_state"] = work_state
        if isinstance(row.get("evidence"), list):
            item["evidence"] += [_safe(e) for e in row["evidence"] if _safe(e)][:6]
        if work_state in ACTIVE:
            handle = item.get("_handle", {})
            proof = _proof_for(execution_proofs, item["id"])
            age = _stamp(item["progress_at"])
            verified = (isinstance(handle, dict) and isinstance(proof, dict) and
                        handle.get("kind") == "pid" and type(handle.get("pid")) is int and
                        type(proof.get("pid")) is int and proof["pid"] == handle["pid"] and
                        type(handle.get("start")) in (int, str) and
                        proof.get("start") == handle.get("start") and
                        proof.get("alive") is True and
                        (age is not None and 0 <= now_ms - age <= max_age_ms) and
                        (lambda t: t is not None and 0 <= now_ms - t <= max_age_ms)(
                            _stamp(proof.get("observed_at"))))
            item["activity"] = "verified_worker" if verified else "activity_unconfirmed"
            item["state"] = "running_verified" if verified else "activity_unconfirmed"
            item["owner"] = "executor identity checked" if verified else "not verified"
            if not verified:
                item["warnings"].append("running claim without matching live PID/start proof")
        elif work_state == "failed":
            item["state"] = "failed"
        elif work_state == "completed" and item.get("ledger_state") not in ("VERIFIED", "DROPPED"):
            item["state"] = "completed_unaccepted"
            item["warnings"].append("work completion is not engineering acceptance")
        if work_state == "completed" and item.get("ledger_state") == "BLOCKED":
            item["warnings"].append("work/ledger conflict")

    # Ledger DISPATCHED is a claim, not evidence of a live worker.
    for item in items.values():
        if item.get("ledger_state") == "DISPATCHED" and item["activity"] != "verified_worker":
            item["activity"] = "activity_unconfirmed"
            item["state"] = "activity_unconfirmed"
            item["warnings"].append("dispatch has no independently verified executor")

    cron_rows = _objects(cron.get("jobs")) if isinstance(cron, dict) else []
    for row in cron_rows:
        if not isinstance(row, dict) or not _name(row.get("id")):
            warnings.append("cron: invalid id")
            continue
        job_id = _name(row["id"])
        item = get(jobs_link.get(job_id, "JOB:" + job_id))
        touch(item, "cron")
        if item["id"].startswith("JOB:"):
            item["kind"] = "schedule"
        item["title"] = item["title"] or _safe(row.get("name")) or job_id
        last_run = _time(row.get("last_run_at"))
        next_run = _time(row.get("next_run_at"))
        last_dispatch = row.get("last_dispatch")
        expected = _time(last_dispatch.get("scheduled_at")) if isinstance(last_dispatch, dict) else None
        proof = _proof_for(result_proofs, job_id)
        actual_status = _name(row.get("last_status"))
        has_result = bool(proof and proof.get("status") == "ok" and
                          _safe(proof.get("result_ref")) and
                          _stamp(proof.get("run_at")) == _stamp(last_run) and
                          _stamp(last_run) is not None)
        next_ms = _stamp(next_run)
        missed = (row.get("enabled") is True and next_ms is not None and
                  next_ms < now_ms - 60000 and
                  (_stamp(last_run) is None or _stamp(last_run) < next_ms))
        if row.get("enabled") is False:
            run_state = "disabled"
        elif missed:
            run_state = "missed"
        elif actual_status in ("error", "failed", "failure"):
            run_state = "failed"
        elif actual_status in ("ok", "success") and not has_result:
            run_state = "success_without_output"
        elif has_result:
            run_state = "result_proven"
        else:
            run_state = "scheduled_idle" if row.get("enabled") is True else "unknown"
        schedule = row.get("schedule") if isinstance(row.get("schedule"), dict) else {}
        cadence = _name(schedule.get("kind"))
        minutes = schedule.get("minutes")
        if cadence in ("every", "interval") and type(minutes) is int and 0 < minutes <= 10080:
            cadence_text = str(minutes) + " min interval"
        elif cadence == "cron":
            cadence_text = "cron expression (timezone semantics unverified)"
        else:
            cadence_text = "unknown"
        item["schedule"] = {
            "job_id": job_id, "cadence": cadence_text, "enabled": row.get("enabled") is True,
            "expected_at": expected, "last_run_at": last_run, "next_run_at": next_run,
            "last_status": actual_status, "run_state": run_state,
            "result_ref": _safe(proof.get("result_ref")) if has_result else None,
            "output_at": _time(proof.get("output_at")) if has_result else None,
        }
        if item["activity"] not in ("verified_worker", "activity_unconfirmed") and item["state"] not in (
                "engineering_accepted", "blocked", "dropped"):
            item["state"] = run_state
            item["activity"] = "scheduled_idle" if run_state != "missed" else "not_proven"
        if run_state in ("missed", "failed", "success_without_output"):
            item["warnings"].append("scheduler state: " + run_state)

    for item in items.values():
        item.pop("_handle", None)
        if item["stage"] is None:
            item["stage"] = "unclassified"
        unresolved = [x for x in item["dependencies"]
                      if x not in ledger_index or ledger_index[x].get("state") != "VERIFIED"]
        for blocked in item["blocker_details"]:
            if blocked["type"] == "item" and blocked["ref"] in ledger_index:
                blocked["resolution"] = ("resolved" if
                    ledger_index[blocked["ref"]].get("state") == "VERIFIED" else "unresolved")
            elif blocked["type"] == "item":
                blocked["resolution"] = "unknown"
        item["blockers"] = [b["label"] for b in item["blocker_details"]
                            if b["resolution"] != "resolved"] + [
                            x for x in unresolved if x not in item["dependencies"]]
        if item["blockers"] and item["state"] not in ("running_verified", "activity_unconfirmed"):
            item["state"] = "blocked"
        if item["blocker_details"] and not item["blockers"] and item.get("ledger_state") == "BLOCKED":
            item["warnings"].append("recorded blocker resolved; ledger still blocked")
        recheck = _stamp(item["recheck_at"])
        if item["deferral_state"] == "pending" and recheck is not None and now_ms > recheck:
            item["deferral_state"] = "overdue"
            item["warnings"].append("deferral recheck overdue")
        if item.get("work_state") in ACTIVE and item.get("ledger_state") in ("VERIFIED", "DROPPED"):
            item["warnings"].append("ledger/work state conflict")
        if item["source_keys"] == ["work"] or item["kind"] == "schedule":
            item["warnings"].append("no linked roadmap stage")
        item["evidence"] = list(dict.fromkeys(item["evidence"]))[:10]
        item["source_keys"].sort()
        if item["warnings"]:
            item["warnings"] = list(dict.fromkeys(item["warnings"]))
    safe_events = []
    for row in _objects(journal)[-20:]:
        if isinstance(row, dict) and _safe(row.get("public_summary")):
            safe_events.append({"at": _safe(row.get("at"), 64),
                                "summary": _safe(row.get("public_summary")),
                                "kind": _name(row.get("kind")),
                                "proof_ref": _safe(row.get("proof_ref")),
                                "verdict": _name(row.get("verdict"))})
    result = sorted(items.values(), key=lambda x: x["id"])
    return {
        "schema_version": 1, "observer_only": True, "deployed": False,
        "generated_at": datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat(),
        "sources": sources, "warnings": warnings, "stages": stages,
        "stage_current": _name(roadmap.get("current_stage")) if isinstance(roadmap, dict) else None,
        "totals": {"ledger": len(ledger_rows) if ledger is not None else None,
                   "roadmap": len(roadmap_rows) if roadmap is not None else None,
                   "work": len(work_rows) if work is not None else None,
                   "cron": len(cron_rows) if cron is not None else None,
                   "union": len(result)},
        "tasks": result, "events": safe_events,
        "limitations": ["No worker is verified without PID and start identity matched to independent observation",
                        "Cron success is not accepted output; deployment and unattended closure unconfirmed",
                        "Stage exit gates/targets unsupported unless explicitly supplied"],
    }


def read_activity(profile_root, *, now_ms, max_age_ms=86400000, max_bytes=1048576,
                  execution_proofs=None, result_proofs=None):
    """Read one configured trusted profile; never accept file names from a request."""
    if type(now_ms) is not int:
        raise ValueError("invalid clock")
    parsed, states = {}, {}
    for key in SOURCES:
        parsed[key], states[key] = _read_source(profile_root, key, max_bytes)
    return build_activity(*(parsed[k] for k in SOURCES), now_ms=now_ms,
                          max_age_ms=max_age_ms, execution_proofs=execution_proofs,
                          result_proofs=result_proofs, source_states=states)
