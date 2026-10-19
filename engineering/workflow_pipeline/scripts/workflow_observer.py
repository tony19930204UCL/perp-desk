"""
workflow_observer.py — deterministic commitment ledger checker.
Pure Python standard library. No network, no writes to ledger.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import time

# ---------------------------------------------------------------------------
# Module-level constants (tests may patch these to speed up retry loops)
# ---------------------------------------------------------------------------
READ_RETRIES = 3
READ_RETRY_DELAY = 0.2  # seconds

SLOT_SECONDS = 300  # 5-minute observation slots
COVERAGE_THRESHOLD = 0.98   # 98 %
ERROR_THRESHOLD = 0.02      # 2 %
VIOLATION_THRESHOLD = 900   # 15 minutes

DEFAULT_OUT_SUBDIR = "lab/evidence/workflow-pipeline-20261017/observation2"
DEFAULT_LOCK_TIMEOUT = 60   # seconds

# Forbidden imports / calls — checked statically by test
_FORBIDDEN_IMPORTS = {
    "socket", "subprocess", "urllib", "http", "ssl",
    "ctypes", "multiprocessing", "requests",
}

# ---------------------------------------------------------------------------
# /proc helpers
# ---------------------------------------------------------------------------

def _parse_stat(stat_bytes: bytes) -> dict:
    """
    Parse /proc/<pid>/stat content.
    The comm field (field 2) may contain spaces and parentheses, so we find
    the *last* ')' and split from there.
    Returns dict with keys: state (field 3), starttime (field 22, 0-indexed=21).
    """
    text = stat_bytes.decode("latin-1")
    rp = text.rfind(")")
    if rp == -1:
        raise ValueError("no ')' in stat")
    # tokens after the closing ')'
    after = text[rp + 1:].split()
    # after[0] = state (field 3)
    # starttime is field 22, index in after = 22 - 3 = 19
    state_char = after[0]
    starttime = int(after[19])
    return {"state": state_char, "starttime": starttime}


def _pid_info(pid: int, proc_root: str = "/proc") -> dict | None:
    """
    Return dict with keys 'alive', 'zombie', 'starttime' for a pid,
    or None if the process does not exist.
    """
    stat_path = os.path.join(proc_root, str(pid), "stat")
    try:
        with open(stat_path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    try:
        info = _parse_stat(data)
    except (ValueError, IndexError):
        return None
    zombie = info["state"] in ("Z", "X")
    return {
        "alive": not zombie,
        "zombie": zombie,
        "starttime": info["starttime"],
    }


# ---------------------------------------------------------------------------
# Interval / cron schedule helpers
# ---------------------------------------------------------------------------

def _cron_interval_seconds(schedule: dict) -> float | None:
    """
    Return the schedule interval in seconds, or None if unknown.
    For kind=interval: minutes * 60.
    For kind=cron (5-field): heuristic per spec.
    Other kinds: None.
    """
    kind = schedule.get("kind")
    if kind == "interval":
        minutes = schedule.get("minutes")
        if minutes is not None:
            return float(minutes) * 60.0
        return None
    if kind == "cron":
        expr = schedule.get("expr", "")
        fields = expr.split()
        if len(fields) != 5:
            return None
        minute_f, hour_f, dom_f, month_f, dow_f = fields
        if dow_f != "*":
            return 7 * 86400.0
        if dom_f != "*":
            return 31 * 86400.0
        if hour_f != "*":
            return 24 * 3600.0
        if minute_f.startswith("*/"):
            try:
                n = int(minute_f[2:])
                return float(n) * 60.0
            except ValueError:
                return 60 * 60.0
        return 60 * 60.0
    return None


def _parse_iso(ts_str: str) -> float | None:
    """Parse an ISO-8601 datetime string with optional timezone offset. Return epoch float."""
    if not ts_str:
        return None
    try:
        # Python 3.7+ fromisoformat handles +HH:MM offsets
        dt = datetime.datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Ledger reading
# ---------------------------------------------------------------------------

def _read_ledger(path: str) -> tuple[bytes, dict]:
    """
    Read ledger bytes and parse JSON.
    Returns (raw_bytes, parsed_dict).
    Raises ValueError on parse / structure errors, OSError on missing file.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSONDecodeError: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("ledger is not a dict with items list")
    return raw, data


def _read_ledger_with_retry(path: str) -> tuple[bytes, dict]:
    """Read ledger with up to READ_RETRIES attempts."""
    last_exc: Exception | None = None
    last_raw: bytes | None = None
    for attempt in range(READ_RETRIES):
        try:
            raw, data = _read_ledger(path)
            return raw, data
        except (OSError, ValueError) as exc:
            last_exc = exc
            last_raw = None
            # try to capture bytes for error reporting
            try:
                with open(path, "rb") as fh:
                    last_raw = fh.read()
            except OSError:
                pass
            if attempt < READ_RETRIES - 1:
                time.sleep(READ_RETRY_DELAY)
    raise _LedgerError(str(last_exc), type(last_exc).__name__, last_raw)


class _LedgerError(Exception):
    def __init__(self, msg: str, exc_type: str, raw: bytes | None):
        super().__init__(msg)
        self.exc_type = exc_type
        self.raw = raw


# ---------------------------------------------------------------------------
# Jobs loading
# ---------------------------------------------------------------------------

def _load_jobs(jobs_path: str) -> dict[str, dict]:
    """Load cron/jobs.json and return dict keyed by job id."""
    try:
        with open(jobs_path, "rb") as fh:
            raw = fh.read()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise _LedgerError(str(exc), type(exc).__name__, None)
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise _LedgerError("jobs.json missing jobs list", "ValueError", None)
    return {j["id"]: j for j in data["jobs"] if isinstance(j, dict) and "id" in j}


# ---------------------------------------------------------------------------
# Blocker checking
# ---------------------------------------------------------------------------

def _check_blocker(
    blocked_by: str,
    item_map: dict[str, dict],
    checks: list,
    unverifiable: list,
    info: list,
    item_id: str,
) -> list[dict]:
    """
    Check a single blocked_by string.
    Returns list of violation dicts (may be empty).
    Also appends to checks/unverifiable/info as appropriate.
    """
    violations = []
    if blocked_by.startswith("item:"):
        target_id = blocked_by[5:]
        target = item_map.get(target_id)
        if target is None:
            violations.append({
                "type": "INVALID_BLOCK",
                "item": item_id,
                "detail": f"target_missing:{target_id}",
            })
        elif target["state"] in ("VERIFIED", "DROPPED"):
            violations.append({
                "type": "INVALID_BLOCK",
                "item": item_id,
                "detail": f"target_state:{target['state']}:{target_id}",
            })
        # else: target is OPEN/DISPATCHED/BLOCKED → blocker still valid
    elif blocked_by.startswith("file:"):
        path = blocked_by[5:]
        if os.path.lexists(path):
            violations.append({
                "type": "INVALID_BLOCK",
                "item": item_id,
                "detail": f"file_exists:{path}",
            })
        else:
            pass  # file does not exist → blocker holds
    elif blocked_by.startswith("cmd:"):
        unverifiable.append(item_id)
        # info entry
        info.append({
            "type": "UNVERIFIABLE_CMD",
            "item": item_id,
            "detail": blocked_by,
        })
    elif blocked_by.startswith("user:"):
        pass  # always holds
    else:
        info.append({
            "type": "UNKNOWN_BLOCKER_FORMAT",
            "item": item_id,
            "detail": blocked_by,
        })
    return violations


# ---------------------------------------------------------------------------
# Main check logic
# ---------------------------------------------------------------------------

def _check_items(
    items: list[dict],
    now: float,
    proc_root: str,
    jobs_by_id: dict[str, dict] | None,
) -> dict:
    """
    Check all items and return dict with:
      checks, violations_list, unverifiable, info
    """
    item_map = {i["id"]: i for i in items}
    checks: list[dict] = []
    violations_list: list[dict] = []
    unverifiable: list[str] = []
    info: list[dict] = []

    for item in items:
        state = item.get("state", "")
        item_id = item["id"]
        handle = item.get("handle")
        blocked_by = item.get("blocked_by")

        if state == "DISPATCHED":
            kind = handle.get("kind") if handle else None

            if handle is None:
                # BAD_HANDLE
                viol = {"type": "BAD_HANDLE", "item": item_id, "detail": "handle_null"}
                violations_list.append(viol)
                checks.append({
                    "item": item_id, "state": state, "kind": None,
                    "check": "BAD_HANDLE", "result": "BAD_HANDLE",
                })
                continue

            if kind == "pid":
                _check_pid_item(item, item_id, handle, proc_root, checks, violations_list, info)

            elif kind == "cron":
                _check_cron_item(item, item_id, handle, now, jobs_by_id or {}, checks, violations_list, info)

            elif kind == "manual" or kind not in ("pid", "cron"):
                _check_manual_item(item, item_id, kind, now, checks, violations_list)

        elif state == "BLOCKED":
            if not blocked_by:
                # Info only, not a violation
                info.append({
                    "type": "NO_CHECKABLE_BLOCKER",
                    "item": item_id,
                    "detail": "BLOCKED with null blocked_by",
                })
                checks.append({
                    "item": item_id, "state": state,
                    "kind": handle.get("kind") if handle else None,
                    "check": "NO_CHECKABLE_BLOCKER", "result": "INFO",
                })
            else:
                viols = _check_blocker(blocked_by, item_map, checks, unverifiable, info, item_id)
                violations_list.extend(viols)
                result = viols[0]["type"] if viols else "OK"
                checks.append({
                    "item": item_id, "state": state,
                    "kind": handle.get("kind") if handle else None,
                    "check": "blocker", "result": result,
                })

        elif state == "OPEN":
            if blocked_by:
                viols = _check_blocker(blocked_by, item_map, checks, unverifiable, info, item_id)
                violations_list.extend(viols)
                result = viols[0]["type"] if viols else "OK"
                checks.append({
                    "item": item_id, "state": state,
                    "kind": None,
                    "check": "blocker", "result": result,
                })
            # else: OPEN without blocked_by → ignore entirely

        # VERIFIED/DROPPED → ignore

    return {
        "checks": checks,
        "violations_list": violations_list,
        "unverifiable": unverifiable,
        "info": info,
    }


def _check_pid_item(item, item_id, handle, proc_root, checks, violations_list, info):
    """Check a DISPATCHED+pid item."""
    # Parse pid
    raw_pid = handle.get("pid")
    raw_ref = handle.get("ref", "")
    pid_int: int | None = None
    bad_handle = False

    if isinstance(raw_pid, bool):
        # bool is not an integer pid → try ref
        try:
            if isinstance(raw_ref, bool):
                pid_int = None
                bad_handle = True
            else:
                pid_int = int(raw_ref)
        except (ValueError, TypeError):
            pid_int = None
            bad_handle = True
    elif isinstance(raw_pid, int):
        pid_int = raw_pid
    elif isinstance(raw_pid, float) and raw_pid == int(raw_pid):
        pid_int = int(raw_pid)
    else:
        # pid not integer → try ref
        try:
            if isinstance(raw_ref, bool):
                pid_int = None
                bad_handle = True
            else:
                pid_int = int(raw_ref)
        except (ValueError, TypeError):
            pid_int = None
            bad_handle = True

    if bad_handle or pid_int is None:
        violations_list.append({
            "type": "BAD_HANDLE",
            "item": item_id,
            "detail": f"pid_not_int:ref={raw_ref}",
        })
        checks.append({
            "item": item_id, "state": "DISPATCHED", "kind": "pid",
            "check": "BAD_HANDLE", "result": "BAD_HANDLE",
        })
        return

    expected_start = handle.get("start")
    proc_info = _pid_info(pid_int, proc_root)

    if proc_info is None:
        violations_list.append({
            "type": "DEAD_PID",
            "item": item_id,
            "detail": f"not_found:pid={pid_int}",
        })
        checks.append({
            "item": item_id, "state": "DISPATCHED", "kind": "pid",
            "check": "DEAD_PID", "result": "DEAD_PID",
        })
        return

    if proc_info["zombie"]:
        violations_list.append({
            "type": "DEAD_PID",
            "item": item_id,
            "detail": f"zombie:pid={pid_int}",
        })
        checks.append({
            "item": item_id, "state": "DISPATCHED", "kind": "pid",
            "check": "DEAD_PID", "result": "DEAD_PID",
        })
        return

    # Process is alive
    actual_start = proc_info["starttime"]
    if expected_start is None:
        # start field missing: only verify alive, record info
        info.append({
            "type": "start_unverified",
            "item": item_id,
            "detail": f"pid={pid_int} alive but start not in handle",
        })
        checks.append({
            "item": item_id, "state": "DISPATCHED", "kind": "pid",
            "check": "pid_alive_start_unverified", "result": "OK",
        })
        return

    expected_start_int = int(expected_start)
    if actual_start != expected_start_int:
        violations_list.append({
            "type": "DEAD_PID",
            "item": item_id,
            "detail": (
                f"start_mismatch:pid={pid_int}"
                f":expected={expected_start_int}"
                f":actual={actual_start}"
            ),
        })
        checks.append({
            "item": item_id, "state": "DISPATCHED", "kind": "pid",
            "check": "DEAD_PID", "result": "DEAD_PID",
        })
        return

    checks.append({
        "item": item_id, "state": "DISPATCHED", "kind": "pid",
        "check": "pid_alive", "result": "OK",
    })


def _check_cron_item(item, item_id, handle, now, jobs_by_id, checks, violations_list, info):
    """Check a DISPATCHED+cron item."""
    ref = handle.get("ref", "")
    job = jobs_by_id.get(ref)

    if job is None:
        violations_list.append({
            "type": "CRON_MISSING",
            "item": item_id,
            "detail": f"job_not_found:{ref}",
        })
        checks.append({
            "item": item_id, "state": "DISPATCHED", "kind": "cron",
            "check": "CRON_MISSING", "result": "CRON_MISSING",
        })
        return

    local_violations = []

    # CRON_DISABLED (always checked, even during first-schedule grace)
    if job.get("enabled") is not True:
        local_violations.append({
            "type": "CRON_DISABLED",
            "item": item_id,
            "detail": f"enabled={job.get('enabled')}",
        })

    # CRON_STALE and CRON_STATUS
    schedule = job.get("schedule", {})
    interval_sec = _cron_interval_seconds(schedule)
    last_run_at_str = job.get("last_run_at")

    if interval_sec is None:
        # Unknown schedule: cannot determine grace or stale threshold
        info.append({
            "type": "schedule_unknown",
            "item": item_id,
            "detail": f"schedule kind={schedule.get('kind')} unknown interval",
        })
        # CRON_STATUS always applies regardless of last_run_at (no CRON_STALE, no grace)
        last_status = job.get("last_status")
        if last_status != "ok":
            local_violations.append({
                "type": "CRON_STATUS",
                "item": item_id,
                "detail": f"last_status={last_status}",
            })
    elif last_run_at_str in (None, ""):
        # last_run_at is None or empty string — check first-schedule grace period
        created_at_str = job.get("created_at")
        created_at_ts = _parse_iso(created_at_str) if created_at_str else None
        grace_sec = 2 * interval_sec + 300

        in_grace = False
        if created_at_ts is not None:
            elapsed = now - created_at_ts
            # now < created_at (future clock skew) counts as in-grace (elapsed < 0)
            # 0 <= elapsed <= grace_sec is in-grace (boundary: == grace_sec is still in grace)
            in_grace = elapsed <= grace_sec

        if in_grace:
            # Within first-schedule grace: suppress CRON_STALE and CRON_STATUS
            elapsed_for_info = now - created_at_ts  # type: ignore[operator]
            info.append({
                "type": "cron_first_schedule_grace",
                "item": item_id,
                "detail": (
                    f"created_at={created_at_str} "
                    f"grace_sec={grace_sec} "
                    f"elapsed_sec={elapsed_for_info:.1f}"
                ),
            })
            checks.append({
                "item": item_id, "state": "DISPATCHED", "kind": "cron",
                "check": "cron_health", "result": "INFO_FIRST_SCHEDULE_GRACE",
            })
            violations_list.extend(local_violations)
            return
        else:
            # Grace expired or created_at missing/unparseable: report CRON_STALE
            detail = (
                f"last_run_at={last_run_at_str} "
                f"interval={interval_sec}s "
                f"now={now}"
            )
            local_violations.append({
                "type": "CRON_STALE",
                "item": item_id,
                "detail": detail,
            })
            # Also check CRON_STATUS (last_run_at is null → last_status is typically null too)
            last_status = job.get("last_status")
            if last_status != "ok":
                local_violations.append({
                    "type": "CRON_STATUS",
                    "item": item_id,
                    "detail": f"last_status={last_status}",
                })
    else:
        # last_run_at is non-empty: parse ISO
        last_run_ts = _parse_iso(last_run_at_str)
        is_stale = False
        if last_run_ts is None:
            # Non-empty but unparseable: maintain old behavior (CRON_STALE),
            # even if created_at is newly created (grace does NOT apply)
            is_stale = True
        elif (now - last_run_ts) > 3 * interval_sec:
            # > 3x interval (strictly greater, not >=)
            is_stale = True

        if is_stale:
            detail = (
                f"last_run_at={last_run_at_str} "
                f"interval={interval_sec}s "
                f"now={now}"
            )
            local_violations.append({
                "type": "CRON_STALE",
                "item": item_id,
                "detail": detail,
            })

        # CRON_STATUS
        last_status = job.get("last_status")
        if last_status != "ok":
            local_violations.append({
                "type": "CRON_STATUS",
                "item": item_id,
                "detail": f"last_status={last_status}",
            })

    violations_list.extend(local_violations)
    if local_violations:
        result = local_violations[0]["type"]
    else:
        result = "OK"
    checks.append({
        "item": item_id, "state": "DISPATCHED", "kind": "cron",
        "check": "cron_health", "result": result,
    })



def _check_manual_item(item, item_id, kind, now, checks, violations_list):
    """Check a DISPATCHED+manual (or other) item."""
    due = item.get("due")
    if isinstance(due, (int, float)) and now > due:
        violations_list.append({
            "type": "OVERDUE_MANUAL",
            "item": item_id,
            "detail": f"due={due} now={now}",
        })
        checks.append({
            "item": item_id, "state": "DISPATCHED", "kind": kind,
            "check": "OVERDUE_MANUAL", "result": "OVERDUE_MANUAL",
        })
    else:
        checks.append({
            "item": item_id, "state": "DISPATCHED", "kind": kind,
            "check": "manual_due", "result": "OK",
        })


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------

def _fsync_dir(path: str) -> None:
    """fsync a directory."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_jsonl(path: str, obj: dict) -> None:
    """Append one JSON line to a .jsonl file atomically (O_APPEND) then fsync."""
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"
    encoded = line.encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    new_file = not os.path.exists(path)
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    if new_file:
        os.chmod(path, 0o644)
        _fsync_dir(os.path.dirname(path))


def _atomic_write_json(path: str, obj: dict) -> None:
    """Write JSON atomically to path using tmp+replace+fsync."""
    dir_path = os.path.dirname(path)
    content = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    encoded = content.encode("utf-8")
    # Write to temp file in same directory
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        os.write(tmp_fd, encoded)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = -1
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
        _fsync_dir(dir_path)
    except Exception:
        if tmp_fd != -1:
            os.close(tmp_fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Violations state management
# ---------------------------------------------------------------------------

def _load_violations(path: str) -> tuple[dict[tuple, dict], bool]:
    """
    Load violations.json. Returns (violations_by_key, state_reset).
    violations_by_key maps (type, item) -> violation record.
    If file is missing → empty dict, no reset.
    If file is corrupt → empty dict, state_reset=True (caller renames file).
    """
    if not os.path.exists(path):
        return {}, False
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}, True
        viols_raw = data.get("violations")
        if not isinstance(viols_raw, list):
            return {}, True
        viols = {}
        for v in viols_raw:
            if not isinstance(v, dict):
                return {}, True
            if "type" not in v or "item" not in v or "first_seen" not in v:
                return {}, True
            fs = v["first_seen"]
            if isinstance(fs, bool) or not isinstance(fs, (int, float)):
                return {}, True
            key = (v["type"], v["item"])
            viols[key] = v
        return viols, False
    except Exception:
        return {}, True


# ---------------------------------------------------------------------------
# Lock helpers
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: str, timeout: float) -> int:
    """
    Acquire exclusive flock on lock_path with polling until timeout.
    Returns open file descriptor. Raises TimeoutError if cannot acquire.
    """
    new_file = not os.path.exists(lock_path)
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    if new_file:
        os.fchmod(fd, 0o644)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(f"Could not acquire lock {lock_path!r} within {timeout}s")
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Normal observation run
# ---------------------------------------------------------------------------

def _observation_run(
    home: str,
    out_dir: str,
    now: float,
    proc_root: str,
    lock_timeout: float,
) -> int:
    """
    Perform one observation run. Returns exit code (0 or 1).

    Exit codes:
      0 — observation completed successfully, regardless of whether there are
          existing or new violations. Violations are communicated via stdout
          VIOLATION lines (new only), violations.json, anomalies.jsonl, and
          observations.jsonl. Exit 0 is critical: the cron system treats any
          non-zero exit as a task failure (last_status=error), which would cause
          the observer to flag its own cron as CRON_STATUS — a self-amplifying
          loop that never clears automatically.
      1 — observer itself failed (OBSERVER_ERROR): lock timeout, ledger
          unreadable/corrupt, or unhandled exception. A best-effort
          OBSERVER_ERROR record is written to observations.jsonl.

    The program never returns 2 in normal (scheduled) mode.
    """
    ledger_path = os.path.join(home, "commitments", "ledger.json")
    jobs_path = os.path.join(home, "cron", "jobs.json")

    # Ensure output dir exists
    os.makedirs(out_dir, mode=0o755, exist_ok=True)

    # Acquire lock
    lock_path = os.path.join(out_dir, "observer.lock")
    try:
        lock_fd = _acquire_lock(lock_path, lock_timeout)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        return _run_locked(
            ledger_path, jobs_path, out_dir, now, proc_root, lock_fd
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _run_locked(
    ledger_path: str,
    jobs_path: str,
    out_dir: str,
    now: float,
    proc_root: str,
    lock_fd: int,
) -> int:
    """
    Core observation logic, called while holding the lock.
    Returns exit code (0 or 1).

    Exit codes:
      0 — observation completed successfully (even if violations exist).
          Violations are reported via stdout/violations.json/anomalies.jsonl.
          Exit 0 prevents the cron system from marking the observer's own job
          as last_status=error, which would cause an unresolvable CRON_STATUS
          self-violation.
      1 — OBSERVER_ERROR: ledger unreadable, jobs.json corrupt, or other
          unhandled exception.
    """
    obs_path = os.path.join(out_dir, "observations.jsonl")
    anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
    violations_path = os.path.join(out_dir, "violations.json")

    # Step 2: Read ledger
    try:
        ledger_raw, ledger_data = _read_ledger_with_retry(ledger_path)
    except _LedgerError as exc:
        ledger_sha = hashlib.sha256(exc.raw).hexdigest() if exc.raw else None
        error_record = {
            "v": 1,
            "ts": now,
            "ts_iso": datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).isoformat(),
            "type": "OBSERVER_ERROR",
            "error": str(exc),
            "exc_type": exc.exc_type,
        }
        if ledger_sha:
            error_record["ledger_sha256"] = ledger_sha
        try:
            _append_jsonl(obs_path, error_record)
        except Exception as write_exc:
            print(f"Failed to write OBSERVER_ERROR: {write_exc}", file=sys.stderr)
        print(f"OBSERVER_ERROR: {exc}", file=sys.stderr)
        return 1

    ledger_sha = hashlib.sha256(ledger_raw).hexdigest()

    try:
        items = ledger_data["items"]
        if not isinstance(items, list):
            raise TypeError("ledger items must be a list")

        # Check if any item has cron handle → need jobs
        needs_jobs = any(
            isinstance(i, dict)
            and i.get("state") == "DISPATCHED"
            and isinstance(i.get("handle"), dict)
            and i["handle"].get("kind") == "cron"
            for i in items
        )
        jobs_by_id: dict[str, dict] | None = None
        if needs_jobs:
            jobs_by_id = _load_jobs(jobs_path)

        # Step 3: Compute checks and violations
        result = _check_items(items, now, proc_root, jobs_by_id)
        checks = result["checks"]
        new_violations = result["violations_list"]
        unverifiable = result["unverifiable"]
        info = result["info"]

        # Deduplicate new_violations by key (keep all instances though)
        # Build violation key set
        new_viol_by_key: dict[tuple, dict] = {}
        for v in new_violations:
            key = (v["type"], v["item"])
            if key not in new_viol_by_key:
                new_viol_by_key[key] = v

        # Step 4: Load existing violations.json
        old_viols, state_reset = _load_violations(violations_path)

        if state_reset:
            # Rename corrupt file
            corrupt_path = f"{violations_path}.corrupt-{int(now)}"
            try:
                os.rename(violations_path, corrupt_path)
            except OSError:
                pass

        # Merge: determine first_seen for each violation
        merged_viols: dict[tuple, dict] = {}
        for key, viol in new_viol_by_key.items():
            if key in old_viols:
                first_seen = old_viols[key]["first_seen"]
            else:
                first_seen = now
            merged_viols[key] = {
                "type": viol["type"],
                "item": viol["item"],
                "detail": viol.get("detail", ""),
                "first_seen": first_seen,
                "last_seen": now,
                "seconds_open": now - first_seen,
            }

        # Cleared violations: in old but not in new
        cleared_viols = []
        for key, old_v in old_viols.items():
            if key not in new_viol_by_key:
                cleared_viols.append({
                    "ts": now,
                    "type": old_v["type"],
                    "item": old_v["item"],
                    "detail": old_v.get("detail", ""),
                    "first_seen": old_v["first_seen"],
                    "cleared_at": now,
                    "seconds_open": now - old_v["first_seen"],
                })

        # New violations (not in old)
        truly_new_keys = set(new_viol_by_key.keys()) - set(old_viols.keys())

        # Build observation record
        obs_violations = []
        for key, mv in merged_viols.items():
            obs_violations.append({
                **mv,
                "new": key in truly_new_keys,
            })

        obs_record = {
            "v": 1,
            "ts": now,
            "ts_iso": datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).isoformat(),
            "type": "OBSERVATION",
            "ledger_sha256": ledger_sha,
            "ledger_items": len(items),
            "checks": checks,
            "violations": obs_violations,
            "unverifiable": unverifiable,
            "info": info,
            "counts": {
                "checked": len(checks),
                "violations": len(merged_viols),
                "new_violations": len(truly_new_keys),
            },
        }
        if state_reset:
            obs_record["state_reset"] = True

        viol_list = list(merged_viols.values())
        violations_doc = {
            "updated": now,
            "violations": viol_list,
        }
    except Exception as exc:
        exc_type = exc.exc_type if isinstance(exc, _LedgerError) else type(exc).__name__
        error_record = {
            "v": 1,
            "ts": now,
            "ts_iso": datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc).isoformat(),
            "type": "OBSERVER_ERROR",
            "error": str(exc),
            "exc_type": exc_type,
            "ledger_sha256": ledger_sha,
        }
        try:
            _append_jsonl(obs_path, error_record)
        except Exception as write_exc:
            print(f"Failed to write OBSERVER_ERROR: {write_exc}", file=sys.stderr)
        print(f"OBSERVER_ERROR: {exc}", file=sys.stderr)
        return 1

    # Step 5: Write outputs in order
    # (a) Append observation
    _append_jsonl(obs_path, obs_record)

    # (b) Append cleared anomalies
    for cleared in cleared_viols:
        _append_jsonl(anomalies_path, cleared)

    # (c) Atomic write violations.json
    _atomic_write_json(violations_path, violations_doc)

    # Step 6/7: stdout for new violations
    sorted_new_keys = sorted(truly_new_keys, key=lambda k: (str(k[0]), str(k[1])))
    for key in sorted_new_keys:
        mv = merged_viols[key]
        print(f"VIOLATION {mv['type']} {mv['item']} 0")

    # Step 8: exit code
    # Always exit 0 on a successful observation run — even if there are violations.
    # The cron system treats any non-zero exit as last_status=error, which would
    # flag the observer's own cron as CRON_STATUS, creating a self-perpetuating loop.
    # Violations are communicated via stdout (new only), violations.json,
    # anomalies.jsonl, and observations.jsonl instead.
    return 0


# ---------------------------------------------------------------------------
# Verdict mode
# ---------------------------------------------------------------------------

def _verdict_run(
    out_dir: str,
    start: float,
    end: float,
    now: float,
) -> int:
    """
    Run in --verdict mode. Prints JSON to stdout, returns 0=PASS, 2=FAIL.
    """
    if end <= start:
        print("verdict: end must be > start", file=sys.stderr)
        return 1

    expected = int((end - start) // SLOT_SECONDS)
    if expected < 1:
        print("verdict: expected slots < 1 (interval too short)", file=sys.stderr)
        return 1

    obs_path = os.path.join(out_dir, "observations.jsonl")
    violations_path = os.path.join(out_dir, "violations.json")
    anomalies_path = os.path.join(out_dir, "anomalies.jsonl")

    # Read observations.jsonl
    obs_records: list[dict] = []
    bad_lines = 0
    if os.path.exists(obs_path):
        with open(obs_path, "rb") as fh:
            for raw_line in fh:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        bad_lines += 1
                        continue
                    obs_records.append(rec)
                except json.JSONDecodeError:
                    bad_lines += 1

    # Coverage: slots with at least one record in [start, start+300*expected)
    upper = start + SLOT_SECONDS * expected
    covered_slots: set[int] = set()
    error_slots: set[int] = set()
    for rec in obs_records:
        ts = rec.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        if ts < start or ts >= upper:
            continue
        slot_k = int((ts - start) // SLOT_SECONDS)
        covered_slots.add(slot_k)
        if rec.get("type") == "OBSERVER_ERROR":
            error_slots.add(slot_k)

    covered = len(covered_slots)
    error_slot_count = len(error_slots)

    coverage_ratio = covered / expected
    coverage_pass = (covered * 100 >= expected * 98)  # integer comparison
    error_ratio = error_slot_count / expected
    error_pass = (error_slot_count * 100 <= expected * 2)

    # Violations
    # Read violations.json (current still-open)
    current_viols: list[dict] = []
    if os.path.exists(violations_path):
        try:
            with open(violations_path, "rb") as fh:
                vdata = json.loads(fh.read())
            if isinstance(vdata, dict):
                raw_viols = vdata.get("violations", [])
                if isinstance(raw_viols, list):
                    current_viols = [v for v in raw_viols if isinstance(v, dict)]
        except (OSError, json.JSONDecodeError):
            pass

    # Read anomalies.jsonl (cleared violations)
    anomaly_records: list[dict] = []
    if os.path.exists(anomalies_path):
        with open(anomalies_path, "rb") as fh:
            for raw_line in fh:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if not isinstance(rec, dict):
                        bad_lines += 1
                        continue
                    anomaly_records.append(rec)
                except json.JSONDecodeError:
                    bad_lines += 1

    # Build violation items for verdict
    # From anomalies: cleared violations
    # Deduplicate by (type, item, first_seen)
    seen_anomaly_keys: set[tuple] = set()
    viol_items: list[dict] = []

    for rec in anomaly_records:
        fs = rec.get("first_seen")
        if not isinstance(fs, (int, float)):
            continue
        key = (rec.get("type"), rec.get("item"), fs)
        if key in seen_anomaly_keys:
            continue
        seen_anomaly_keys.add(key)
        cleared_at = rec.get("cleared_at")
        ca_val = cleared_at if isinstance(cleared_at, (int, float)) else None

        # Effective right edge bounded by cleared time, period end, and evaluation now
        effective_right_edge = min(
            ca_val if ca_val is not None else now,
            end,
            now
        )
        # Positive overlap with elapsed window [start, min(end, now)) decides MEMBERSHIP ONLY.
        # Excludes historical contamination (cleared at/before start), episodes starting at/after
        # end or now, now <= start, and invalid/negative-duration episodes.
        if effective_right_edge <= max(fs, start):
            continue

        # For genuinely overlapping episodes, RETAIN original first_seen-based duration to preserve
        # the conservative 900s continuity contract without silently resetting an episode's age.
        duration = effective_right_edge - fs
        viol_items.append({
            "type": rec.get("type"),
            "item": rec.get("item"),
            "first_seen": fs,
            "cleared_at": ca_val,
            "duration": duration,
            "pass": duration <= VIOLATION_THRESHOLD,
        })

    # From current violations (still open)
    for v in current_viols:
        fs = v.get("first_seen")
        if not isinstance(fs, (int, float)):
            continue
        # Check if already accounted for in anomalies
        key = (v.get("type"), v.get("item"), fs)
        if key in seen_anomaly_keys:
            continue
        seen_anomaly_keys.add(key)

        effective_right_edge = min(end, now)
        # Positive overlap with elapsed window [start, min(end, now)) decides MEMBERSHIP ONLY.
        if effective_right_edge <= max(fs, start):
            continue

        # Retain original first_seen-based duration for genuinely overlapping open episodes.
        duration = effective_right_edge - fs
        viol_items.append({
            "type": v.get("type"),
            "item": v.get("item"),
            "first_seen": fs,
            "cleared_at": None,
            "duration": duration,
            "pass": duration <= VIOLATION_THRESHOLD,
        })

    max_seconds = max((v["duration"] for v in viol_items), default=0.0)
    violations_pass = all(v["pass"] for v in viol_items)

    # Disposal evidence: at least 1 anomaly with cleared_at in [start, end]
    disposal_count = sum(
        1 for rec in anomaly_records
        if isinstance(rec.get("cleared_at"), (int, float))
        and start <= rec["cleared_at"] <= end
    )
    disposal_pass = disposal_count >= 1

    # Overall verdict
    reasons = []
    if not coverage_pass:
        reasons.append(
            f"coverage {covered}/{expected} ({coverage_ratio:.3f}) < threshold {COVERAGE_THRESHOLD}"
        )
    if not error_pass:
        reasons.append(
            f"observer_errors {error_slot_count}/{expected} ({error_ratio:.3f}) > threshold {ERROR_THRESHOLD}"
        )
    if not violations_pass:
        reasons.append(
            f"violation max_seconds {max_seconds:.1f} > threshold {VIOLATION_THRESHOLD}"
        )
    if not disposal_pass:
        reasons.append("no disposal evidence (no cleared_at in [start, end])")

    verdict = "PASS" if not reasons else "FAIL"

    output = {
        "verdict": verdict,
        "reasons": reasons,
        "start": start,
        "end": end,
        "coverage": {
            "expected": expected,
            "covered": covered,
            "ratio": coverage_ratio,
            "threshold": COVERAGE_THRESHOLD,
            "pass": coverage_pass,
        },
        "observer_errors": {
            "error_slots": error_slot_count,
            "ratio": error_ratio,
            "threshold": ERROR_THRESHOLD,
            "pass": error_pass,
        },
        "violations": {
            "max_seconds": max_seconds,
            "threshold_seconds": VIOLATION_THRESHOLD,
            "items": viol_items,
            "pass": violations_pass,
        },
        "disposal_evidence": {
            "count": disposal_count,
            "pass": disposal_pass,
        },
        "bad_lines": bad_lines,
    }

    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if verdict == "PASS" else 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_home() -> str:
    """Return realpath of parent of script directory."""
    script_dir = os.path.dirname(os.path.realpath(__file__))
    return os.path.dirname(script_dir)


def main(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(prog="workflow_observer")
        parser.add_argument("--home", default=None)
        parser.add_argument("--out-dir", default=None)
        parser.add_argument("--now", type=float, default=None)
        parser.add_argument("--proc-root", default="/proc")
        parser.add_argument("--lock-timeout", type=float, default=DEFAULT_LOCK_TIMEOUT)

        # Verdict sub-mode
        parser.add_argument("--verdict", action="store_true")
        parser.add_argument("--start", type=float, default=None)
        parser.add_argument("--end", type=float, default=None)

        args = parser.parse_args(argv)

        now = args.now if args.now is not None else time.time()
        home = args.home if args.home else _default_home()

        if args.verdict:
            if args.start is None or args.end is None:
                print("--verdict requires --start and --end", file=sys.stderr)
                return 1
            out_dir = args.out_dir if args.out_dir else os.path.join(home, DEFAULT_OUT_SUBDIR)
            return _verdict_run(out_dir, args.start, args.end, now)
        else:
            out_dir = args.out_dir if args.out_dir else os.path.join(home, DEFAULT_OUT_SUBDIR)
            return _observation_run(
                home=home,
                out_dir=out_dir,
                now=now,
                proc_root=args.proc_root,
                lock_timeout=args.lock_timeout,
            )
    except Exception as exc:
        print(f"OBSERVER_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
