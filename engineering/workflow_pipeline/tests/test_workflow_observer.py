"""
tests/test_workflow_observer.py
Comprehensive unittest suite for workflow_observer.py.
At least 55 tests, all isolated with tempfile.TemporaryDirectory.
"""
import ast
import datetime
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Insert scripts/ directory so we can import workflow_observer directly
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, os.path.realpath(_SCRIPTS_DIR))

import workflow_observer as wo


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def _make_profile(tmp: str, items=None, jobs=None):
    """Create a minimal profile tree in tmp directory."""
    commitments = os.path.join(tmp, "commitments")
    cron_dir = os.path.join(tmp, "cron")
    os.makedirs(commitments, exist_ok=True)
    os.makedirs(cron_dir, exist_ok=True)
    _write_ledger(tmp, items or [])
    if jobs is not None:
        _write_jobs(tmp, jobs)
    else:
        _write_jobs(tmp, [])
    return tmp


def _write_ledger(home: str, items: list):
    path = os.path.join(home, "commitments", "ledger.json")
    data = {"next": len(items) + 1, "items": items}
    with open(path, "wb") as fh:
        fh.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _write_jobs(home: str, jobs: list):
    path = os.path.join(home, "cron", "jobs.json")
    data = {"jobs": jobs, "updated_at": "2026-10-19T10:00:00+00:00"}
    with open(path, "wb") as fh:
        fh.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))


def _make_item(
    id="C-0001", state="DISPATCHED", handle=None, blocked_by=None,
    due=None, text="test item"
):
    return {
        "id": id, "text": text, "created": 1700000000.0,
        "updated": 1700000000.0, "state": state,
        "handle": handle, "blocked_by": blocked_by, "due": due,
        "reason": None, "defer": None, "history": [],
    }


def _make_pid_handle(pid, start=None, ref=None):
    h = {"kind": "pid", "pid": pid, "ref": str(ref or pid)}
    if start is not None:
        h["start"] = start
    return h


def _make_cron_handle(ref):
    return {"kind": "cron", "ref": ref}


def _make_manual_handle(ref="sa-0-abc123"):
    return {"kind": "manual", "ref": ref}


def _make_job(id, enabled=True, state="scheduled", last_run_at=None,
              last_status="ok", schedule=None):
    if schedule is None:
        schedule = {"kind": "interval", "minutes": 10, "display": "every 10m"}
    return {
        "id": id, "name": f"job-{id}",
        "enabled": enabled, "state": state,
        "last_run_at": last_run_at, "last_status": last_status,
        "schedule": schedule,
    }


def _run_observer(home, extra_args=None, out_dir=None, now=None, proc_root=None):
    """Run main() in-process and return (exit_code, stdout_lines)."""
    args = ["--home", home]
    if out_dir:
        args += ["--out-dir", out_dir]
    if now is not None:
        args += ["--now", str(now)]
    if proc_root:
        args += ["--proc-root", proc_root]
    if extra_args:
        args.extend(extra_args)

    import io
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = wo.main(args)
    finally:
        captured = sys.stdout.getvalue()
        sys.stdout = old_stdout

    return rc, captured


def _make_fake_proc(tmpdir, pid, starttime, state_char="S"):
    """Create a fake /proc/<pid>/stat file."""
    proc_dir = os.path.join(tmpdir, str(pid))
    os.makedirs(proc_dir, exist_ok=True)
    # Build minimal /proc/<pid>/stat
    # Format: pid (comm) state ppid pgroup session ... starttime ...
    # We put enough fields; starttime is field 22 (1-indexed), index 21 (0-indexed)
    # After ')' (field 2), next fields are: state(f3), ppid(f4), ..., starttime(f22)
    # So after ')': [state, ppid, pgrp, session, tty, tpgid, flags, minflt, cminflt,
    #                majflt, cmajflt, utime, stime, cutime, cstime, priority, nice,
    #                num_threads, itrealvalue, starttime, ...]
    # indices after ')': 0=state, ..., 19=starttime
    after_fields = [state_char] + ["0"] * 19
    after_fields[19] = str(starttime)
    stat_content = f"{pid} (test_proc) " + " ".join(after_fields) + " 0 0 0 0\n"
    stat_path = os.path.join(proc_dir, "stat")
    with open(stat_path, "w") as fh:
        fh.write(stat_content)
    return proc_dir


def _make_fake_proc_with_comm(tmpdir, pid, starttime, comm, state_char="S"):
    """Create fake /proc/<pid>/stat where comm contains spaces/parentheses."""
    proc_dir = os.path.join(tmpdir, str(pid))
    os.makedirs(proc_dir, exist_ok=True)
    after_fields = [state_char] + ["0"] * 19
    after_fields[19] = str(starttime)
    stat_content = f"{pid} ({comm}) " + " ".join(after_fields) + " 0 0 0 0\n"
    stat_path = os.path.join(proc_dir, "stat")
    with open(stat_path, "w") as fh:
        fh.write(stat_content)
    return proc_dir


def _now_iso(epoch):
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.isoformat()


# A fixed "now" for tests: 2026-10-19 09:00 UTC
T_NOW = 1792389600.0  # 2026-10-19 09:00 UTC approx


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestPidAlive(unittest.TestCase):
    """PID is alive and start matches → no violation."""
    def test_pid_alive_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tempfile.mkdtemp(dir=tmp)
            _make_fake_proc(proc_root, 1234, 9999)
            handle = _make_pid_handle(1234, start=9999)
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0, f"Expected 0, got {rc}")
            self.assertEqual(stdout.strip(), "", f"Expected empty stdout, got {stdout!r}")


class TestPidDead(unittest.TestCase):
    """PID does not exist → DEAD_PID violation."""
    def test_pid_dead_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            handle = _make_pid_handle(9999999, start=1234)
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("VIOLATION DEAD_PID", stdout)
            self.assertIn("0", stdout)  # seconds_open=0 for new violation


class TestPidReused(unittest.TestCase):
    """PID exists but starttime mismatch → DEAD_PID (start_mismatch)."""
    def test_pid_reused_start_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tempfile.mkdtemp(dir=tmp)
            _make_fake_proc(proc_root, 555, 99999)  # actual starttime
            handle = _make_pid_handle(555, start=11111)  # expected different
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("DEAD_PID", stdout)
            # Check detail
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            details = [v["detail"] for v in obs["violations"]]
            self.assertTrue(any("start_mismatch" in d for d in details),
                            f"Expected start_mismatch in {details}")


class TestPidZombie(unittest.TestCase):
    """PID is zombie → DEAD_PID."""
    def test_pid_zombie(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tempfile.mkdtemp(dir=tmp)
            _make_fake_proc(proc_root, 777, 5000, state_char="Z")
            handle = _make_pid_handle(777, start=5000)
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            details = [v["detail"] for v in obs["violations"]]
            self.assertTrue(any("zombie" in d for d in details),
                            f"Expected zombie in {details}")


class TestPidStartMissing(unittest.TestCase):
    """Handle has no 'start' field: only verify alive, no violation."""
    def test_pid_start_missing_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tempfile.mkdtemp(dir=tmp)
            _make_fake_proc(proc_root, 222, 12345)
            handle = {"kind": "pid", "pid": 222, "ref": "222"}  # no 'start'
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0, f"Expected 0, got {rc}")
            # check info contains start_unverified
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertTrue(any(i.get("type") == "start_unverified"
                                for i in obs.get("info", [])),
                            f"Expected start_unverified info, got {obs['info']}")


class TestPidBadHandle(unittest.TestCase):
    """Handle with null → BAD_HANDLE."""
    def test_null_handle_dispatched(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            items = [_make_item(state="DISPATCHED", handle=None)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("VIOLATION BAD_HANDLE", stdout)


class TestPidNotIntUsesRef(unittest.TestCase):
    """pid field not integer → use int(ref), still records BAD_HANDLE if fails."""
    def test_pid_not_int_falls_back_to_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tempfile.mkdtemp(dir=tmp)
            _make_fake_proc(proc_root, 888, 77777)
            # pid as string (not int), ref=888 which is int-able
            handle = {"kind": "pid", "pid": "888", "ref": "888", "start": 77777}
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            # ref "888" → int(888) → pid found alive → OK
            self.assertEqual(rc, 0)


class TestPidNotIntBadRef(unittest.TestCase):
    """pid field not int, ref also not parseable → BAD_HANDLE violation."""
    def test_pid_not_int_bad_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            handle = {"kind": "pid", "pid": "not-a-pid", "ref": "also-bad"}
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("BAD_HANDLE", stdout)


class TestStatCommWithSpacesAndParens(unittest.TestCase):
    """Parsing /proc/<pid>/stat where comm contains spaces and parentheses."""
    def test_comm_spaces_parens(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tempfile.mkdtemp(dir=tmp)
            # comm = "python3 (my (app))"
            _make_fake_proc_with_comm(proc_root, 999, 54321,
                                      "python3 (my (app))")
            handle = _make_pid_handle(999, start=54321)
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0, f"Got rc={rc}, stdout={stdout!r}")


class TestCronNormal(unittest.TestCase):
    """Cron job healthy → no violation."""
    def test_cron_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            last_run = _now_iso(T_NOW - 300)  # 5 min ago
            job = _make_job("job1", enabled=True, last_run_at=last_run,
                            last_status="ok",
                            schedule={"kind": "interval", "minutes": 10})
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.strip(), "")


class TestCronStale(unittest.TestCase):
    """Cron last_run_at > 3× interval → CRON_STALE."""
    def test_cron_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            interval_min = 10
            # last run was 31 minutes ago (> 3*10)
            last_run = _now_iso(T_NOW - 31 * 60)
            job = _make_job("job1", enabled=True, last_run_at=last_run,
                            last_status="ok",
                            schedule={"kind": "interval", "minutes": interval_min})
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout)


class TestCronStaleBoundary(unittest.TestCase):
    """Exactly 3× interval → NOT stale (> not >=)."""
    def test_cron_exactly_3x_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            interval_min = 10
            interval_sec = interval_min * 60
            # Exactly 3× interval
            last_run = _now_iso(T_NOW - 3 * interval_sec)
            job = _make_job("job1", enabled=True, last_run_at=last_run,
                            last_status="ok",
                            schedule={"kind": "interval", "minutes": interval_min})
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            # Exactly 3x should NOT be stale
            self.assertEqual(rc, 0, f"Exactly 3x should not be stale. rc={rc} stdout={stdout!r}")


class TestCronDisabled(unittest.TestCase):
    """Cron job disabled → CRON_DISABLED."""
    def test_cron_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            last_run = _now_iso(T_NOW - 300)
            job = _make_job("job1", enabled=False, last_run_at=last_run,
                            last_status="ok")
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_DISABLED", stdout)


class TestCronMissing(unittest.TestCase):
    """Cron job not in jobs.json → CRON_MISSING."""
    def test_cron_job_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            handle = _make_cron_handle("nonexistent_job")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[])  # empty jobs
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_MISSING", stdout)


class TestCronLastStatusError(unittest.TestCase):
    """Cron last_status=error → CRON_STATUS."""
    def test_cron_status_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            last_run = _now_iso(T_NOW - 300)
            job = _make_job("job1", enabled=True, last_run_at=last_run,
                            last_status="error")
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STATUS", stdout)


class TestCronLastRunNull(unittest.TestCase):
    """Cron last_run_at=null → CRON_STALE."""
    def test_cron_last_run_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            job = _make_job("job1", enabled=True, last_run_at=None,
                            last_status="ok")
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout)


class TestCronHeuristicDaily(unittest.TestCase):
    """kind=cron with daily schedule (hour non-*) → 24h interval heuristic."""
    def test_cron_heuristic_daily_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            # daily at 9pm: expr "0 21 * * *"
            # interval = 24h. Last run 2h ago → not stale
            last_run = _now_iso(T_NOW - 2 * 3600)
            job = _make_job("job1", enabled=True, last_run_at=last_run,
                            last_status="ok",
                            schedule={"kind": "cron", "expr": "0 21 * * *"})
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)

    def test_cron_heuristic_daily_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            # daily: 3× 24h = 72h. Last run 73h ago → stale
            last_run = _now_iso(T_NOW - 73 * 3600)
            job = _make_job("job1", enabled=True, last_run_at=last_run,
                            last_status="ok",
                            schedule={"kind": "cron", "expr": "0 21 * * *"})
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout)


class TestCronHeuristicWeekly(unittest.TestCase):
    """kind=cron with weekly schedule (dow non-*) → 7-day interval."""
    def test_cron_heuristic_weekly_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            # Sunday 8pm: "0 20 * * 0" → dow non-* → 7 days
            # Last run 5 days ago → not stale (3×7=21 days)
            last_run = _now_iso(T_NOW - 5 * 86400)
            job = _make_job("job1", enabled=True, last_run_at=last_run,
                            last_status="ok",
                            schedule={"kind": "cron", "expr": "0 20 * * 0"})
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)


class TestCronUnknownSchedule(unittest.TestCase):
    """Unknown schedule kind → info 'schedule_unknown', no CRON_STALE."""
    def test_cron_unknown_schedule_no_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            job = _make_job("job1", enabled=True, last_run_at=None,
                            last_status="ok",
                            schedule={"kind": "exotic", "display": "sometime"})
            handle = _make_cron_handle("job1")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items, jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            # No CRON_STALE, but enabled=True and last_status=ok → only schedule_unknown info
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertTrue(any(i.get("type") == "schedule_unknown"
                                for i in obs.get("info", [])))
            # No CRON_STALE violation
            viols = [v["type"] for v in obs.get("violations", [])]
            self.assertNotIn("CRON_STALE", viols)


class TestManualOverdue(unittest.TestCase):
    """Manual handle with past due → OVERDUE_MANUAL."""
    def test_manual_overdue(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            handle = _make_manual_handle()
            due = T_NOW - 3600  # 1 hour past due
            items = [_make_item(state="DISPATCHED", handle=handle, due=due)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("OVERDUE_MANUAL", stdout)


class TestManualExactlyDue(unittest.TestCase):
    """Manual handle with due == now → NOT overdue (> not >=)."""
    def test_manual_exactly_due_not_overdue(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            handle = _make_manual_handle()
            due = T_NOW  # exactly now
            items = [_make_item(state="DISPATCHED", handle=handle, due=due)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0, f"Exactly due should not be overdue. rc={rc}")


class TestManualDueNull(unittest.TestCase):
    """Manual handle with due=null → not overdue."""
    def test_manual_due_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            handle = _make_manual_handle()
            items = [_make_item(state="DISPATCHED", handle=handle, due=None)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)


class TestItemBlockVerified(unittest.TestCase):
    """OPEN item blocked by item that is VERIFIED → INVALID_BLOCK."""
    def test_block_by_verified_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            target = _make_item(id="C-0010", state="VERIFIED")
            blocker = _make_item(id="C-0011", state="OPEN",
                                  blocked_by="item:C-0010")
            home = _make_profile(tmp + "/home", [target, blocker])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("INVALID_BLOCK", stdout)


class TestItemBlockDropped(unittest.TestCase):
    """OPEN item blocked by item that is DROPPED → INVALID_BLOCK."""
    def test_block_by_dropped_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            target = _make_item(id="C-0010", state="DROPPED")
            blocker = _make_item(id="C-0011", state="OPEN",
                                  blocked_by="item:C-0010")
            home = _make_profile(tmp + "/home", [target, blocker])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("INVALID_BLOCK", stdout)


class TestItemBlockOpen(unittest.TestCase):
    """OPEN item blocked by OPEN item → valid, no violation."""
    def test_block_by_open_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            target = _make_item(id="C-0010", state="OPEN")
            blocker = _make_item(id="C-0011", state="OPEN",
                                  blocked_by="item:C-0010")
            home = _make_profile(tmp + "/home", [target, blocker])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)


class TestItemBlockDispatched(unittest.TestCase):
    """OPEN item blocked by DISPATCHED item → valid (no violation)."""
    def test_block_by_dispatched_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tempfile.mkdtemp(dir=tmp)
            _make_fake_proc(proc_root, 123, 456)
            target = _make_item(id="C-0010", state="DISPATCHED",
                                 handle=_make_pid_handle(123, start=456))
            blocker = _make_item(id="C-0011", state="OPEN",
                                  blocked_by="item:C-0010")
            home = _make_profile(tmp + "/home", [target, blocker])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)


class TestItemBlockBlocked(unittest.TestCase):
    """OPEN item blocked by BLOCKED item → valid (no INVALID_BLOCK)."""
    def test_block_by_blocked_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            target = _make_item(id="C-0010", state="BLOCKED",
                                 blocked_by="user:some reason")
            blocker = _make_item(id="C-0011", state="OPEN",
                                  blocked_by="item:C-0010")
            home = _make_profile(tmp + "/home", [target, blocker])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            # BLOCKED item itself has user: blocker → OK. C-0011 blocks on BLOCKED → valid
            # But wait: target C-0010 has user: → valid. rc should be 0.
            self.assertEqual(rc, 0)


class TestItemBlockMissing(unittest.TestCase):
    """OPEN item blocked by nonexistent item ID → INVALID_BLOCK (target_missing)."""
    def test_block_by_missing_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            blocker = _make_item(id="C-0011", state="OPEN",
                                  blocked_by="item:C-MISSING")
            home = _make_profile(tmp + "/home", [blocker])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("INVALID_BLOCK", stdout)
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            details = [v["detail"] for v in obs["violations"]]
            self.assertTrue(any("target_missing" in d for d in details))


class TestOpenNoBlockedByIgnored(unittest.TestCase):
    """OPEN item with no blocked_by → completely ignored."""
    def test_open_no_blocker_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            item = _make_item(id="C-0001", state="OPEN", blocked_by=None)
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            # Item should not appear in checks at all
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            check_ids = [c["item"] for c in obs.get("checks", [])]
            self.assertNotIn("C-0001", check_ids)


class TestVerifiedDroppedIgnored(unittest.TestCase):
    """VERIFIED and DROPPED items are completely ignored."""
    def test_verified_dropped_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            items = [
                _make_item(id="C-0001", state="VERIFIED"),
                _make_item(id="C-0002", state="DROPPED"),
            ]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertEqual(obs["counts"]["violations"], 0)
            self.assertEqual(obs["counts"]["checked"], 0)


class TestFileBlockHolds(unittest.TestCase):
    """file: blocker where file exists → INVALID_BLOCK (flipped semantics)."""
    def test_file_block_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            # Create a real file to block on
            blocker_file = tmp + "/some_file.txt"
            with open(blocker_file, "w") as fh:
                fh.write("exists")
            item = _make_item(id="C-0001", state="OPEN",
                               blocked_by=f"file:{blocker_file}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("INVALID_BLOCK", stdout)


class TestFileBlockMissing(unittest.TestCase):
    """file: blocker where file does NOT exist → blocker holds, no violation (flipped semantics)."""
    def test_file_block_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            blocker_file = tmp + "/nonexistent_file.txt"
            item = _make_item(id="C-0001", state="OPEN",
                               blocked_by=f"file:{blocker_file}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.strip(), "")


class TestFileBlockSemanticsF1(unittest.TestCase):
    """
    F1 Requirements:
    (a) File does not exist → no violation
    (b) File exists (regular file) → INVALID_BLOCK and detail contains file_exists
    (c) Directory exists also counts as existing → INVALID_BLOCK
    (d) Broken symlink (lexists is True) → INVALID_BLOCK
    (e) Two-round lifecycle: blocker file appears (violation), file deleted (clears to anomalies.jsonl).
        Tested for both BLOCKED and OPEN states.
    """
    def test_a_file_missing_no_violation_blocked(self):
        """(a) File does not exist for state=BLOCKED → no violation."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            missing = os.path.join(tmp, "does_not_exist.txt")
            item = _make_item(id="C-A001", state="BLOCKED", blocked_by=f"file:{missing}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.strip(), "")
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertEqual(obs["counts"]["violations"], 0)
            chk = [c for c in obs["checks"] if c["item"] == "C-A001"][0]
            self.assertEqual(chk["result"], "OK")

    def test_a_file_missing_no_violation_open(self):
        """(a) File does not exist for state=OPEN → no violation."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            missing = os.path.join(tmp, "does_not_exist_open.txt")
            item = _make_item(id="C-A002", state="OPEN", blocked_by=f"file:{missing}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.strip(), "")
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertEqual(obs["counts"]["violations"], 0)
            chk = [c for c in obs["checks"] if c["item"] == "C-A002"][0]
            self.assertEqual(chk["result"], "OK")

    def test_b_regular_file_exists_invalid_block_blocked(self):
        """(b) Regular file exists for state=BLOCKED → INVALID_BLOCK with detail containing file_exists."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            regular_file = os.path.join(tmp, "present.txt")
            with open(regular_file, "w") as fh:
                fh.write("data")
            item = _make_item(id="C-B001", state="BLOCKED", blocked_by=f"file:{regular_file}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("VIOLATION INVALID_BLOCK C-B001", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            v = [x for x in obs["violations"] if x["item"] == "C-B001"][0]
            self.assertEqual(v["type"], "INVALID_BLOCK")
            self.assertIn("file_exists", v["detail"])
            self.assertEqual(v["detail"], f"file_exists:{regular_file}")

    def test_b_regular_file_exists_invalid_block_open(self):
        """(b) Regular file exists for state=OPEN → INVALID_BLOCK with detail containing file_exists."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            regular_file = os.path.join(tmp, "present_open.txt")
            with open(regular_file, "w") as fh:
                fh.write("data")
            item = _make_item(id="C-B002", state="OPEN", blocked_by=f"file:{regular_file}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("VIOLATION INVALID_BLOCK C-B002", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            v = [x for x in obs["violations"] if x["item"] == "C-B002"][0]
            self.assertEqual(v["type"], "INVALID_BLOCK")
            self.assertIn("file_exists", v["detail"])
            self.assertEqual(v["detail"], f"file_exists:{regular_file}")

    def test_c_directory_exists_invalid_block_blocked(self):
        """(c) Directory exists counts as existing for state=BLOCKED → INVALID_BLOCK."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            blocker_dir = os.path.join(tmp, "blocker_dir")
            os.makedirs(blocker_dir)
            item = _make_item(id="C-C001", state="BLOCKED", blocked_by=f"file:{blocker_dir}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("VIOLATION INVALID_BLOCK C-C001", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            v = [x for x in obs["violations"] if x["item"] == "C-C001"][0]
            self.assertEqual(v["type"], "INVALID_BLOCK")
            self.assertIn("file_exists", v["detail"])
            self.assertEqual(v["detail"], f"file_exists:{blocker_dir}")

    def test_c_directory_exists_invalid_block_open(self):
        """(c) Directory exists counts as existing for state=OPEN → INVALID_BLOCK."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            blocker_dir = os.path.join(tmp, "blocker_dir_open")
            os.makedirs(blocker_dir)
            item = _make_item(id="C-C002", state="OPEN", blocked_by=f"file:{blocker_dir}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("VIOLATION INVALID_BLOCK C-C002", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            v = [x for x in obs["violations"] if x["item"] == "C-C002"][0]
            self.assertEqual(v["type"], "INVALID_BLOCK")
            self.assertIn("file_exists", v["detail"])

    def test_d_broken_symlink_invalid_block_blocked(self):
        """(d) Broken symlink (lexists is True, exists is False) for state=BLOCKED → INVALID_BLOCK."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            broken_symlink = os.path.join(tmp, "broken_link")
            nonexistent = os.path.join(tmp, "no_such_target")
            os.symlink(nonexistent, broken_symlink)
            self.assertTrue(os.path.lexists(broken_symlink))
            self.assertFalse(os.path.exists(broken_symlink))

            item = _make_item(id="C-D001", state="BLOCKED", blocked_by=f"file:{broken_symlink}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("VIOLATION INVALID_BLOCK C-D001", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            v = [x for x in obs["violations"] if x["item"] == "C-D001"][0]
            self.assertEqual(v["type"], "INVALID_BLOCK")
            self.assertIn("file_exists", v["detail"])
            self.assertEqual(v["detail"], f"file_exists:{broken_symlink}")

    def test_d_broken_symlink_invalid_block_open(self):
        """(d) Broken symlink (lexists is True, exists is False) for state=OPEN → INVALID_BLOCK."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            broken_symlink = os.path.join(tmp, "broken_link_open")
            nonexistent = os.path.join(tmp, "no_such_target_open")
            os.symlink(nonexistent, broken_symlink)
            self.assertTrue(os.path.lexists(broken_symlink))
            self.assertFalse(os.path.exists(broken_symlink))

            item = _make_item(id="C-D002", state="OPEN", blocked_by=f"file:{broken_symlink}")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("VIOLATION INVALID_BLOCK C-D002", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            v = [x for x in obs["violations"] if x["item"] == "C-D002"][0]
            self.assertEqual(v["type"], "INVALID_BLOCK")
            self.assertIn("file_exists", v["detail"])

    def test_e_lifecycle_two_rounds_blocked_state(self):
        """(e) state=BLOCKED: file appears → violation appears; file deleted → violation cleared into anomalies.jsonl."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            out_dir = tmp + "/out"

            blocker_file = os.path.join(tmp, "checkpoint.done")
            # Round 1: file exists (appears)
            with open(blocker_file, "w") as fh:
                fh.write("done")
            item = _make_item(id="C-E001", state="BLOCKED", blocked_by=f"file:{blocker_file}")
            _make_profile(home, [item])

            t1 = T_NOW
            rc1, out1 = _run_observer(home, out_dir=out_dir, now=t1, proc_root=proc_root)
            self.assertEqual(rc1, 0)
            self.assertIn("VIOLATION INVALID_BLOCK C-E001 0", out1)

            # Check violations.json has 1 violation with first_seen=t1
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path) as fh:
                vdata1 = json.load(fh)
            self.assertEqual(len(vdata1["violations"]), 1)
            self.assertEqual(vdata1["violations"][0]["type"], "INVALID_BLOCK")
            self.assertEqual(vdata1["violations"][0]["item"], "C-E001")
            self.assertEqual(vdata1["violations"][0]["first_seen"], t1)

            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            self.assertFalse(os.path.exists(anomalies_path))

            # Round 2: file deleted (waiting for file, blocker holds again)
            os.unlink(blocker_file)
            self.assertFalse(os.path.lexists(blocker_file))

            t2 = T_NOW + 300
            rc2, out2 = _run_observer(home, out_dir=out_dir, now=t2, proc_root=proc_root)
            self.assertEqual(rc2, 0)
            self.assertEqual(out2.strip(), "")

            # Check violations.json is cleared
            with open(viols_path) as fh:
                vdata2 = json.load(fh)
            self.assertEqual(len(vdata2["violations"]), 0)

            # Check anomalies.jsonl has the cleared anomaly
            self.assertTrue(os.path.exists(anomalies_path))
            with open(anomalies_path) as fh:
                anomaly_lines = [l.strip() for l in fh if l.strip()]
            self.assertEqual(len(anomaly_lines), 1)
            anomaly = json.loads(anomaly_lines[0])
            self.assertEqual(anomaly["type"], "INVALID_BLOCK")
            self.assertEqual(anomaly["item"], "C-E001")
            self.assertEqual(anomaly["first_seen"], t1)
            self.assertEqual(anomaly["cleared_at"], t2)
            self.assertEqual(anomaly["seconds_open"], 300)

    def test_e_lifecycle_two_rounds_open_state(self):
        """(e) state=OPEN: file appears → violation appears; file deleted → violation cleared into anomalies.jsonl."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            out_dir = tmp + "/out"

            blocker_file = os.path.join(tmp, "checkpoint_open.done")
            # Round 1: file exists (appears)
            with open(blocker_file, "w") as fh:
                fh.write("done")
            item = _make_item(id="C-E002", state="OPEN", blocked_by=f"file:{blocker_file}")
            _make_profile(home, [item])

            t1 = T_NOW
            rc1, out1 = _run_observer(home, out_dir=out_dir, now=t1, proc_root=proc_root)
            self.assertEqual(rc1, 0)
            self.assertIn("VIOLATION INVALID_BLOCK C-E002 0", out1)

            # Check violations.json has 1 violation with first_seen=t1
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path) as fh:
                vdata1 = json.load(fh)
            self.assertEqual(len(vdata1["violations"]), 1)
            self.assertEqual(vdata1["violations"][0]["type"], "INVALID_BLOCK")
            self.assertEqual(vdata1["violations"][0]["item"], "C-E002")
            self.assertEqual(vdata1["violations"][0]["first_seen"], t1)

            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            self.assertFalse(os.path.exists(anomalies_path))

            # Round 2: file deleted (waiting for file, blocker holds again)
            os.unlink(blocker_file)
            self.assertFalse(os.path.lexists(blocker_file))

            t2 = T_NOW + 300
            rc2, out2 = _run_observer(home, out_dir=out_dir, now=t2, proc_root=proc_root)
            self.assertEqual(rc2, 0)
            self.assertEqual(out2.strip(), "")

            # Check violations.json is cleared
            with open(viols_path) as fh:
                vdata2 = json.load(fh)
            self.assertEqual(len(vdata2["violations"]), 0)

            # Check anomalies.jsonl has the cleared anomaly
            self.assertTrue(os.path.exists(anomalies_path))
            with open(anomalies_path) as fh:
                anomaly_lines = [l.strip() for l in fh if l.strip()]
            self.assertEqual(len(anomaly_lines), 1)
            anomaly = json.loads(anomaly_lines[0])
            self.assertEqual(anomaly["type"], "INVALID_BLOCK")
            self.assertEqual(anomaly["item"], "C-E002")
            self.assertEqual(anomaly["first_seen"], t1)
            self.assertEqual(anomaly["cleared_at"], t2)
            self.assertEqual(anomaly["seconds_open"], 300)


class TestCmdNotExecuted(unittest.TestCase):
    """cmd: blocker is never executed, listed as unverifiable, not a violation."""
    def test_cmd_not_executed_no_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            # If the cmd were executed, it would create this marker file
            marker_file = tmp + "/marker_should_not_exist.txt"
            cmd = f"cmd:touch {marker_file}"
            item = _make_item(id="C-0001", state="OPEN", blocked_by=cmd)
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            # Must NOT create the marker file
            self.assertFalse(os.path.exists(marker_file),
                             "cmd was executed! Marker file created.")
            # Must be listed as unverifiable, not a violation
            self.assertEqual(rc, 0)
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertIn("C-0001", obs.get("unverifiable", []))
            viols = [v["type"] for v in obs.get("violations", [])]
            self.assertNotIn("INVALID_BLOCK", viols)


class TestUserBlockHolds(unittest.TestCase):
    """user: blocker always holds → no violation."""
    def test_user_block_holds(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            item = _make_item(id="C-0001", state="OPEN",
                               blocked_by="user:please authorize this")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)


class TestBlockedNoBlockedByIsInfo(unittest.TestCase):
    """BLOCKED item with blocked_by=null → info NO_CHECKABLE_BLOCKER, not violation."""
    def test_blocked_no_blocked_by_is_info_not_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            item = _make_item(id="C-0001", state="BLOCKED", blocked_by=None)
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0, f"BLOCKED+null blocked_by should not be violation. rc={rc}")
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertTrue(any(i.get("type") == "NO_CHECKABLE_BLOCKER"
                                for i in obs.get("info", [])),
                            f"Expected NO_CHECKABLE_BLOCKER in info: {obs['info']}")
            self.assertEqual(obs["counts"]["violations"], 0)


class TestUnknownBlockerFormat(unittest.TestCase):
    """blocked_by not starting with known prefix → UNKNOWN_BLOCKER_FORMAT info, not violation."""
    def test_unknown_blocker_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            item = _make_item(id="C-0001", state="OPEN",
                               blocked_by="someother:weird format")
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertTrue(any(i.get("type") == "UNKNOWN_BLOCKER_FORMAT"
                                for i in obs.get("info", [])))


class TestLedgerBytesUnchanged(unittest.TestCase):
    """Ledger bytes and mtime must be unchanged before/after observer run."""
    def test_ledger_bytes_mtime_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = _make_profile(tmp + "/home", [])
            ledger_path = os.path.join(home, "commitments", "ledger.json")
            with open(ledger_path, "rb") as fh:
                before_bytes = fh.read()
            before_mtime = os.stat(ledger_path).st_mtime_ns
            before_sha = hashlib.sha256(before_bytes).hexdigest()

            out_dir = tmp + "/out"
            _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)

            with open(ledger_path, "rb") as fh:
                after_bytes = fh.read()
            after_mtime = os.stat(ledger_path).st_mtime_ns
            after_sha = hashlib.sha256(after_bytes).hexdigest()

            self.assertEqual(before_sha, after_sha, "Ledger bytes changed!")
            self.assertEqual(before_mtime, after_mtime, "Ledger mtime changed!")


class TestRepeatedRunsAppend(unittest.TestCase):
    """Repeated runs append to observations.jsonl (line count increases, existing lines unchanged)."""
    def test_repeated_runs_append_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = _make_profile(tmp + "/home", [])
            out_dir = tmp + "/out"

            for i in range(3):
                _run_observer(home, out_dir=out_dir, now=T_NOW + i * 300,
                               proc_root=proc_root)

            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                lines = [l.strip() for l in fh if l.strip()]
            self.assertEqual(len(lines), 3, f"Expected 3 lines, got {len(lines)}")
            # Each line should be valid JSON
            for line in lines:
                obj = json.loads(line)
                self.assertEqual(obj["type"], "OBSERVATION")
            # Second run's line should be unchanged after third run
            first_line = lines[0]
            # Re-read to check
            with open(obs_path) as fh:
                all_lines = [l.strip() for l in fh if l.strip()]
            self.assertEqual(all_lines[0], first_line)


class TestParallelInstances(unittest.TestCase):
    """8 parallel instances complete without corruption."""
    def test_8_parallel_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = _make_profile(tmp + "/home", [])
            out_dir = tmp + "/out"
            os.makedirs(out_dir, exist_ok=True)

            script = os.path.join(os.path.dirname(__file__), "..", "scripts",
                                   "workflow_observer.py")
            procs = []
            for i in range(8):
                p = subprocess.Popen(
                    [sys.executable, script,
                     "--home", home,
                     "--out-dir", out_dir,
                     "--now", str(T_NOW + i),
                     "--proc-root", proc_root],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                procs.append(p)

            for p in procs:
                p.wait(timeout=30)

            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                lines = [l.strip() for l in fh if l.strip()]

            self.assertEqual(len(lines), 8, f"Expected 8 lines, got {len(lines)}")
            for line in lines:
                obj = json.loads(line)  # must be valid JSON
                self.assertEqual(obj["type"], "OBSERVATION")

            # violations.json must be valid
            viols_path = out_dir + "/violations.json"
            with open(viols_path) as fh:
                vdata = json.load(fh)
            self.assertIn("violations", vdata)


class TestViolationLifecycle(unittest.TestCase):
    """Full lifecycle: appear (stdout, exit 2, seconds=0), continue (silent, seconds↑), clear."""
    def test_violation_appear_continue_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            os.makedirs(os.path.join(home, "commitments"))
            os.makedirs(os.path.join(home, "cron"))
            out_dir = tmp + "/out"

            # Step 1: Violation appears (dead pid)
            item = _make_item(id="C-0001", state="DISPATCHED",
                               handle=_make_pid_handle(99999999, start=1))
            _write_ledger(home, [item])
            _write_jobs(home, [])
            rc1, out1 = _run_observer(home, out_dir=out_dir, now=T_NOW,
                                       proc_root=proc_root)
            self.assertEqual(rc1, 0)
            self.assertIn("VIOLATION DEAD_PID C-0001 0", out1)

            # Step 2: Violation continues (silent stdout, exit 2)
            rc2, out2 = _run_observer(home, out_dir=out_dir, now=T_NOW + 300,
                                       proc_root=proc_root)
            self.assertEqual(rc2, 0)
            self.assertEqual(out2.strip(), "", f"Expected empty stdout, got {out2!r}")

            # Check first_seen unchanged and seconds_open increased
            viols_path = out_dir + "/violations.json"
            with open(viols_path) as fh:
                vdata = json.load(fh)
            v = vdata["violations"][0]
            self.assertAlmostEqual(v["first_seen"], T_NOW, delta=1)
            self.assertGreater(v["seconds_open"], 200)

            # Step 3: Violation clears (no violation item)
            good_item = _make_item(id="C-0001", state="VERIFIED")
            _write_ledger(home, [good_item])
            rc3, out3 = _run_observer(home, out_dir=out_dir, now=T_NOW + 600,
                                       proc_root=proc_root)
            self.assertEqual(rc3, 0)
            self.assertEqual(out3.strip(), "", f"Expected empty stdout on clear: {out3!r}")

            # anomalies.jsonl should have the cleared record
            anomalies_path = out_dir + "/anomalies.jsonl"
            with open(anomalies_path) as fh:
                anomaly_lines = [l.strip() for l in fh if l.strip()]
            self.assertGreater(len(anomaly_lines), 0)
            anomaly = json.loads(anomaly_lines[-1])
            self.assertIn("cleared_at", anomaly)
            self.assertIn("first_seen", anomaly)
            self.assertIn("seconds_open", anomaly)

            # violations.json should have empty violations list
            with open(viols_path) as fh:
                vdata = json.load(fh)
            self.assertEqual(len(vdata["violations"]), 0)


class TestNoViolationsEmptyStdout(unittest.TestCase):
    """No violations → stdout is exactly empty (0 bytes) and exit 0."""
    def test_no_violations_empty_stdout_exit_0(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = _make_profile(tmp + "/home", [])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "", f"Expected empty stdout, got {stdout!r}")


class TestCorruptLedgerError(unittest.TestCase):
    """Corrupt ledger → OBSERVER_ERROR in observations, exit 1."""
    def test_corrupt_ledger_observer_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            os.makedirs(os.path.join(home, "commitments"))
            os.makedirs(os.path.join(home, "cron"))
            ledger_path = os.path.join(home, "commitments", "ledger.json")
            with open(ledger_path, "wb") as fh:
                fh.write(b"{{not valid json{{")
            _write_jobs(home, [])
            out_dir = tmp + "/out"

            # patch retry delay and count to speed up
            with patch.object(wo, "READ_RETRY_DELAY", 0), \
                 patch.object(wo, "READ_RETRIES", 1):
                rc, stdout = _run_observer(home, out_dir=out_dir,
                                           now=T_NOW, proc_root=proc_root)

            self.assertEqual(rc, 1)

            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                lines = [l.strip() for l in fh if l.strip()]
            self.assertEqual(len(lines), 1)
            obs = json.loads(lines[0])
            self.assertEqual(obs["type"], "OBSERVER_ERROR")


class TestLedgerMissing(unittest.TestCase):
    """Missing ledger file → OBSERVER_ERROR, exit 1."""
    def test_missing_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            os.makedirs(os.path.join(home, "commitments"))
            os.makedirs(os.path.join(home, "cron"))
            # Do NOT write ledger
            _write_jobs(home, [])
            out_dir = tmp + "/out"
            with patch.object(wo, "READ_RETRY_DELAY", 0), \
                 patch.object(wo, "READ_RETRIES", 1):
                rc, stdout = _run_observer(home, out_dir=out_dir,
                                           now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 1)


class TestLedgerNotDict(unittest.TestCase):
    """Ledger that is a JSON array (not dict) → OBSERVER_ERROR."""
    def test_ledger_not_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            os.makedirs(os.path.join(home, "commitments"))
            os.makedirs(os.path.join(home, "cron"))
            ledger_path = os.path.join(home, "commitments", "ledger.json")
            with open(ledger_path, "wb") as fh:
                fh.write(b"[1, 2, 3]")
            _write_jobs(home, [])
            out_dir = tmp + "/out"
            with patch.object(wo, "READ_RETRY_DELAY", 0), \
                 patch.object(wo, "READ_RETRIES", 1):
                rc, _ = _run_observer(home, out_dir=out_dir,
                                      now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 1)


class TestJobsJsonCorruptWithCron(unittest.TestCase):
    """Corrupt jobs.json when cron items exist → OBSERVER_ERROR."""
    def test_corrupt_jobs_with_cron(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            handle = _make_cron_handle("somejob")
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = tmp + "/home"
            os.makedirs(os.path.join(home, "commitments"))
            os.makedirs(os.path.join(home, "cron"))
            _write_ledger(home, items)
            jobs_path = os.path.join(home, "cron", "jobs.json")
            with open(jobs_path, "wb") as fh:
                fh.write(b"{{{broken json")
            out_dir = tmp + "/out"
            with patch.object(wo, "READ_RETRY_DELAY", 0), \
                 patch.object(wo, "READ_RETRIES", 1):
                rc, _ = _run_observer(home, out_dir=out_dir,
                                      now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 1)


class TestTornReadRetry(unittest.TestCase):
    """First read returns broken content, second read returns valid: retry succeeds."""
    def test_torn_read_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = _make_profile(tmp + "/home", [])

            call_count = [0]
            original_read = wo._read_ledger

            def flaky_read(path):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise ValueError("simulated torn read")
                return original_read(path)

            out_dir = tmp + "/out"
            with patch.object(wo, "READ_RETRY_DELAY", 0), \
                 patch.object(wo, "READ_RETRIES", 3), \
                 patch.object(wo, "_read_ledger", flaky_read):
                rc, _ = _run_observer(home, out_dir=out_dir,
                                      now=T_NOW, proc_root=proc_root)

            self.assertEqual(rc, 0, "Should succeed after retry")
            self.assertGreaterEqual(call_count[0], 2, "Should have retried")


class TestLockTimeout(unittest.TestCase):
    """Another process holds the lock → lock timeout → exit 1, no output written."""
    def test_lock_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = _make_profile(tmp + "/home", [])
            out_dir = tmp + "/out"
            os.makedirs(out_dir)

            lock_path = out_dir + "/observer.lock"
            # Acquire lock in this process
            lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                rc, stdout = _run_observer(
                    home, out_dir=out_dir, now=T_NOW, proc_root=proc_root,
                    extra_args=["--lock-timeout", "1"]
                )
                self.assertEqual(rc, 1)
                # No observations written
                obs_path = out_dir + "/observations.jsonl"
                self.assertFalse(os.path.exists(obs_path),
                                 "observations.jsonl should not exist when lock failed")
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)


class TestOutputFileMode(unittest.TestCase):
    """Output files must be mode 0o644 regardless of umask."""
    def _check_modes(self, umask_val):
        old_umask = os.umask(umask_val)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                proc_root = tmp + "/proc"
                os.makedirs(proc_root)
                home = _make_profile(tmp + "/home", [])
                out_dir = tmp + "/out"
                _run_observer(home, out_dir=out_dir,
                               now=T_NOW, proc_root=proc_root)
                obs_path = out_dir + "/observations.jsonl"
                viols_path = out_dir + "/violations.json"
                for path in [obs_path, viols_path]:
                    if os.path.exists(path):
                        mode = stat.S_IMODE(os.stat(path).st_mode)
                        self.assertEqual(
                            mode, 0o644,
                            f"{path} has mode {oct(mode)}, expected 0o644 (umask={oct(umask_val)})"
                        )
        finally:
            os.umask(old_umask)

    def test_file_mode_umask_0022(self):
        self._check_modes(0o022)

    def test_file_mode_umask_0002(self):
        self._check_modes(0o002)


class TestFsyncCalled(unittest.TestCase):
    """os.fsync is called for observations, anomalies, violations, and dir."""
    def test_fsync_called(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            # Add a dead pid to generate anomaly on second run
            home = _make_profile(tmp + "/home", [])
            out_dir = tmp + "/out"

            fsync_count = [0]
            original_fsync = os.fsync

            def counting_fsync(fd):
                fsync_count[0] += 1
                return original_fsync(fd)

            with patch("os.fsync", side_effect=counting_fsync):
                _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)

            self.assertGreater(fsync_count[0], 0,
                               "os.fsync should have been called")


class TestViolationsJsonCorruptReset(unittest.TestCase):
    """Corrupt violations.json → renamed to .corrupt-<epoch>, state_reset=True in obs."""
    def test_corrupt_violations_renamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = _make_profile(tmp + "/home", [])
            out_dir = tmp + "/out"
            os.makedirs(out_dir)

            # Write corrupt violations.json
            viols_path = out_dir + "/violations.json"
            with open(viols_path, "w") as fh:
                fh.write("{{{not json")

            _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)

            # Check corrupt file was renamed
            corrupt_files = [f for f in os.listdir(out_dir)
                             if f.startswith("violations.json.corrupt-")]
            self.assertTrue(len(corrupt_files) > 0,
                            "Expected corrupt file to be renamed")

            # Check state_reset in observation
            obs_path = out_dir + "/observations.jsonl"
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertTrue(obs.get("state_reset"), "Expected state_reset=True in obs")


# ---------------------------------------------------------------------------
# Verdict mode tests
# ---------------------------------------------------------------------------

def _make_obs_record(ts, type_="OBSERVATION"):
    return {
        "v": 1, "ts": ts, "ts_iso": _now_iso(ts),
        "type": type_, "ledger_sha256": "abc", "ledger_items": 0,
        "checks": [], "violations": [], "unverifiable": [], "info": [],
        "counts": {"checked": 0, "violations": 0, "new_violations": 0},
    }


def _write_obs_records(obs_path, records):
    with open(obs_path, "w") as fh:
        for r in records:
            fh.write(json.dumps(r, sort_keys=True) + "\n")


def _run_verdict(out_dir, start, end, now=None):
    """Run verdict mode and return (exit_code, parsed_json_output)."""
    if now is None:
        now = end + 3600
    import io
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        rc = wo.main([
            "--verdict",
            "--out-dir", out_dir,
            "--start", str(start),
            "--end", str(end),
            "--now", str(now),
        ])
    finally:
        captured = sys.stdout.getvalue()
        sys.stdout = old_stdout
    result = json.loads(captured) if captured.strip() else {}
    return rc, result


class TestVerdictCoverage98Pass(unittest.TestCase):
    """Coverage exactly 98% → PASS on coverage."""
    def test_coverage_98_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            # expected = 1000 slots, need 980 covered (98%)
            expected = 1000
            end = start + expected * 300
            # Generate 980 observation records, one per slot
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = [_make_obs_record(start + i * 300 + 1) for i in range(980)]
            _write_obs_records(obs_path, records)
            # Also write anomalies with cleared_at in range
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": start + 300, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": start + 100,
                "cleared_at": start + 400, "seconds_open": 300,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
            rc, result = _run_verdict(out_dir, start, end)
            self.assertTrue(result["coverage"]["pass"],
                            f"Coverage 980/1000 should pass. Got: {result['coverage']}")


class TestVerdictCoverage979Fail(unittest.TestCase):
    """Coverage 979/1000 (97.9%) → coverage FAIL."""
    def test_coverage_979_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 1000
            end = start + expected * 300
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = [_make_obs_record(start + i * 300 + 1) for i in range(979)]
            _write_obs_records(obs_path, records)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": start + 300, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": start + 100,
                "cleared_at": start + 400, "seconds_open": 300,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
            rc, result = _run_verdict(out_dir, start, end)
            self.assertFalse(result["coverage"]["pass"],
                             f"Coverage 979/1000 should fail. Got: {result['coverage']}")
            self.assertEqual(rc, 2)


class TestVerdictEmptyFail(unittest.TestCase):
    """No records at all → coverage FAIL."""
    def test_verdict_empty_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            end = start + 1000 * 300
            rc, result = _run_verdict(out_dir, start, end)
            self.assertEqual(result["verdict"], "FAIL")
            self.assertFalse(result["coverage"]["pass"])


class TestVerdictDuplicateSlotCountsOnce(unittest.TestCase):
    """Multiple records in same slot → counted as 1."""
    def test_duplicate_slot_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 2
            end = start + expected * 300
            obs_path = os.path.join(out_dir, "observations.jsonl")
            # Both records fall in slot 0
            records = [
                _make_obs_record(start + 1),
                _make_obs_record(start + 2),
            ]
            _write_obs_records(obs_path, records)
            _, result = _run_verdict(out_dir, start, end)
            self.assertEqual(result["coverage"]["covered"], 1,
                             "Two records in same slot should count as 1")


class TestVerdictOutsidePeriodIgnored(unittest.TestCase):
    """Records outside [start, end) are not counted."""
    def test_outside_period_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 1000.0
            expected = 10
            end = start + expected * 300
            obs_path = os.path.join(out_dir, "observations.jsonl")
            # Record before start and after end
            records = [
                _make_obs_record(start - 1),
                _make_obs_record(end + 1),
            ]
            _write_obs_records(obs_path, records)
            _, result = _run_verdict(out_dir, start, end)
            self.assertEqual(result["coverage"]["covered"], 0)


class TestVerdictObserverErrors21of1000Fail(unittest.TestCase):
    """21/1000 OBSERVER_ERROR slots → error rate > 2% → FAIL."""
    def test_error_slots_21_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 1000
            end = start + expected * 300
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = []
            # 979 normal OBSERVATION slots
            for i in range(979):
                records.append(_make_obs_record(start + i * 300 + 1))
            # 21 OBSERVER_ERROR slots (distinct slots starting from 979)
            for i in range(21):
                slot_ts = start + (979 + i) * 300 + 1
                records.append(_make_obs_record(slot_ts, type_="OBSERVER_ERROR"))
            _write_obs_records(obs_path, records)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": start + 300, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": start + 100,
                "cleared_at": start + 400, "seconds_open": 300,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
            rc, result = _run_verdict(out_dir, start, end)
            self.assertEqual(rc, 2)
            self.assertEqual(result["verdict"], "FAIL")
            self.assertFalse(result["observer_errors"]["pass"],
                             f"21/1000 error slots should fail: {result['observer_errors']}")
            self.assertTrue(any("observer_errors" in r for r in result["reasons"]),
                            f"Reasons should include observer_errors: {result['reasons']}")


class TestVerdictObserverErrors20of1000Pass(unittest.TestCase):
    """20/1000 OBSERVER_ERROR slots → error rate exactly 2% → PASS on errors."""
    def test_error_slots_20_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 1000
            end = start + expected * 300
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = []
            for i in range(980):
                records.append(_make_obs_record(start + i * 300 + 1))
            for i in range(20):
                slot_ts = start + (980 + i) * 300 + 1
                records.append(_make_obs_record(slot_ts, type_="OBSERVER_ERROR"))
            _write_obs_records(obs_path, records)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": start + 300, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": start + 100,
                "cleared_at": start + 400, "seconds_open": 300,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
            _, result = _run_verdict(out_dir, start, end)
            self.assertTrue(result["observer_errors"]["pass"],
                            f"20/1000 error slots should pass: {result['observer_errors']}")


class TestVerdictViolation900Pass(unittest.TestCase):
    """Violation duration exactly 900s → pass."""
    def test_violation_900_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 1000
            end = start + expected * 300
            now = end + 3600
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = [_make_obs_record(start + i * 300 + 1) for i in range(980)]
            _write_obs_records(obs_path, records)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            # violation lasting exactly 900s
            fs = start + 100
            anomaly = {
                "ts": start + 300, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": fs,
                "cleared_at": fs + 900, "seconds_open": 900,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertTrue(len(viol_items) > 0)
            self.assertTrue(viol_items[0]["pass"],
                            f"Duration 900 should pass. Items: {viol_items}")


class TestVerdictViolation901Fail(unittest.TestCase):
    """Violation duration 901s → fail."""
    def test_violation_901_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 1000
            end = start + expected * 300
            now = end + 3600
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = [_make_obs_record(start + i * 300 + 1) for i in range(980)]
            _write_obs_records(obs_path, records)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            fs = start + 100
            anomaly = {
                "ts": start + 300, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": fs,
                "cleared_at": fs + 901, "seconds_open": 901,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertTrue(len(viol_items) > 0)
            self.assertFalse(viol_items[0]["pass"],
                             f"Duration 901 should fail. Items: {viol_items}")


class TestVerdictOngoingViolationClampedAtEnd(unittest.TestCase):
    """Still-open violation: duration = min(end, now) - first_seen."""
    def test_ongoing_violation_clamped_at_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 1000
            end = start + expected * 300
            now = end + 3600  # now is after end
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = [_make_obs_record(start + i * 300 + 1) for i in range(980)]
            _write_obs_records(obs_path, records)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            with open(anomalies_path, "w") as fh:
                pass  # empty anomalies
            # Write current violations.json with a still-open violation
            fs = end - 500  # first_seen 500s before end
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001",
                        "detail": "", "first_seen": fs,
                        "last_seen": now, "seconds_open": now - fs,
                    }]
                }, fh)
            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertTrue(len(viol_items) > 0)
            # duration should be clamped to min(end, now) - first_seen = end - fs = 500
            self.assertAlmostEqual(viol_items[0]["duration"], 500, delta=2)


class TestVerdictPriorViolationFromFirstSeen(unittest.TestCase):
    """Prior violation first_seen before period start is not clipped to start."""
    def test_prior_violation_uses_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            expected = 100
            end = start + expected * 300
            now = end + 100
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = [_make_obs_record(start + i * 300 + 1) for i in range(98)]
            _write_obs_records(obs_path, records)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            # first_seen is BEFORE start
            fs = start - 500
            cleared_at = start + 200
            anomaly = {
                "ts": cleared_at, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": fs,
                "cleared_at": cleared_at, "seconds_open": cleared_at - fs,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertTrue(len(viol_items) > 0)
            # duration = min(cleared_at, end, now) - fs = cleared_at - fs = 700
            expected_dur = cleared_at - fs
            self.assertAlmostEqual(viol_items[0]["duration"], expected_dur, delta=2)


class TestVerdictNoDisposalFail(unittest.TestCase):
    """No cleared_at in [start, end] → disposal_evidence FAIL."""
    def test_no_disposal_evidence_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 1000
            end = start + expected * 300
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = [_make_obs_record(start + i * 300 + 1) for i in range(980)]
            _write_obs_records(obs_path, records)
            # anomalies with cleared_at OUTSIDE [start, end]
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": end + 1000, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": start + 100,
                "cleared_at": end + 1000,  # outside period
                "seconds_open": 900,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
            _, result = _run_verdict(out_dir, start, end)
            self.assertFalse(result["disposal_evidence"]["pass"],
                             f"No disposal in period should fail: {result['disposal_evidence']}")


class TestVerdictAnomalyDedup(unittest.TestCase):
    """Duplicate anomaly records (same type,item,first_seen) are counted once."""
    def test_anomaly_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 1000
            end = start + expected * 300
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = [_make_obs_record(start + i * 300 + 1) for i in range(980)]
            _write_obs_records(obs_path, records)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            fs = start + 100
            # Same anomaly written twice
            anomaly = {
                "ts": start + 400, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": fs,
                "cleared_at": start + 400, "seconds_open": 300,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
                fh.write(json.dumps(anomaly) + "\n")
            _, result = _run_verdict(out_dir, start, end)
            # Should be deduplicated to 1 violation item
            self.assertEqual(len(result["violations"]["items"]), 1,
                             f"Duplicate anomalies should be deduped: {result['violations']['items']}")


class TestVerdictBadLinesIgnored(unittest.TestCase):
    """Bad JSON lines in observations.jsonl are skipped and counted."""
    def test_bad_lines_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 5
            end = start + expected * 300
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path, "w") as fh:
                fh.write(json.dumps(_make_obs_record(start + 1)) + "\n")
                fh.write("{bad json\n")  # bad line
                fh.write(json.dumps(_make_obs_record(start + 301)) + "\n")
            _, result = _run_verdict(out_dir, start, end)
            self.assertEqual(result["bad_lines"], 1)


class TestVerdictEndLeStart(unittest.TestCase):
    """end <= start → exit 1."""
    def test_end_le_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            rc, result = _run_verdict(out_dir, start=1000.0, end=1000.0)
            self.assertEqual(rc, 1)

    def test_end_lt_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            rc, result = _run_verdict(out_dir, start=1000.0, end=500.0)
            self.assertEqual(rc, 1)


class TestVerdictExpectedLessThan1(unittest.TestCase):
    """Period too short (expected < 1) → exit 1."""
    def test_expected_less_than_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            # end - start < 300 → expected = 0
            rc, result = _run_verdict(out_dir, start=0.0, end=100.0)
            self.assertEqual(rc, 1)


class TestVerdictDoesNotWriteFiles(unittest.TestCase):
    """Verdict mode writes NO files and creates NO directories."""
    def test_verdict_no_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            # Write some existing content
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path, "w") as fh:
                fh.write(json.dumps(_make_obs_record(1.0)) + "\n")

            before_files = set(os.listdir(out_dir))
            before_hashes = {}
            for f in before_files:
                fp = os.path.join(out_dir, f)
                with open(fp, "rb") as fh:
                    before_hashes[f] = hashlib.sha256(fh.read()).hexdigest()

            # Run verdict with period that includes the record
            _run_verdict(out_dir, start=0.0, end=300.0 * 1000)

            after_files = set(os.listdir(out_dir))
            self.assertEqual(before_files, after_files,
                             f"Verdict mode created files: {after_files - before_files}")
            for f in before_files:
                fp = os.path.join(out_dir, f)
                with open(fp, "rb") as fh:
                    h = hashlib.sha256(fh.read()).hexdigest()
                self.assertEqual(h, before_hashes[f],
                                 f"Verdict mode modified {f}")


class TestVerdictAllPassGivesPass(unittest.TestCase):
    """All three checks pass → verdict PASS."""
    def test_all_pass_verdict_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 100
            end = start + expected * 300
            now = end + 100
            obs_path = os.path.join(out_dir, "observations.jsonl")
            records = [_make_obs_record(start + i * 300 + 1) for i in range(100)]
            _write_obs_records(obs_path, records)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            fs = start + 100
            anomaly = {
                "ts": start + 600, "type": "DEAD_PID", "item": "C-0001",
                "detail": "", "first_seen": fs,
                "cleared_at": fs + 500,  # 500s < 900 threshold
                "seconds_open": 500,
            }
            with open(anomalies_path, "w") as fh:
                fh.write(json.dumps(anomaly) + "\n")
            rc, result = _run_verdict(out_dir, start, end, now=now)
            self.assertEqual(result["verdict"], "PASS", f"Expected PASS: {result}")
            self.assertEqual(rc, 0)


# ---------------------------------------------------------------------------
# Static analysis tests
# ---------------------------------------------------------------------------

class TestStaticChecks(unittest.TestCase):
    """AST-based checks on workflow_observer.py source code."""
    def _get_source_ast(self):
        script_path = os.path.join(_SCRIPTS_DIR, "workflow_observer.py")
        with open(script_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        return ast.parse(source), source

    def test_no_forbidden_imports(self):
        tree, _ = self._get_source_ast()
        forbidden = {
            "socket", "subprocess", "urllib", "http", "ssl",
            "ctypes", "multiprocessing", "requests",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        alias.name.split(".")[0], forbidden,
                        f"Forbidden import found: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertNotIn(
                        node.module.split(".")[0], forbidden,
                        f"Forbidden from-import found: {node.module}"
                    )

    def test_no_forbidden_os_calls(self):
        tree, _ = self._get_source_ast()
        forbidden_attrs = {"system", "popen"}
        forbidden_prefixes = ("exec", "spawn")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if (isinstance(node.value, ast.Name)
                        and node.value.id == "os"):
                    attr = node.attr
                    self.assertNotIn(attr, forbidden_attrs,
                                     f"Forbidden os.{attr} call found")
                    for pfx in forbidden_prefixes:
                        self.assertFalse(
                            attr.startswith(pfx),
                            f"Forbidden os.{attr} call found"
                        )

    def test_ledger_only_opened_rb(self):
        """Check that ledger is only opened with 'rb' mode."""
        _, source = self._get_source_ast()
        # Look for open(...) calls that might open ledger in write/append modes
        tree = ast.parse(source)
        bad_modes = {"w", "a", "r+", "x", "wb", "ab", "w+", "a+"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # open(something, mode)
                if isinstance(func, ast.Name) and func.id == "open":
                    if len(node.args) >= 2:
                        mode_arg = node.args[1]
                        if isinstance(mode_arg, ast.Constant):
                            if mode_arg.value in bad_modes:
                                # Make sure this isn't for ledger reading
                                # We need to ensure no 'rb' alternative is used for writing
                                # The simplest check: no open() with write modes at all
                                # (product code only reads with 'rb')
                                self.fail(
                                    f"Found open() with potentially write mode '{mode_arg.value}' "
                                    f"at line {node.lineno}"
                                )


# ---------------------------------------------------------------------------
# Integration test with fixtures
# ---------------------------------------------------------------------------

class TestIntegrationWithFixtures(unittest.TestCase):
    """Integration test using real fixture files."""
    def test_fixtures_run_no_crash(self):
        _FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures")
        ledger_fixture = os.path.join(_FIXTURES, "ledger.sample.json")
        jobs_fixture = os.path.join(_FIXTURES, "jobs.sample.json")
        if not os.path.exists(ledger_fixture) or not os.path.exists(jobs_fixture):
            self.skipTest("Fixture files not found")

        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            os.makedirs(os.path.join(home, "commitments"))
            os.makedirs(os.path.join(home, "cron"))

            shutil.copy(ledger_fixture, os.path.join(home, "commitments", "ledger.json"))
            shutil.copy(jobs_fixture, os.path.join(home, "cron", "jobs.json"))

            # Find max updated time in ledger
            with open(ledger_fixture, "rb") as fh:
                raw = fh.read()
            data = json.loads(raw)
            expected_sha = hashlib.sha256(raw).hexdigest()
            max_updated = max(
                (i.get("updated") or 0 for i in data["items"]),
                default=T_NOW
            )
            # Use max_updated + 1 hour as 'now'
            obs_now = max_updated + 3600

            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir,
                                       now=obs_now, proc_root=proc_root)

            # Should not crash (rc 0 or 2)
            self.assertIn(rc, (0, 2), f"Unexpected rc={rc}")

            # Check sha256 in observation record
            obs_path = out_dir + "/observations.jsonl"
            self.assertTrue(os.path.exists(obs_path))
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertEqual(obs.get("ledger_sha256"), expected_sha,
                             "ledger_sha256 mismatch")


# ---------------------------------------------------------------------------
# D1: Malformed Ledger and Job Exception Tests
# ---------------------------------------------------------------------------

class TestD1MalformedData(unittest.TestCase):
    """
    Test that unexpected exceptions during calculation/preparation
    record OBSERVER_ERROR in observations.jsonl, leave violations.json unchanged,
    do not write anomalies.jsonl, produce empty stdout, and exit 1.
    """
    def _check_d1_malformed(self, items, jobs=None, extra_setup=None):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            if extra_setup:
                extra_setup(proc_root)
            home = _make_profile(tmp + "/home", items, jobs=jobs)
            ledger_path = os.path.join(home, "commitments", "ledger.json")
            with open(ledger_path, "rb") as fh:
                expected_sha = hashlib.sha256(fh.read()).hexdigest()

            out_dir = tmp + "/out"
            os.makedirs(out_dir)

            # Pre-populate violations.json to verify bytes unchanged
            viols_path = os.path.join(out_dir, "violations.json")
            initial_viols_bytes = b'{"updated": 1000, "violations": []}\n'
            with open(viols_path, "wb") as fh:
                fh.write(initial_viols_bytes)

            # Pre-populate observations.jsonl with 1 prior line to verify exactly 1 more line
            obs_path = os.path.join(out_dir, "observations.jsonl")
            prior_obs = _make_obs_record(T_NOW - 300)
            _write_obs_records(obs_path, [prior_obs])

            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)

            self.assertEqual(rc, 1, f"Expected rc=1, got {rc}")
            self.assertEqual(stdout, "", f"Expected empty stdout, got {stdout!r}")

            # Check violations.json bytes unchanged
            with open(viols_path, "rb") as fh:
                after_viols_bytes = fh.read()
            self.assertEqual(after_viols_bytes, initial_viols_bytes, "violations.json bytes changed!")

            # Check observations.jsonl has exactly 1 more line (total 2 lines)
            with open(obs_path, "r", encoding="utf-8") as fh:
                lines = [l.strip() for l in fh if l.strip()]
            self.assertEqual(len(lines), 2, f"Expected 2 lines in observations.jsonl, got {len(lines)}")
            rec = json.loads(lines[-1])
            self.assertEqual(rec.get("type"), "OBSERVER_ERROR")
            self.assertEqual(rec.get("ledger_sha256"), expected_sha)
            self.assertIn("error", rec)
            self.assertIn("exc_type", rec)

            # Check anomalies.jsonl was NOT created
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            self.assertFalse(os.path.exists(anomalies_path), "anomalies.jsonl should not be written")

    def test_d1_item_missing_id(self):
        """1. Item missing 'id' field in ledger."""
        items = [{"state": "OPEN", "text": "missing id"}]
        self._check_d1_malformed(items)

    def test_d1_dispatched_handle_string(self):
        """2. DISPATCHED item handle is string rather than dict."""
        items = [_make_item(id="C-0001", state="DISPATCHED", handle="string-handle")]
        self._check_d1_malformed(items)

    def test_d1_blocked_by_list(self):
        """3. blocked_by is list rather than string."""
        items = [_make_item(id="C-0001", state="OPEN", blocked_by=["item:C-0002"])]
        self._check_d1_malformed(items)

    def test_d1_target_missing_state(self):
        """4. item:ID target missing 'state' field."""
        target = {"id": "C-0002", "text": "target without state"}
        blocker = _make_item(id="C-0001", state="OPEN", blocked_by="item:C-0002")
        self._check_d1_malformed([target, blocker])

    def test_d1_cron_schedule_string(self):
        """5. Cron job schedule is string rather than dict."""
        job = _make_job("job1", schedule="every-10m")
        item = _make_item(id="C-0001", state="DISPATCHED", handle=_make_cron_handle("job1"))
        self._check_d1_malformed([item], jobs=[job])

    def test_d1_pid_start_not_int(self):
        """6. pid handle start is non-int string."""
        extra_setup = lambda proc_root: _make_fake_proc(proc_root, 1234, 9999)
        handle = {"kind": "pid", "pid": 1234, "start": "not-an-int", "ref": "1234"}
        item = _make_item(id="C-0001", state="DISPATCHED", handle=handle)
        self._check_d1_malformed([item], extra_setup=extra_setup)

    def test_d1_items_contains_non_dict(self):
        """7. Ledger items contains non-dict element."""
        items = [12345]
        self._check_d1_malformed(items)


# ---------------------------------------------------------------------------
# D2: Corrupt violations.json Tests
# ---------------------------------------------------------------------------

class TestD2CorruptViolationsJson(unittest.TestCase):
    """
    Test corrupt violations.json cases:
    - top-level list
    - violations field is not a list
    - violation element missing first_seen
    - first_seen is not a number
    All must be treated as corrupt: renamed to .corrupt-<epoch>, state_reset=True, treated as empty.
    """
    def _check_corrupt_violations(self, corrupt_content: str):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = _make_profile(tmp + "/home", [])
            out_dir = tmp + "/out"
            os.makedirs(out_dir)

            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                fh.write(corrupt_content)

            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "")

            # Check that violations.json.corrupt-<epoch> was created
            corrupt_files = [f for f in os.listdir(out_dir) if f.startswith("violations.json.corrupt-")]
            self.assertTrue(len(corrupt_files) > 0, f"Expected corrupt file, found {os.listdir(out_dir)}")

            # Check state_reset=True in observations.jsonl
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path, "r", encoding="utf-8") as fh:
                obs = json.loads(fh.readlines()[-1])
            self.assertTrue(obs.get("state_reset"), "Expected state_reset=True")

    def test_d2_top_level_list(self):
        self._check_corrupt_violations(json.dumps([{"type": "DEAD_PID", "item": "C-0001", "first_seen": 100}]))

    def test_d2_violations_not_list(self):
        self._check_corrupt_violations(json.dumps({"updated": 100, "violations": "not-a-list"}))

    def test_d2_element_missing_first_seen(self):
        self._check_corrupt_violations(json.dumps({"updated": 100, "violations": [{"type": "DEAD_PID", "item": "C-0001"}]}))

    def test_d2_first_seen_not_number(self):
        self._check_corrupt_violations(json.dumps({"updated": 100, "violations": [{"type": "DEAD_PID", "item": "C-0001", "first_seen": "not-a-number"}]}))

    def test_d2_first_seen_boolean(self):
        self._check_corrupt_violations(json.dumps({"updated": 100, "violations": [{"type": "DEAD_PID", "item": "C-0001", "first_seen": True}]}))


# ---------------------------------------------------------------------------
# D3: Boolean PID Handle Tests
# ---------------------------------------------------------------------------

class TestD3BoolPidHandle(unittest.TestCase):
    """
    Test that bool pid (True/False) is treated as non-integer:
    falls back to ref; if ref is invalid/missing, results in BAD_HANDLE.
    """
    def test_pid_bool_falls_back_to_valid_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tempfile.mkdtemp(dir=tmp)
            _make_fake_proc(proc_root, 1234, 5555)
            handle = {"kind": "pid", "pid": True, "ref": "1234", "start": 5555}
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout, "")

    def test_pid_bool_bad_ref_bad_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            handle = {"kind": "pid", "pid": False, "ref": "not-an-int"}
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("VIOLATION BAD_HANDLE", stdout)


# ---------------------------------------------------------------------------
# D4: Verdict Non-Dict Handling Tests
# ---------------------------------------------------------------------------

class TestD4VerdictNonDictLines(unittest.TestCase):
    """
    Test verdict handling of non-dict JSON lines in observations/anomalies/violations.
    """
    def test_verdict_observations_non_dict_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 5
            end = start + expected * 300
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(_make_obs_record(start + 1)) + "\n")
                fh.write("123\n")
                fh.write('["an", "array"]\n')
                fh.write(json.dumps(_make_obs_record(start + 301)) + "\n")
            rc, result = _run_verdict(out_dir, start, end)
            self.assertEqual(result["bad_lines"], 2)

    def test_verdict_anomalies_non_dict_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 5
            end = start + expected * 300
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write("456\n")
                fh.write('"just-a-string"\n')
            rc, result = _run_verdict(out_dir, start, end)
            self.assertEqual(result["bad_lines"], 2)

    def test_verdict_violations_non_dict_elements_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 10
            end = start + expected * 300
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": start + 100,
                    "violations": [
                        123,
                        "string",
                        {"type": "DEAD_PID", "item": "C-0001", "first_seen": start + 100}
                    ]
                }, fh)
            rc, result = _run_verdict(out_dir, start, end, now=start + 500)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            self.assertEqual(viol_items[0]["item"], "C-0001")

    def test_verdict_violations_top_level_list_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 0.0
            expected = 10
            end = start + expected * 300
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump([1, 2, 3], fh)
            rc, result = _run_verdict(out_dir, start, end)
            self.assertEqual(len(result["violations"]["items"]), 0)


# ---------------------------------------------------------------------------
# D5: Deterministic Sorted Stdout Order for New Violations
# ---------------------------------------------------------------------------

class TestD5SortedNewViolationsStdout(unittest.TestCase):
    """
    Test that multiple new violations are printed in strictly sorted order by (type, item).
    """
    def test_multiple_new_violations_stdout_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            # Create 4 violations in an unsorted initial arrangement
            # Item 1: DEAD_PID C-0003
            item3 = _make_item(id="C-0003", state="DISPATCHED", handle=_make_pid_handle(99999, start=1))
            # Item 2: CRON_MISSING C-0001
            item1 = _make_item(id="C-0001", state="DISPATCHED", handle=_make_cron_handle("missing_cron"))
            # Item 3: DEAD_PID C-0002
            item2 = _make_item(id="C-0002", state="DISPATCHED", handle=_make_pid_handle(88888, start=1))
            # Item 4: OVERDUE_MANUAL C-0004
            item4 = _make_item(id="C-0004", state="DISPATCHED", handle=_make_manual_handle(), due=T_NOW - 100)

            home = _make_profile(tmp + "/home", [item3, item1, item2, item4])
            out_dir = tmp + "/out"

            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)

            lines = stdout.strip().splitlines()
            expected_lines = [
                "VIOLATION CRON_MISSING C-0001 0",
                "VIOLATION DEAD_PID C-0002 0",
                "VIOLATION DEAD_PID C-0003 0",
                "VIOLATION OVERDUE_MANUAL C-0004 0",
            ]
            self.assertEqual(lines, expected_lines, f"Stdout lines not sorted: {lines}")


# ---------------------------------------------------------------------------
# S1 Mutation Tests: fsync Inode Tracking
# ---------------------------------------------------------------------------

class TestS1FsyncInodes(unittest.TestCase):
    """
    S1 Mutation: Omission of os.fsync(fd) after os.write in _append_jsonl.
    Verifies that fsync is explicitly called on observations.jsonl, anomalies.jsonl,
    violations.json tmp file (before replace), and the output directory itself,
    as well as observations.jsonl on the OBSERVER_ERROR path.
    """
    def test_s1_fsync_inodes_two_round_lifecycle(self):
        fsynced_inodes = set()
        replaced_tmp_inodes = []
        orig_fsync = os.fsync
        orig_replace = os.replace

        def recording_fsync(fd):
            try:
                st = os.fstat(fd)
                fsynced_inodes.add(st.st_ino)
            except OSError:
                pass
            return orig_fsync(fd)

        def recording_replace(src, dst):
            try:
                st = os.stat(src)
                replaced_tmp_inodes.append((st.st_ino, src, dst))
            except OSError:
                pass
            return orig_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            proc_root = os.path.join(tmp, "proc")
            os.makedirs(proc_root, exist_ok=True)
            home = os.path.join(tmp, "home")
            out_dir = os.path.join(tmp, "out")

            # Round 1: Item with DEAD_PID violation -> produces observations.jsonl and violations.json
            item = _make_item(
                id="C-0001",
                state="DISPATCHED",
                handle=_make_pid_handle(9999999, start=1234),
            )
            _make_profile(home, [item])

            with patch("os.fsync", side_effect=recording_fsync), \
                 patch("os.replace", side_effect=recording_replace):
                rc1 = wo.main([
                    "--home", home,
                    "--out-dir", out_dir,
                    "--now", str(T_NOW),
                    "--proc-root", proc_root,
                ])
                self.assertEqual(rc1, 0, f"Round 1 should exit with 0 (violations use stdout/files not rc), got {rc1}")

                # Round 2: Clear violation -> produces anomalies.jsonl, updates observations.jsonl & violations.json
                item["state"] = "VERIFIED"
                _write_ledger(home, [item])

                rc2 = wo.main([
                    "--home", home,
                    "--out-dir", out_dir,
                    "--now", str(T_NOW + 100),
                    "--proc-root", proc_root,
                ])
                self.assertEqual(rc2, 0, f"Round 2 should exit with 0 (clean), got {rc2}")

            obs_path = os.path.join(out_dir, "observations.jsonl")
            anom_path = os.path.join(out_dir, "anomalies.jsonl")
            viol_path = os.path.join(out_dir, "violations.json")

            self.assertTrue(os.path.exists(obs_path), "observations.jsonl must exist")
            self.assertTrue(os.path.exists(anom_path), "anomalies.jsonl must exist")
            self.assertTrue(os.path.exists(viol_path), "violations.json must exist")
            self.assertTrue(os.path.isdir(out_dir), "output directory must exist")

            obs_ino = os.stat(obs_path).st_ino
            anom_ino = os.stat(anom_path).st_ino
            viol_ino = os.stat(viol_path).st_ino
            dir_ino = os.stat(out_dir).st_ino

            # Verify tmp file inode matches final violations.json inode
            self.assertTrue(
                len(replaced_tmp_inodes) >= 1,
                "os.replace must be called at least once for atomic violations.json write",
            )
            last_tmp_ino = replaced_tmp_inodes[-1][0]
            self.assertEqual(
                viol_ino,
                last_tmp_ino,
                f"violations.json inode ({viol_ino}) should equal the fsynced tmp file inode before os.replace ({last_tmp_ino})",
            )

            # Assert each inode is in fsynced_inodes independently with clear error message
            self.assertIn(
                obs_ino,
                fsynced_inodes,
                f"observations.jsonl inode ({obs_ino}) was not fsynced via os.fsync(fd) after os.write in _append_jsonl",
            )
            self.assertIn(
                anom_ino,
                fsynced_inodes,
                f"anomalies.jsonl inode ({anom_ino}) was not fsynced via os.fsync(fd) after os.write in _append_jsonl",
            )
            self.assertIn(
                last_tmp_ino,
                fsynced_inodes,
                f"violations.json tmp file inode ({last_tmp_ino}) was not fsynced before os.replace",
            )
            self.assertIn(
                viol_ino,
                fsynced_inodes,
                f"final violations.json inode ({viol_ino}) was not in fsynced inodes set",
            )
            self.assertIn(
                dir_ino,
                fsynced_inodes,
                f"output directory inode ({dir_ino}) was not fsynced via _fsync_dir",
            )

    def test_s1_fsync_observations_on_corrupt_ledger_error(self):
        fsynced_inodes = set()
        orig_fsync = os.fsync

        def recording_fsync(fd):
            try:
                st = os.fstat(fd)
                fsynced_inodes.add(st.st_ino)
            except OSError:
                pass
            return orig_fsync(fd)

        with tempfile.TemporaryDirectory() as tmp:
            proc_root = os.path.join(tmp, "proc")
            os.makedirs(proc_root, exist_ok=True)
            home = os.path.join(tmp, "home")
            commitments = os.path.join(home, "commitments")
            cron_dir = os.path.join(home, "cron")
            os.makedirs(commitments, exist_ok=True)
            os.makedirs(cron_dir, exist_ok=True)
            _write_jobs(home, [])

            # Write corrupt ledger
            ledger_path = os.path.join(commitments, "ledger.json")
            with open(ledger_path, "wb") as fh:
                fh.write(b"{{invalid json syntax")

            out_dir = os.path.join(tmp, "out")

            with patch("os.fsync", side_effect=recording_fsync), \
                 patch.object(wo, "READ_RETRY_DELAY", 0):
                rc = wo.main([
                    "--home", home,
                    "--out-dir", out_dir,
                    "--now", str(T_NOW),
                    "--proc-root", proc_root,
                ])
                self.assertEqual(rc, 1, f"Expected rc 1 on corrupt ledger, got {rc}")

            obs_path = os.path.join(out_dir, "observations.jsonl")
            self.assertTrue(os.path.exists(obs_path), "observations.jsonl must exist on OBSERVER_ERROR")
            obs_ino = os.stat(obs_path).st_ino

            self.assertIn(
                obs_ino,
                fsynced_inodes,
                f"observations.jsonl inode ({obs_ino}) was not fsynced when recording OBSERVER_ERROR in _append_jsonl",
            )


# ---------------------------------------------------------------------------
# S2 Mutation Tests: Ledger Open Mode
# ---------------------------------------------------------------------------

class TestS2LedgerOpenMode(unittest.TestCase):
    """
    S2 Mutation: _read_ledger opened with 'r+b'.
    Verifies dynamically (via builtins.open / os.open hooks in main) and
    statically (via ast traversal) that only 'rb' mode is used.
    """
    def _run_with_open_recording(self, corrupt_ledger: bool = False):
        import builtins
        ledger_builtin_modes = []
        ledger_os_open_flags = []
        orig_builtin_open = builtins.open
        orig_os_open = os.open

        with tempfile.TemporaryDirectory() as tmp:
            proc_root = os.path.join(tmp, "proc")
            os.makedirs(proc_root, exist_ok=True)
            home = os.path.join(tmp, "home")
            commitments = os.path.join(home, "commitments")
            cron_dir = os.path.join(home, "cron")
            os.makedirs(commitments, exist_ok=True)
            os.makedirs(cron_dir, exist_ok=True)
            _write_jobs(home, [])

            ledger_path = os.path.join(commitments, "ledger.json")
            if corrupt_ledger:
                with open(ledger_path, "wb") as fh:
                    fh.write(b"{{corrupted json syntax")
            else:
                _write_ledger(home, [])

            ledger_real = os.path.realpath(ledger_path)
            out_dir = os.path.join(tmp, "out")

            def recording_open(file, *args, **kwargs):
                try:
                    if isinstance(file, (str, bytes, os.PathLike)):
                        if os.path.realpath(file) == ledger_real:
                            if "mode" in kwargs:
                                mode = kwargs["mode"]
                            elif len(args) > 0:
                                mode = args[0]
                            else:
                                mode = "r"
                            ledger_builtin_modes.append(mode)
                except Exception:
                    pass
                return orig_builtin_open(file, *args, **kwargs)

            def recording_os_open(path, *args, **kwargs):
                try:
                    if isinstance(path, (str, bytes, os.PathLike)):
                        if os.path.realpath(path) == ledger_real:
                            if "flags" in kwargs:
                                flags = kwargs["flags"]
                            elif len(args) > 0:
                                flags = args[0]
                            else:
                                flags = 0
                            ledger_os_open_flags.append(flags)
                except Exception:
                    pass
                return orig_os_open(path, *args, **kwargs)

            with patch("builtins.open", side_effect=recording_open), \
                 patch("os.open", side_effect=recording_os_open), \
                 patch.object(wo, "READ_RETRY_DELAY", 0):
                rc = wo.main([
                    "--home", home,
                    "--out-dir", out_dir,
                    "--now", str(T_NOW),
                    "--proc-root", proc_root,
                ])

            return rc, ledger_builtin_modes, ledger_os_open_flags

    def test_s2_ledger_open_mode_dynamic_normal(self):
        rc, builtin_modes, os_flags = self._run_with_open_recording(corrupt_ledger=False)
        self.assertEqual(rc, 0, f"Expected rc 0, got {rc}")

        total_ledger_opens = len(builtin_modes) + len(os_flags)
        self.assertGreaterEqual(
            total_ledger_opens, 1,
            "At least one read of ledger.json must be recorded (prevent no-op pass)",
        )
        for idx, mode in enumerate(builtin_modes):
            self.assertEqual(
                mode, "rb",
                f"builtins.open() call #{idx+1} on ledger used mode {mode!r}, expected strictly 'rb'",
            )
        for idx, flags in enumerate(os_flags):
            forbidden = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
            self.assertEqual(
                flags & forbidden, 0,
                f"os.open() call #{idx+1} on ledger used non-read-only flags {flags:#x}",
            )

    def test_s2_ledger_open_mode_dynamic_corrupt_error_path(self):
        rc, builtin_modes, os_flags = self._run_with_open_recording(corrupt_ledger=True)
        self.assertEqual(rc, 1, f"Expected rc 1 on corrupt ledger, got {rc}")

        total_ledger_opens = len(builtin_modes) + len(os_flags)
        self.assertGreaterEqual(
            total_ledger_opens, 1,
            "At least one read of ledger.json must be recorded on corrupt error path",
        )
        for idx, mode in enumerate(builtin_modes):
            self.assertEqual(
                mode, "rb",
                f"builtins.open() call #{idx+1} on ledger during error recovery used mode {mode!r}, expected strictly 'rb'",
            )
        for idx, flags in enumerate(os_flags):
            forbidden = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
            self.assertEqual(
                flags & forbidden, 0,
                f"os.open() call #{idx+1} on ledger during error recovery used non-read-only flags {flags:#x}",
            )

    def test_s2_static_all_builtin_open_calls_mode_rb(self):
        script_path = os.path.join(_SCRIPTS_DIR, "workflow_observer.py")
        with open(script_path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)

        open_calls = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                is_builtin_open = False
                if isinstance(func, ast.Name) and func.id == "open":
                    is_builtin_open = True
                elif (isinstance(func, ast.Attribute) and func.attr == "open" and
                      isinstance(func.value, ast.Name) and func.value.id == "builtins"):
                    is_builtin_open = True

                if is_builtin_open:
                    open_calls += 1
                    mode_val = None
                    if len(node.args) >= 2:
                        arg1 = node.args[1]
                        if isinstance(arg1, ast.Constant) and isinstance(arg1.value, str):
                            mode_val = arg1.value
                        else:
                            self.fail(
                                f"scripts/workflow_observer.py line {node.lineno}: open() mode argument "
                                f"is not a string constant ({ast.dump(arg1)})"
                            )
                    elif any(kw.arg == "mode" for kw in node.keywords):
                        kw_val = next(kw.value for kw in node.keywords if kw.arg == "mode")
                        if isinstance(kw_val, ast.Constant) and isinstance(kw_val.value, str):
                            mode_val = kw_val.value
                        else:
                            self.fail(
                                f"scripts/workflow_observer.py line {node.lineno}: open() mode keyword argument "
                                f"is not a string constant ({ast.dump(kw_val)})"
                            )
                    else:
                        self.fail(
                            f"scripts/workflow_observer.py line {node.lineno}: open() call missing mode argument "
                            f"(defaults to text 'r'; product code must strictly read bytes with 'rb')"
                        )

                    # Any open() mode containing w/a/x/+ must fail
                    for forbidden in ("w", "a", "x", "+"):
                        self.assertNotIn(
                            forbidden, mode_val,
                            f"scripts/workflow_observer.py line {node.lineno}: open() mode {mode_val!r} "
                            f"contains forbidden modification flag '{forbidden}'",
                        )
                    # Must strictly be "rb"
                    self.assertEqual(
                        mode_val, "rb",
                        f"scripts/workflow_observer.py line {node.lineno}: open() mode is {mode_val!r}, expected strictly 'rb'",
                    )

        self.assertGreaterEqual(open_calls, 1, "Expected to find at least one open() call in scripts/workflow_observer.py")


# ---------------------------------------------------------------------------
# S3 Mutation Tests: Torn Read Retries
# ---------------------------------------------------------------------------

class TestS3TornReadRetries(unittest.TestCase):
    """
    S3 Mutation: READ_RETRIES = 1 (disables retry on torn reads).
    Verifies that READ_RETRIES >= 3, that transient failures up to READ_RETRIES - 1
    are retried and eventually succeed with rc 0, and that persistent failures
    are retried exactly READ_RETRIES times before failing with rc 1.
    """
    def test_s3_c_read_retries_at_least_three(self):
        self.assertGreaterEqual(
            wo.READ_RETRIES, 3,
            f"workflow_observer.READ_RETRIES is {wo.READ_RETRIES}, must be >= 3 per spec requirement",
        )

    def test_s3_a_torn_read_retries_eventual_success(self):
        self.assertGreaterEqual(
            wo.READ_RETRIES, 3,
            f"READ_RETRIES must be >= 3, found {wo.READ_RETRIES}",
        )
        call_count = [0]
        orig_read = wo._read_ledger
        expected_retries = wo.READ_RETRIES

        def fake_read_ledger(path):
            call_count[0] += 1
            if call_count[0] < expected_retries:
                raise ValueError("torn")
            return orig_read(path)

        with tempfile.TemporaryDirectory() as tmp:
            proc_root = os.path.join(tmp, "proc")
            os.makedirs(proc_root, exist_ok=True)
            home = _make_profile(os.path.join(tmp, "home"), [])
            out_dir = os.path.join(tmp, "out")

            with patch.object(wo, "READ_RETRY_DELAY", 0), \
                 patch.object(wo, "_read_ledger", side_effect=fake_read_ledger):
                rc = wo.main([
                    "--home", home,
                    "--out-dir", out_dir,
                    "--now", str(T_NOW),
                    "--proc-root", proc_root,
                ])

            self.assertEqual(
                rc, 0,
                f"Expected rc 0 when retries eventually succeed on attempt {expected_retries}, got {rc}",
            )
            self.assertEqual(
                call_count[0], expected_retries,
                f"Expected fake _read_ledger to be called exactly READ_RETRIES ({expected_retries}) times, got {call_count[0]}",
            )

            obs_path = os.path.join(out_dir, "observations.jsonl")
            self.assertTrue(os.path.exists(obs_path), "observations.jsonl must exist")
            with open(obs_path, "r", encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]

            self.assertEqual(
                len(lines), 1,
                f"observations.jsonl should have exactly 1 record, found {len(lines)}",
            )
            self.assertEqual(
                lines[0].get("type"), "OBSERVATION",
                f"Expected record type 'OBSERVATION', got {lines[0].get('type')!r}",
            )

    def test_s3_b_torn_read_retries_all_fail(self):
        self.assertGreaterEqual(
            wo.READ_RETRIES, 3,
            f"READ_RETRIES must be >= 3, found {wo.READ_RETRIES}",
        )
        call_count = [0]
        expected_retries = wo.READ_RETRIES

        def fake_read_ledger_always_fail(path):
            call_count[0] += 1
            raise ValueError("torn")

        with tempfile.TemporaryDirectory() as tmp:
            proc_root = os.path.join(tmp, "proc")
            os.makedirs(proc_root, exist_ok=True)
            home = _make_profile(os.path.join(tmp, "home"), [])
            out_dir = os.path.join(tmp, "out")

            with patch.object(wo, "READ_RETRY_DELAY", 0), \
                 patch.object(wo, "_read_ledger", side_effect=fake_read_ledger_always_fail):
                rc = wo.main([
                    "--home", home,
                    "--out-dir", out_dir,
                    "--now", str(T_NOW),
                    "--proc-root", proc_root,
                ])

            self.assertEqual(
                rc, 1,
                f"Expected rc 1 when all retries fail, got {rc}",
            )
            self.assertEqual(
                call_count[0], expected_retries,
                f"Expected fake _read_ledger to be called exactly READ_RETRIES ({expected_retries}) times, got {call_count[0]}",
            )

            obs_path = os.path.join(out_dir, "observations.jsonl")
            self.assertTrue(os.path.exists(obs_path), "observations.jsonl must exist")
            with open(obs_path, "r", encoding="utf-8") as fh:
                lines = [json.loads(line) for line in fh if line.strip()]

            self.assertEqual(
                len(lines), 1,
                f"observations.jsonl should have exactly 1 record, found {len(lines)}",
            )
            self.assertEqual(
                lines[0].get("type"), "OBSERVER_ERROR",
                f"Expected record type 'OBSERVER_ERROR', got {lines[0].get('type')!r}",
            )


# ---------------------------------------------------------------------------
# S4 Gap Tests: Cron Schedule Intervals and Stale Thresholds
# ---------------------------------------------------------------------------

def _iso_plus_0200(epoch: float) -> str:
    """Format epoch timestamp as ISO-8601 string with +02:00 timezone offset."""
    tz = datetime.timezone(datetime.timedelta(hours=2))
    dt = datetime.datetime.fromtimestamp(epoch, tz=tz)
    return dt.isoformat()


class TestCronScheduleIntervalsAndStaleThresholds(unittest.TestCase):
    """
    Direct unit assertions on _cron_interval_seconds and e2e assertions with wo.main([...]).
    Verifies schedule intervals, stale thresholds (> 3x interval), non-5-field expr handling,
    kind=interval, unknown schedule kinds, and precedence rules.
    All e2e assertions inject --now and use ISO-8601 timestamps containing +02:00 offset.
    """

    def _run_e2e_cron_check(
        self, schedule: dict, last_run_at: str | None, now: float
    ) -> tuple[int, str, dict]:
        """Run main([...]) e2e in an isolated temporary directory and return (rc, stdout, obs_record)."""
        if last_run_at is not None:
            self.assertIn(
                "+02:00", last_run_at,
                f"job last_run_at must contain '+02:00', got {last_run_at}"
            )

        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            out_dir = os.path.join(tmp, "out")
            proc_root = os.path.join(tmp, "proc")
            os.makedirs(proc_root, exist_ok=True)
            commitments_dir = os.path.join(home, "commitments")
            cron_dir = os.path.join(home, "cron")
            os.makedirs(commitments_dir, exist_ok=True)
            os.makedirs(cron_dir, exist_ok=True)

            item = {
                "id": "C-CRON-TEST",
                "text": "cron e2e check",
                "created": now,
                "updated": now,
                "state": "DISPATCHED",
                "handle": {"kind": "cron", "ref": "test_job"},
                "blocked_by": None,
                "due": None,
                "reason": None,
                "defer": None,
                "history": [],
            }
            ledger_path = os.path.join(commitments_dir, "ledger.json")
            with open(ledger_path, "wb") as fh:
                fh.write(json.dumps({"next": 2, "items": [item]}, ensure_ascii=False).encode("utf-8"))

            job = {
                "id": "test_job",
                "name": "test_job",
                "enabled": True,
                "state": "scheduled",
                "last_run_at": last_run_at,
                "last_status": "ok",
                "schedule": schedule,
            }
            jobs_path = os.path.join(cron_dir, "jobs.json")
            with open(jobs_path, "wb") as fh:
                fh.write(json.dumps({"jobs": [job], "updated_at": _iso_plus_0200(now)}, ensure_ascii=False).encode("utf-8"))

            import io
            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            try:
                rc = wo.main([
                    "--home", home,
                    "--out-dir", out_dir,
                    "--now", str(now),
                    "--proc-root", proc_root,
                ])
            finally:
                captured = sys.stdout.getvalue()
                sys.stdout = old_stdout

            obs_path = os.path.join(out_dir, "observations.jsonl")
            obs_record = {}
            if os.path.exists(obs_path):
                with open(obs_path, "r", encoding="utf-8") as fh:
                    lines = [json.loads(line) for line in fh if line.strip()]
                    if lines:
                        obs_record = lines[-1]

            return rc, captured, obs_record

    def test_1_cron_daily_hour_specified_unit_and_e2e(self):
        """
        (1) schedule {"kind":"cron","expr":"0 21 * * *"} → interval 86400;
        last_run 14h, 71h ago → no CRON_STALE;
        exactly 72h ago → no CRON_STALE (> 3x required);
        72h and 1s ago → CRON_STALE.
        """
        schedule = {"kind": "cron", "expr": "0 21 * * *"}
        # Unit assertion
        interval = wo._cron_interval_seconds(schedule)
        self.assertEqual(
            interval, 86400.0,
            f"(1) Unit assertion failed: expected interval 86400.0 for daily schedule {schedule}, got {interval}"
        )

        now = T_NOW

        # E2E 14 hours ago: age = 14*3600 <= 3*86400 (72h) -> no CRON_STALE
        last_run_14h = _iso_plus_0200(now - 14 * 3600.0)
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_14h, now)
        self.assertEqual(
            rc, 0,
            f"(1) E2E failed: daily cron with last_run 14h ago must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(1) E2E failed: daily cron with last_run 14h ago must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(1) E2E failed: daily cron with last_run 14h ago must not have CRON_STALE violation in observations: {obs.get('violations')}"
        )

        # E2E 71 hours ago: age = 71*3600 <= 3*86400 (72h) -> no CRON_STALE
        last_run_71h = _iso_plus_0200(now - 71 * 3600.0)
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_71h, now)
        self.assertEqual(
            rc, 0,
            f"(1) E2E failed: daily cron with last_run 71h ago must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(1) E2E failed: daily cron with last_run 71h ago must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(1) E2E failed: daily cron with last_run 71h ago must not have CRON_STALE violation in observations: {obs.get('violations')}"
        )

        # E2E exactly 72 hours ago: age == 72*3600 == 3*interval, not strictly > -> no CRON_STALE
        last_run_72h = _iso_plus_0200(now - 72 * 3600.0)
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_72h, now)
        self.assertEqual(
            rc, 0,
            f"(1) E2E failed: daily cron with last_run exactly 72h ago must exit 0 (> strictly required), got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(1) E2E failed: daily cron with last_run exactly 72h ago must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(1) E2E failed: daily cron with last_run exactly 72h ago must not have CRON_STALE violation in observations: {obs.get('violations')}"
        )

        # E2E 72 hours and 1 second ago: age = 72*3600 + 1 > 3*interval -> CRON_STALE
        last_run_72h1s = _iso_plus_0200(now - (72 * 3600.0 + 1.0))
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_72h1s, now)
        self.assertEqual(
            rc, 0,
            f"(1) E2E failed: daily cron with last_run 72h+1s ago must exit 0 (violation signals via stdout/files), got rc={rc}, stdout={stdout}"
        )
        self.assertIn(
            "CRON_STALE", stdout,
            f"(1) E2E failed: daily cron with last_run 72h+1s ago must output CRON_STALE in stdout: {stdout}"
        )
        self.assertTrue(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(1) E2E failed: daily cron with last_run 72h+1s ago must record CRON_STALE in observations: {obs.get('violations')}"
        )

    def test_2_cron_weekly_dow_specified_unit_and_e2e(self):
        """
        (2) dow non-'*' ("0 20 * * 0") → interval 604800 (7*86400);
        last_run 20 days ago → no stale;
        21 days and 1s ago → stale.
        """
        schedule = {"kind": "cron", "expr": "0 20 * * 0"}
        # Unit assertion
        interval = wo._cron_interval_seconds(schedule)
        self.assertEqual(
            interval, 604800.0,
            f"(2) Unit assertion failed: expected interval 604800.0 (7*86400) for weekly dow schedule {schedule}, got {interval}"
        )

        now = T_NOW

        # E2E 20 days ago: age = 20*86400 < 3*7*86400 (21 days) -> no CRON_STALE
        last_run_20d = _iso_plus_0200(now - 20 * 86400.0)
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_20d, now)
        self.assertEqual(
            rc, 0,
            f"(2) E2E failed: weekly cron with last_run 20d ago must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(2) E2E failed: weekly cron with last_run 20d ago must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(2) E2E failed: weekly cron with last_run 20d ago must not have CRON_STALE in observations: {obs.get('violations')}"
        )

        # E2E 21 days and 1 second ago: age = 21*86400 + 1 > 3*7*86400 -> CRON_STALE
        last_run_21d1s = _iso_plus_0200(now - (21 * 86400.0 + 1.0))
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_21d1s, now)
        self.assertEqual(
            rc, 0,
            f"(2) E2E failed: weekly cron with last_run 21d+1s ago must exit 0 (violations via stdout/files), got rc={rc}, stdout={stdout}"
        )
        self.assertIn(
            "CRON_STALE", stdout,
            f"(2) E2E failed: weekly cron with last_run 21d+1s ago must output CRON_STALE: {stdout}"
        )
        self.assertTrue(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(2) E2E failed: weekly cron with last_run 21d+1s ago must record CRON_STALE in observations: {obs.get('violations')}"
        )

    def test_3_cron_monthly_dom_specified_unit_and_e2e(self):
        """
        (3) dom non-'*' ("0 3 1 * *") → interval 31*86400;
        last_run 90 days ago → no stale;
        93 days and 1s ago → stale.
        """
        schedule = {"kind": "cron", "expr": "0 3 1 * *"}
        # Unit assertion
        interval = wo._cron_interval_seconds(schedule)
        self.assertEqual(
            interval, 31 * 86400.0,
            f"(3) Unit assertion failed: expected interval {31 * 86400.0} (31*86400) for monthly dom schedule {schedule}, got {interval}"
        )

        now = T_NOW

        # E2E 90 days ago: age = 90*86400 < 3*31*86400 (93 days) -> no CRON_STALE
        last_run_90d = _iso_plus_0200(now - 90 * 86400.0)
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_90d, now)
        self.assertEqual(
            rc, 0,
            f"(3) E2E failed: monthly cron with last_run 90d ago must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(3) E2E failed: monthly cron with last_run 90d ago must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(3) E2E failed: monthly cron with last_run 90d ago must not have CRON_STALE in observations: {obs.get('violations')}"
        )

        # E2E 93 days and 1 second ago: age = 93*86400 + 1 > 3*31*86400 -> CRON_STALE
        last_run_93d1s = _iso_plus_0200(now - (93 * 86400.0 + 1.0))
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_93d1s, now)
        self.assertEqual(
            rc, 0,
            f"(3) E2E failed: monthly cron with last_run 93d+1s ago must exit 0 (violations via stdout/files), got rc={rc}, stdout={stdout}"
        )
        self.assertIn(
            "CRON_STALE", stdout,
            f"(3) E2E failed: monthly cron with last_run 93d+1s ago must output CRON_STALE: {stdout}"
        )
        self.assertTrue(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(3) E2E failed: monthly cron with last_run 93d+1s ago must record CRON_STALE in observations: {obs.get('violations')}"
        )

    def test_4_cron_step_minutes_unit_and_e2e(self):
        """
        (4) minute step "*/15 * * * *" → interval 900;
        last_run 45 minutes ago → no stale;
        45 minutes and 1s ago → stale.
        """
        schedule = {"kind": "cron", "expr": "*/15 * * * *"}
        # Unit assertion
        interval = wo._cron_interval_seconds(schedule)
        self.assertEqual(
            interval, 900.0,
            f"(4) Unit assertion failed: expected interval 900.0 (15*60) for step minute schedule {schedule}, got {interval}"
        )

        now = T_NOW

        # E2E 45 minutes ago: age = 45*60 == 3*900 (not strictly >) -> no CRON_STALE
        last_run_45m = _iso_plus_0200(now - 45 * 60.0)
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_45m, now)
        self.assertEqual(
            rc, 0,
            f"(4) E2E failed: 15m step cron with last_run 45m ago must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(4) E2E failed: 15m step cron with last_run 45m ago must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(4) E2E failed: 15m step cron with last_run 45m ago must not have CRON_STALE in observations: {obs.get('violations')}"
        )

        # E2E 45 minutes and 1 second ago: age = 45*60 + 1 > 3*900 -> CRON_STALE
        last_run_45m1s = _iso_plus_0200(now - (45 * 60.0 + 1.0))
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_45m1s, now)
        self.assertEqual(
            rc, 0,
            f"(4) E2E failed: 15m step cron with last_run 45m+1s ago must exit 0 (violations via stdout/files), got rc={rc}, stdout={stdout}"
        )
        self.assertIn(
            "CRON_STALE", stdout,
            f"(4) E2E failed: 15m step cron with last_run 45m+1s ago must output CRON_STALE: {stdout}"
        )
        self.assertTrue(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(4) E2E failed: 15m step cron with last_run 45m+1s ago must record CRON_STALE in observations: {obs.get('violations')}"
        )

    def test_5_cron_hourly_unit_and_e2e(self):
        """
        (5) "0 * * * *" (hour is '*', minute is not '*/N') → interval 3600;
        last_run 3 hours ago → no stale;
        3 hours and 1s ago → stale.
        """
        schedule = {"kind": "cron", "expr": "0 * * * *"}
        # Unit assertion
        interval = wo._cron_interval_seconds(schedule)
        self.assertEqual(
            interval, 3600.0,
            f"(5) Unit assertion failed: expected interval 3600.0 (60*60) for hourly schedule {schedule}, got {interval}"
        )

        now = T_NOW

        # E2E 3 hours ago: age = 3*3600 == 3*interval (not strictly >) -> no CRON_STALE
        last_run_3h = _iso_plus_0200(now - 3 * 3600.0)
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_3h, now)
        self.assertEqual(
            rc, 0,
            f"(5) E2E failed: hourly cron with last_run 3h ago must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(5) E2E failed: hourly cron with last_run 3h ago must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(5) E2E failed: hourly cron with last_run 3h ago must not have CRON_STALE in observations: {obs.get('violations')}"
        )

        # E2E 3 hours and 1 second ago: age = 3*3600 + 1 > 3*3600 -> CRON_STALE
        last_run_3h1s = _iso_plus_0200(now - (3 * 3600.0 + 1.0))
        rc, stdout, obs = self._run_e2e_cron_check(schedule, last_run_3h1s, now)
        self.assertEqual(
            rc, 0,
            f"(5) E2E failed: hourly cron with last_run 3h+1s ago must exit 0 (violations via stdout/files), got rc={rc}, stdout={stdout}"
        )
        self.assertIn(
            "CRON_STALE", stdout,
            f"(5) E2E failed: hourly cron with last_run 3h+1s ago must output CRON_STALE: {stdout}"
        )
        self.assertTrue(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(5) E2E failed: hourly cron with last_run 3h+1s ago must record CRON_STALE in observations: {obs.get('violations')}"
        )

    def test_6_cron_fallback_invalid_and_kinds_unit_and_e2e(self):
        """
        (6)
        - "*/x * * * *" (non-integer after */) → 3600 (conservative default);
        - expr field count != 5 → None, no stale violation, info contains schedule_unknown;
        - kind=interval minutes=10 → 600, 30 min ago no stale, 30 min + 1s stale;
        - kind as other string → None.
        """
        now = T_NOW

        # 6a: "*/x * * * *" -> 3600
        sched_invalid_step = {"kind": "cron", "expr": "*/x * * * *"}
        interval_invalid_step = wo._cron_interval_seconds(sched_invalid_step)
        self.assertEqual(
            interval_invalid_step, 3600.0,
            f"(6a) Unit assertion failed: expected 3600.0 conservative fallback for invalid step {sched_invalid_step}, got {interval_invalid_step}"
        )
        # E2E 3h ago no stale
        rc, stdout, obs = self._run_e2e_cron_check(sched_invalid_step, _iso_plus_0200(now - 3 * 3600.0), now)
        self.assertEqual(
            rc, 0,
            f"(6a) E2E failed: '*/x * * * *' with last_run 3h ago must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(6a) E2E failed: '*/x * * * *' with last_run 3h ago must not output CRON_STALE: {stdout}"
        )
        # E2E 3h+1s ago stale
        rc, stdout, obs = self._run_e2e_cron_check(sched_invalid_step, _iso_plus_0200(now - (3 * 3600.0 + 1.0)), now)
        self.assertEqual(
            rc, 0,
            f"(6a) E2E failed: '*/x * * * *' with last_run 3h+1s ago must exit 0 (violations via stdout/files), got rc={rc}, stdout={stdout}"
        )
        self.assertIn(
            "CRON_STALE", stdout,
            f"(6a) E2E failed: '*/x * * * *' with last_run 3h+1s ago must output CRON_STALE: {stdout}"
        )

        # 6b: expr field count != 5 -> None and E2E: no stale, info has schedule_unknown
        for bad_expr in ("* * *", "0 21 * *", "0 21 * * * *", ""):
            sched_bad = {"kind": "cron", "expr": bad_expr}
            res = wo._cron_interval_seconds(sched_bad)
            self.assertIsNone(
                res,
                f"(6b) Unit assertion failed: _cron_interval_seconds({sched_bad}) expected None for non-5-field expr, got {res}"
            )

        # E2E with 4-field expr and last_run 100 days ago
        sched_4f = {"kind": "cron", "expr": "0 21 * *"}
        last_run_100d = _iso_plus_0200(now - 100 * 86400.0)
        rc, stdout, obs = self._run_e2e_cron_check(sched_4f, last_run_100d, now)
        self.assertEqual(
            rc, 0,
            f"(6b) E2E failed: non-5-field expr must exit 0 (no stale), got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(6b) E2E failed: non-5-field expr must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(6b) E2E failed: non-5-field expr must not have CRON_STALE in observations violations: {obs.get('violations')}"
        )
        info_types_4f = [i.get("type") for i in obs.get("info", [])]
        self.assertIn(
            "schedule_unknown", info_types_4f,
            f"(6b) E2E failed: non-5-field expr must record 'schedule_unknown' in observations.info, got {obs.get('info')}"
        )

        # 6c: kind=interval minutes=10 -> 600, 30 min ago no stale, 30 min + 1s stale
        sched_int10 = {"kind": "interval", "minutes": 10}
        interval_int10 = wo._cron_interval_seconds(sched_int10)
        self.assertEqual(
            interval_int10, 600.0,
            f"(6c) Unit assertion failed: _cron_interval_seconds({sched_int10}) expected 600.0 (10*60), got {interval_int10}"
        )
        # E2E 30 minutes ago: age = 30*60 == 3*600 (not strictly >) -> no CRON_STALE
        last_run_30m = _iso_plus_0200(now - 30 * 60.0)
        rc, stdout, obs = self._run_e2e_cron_check(sched_int10, last_run_30m, now)
        self.assertEqual(
            rc, 0,
            f"(6c) E2E failed: interval 10m with last_run 30m ago must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(6c) E2E failed: interval 10m with last_run 30m ago must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(6c) E2E failed: interval 10m with last_run 30m ago must not have CRON_STALE in observations: {obs.get('violations')}"
        )
        # E2E 30 minutes and 1 second ago: age = 30*60 + 1 > 3*600 -> CRON_STALE
        last_run_30m1s = _iso_plus_0200(now - (30 * 60.0 + 1.0))
        rc, stdout, obs = self._run_e2e_cron_check(sched_int10, last_run_30m1s, now)
        self.assertEqual(
            rc, 0,
            f"(6c) E2E failed: interval 10m with last_run 30m+1s ago must exit 0 (violations via stdout/files), got rc={rc}, stdout={stdout}"
        )
        self.assertIn(
            "CRON_STALE", stdout,
            f"(6c) E2E failed: interval 10m with last_run 30m+1s ago must output CRON_STALE: {stdout}"
        )
        self.assertTrue(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(6c) E2E failed: interval 10m with last_run 30m+1s ago must record CRON_STALE in observations: {obs.get('violations')}"
        )

        # 6d: kind as other string -> None and E2E: no stale, info has schedule_unknown
        for other_kind in ("custom", "random_string", "unsupported", ""):
            sched_other = {"kind": other_kind}
            res = wo._cron_interval_seconds(sched_other)
            self.assertIsNone(
                res,
                f"(6d) Unit assertion failed: _cron_interval_seconds({sched_other}) expected None for kind={other_kind!r}, got {res}"
            )
        self.assertIsNone(
            wo._cron_interval_seconds({}),
            "(6d) Unit assertion failed: _cron_interval_seconds({}) expected None for empty dict"
        )

        # E2E with other kind and last_run 100 days ago
        sched_custom = {"kind": "custom_job_type"}
        rc, stdout, obs = self._run_e2e_cron_check(sched_custom, last_run_100d, now)
        self.assertEqual(
            rc, 0,
            f"(6d) E2E failed: unknown kind must exit 0 (no stale), got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(6d) E2E failed: unknown kind must not output CRON_STALE: {stdout}"
        )
        self.assertFalse(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(6d) E2E failed: unknown kind must not have CRON_STALE in observations violations: {obs.get('violations')}"
        )
        info_types_custom = [i.get("type") for i in obs.get("info", [])]
        self.assertIn(
            "schedule_unknown", info_types_custom,
            f"(6d) E2E failed: unknown kind must record 'schedule_unknown' in observations.info, got {obs.get('info')}"
        )

    def test_7_cron_precedence_rules_unit_and_e2e(self):
        """
        (7) Precedence:
        - dow non-'*' while dom non-'*' → dow (604800) takes precedence;
        - dom non-'*' while hour non-'*' → dom (31*86400) takes precedence.
        """
        now = T_NOW

        # 7a: dow non-'*' AND dom non-'*' -> dow (604800 = 7*86400)
        sched_dow_dom = {"kind": "cron", "expr": "0 0 1 * 0"}  # dom='1', dow='0'
        interval_dow_dom = wo._cron_interval_seconds(sched_dow_dom)
        self.assertEqual(
            interval_dow_dom, 604800.0,
            f"(7a) Unit assertion failed: precedence dow over dom expected 604800.0 for {sched_dow_dom}, got {interval_dow_dom}"
        )
        # E2E 20 days ago: within 3*7d = 21d -> no stale
        last_run_20d = _iso_plus_0200(now - 20 * 86400.0)
        rc, stdout, obs = self._run_e2e_cron_check(sched_dow_dom, last_run_20d, now)
        self.assertEqual(
            rc, 0,
            f"(7a) E2E failed: dow precedence with last_run 20d ago (< 21d) must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(7a) E2E failed: dow precedence with last_run 20d ago must not output CRON_STALE: {stdout}"
        )
        # E2E 21 days and 1s ago: age > 21d -> stale (if dom was mistakenly used, 93d would NOT be stale!)
        last_run_21d1s = _iso_plus_0200(now - (21 * 86400.0 + 1.0))
        rc, stdout, obs = self._run_e2e_cron_check(sched_dow_dom, last_run_21d1s, now)
        self.assertEqual(
            rc, 0,
            f"(7a) E2E failed: dow precedence with last_run 21d+1s ago (> 21d) must exit 0 (CRON_STALE signals via stdout/files), got rc={rc}, stdout={stdout}"
        )
        self.assertIn(
            "CRON_STALE", stdout,
            f"(7a) E2E failed: dow precedence with last_run 21d+1s ago must output CRON_STALE: {stdout}"
        )
        self.assertTrue(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(7a) E2E failed: dow precedence with last_run 21d+1s ago must record CRON_STALE in observations: {obs.get('violations')}"
        )

        # 7b: dom non-'*' AND hour non-'*' -> dom (31*86400 = 2678400)
        sched_dom_hour = {"kind": "cron", "expr": "0 21 1 * *"}  # hour='21', dom='1', dow='*'
        interval_dom_hour = wo._cron_interval_seconds(sched_dom_hour)
        self.assertEqual(
            interval_dom_hour, 31 * 86400.0,
            f"(7b) Unit assertion failed: precedence dom over hour expected {31 * 86400.0} for {sched_dom_hour}, got {interval_dom_hour}"
        )
        # E2E 4 days ago (96h): if hour was mistakenly used (3*24h=72h), 96h would be stale! But dom gives 93 days, so no stale!
        last_run_4d = _iso_plus_0200(now - 4 * 86400.0)
        rc, stdout, obs = self._run_e2e_cron_check(sched_dom_hour, last_run_4d, now)
        self.assertEqual(
            rc, 0,
            f"(7b) E2E failed: dom precedence with last_run 4d ago (< 93d) must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(7b) E2E failed: dom precedence with last_run 4d ago must not output CRON_STALE: {stdout}"
        )
        # E2E 90 days ago: within 3*31d = 93d -> no stale
        last_run_90d = _iso_plus_0200(now - 90 * 86400.0)
        rc, stdout, obs = self._run_e2e_cron_check(sched_dom_hour, last_run_90d, now)
        self.assertEqual(
            rc, 0,
            f"(7b) E2E failed: dom precedence with last_run 90d ago (< 93d) must exit 0, got rc={rc}, stdout={stdout}"
        )
        self.assertNotIn(
            "CRON_STALE", stdout,
            f"(7b) E2E failed: dom precedence with last_run 90d ago must not output CRON_STALE: {stdout}"
        )
        # E2E 93 days and 1s ago: age > 93d -> stale
        last_run_93d1s = _iso_plus_0200(now - (93 * 86400.0 + 1.0))
        rc, stdout, obs = self._run_e2e_cron_check(sched_dom_hour, last_run_93d1s, now)
        self.assertEqual(
            rc, 0,
            f"(7b) E2E failed: dom precedence with last_run 93d+1s ago (> 93d) must exit 0 (CRON_STALE signals via stdout/files), got rc={rc}, stdout={stdout}"
        )
        self.assertIn(
            "CRON_STALE", stdout,
            f"(7b) E2E failed: dom precedence with last_run 93d+1s ago must output CRON_STALE: {stdout}"
        )
        self.assertTrue(
            any(v.get("type") == "CRON_STALE" for v in obs.get("violations", [])),
            f"(7b) E2E failed: dom precedence with last_run 93d+1s ago must record CRON_STALE in observations: {obs.get('violations')}"
        )




if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# NEW TESTS: (a) Exit-code semantics
# ---------------------------------------------------------------------------

def _make_job_with_created(id, enabled=True, state="scheduled", last_run_at=None,
                            last_status="ok", schedule=None, created_at=None):
    """Like _make_job but with optional created_at field."""
    j = _make_job(id, enabled=enabled, state=state, last_run_at=last_run_at,
                  last_status=last_status, schedule=schedule)
    if created_at is not None:
        j["created_at"] = created_at
    return j


class TestExitCodeSemantics(unittest.TestCase):
    """Verify normal (scheduled) mode always exits 0, even with violations.
    Violations must still appear in stdout (new only) and files."""

    def test_violation_rc_is_0(self):
        """A DEAD_PID violation → rc==0, VIOLATION in stdout."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            handle = _make_pid_handle(9999999, start=1)
            items = [_make_item(state="DISPATCHED", handle=handle)]
            home = _make_profile(tmp + "/home", items)
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0, f"Expected rc==0 with violation, got {rc}")
            self.assertIn("VIOLATION", stdout, "Expected VIOLATION in stdout")

    def test_multiple_violations_rc_is_0(self):
        """Multiple violations (DEAD_PID + CRON_MISSING) → rc==0."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            item1 = _make_item(id="C-V001", state="DISPATCHED",
                               handle=_make_pid_handle(9999997, start=1))
            item2 = _make_item(id="C-V002", state="DISPATCHED",
                               handle=_make_cron_handle("missing_job_xyz"))
            home = _make_profile(tmp + "/home", [item1, item2], jobs=[])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0, f"Expected rc==0 with multiple violations, got {rc}")
            self.assertIn("VIOLATION", stdout)

    def test_continuing_violation_second_round_rc_0_stdout_empty(self):
        """Continuing violation (2nd round): rc==0 and stdout is empty."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            item = _make_item(id="C-V003", state="DISPATCHED",
                              handle=_make_pid_handle(9999996, start=1))
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            # Round 1: new violation
            rc1, stdout1 = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc1, 0)
            self.assertIn("VIOLATION", stdout1)
            # Round 2: continuing violation, stdout must be empty
            rc2, stdout2 = _run_observer(home, out_dir=out_dir, now=T_NOW + 300,
                                         proc_root=proc_root)
            self.assertEqual(rc2, 0, f"Continuing violation must give rc==0, got {rc2}")
            self.assertEqual(stdout2.strip(), "",
                             f"Continuing violation must produce empty stdout, got {stdout2!r}")

    def test_violation_cleared_rc_0(self):
        """After violation clears, rc==0 and stdout empty."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            item = _make_item(id="C-V004", state="DISPATCHED",
                              handle=_make_pid_handle(9999995, start=1))
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            # Round 1: create violation
            _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            # Round 2: clear it
            good_item = _make_item(id="C-V004", state="VERIFIED")
            _write_ledger(home, [good_item])
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW + 300,
                                       proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.strip(), "")

    def test_violation_rc0_means_cron_would_not_error(self):
        """Verify: with violations present, rc==0, confirming cron won't set last_status=error."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            item = _make_item(id="C-V005", state="DISPATCHED",
                              handle=_make_pid_handle(9999994, start=1))
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertIn("VIOLATION", stdout, "Violation must be reported in stdout")
            self.assertEqual(rc, 0, "With violations, rc MUST be 0 so cron won't set last_status=error")

    def test_bad_ledger_still_rc_1(self):
        """OBSERVER_ERROR (bad ledger) → rc==1, not 0."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            os.makedirs(os.path.join(home, "commitments"))
            os.makedirs(os.path.join(home, "cron"))
            with open(os.path.join(home, "commitments", "ledger.json"), "wb") as fh:
                fh.write(b"{{bad json")
            _write_jobs(home, [])
            out_dir = tmp + "/out"
            with patch.object(wo, "READ_RETRY_DELAY", 0), \
                 patch.object(wo, "READ_RETRIES", 1):
                rc, _ = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 1, f"Corrupt ledger must give rc==1, got {rc}")

    def test_violation_and_bad_ledger_not_rc_0(self):
        """Pre-existing violations + corrupt ledger → OBSERVER_ERROR → rc==1 (not 0)."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            os.makedirs(os.path.join(home, "commitments"))
            os.makedirs(os.path.join(home, "cron"))
            # First run with valid ledger creates a violation
            item = _make_item(id="C-V006", state="DISPATCHED",
                              handle=_make_pid_handle(9999993, start=1))
            _write_ledger(home, [item])
            _write_jobs(home, [])
            out_dir = tmp + "/out"
            _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            # Now corrupt the ledger
            with open(os.path.join(home, "commitments", "ledger.json"), "wb") as fh:
                fh.write(b"{{still bad")
            with patch.object(wo, "READ_RETRY_DELAY", 0), \
                 patch.object(wo, "READ_RETRIES", 1):
                rc, _ = _run_observer(home, out_dir=out_dir, now=T_NOW + 300,
                                      proc_root=proc_root)
            self.assertEqual(rc, 1, "Corrupt ledger must give rc==1 even with existing violations")

    def test_subprocess_violation_rc0_stdout_violation(self):
        """E2E subprocess test: with violation, returncode==0 and stdout contains VIOLATION."""
        import subprocess as sp
        script = os.path.join(os.path.dirname(__file__), "..", "scripts", "workflow_observer.py")
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            item = _make_item(id="C-SP01", state="DISPATCHED",
                              handle=_make_pid_handle(9999992, start=1))
            home = _make_profile(tmp + "/home", [item])
            out_dir = tmp + "/out"
            result = sp.run(
                [sys.executable, script,
                 "--home", home, "--out-dir", out_dir,
                 "--now", str(T_NOW), "--proc-root", proc_root],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0,
                             f"returncode must be 0, got {result.returncode}. stderr={result.stderr!r}")
            self.assertIn("VIOLATION", result.stdout,
                          f"stdout must contain VIOLATION. stdout={result.stdout!r}")


# ---------------------------------------------------------------------------
# NEW TESTS: (b) Self-referential cron sequence
# ---------------------------------------------------------------------------

class TestSelfReferentialCronSequence(unittest.TestCase):
    """
    Simulate the observer's own cron job being monitored.
    Round 1: last_status=error → CRON_STATUS reported, rc==0.
    Round 2: update jobs.json to last_status=ok → CRON_STATUS cleared,
             anomalies.jsonl has cleared record, violations.json is empty.
    """

    def _make_observer_cron_item(self, job_id="observer-cron"):
        return _make_item(
            id="C-OBS01", state="DISPATCHED",
            handle=_make_cron_handle(job_id),
        )

    def test_self_ref_cron_error_then_ok(self):
        """Round 1 (last_status=error): CRON_STATUS violation reported, rc==0.
           Round 2 (last_status=ok simulated by cron after exit 0): violation clears."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"
            os.makedirs(proc_root)
            home = tmp + "/home"
            out_dir = tmp + "/out"
            job_id = "observer-cron-self"
            item = self._make_observer_cron_item(job_id)

            # Round 1: job exists but last_status=error (old version exited 2)
            last_run_r1 = _now_iso(T_NOW - 60)  # recent run, not stale
            job_r1 = _make_job(job_id, enabled=True, last_run_at=last_run_r1,
                               last_status="error",
                               schedule={"kind": "interval", "minutes": 5})
            _make_profile(home, [item], jobs=[job_r1])

            rc1, stdout1 = _run_observer(home, out_dir=out_dir, now=T_NOW,
                                         proc_root=proc_root)
            self.assertEqual(rc1, 0, f"Round 1 must exit 0, got {rc1}")
            self.assertIn("CRON_STATUS", stdout1,
                          f"Round 1 must report CRON_STATUS in stdout, got {stdout1!r}")

            # Verify violations.json has CRON_STATUS
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path) as fh:
                vdata = json.load(fh)
            viol_types = [v["type"] for v in vdata["violations"]]
            self.assertIn("CRON_STATUS", viol_types)

            # Round 2: cron wrote last_status=ok after successful exit 0
            job_r2 = _make_job(job_id, enabled=True, last_run_at=_now_iso(T_NOW),
                               last_status="ok",
                               schedule={"kind": "interval", "minutes": 5})
            _write_jobs(home, [job_r2])

            rc2, stdout2 = _run_observer(home, out_dir=out_dir, now=T_NOW + 300,
                                         proc_root=proc_root)
            self.assertEqual(rc2, 0, f"Round 2 must exit 0, got {rc2}")
            self.assertEqual(stdout2.strip(), "",
                             f"Round 2 stdout must be empty (cleared), got {stdout2!r}")

            # violations.json must be empty
            with open(viols_path) as fh:
                vdata2 = json.load(fh)
            self.assertEqual(len(vdata2["violations"]), 0,
                             f"violations.json must be empty after clear, got {vdata2['violations']}")

            # anomalies.jsonl must have a cleared record for CRON_STATUS
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            self.assertTrue(os.path.exists(anomalies_path), "anomalies.jsonl must exist")
            with open(anomalies_path) as fh:
                anomalies = [json.loads(l) for l in fh if l.strip()]
            cron_status_cleared = [a for a in anomalies if a.get("type") == "CRON_STATUS"]
            self.assertTrue(len(cron_status_cleared) > 0,
                            "anomalies.jsonl must have CRON_STATUS cleared record")
            self.assertIn("cleared_at", cron_status_cleared[0])


# ---------------------------------------------------------------------------
# NEW TESTS: (c) First-schedule grace period
# ---------------------------------------------------------------------------

def _now_iso_from_epoch(epoch):
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.isoformat()


class TestFirstScheduleGrace(unittest.TestCase):
    """Test first-schedule grace: last_run_at=null, within 2*interval+300 sec of created_at."""

    def _make_cron_item_and_job(self, job_id, interval_minutes, created_at_epoch,
                                last_status=None, enabled=True, last_run_at=None):
        item = _make_item(id="C-GRACE01", state="DISPATCHED",
                          handle=_make_cron_handle(job_id))
        job = _make_job_with_created(
            job_id,
            enabled=enabled,
            last_run_at=last_run_at,
            last_status=last_status,
            schedule={"kind": "interval", "minutes": interval_minutes},
            created_at=_now_iso_from_epoch(created_at_epoch),
        )
        return item, job

    def test_grace_boundary_at_900s_5min_interval(self):
        """interval=5min → grace=2*300+300=900s. created_at=now-900 → in grace (no STALE/STATUS)."""
        interval_min = 5
        grace_sec = 2 * interval_min * 60 + 300  # = 900
        created_at = T_NOW - grace_sec  # exactly at boundary
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item, job = self._make_cron_item_and_job(
                "g5min", interval_min, created_at, last_status=None)
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0, f"At grace boundary, rc must be 0, got {rc}")
            self.assertNotIn("CRON_STALE", stdout, "Must not report CRON_STALE during grace")
            self.assertNotIn("CRON_STATUS", stdout, "Must not report CRON_STATUS during grace")
            # Verify info has cron_first_schedule_grace
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            info_types = [i["type"] for i in obs.get("info", [])]
            self.assertIn("cron_first_schedule_grace", info_types,
                          f"info must contain cron_first_schedule_grace, got {obs.get('info')}")

    def test_grace_boundary_exceeded_by_1s(self):
        """created_at=now-901 → grace exceeded (900s), CRON_STALE and CRON_STATUS reported."""
        interval_min = 5
        grace_sec = 900
        created_at = T_NOW - (grace_sec + 1)  # 1s past boundary
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item, job = self._make_cron_item_and_job(
                "g5min2", interval_min, created_at, last_status=None)
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0, f"rc must be 0 (violations via stdout), got {rc}")
            self.assertIn("CRON_STALE", stdout, "Must report CRON_STALE after grace expired")

    def test_grace_within_899s(self):
        """created_at=now-899 → within grace → no CRON_STALE."""
        interval_min = 5
        created_at = T_NOW - 899
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item, job = self._make_cron_item_and_job(
                "g5min3", interval_min, created_at, last_status=None)
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertNotIn("CRON_STALE", stdout)
            self.assertNotIn("CRON_STATUS", stdout)

    def test_grace_created_at_future(self):
        """created_at in future (clock skew) → counted as in grace → no CRON_STALE."""
        interval_min = 5
        created_at = T_NOW + 60  # 60s in the future
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item, job = self._make_cron_item_and_job(
                "g5future", interval_min, created_at, last_status=None)
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertNotIn("CRON_STALE", stdout)
            self.assertNotIn("CRON_STATUS", stdout)

    def test_grace_missing_created_at_judges_stale(self):
        """created_at absent → no grace → CRON_STALE reported."""
        interval_min = 5
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE02", state="DISPATCHED",
                              handle=_make_cron_handle("gnocreate"))
            job = _make_job("gnocreate", enabled=True, last_run_at=None,
                            last_status=None,
                            schedule={"kind": "interval", "minutes": interval_min})
            # No created_at in job
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout, "Without created_at, must fall back to CRON_STALE")

    def test_grace_unparseable_created_at_judges_stale(self):
        """created_at is garbage → unparseable → no grace → CRON_STALE."""
        interval_min = 5
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE03", state="DISPATCHED",
                              handle=_make_cron_handle("gbadts"))
            job = _make_job("gbadts", enabled=True, last_run_at=None,
                            last_status=None,
                            schedule={"kind": "interval", "minutes": interval_min})
            job["created_at"] = "not-a-timestamp"
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout, "Unparseable created_at must fall back to CRON_STALE")

    def test_grace_does_not_apply_when_last_run_at_set(self):
        """last_run_at is set (even if old) → grace doesn't apply → CRON_STALE judged normally."""
        interval_min = 5
        created_at = T_NOW - 10  # very recent created_at
        last_run_at = T_NOW - 3600  # stale: >3*300=900s
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE04", state="DISPATCHED",
                              handle=_make_cron_handle("glastrun"))
            job = _make_job_with_created(
                "glastrun", enabled=True,
                last_run_at=_now_iso_from_epoch(last_run_at),
                last_status="ok",
                schedule={"kind": "interval", "minutes": interval_min},
                created_at=_now_iso_from_epoch(created_at),
            )
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout,
                          "last_run_at set and stale → must report CRON_STALE even with recent created_at")

    def test_grace_disabled_still_reports_cron_disabled(self):
        """Grace period: enabled=False → CRON_DISABLED still reported (grace only exempts STALE/STATUS)."""
        interval_min = 5
        created_at = T_NOW - 100  # within 900s grace
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item, job = self._make_cron_item_and_job(
                "gdisabled", interval_min, created_at,
                last_status=None, enabled=False)
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_DISABLED", stdout,
                          "CRON_DISABLED must still be reported during first-schedule grace")
            self.assertNotIn("CRON_STALE", stdout)
            self.assertNotIn("CRON_STATUS", stdout)

    def test_grace_missing_job_still_reports_cron_missing(self):
        """Grace period doesn't affect CRON_MISSING (job not in jobs.json → CRON_MISSING)."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE05", state="DISPATCHED",
                              handle=_make_cron_handle("no_such_job_grace"))
            home = _make_profile(tmp + "/home", [item], jobs=[])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_MISSING", stdout, "CRON_MISSING must be reported regardless of grace")

    def test_grace_10min_interval_boundary_1500s(self):
        """interval=10min → grace=2*600+300=1500s. created_at=now-1500 → in grace."""
        interval_min = 10
        grace_sec = 2 * interval_min * 60 + 300  # = 1500
        created_at = T_NOW - grace_sec  # exactly at boundary
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item, job = self._make_cron_item_and_job(
                "g10min", interval_min, created_at, last_status=None)
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertNotIn("CRON_STALE", stdout, "At 1500s boundary for 10min interval, must be in grace")

    def test_grace_10min_interval_exceeded_at_1501s(self):
        """interval=10min, grace=1500s. created_at=now-1501 → grace expired → CRON_STALE."""
        interval_min = 10
        created_at = T_NOW - 1501
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item, job = self._make_cron_item_and_job(
                "g10min2", interval_min, created_at, last_status=None)
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout, "At 1501s for 10min interval, grace expired → CRON_STALE")

    def test_grace_unknown_schedule_no_grace(self):
        """Unknown schedule kind → interval unknown → no grace → CRON_STALE/STATUS not fired but schedule_unknown info."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE06", state="DISPATCHED",
                              handle=_make_cron_handle("gunkown"))
            job = _make_job_with_created(
                "gunkown", enabled=True,
                last_run_at=None, last_status=None,
                schedule={"kind": "exotic_unknown", "display": "sometime"},
                created_at=_now_iso_from_epoch(T_NOW - 10),
            )
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            # schedule_unknown → no STALE (existing behavior), no grace applies either; CRON_STATUS reported
            self.assertNotIn("CRON_STALE", stdout)
            self.assertIn("CRON_STATUS", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            info_types = [i["type"] for i in obs.get("info", [])]
            self.assertIn("schedule_unknown", info_types)
            # No first-schedule-grace info (schedule unknown → can't compute grace)
            self.assertNotIn("cron_first_schedule_grace", info_types)

    def test_grace_info_written_to_observations_jsonl(self):
        """Grace info record is written to observations.jsonl with correct type and item fields."""
        interval_min = 5
        created_at = T_NOW - 300  # well within grace (900s)
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item, job = self._make_cron_item_and_job(
                "ginfo", interval_min, created_at, last_status=None)
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            self.assertTrue(os.path.exists(obs_path))
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            grace_infos = [i for i in obs.get("info", [])
                           if i.get("type") == "cron_first_schedule_grace"]
            self.assertEqual(len(grace_infos), 1, f"Expected 1 grace info, got {obs.get('info')}")
            self.assertEqual(grace_infos[0]["item"], "C-GRACE01")
            # Also verify the check result reflects grace
            grace_checks = [c for c in obs.get("checks", [])
                            if c.get("result") == "INFO_FIRST_SCHEDULE_GRACE"]
            self.assertEqual(len(grace_checks), 1,
                             f"Expected 1 check with INFO_FIRST_SCHEDULE_GRACE, got {obs.get('checks')}")

    def test_unknown_schedule_last_status_ok_no_violation(self):
        """Unknown schedule + last_status=ok → no violation."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE07", state="DISPATCHED",
                              handle=_make_cron_handle("gunkown_ok"))
            job = _make_job_with_created(
                "gunkown_ok", enabled=True,
                last_run_at=None, last_status="ok",
                schedule={"kind": "exotic_unknown", "display": "sometime"},
                created_at=_now_iso_from_epoch(T_NOW - 10),
            )
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.strip(), "")
            self.assertNotIn("CRON_STATUS", stdout)
            self.assertNotIn("CRON_STALE", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            info_types = [i["type"] for i in obs.get("info", [])]
            self.assertIn("schedule_unknown", info_types)
            self.assertNotIn("cron_first_schedule_grace", info_types)
            viols = [v["type"] for v in obs.get("violations", [])]
            self.assertEqual(viols, [])

    def test_unknown_schedule_last_run_null_last_status_null_reports_cron_status(self):
        """Unknown schedule + last_run_at=null + last_status=null → CRON_STATUS, no CRON_STALE, no grace info."""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE08", state="DISPATCHED",
                              handle=_make_cron_handle("gunkown_null"))
            job = _make_job_with_created(
                "gunkown_null", enabled=True,
                last_run_at=None, last_status=None,
                schedule={"kind": "exotic_unknown", "display": "sometime"},
                created_at=_now_iso_from_epoch(T_NOW - 10),
            )
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STATUS", stdout)
            self.assertNotIn("CRON_STALE", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            viols = [v["type"] for v in obs.get("violations", [])]
            self.assertIn("CRON_STATUS", viols)
            self.assertNotIn("CRON_STALE", viols)
            info_types = [i["type"] for i in obs.get("info", [])]
            self.assertIn("schedule_unknown", info_types)
            self.assertNotIn("cron_first_schedule_grace", info_types)

    def test_grace_unparseable_last_run_at_with_recent_created_at_judges_stale(self):
        """last_run_at is unparseable ('not-a-date') + recent created_at → no grace, reports CRON_STALE."""
        interval_min = 5
        created_at = T_NOW - 10  # recent created_at
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE09", state="DISPATCHED",
                              handle=_make_cron_handle("gbaddate"))
            job = _make_job_with_created(
                "gbaddate", enabled=True,
                last_run_at="not-a-date",
                last_status="ok",
                schedule={"kind": "interval", "minutes": interval_min},
                created_at=_now_iso_from_epoch(created_at),
            )
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout,
                          "Unparseable last_run_at must report CRON_STALE even with recent created_at")
            self.assertNotIn("CRON_STATUS", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            info_types = [i["type"] for i in obs.get("info", [])]
            self.assertNotIn("cron_first_schedule_grace", info_types)

    def test_grace_unparseable_last_run_at_with_status_error(self):
        """last_run_at unparseable + last_status=error + recent created_at → reports both CRON_STALE and CRON_STATUS."""
        interval_min = 5
        created_at = T_NOW - 10
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE10", state="DISPATCHED",
                              handle=_make_cron_handle("gbaddate_err"))
            job = _make_job_with_created(
                "gbaddate_err", enabled=True,
                last_run_at="not-a-date",
                last_status="error",
                schedule={"kind": "interval", "minutes": interval_min},
                created_at=_now_iso_from_epoch(created_at),
            )
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout)
            self.assertIn("CRON_STATUS", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            info_types = [i["type"] for i in obs.get("info", [])]
            self.assertNotIn("cron_first_schedule_grace", info_types)

    def test_grace_empty_string_last_run_at_within_grace(self):
        """last_run_at='' (empty string) + recent created_at → within grace, no STALE/STATUS."""
        interval_min = 5
        created_at = T_NOW - 100  # within 900s grace
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE11", state="DISPATCHED",
                              handle=_make_cron_handle("gempty_last"))
            job = _make_job_with_created(
                "gempty_last", enabled=True,
                last_run_at="",
                last_status=None,
                schedule={"kind": "interval", "minutes": interval_min},
                created_at=_now_iso_from_epoch(created_at),
            )
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertEqual(stdout.strip(), "")
            self.assertNotIn("CRON_STALE", stdout)
            self.assertNotIn("CRON_STATUS", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            info_types = [i["type"] for i in obs.get("info", [])]
            self.assertIn("cron_first_schedule_grace", info_types)

    def test_grace_empty_string_last_run_at_expired_grace(self):
        """last_run_at='' + expired created_at → grace expired, reports CRON_STALE and CRON_STATUS."""
        interval_min = 5
        created_at = T_NOW - 1000  # past 900s grace
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = tmp + "/proc"; os.makedirs(proc_root)
            item = _make_item(id="C-GRACE12", state="DISPATCHED",
                              handle=_make_cron_handle("gempty_exp"))
            job = _make_job_with_created(
                "gempty_exp", enabled=True,
                last_run_at="",
                last_status=None,
                schedule={"kind": "interval", "minutes": interval_min},
                created_at=_now_iso_from_epoch(created_at),
            )
            home = _make_profile(tmp + "/home", [item], jobs=[job])
            out_dir = tmp + "/out"
            rc, stdout = _run_observer(home, out_dir=out_dir, now=T_NOW, proc_root=proc_root)
            self.assertEqual(rc, 0)
            self.assertIn("CRON_STALE", stdout)
            self.assertIn("CRON_STATUS", stdout)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path) as fh:
                obs = json.loads(fh.readlines()[-1])
            info_types = [i["type"] for i in obs.get("info", [])]
            self.assertNotIn("cron_first_schedule_grace", info_types)


# ---------------------------------------------------------------------------
# TestVerdictWindowOverlap: Period-scoped violation overlap / membership regressions
# ---------------------------------------------------------------------------

class TestVerdictWindowOverlap(unittest.TestCase):
    """
    Regressions for period-scoped violation membership and duration in verdict mode.
    Positive overlap with elapsed window [start, min(end, now)) decides MEMBERSHIP ONLY.
    For included overlapping episodes, original first_seen-based duration is retained
    to enforce the conservative 900s continuity contract without resetting violation age.
    """

    def test_c0715_historical_exclusion_regression(self):
        """
        Exact measured defect: historical C-0715 CRON_STATUS entirely before window must not count.
        Window: start=1792411050, end=1792497450 (288 slots).
        Anomaly: first_seen=1792404664.1131294, cleared_at=1792409583.8137968 (cleared before start).
        Observed period is incomplete (4/288); coverage behavior unchanged.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 1792411050.0
            end = 1792497450.0
            now = end + 3600.0

            # 4 observed slots out of 288 (incomplete period)
            obs_path = os.path.join(out_dir, "observations.jsonl")
            obs_records = [_make_obs_record(start + i * 300 + 10) for i in range(4)]
            _write_obs_records(obs_path, obs_records)

            # Historical anomaly C-0715 entirely before start (cleared_at < start)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": 1792409583.8137968,
                "type": "CRON_STATUS",
                "item": "C-0715",
                "detail": "last_status:error",
                "first_seen": 1792404664.1131294,
                "cleared_at": 1792409583.8137968,
                "seconds_open": 4919.700667381287,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            rc, result = _run_verdict(out_dir, start, end, now=now)

            # Incomplete coverage behavior unchanged (4/288 slots covered)
            self.assertEqual(result["coverage"]["expected"], 288)
            self.assertEqual(result["coverage"]["covered"], 4)
            self.assertFalse(result["coverage"]["pass"])

            # C-0715 must NOT be counted as a violation in this window
            c0715_items = [v for v in result["violations"]["items"] if v.get("item") == "C-0715"]
            self.assertEqual(c0715_items, [], f"C-0715 must not be included in violations: {c0715_items}")
            self.assertEqual(len(result["violations"]["items"]), 0)
            self.assertEqual(result["violations"]["max_seconds"], 0.0)
            self.assertTrue(result["violations"]["pass"], "Violations check must pass since no violations overlap window")

            # Historical clear before start does not count as disposal evidence in [start, end]
            self.assertEqual(result["disposal_evidence"]["count"], 0)
            self.assertFalse(result["disposal_evidence"]["pass"])

            # Overall verdict is FAIL because coverage is incomplete and no disposal evidence,
            # but NOT because of violations
            self.assertEqual(rc, 2)
            self.assertEqual(result["verdict"], "FAIL")
            self.assertFalse(any("violation" in r for r in result["reasons"]),
                             f"Violations must not be in failure reasons: {result['reasons']}")

    def test_anomaly_cleared_before_start_excluded(self):
        """Anomaly with first_seen < start and cleared_at < start must not count in violations."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": start - 50.0,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": start - 500.0,
                "cleared_at": start - 50.0,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            self.assertEqual(result["violations"]["items"], [])
            self.assertEqual(result["violations"]["max_seconds"], 0.0)
            self.assertTrue(result["violations"]["pass"])
            self.assertEqual(result["disposal_evidence"]["count"], 0)

    def test_anomaly_cleared_exactly_at_start_excluded_from_violations(self):
        """Boundary: cleared_at exactly at start has 0 overlap with [start, min(end, now)) -> must not count."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": start,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": start - 500.0,
                "cleared_at": start,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            self.assertEqual(result["violations"]["items"], [])
            self.assertEqual(result["violations"]["max_seconds"], 0.0)
            self.assertTrue(result["violations"]["pass"])
            # cleared_at == start IS within [start, end] contract for disposal evidence
            self.assertEqual(result["disposal_evidence"]["count"], 1)
            self.assertTrue(result["disposal_evidence"]["pass"])

    def test_anomaly_first_seen_exactly_at_end_excluded(self):
        """Boundary: anomaly with first_seen exactly at end has 0 overlap with [start, end) -> must not count."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": end + 100.0,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": end,
                "cleared_at": end + 100.0,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            self.assertEqual(result["violations"]["items"], [])
            self.assertEqual(result["violations"]["max_seconds"], 0.0)
            self.assertTrue(result["violations"]["pass"])

    def test_open_violation_first_seen_exactly_at_end_excluded(self):
        """Boundary: open violation with first_seen exactly at end -> must not count."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": end,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            self.assertEqual(result["violations"]["items"], [])
            self.assertEqual(result["violations"]["max_seconds"], 0.0)
            self.assertTrue(result["violations"]["pass"])

    def test_anomaly_first_seen_after_end_excluded(self):
        """Anomaly with first_seen > end -> must not count."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": end + 200.0,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": end + 10.0,
                "cleared_at": end + 200.0,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            self.assertEqual(result["violations"]["items"], [])
            self.assertEqual(result["violations"]["max_seconds"], 0.0)

    def test_open_violation_first_seen_after_end_excluded(self):
        """Open violation with first_seen > end -> must not count."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": end + 10.0,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            self.assertEqual(result["violations"]["items"], [])
            self.assertEqual(result["violations"]["max_seconds"], 0.0)

    def test_violation_first_seen_at_or_after_now_excluded(self):
        """Boundary: ongoing window (now < end); violations with first_seen >= now must not count."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 30000.0
            now = start + 2000.0  # now < end

            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": now + 50.0,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": now,  # exactly at now
                "cleared_at": now + 50.0,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "CRON_STALE", "item": "C-0002", "first_seen": now + 10.0,  # after now
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            self.assertEqual(result["violations"]["items"], [])
            self.assertEqual(result["violations"]["max_seconds"], 0.0)
            self.assertTrue(result["violations"]["pass"])

    def test_cross_start_episode_older_than_900_fails_even_with_short_window_overlap(self):
        """
        Explicit regression: an episode starting >900s before start, clearing shortly after start,
        has positive in-window overlap so must remain INCLUDED, but FAILS based on preserved lifetime age.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0

            fs = start - 1000.0  # began 1000s before start
            cleared_at = start + 50.0  # clears 50s after start (50s in-window overlap)
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": cleared_at,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": fs,
                "cleared_at": cleared_at,
                "seconds_open": cleared_at - fs,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            item = viol_items[0]
            # Provenance preserved
            self.assertEqual(item["first_seen"], fs)
            self.assertEqual(item["cleared_at"], cleared_at)
            # Duration retains first_seen lifetime: cleared_at - fs = 1050.0s > 900s
            self.assertEqual(item["duration"], 1050.0)
            self.assertFalse(item["pass"])
            self.assertEqual(result["violations"]["max_seconds"], 1050.0)
            self.assertFalse(result["violations"]["pass"])
            self.assertTrue(any("violation max_seconds" in r for r in result["reasons"]))

    def test_cross_start_episode_younger_than_900_passes(self):
        """Cross-start episode whose total lifetime age at clear is <= 900s passes."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0

            fs = start - 300.0
            cleared_at = start + 600.0  # total lifetime: 900.0s
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": cleared_at,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": fs,
                "cleared_at": cleared_at,
                "seconds_open": cleared_at - fs,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            item = viol_items[0]
            self.assertEqual(item["first_seen"], fs)
            self.assertEqual(item["cleared_at"], cleared_at)
            self.assertEqual(item["duration"], 900.0)
            self.assertTrue(item["pass"])
            self.assertEqual(result["violations"]["max_seconds"], 900.0)
            self.assertTrue(result["violations"]["pass"])

    def test_cross_start_episode_lifetime_901_fails(self):
        """Cross-start episode whose total lifetime age at clear is 901s fails."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0

            fs = start - 300.0
            cleared_at = start + 601.0  # total lifetime: 901.0s
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": cleared_at,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": fs,
                "cleared_at": cleared_at,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            item = viol_items[0]
            self.assertEqual(item["duration"], 901.0)
            self.assertFalse(item["pass"])
            self.assertEqual(result["violations"]["max_seconds"], 901.0)
            self.assertFalse(result["violations"]["pass"])

    def test_within_window_exact_900_pass_and_901_fail(self):
        """Isolated threshold tests within window: exactly 900s passes; 901s fails."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0

            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            fs1 = start + 100.0
            fs2 = start + 1200.0
            a1 = {"type": "DEAD_PID", "item": "C-PASS", "first_seen": fs1, "cleared_at": fs1 + 900.0}
            a2 = {"type": "DEAD_PID", "item": "C-FAIL", "first_seen": fs2, "cleared_at": fs2 + 901.0}
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(a1) + "\n")
                fh.write(json.dumps(a2) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            items_by_id = {v["item"]: v for v in result["violations"]["items"]}
            self.assertEqual(len(items_by_id), 2)
            self.assertEqual(items_by_id["C-PASS"]["duration"], 900.0)
            self.assertTrue(items_by_id["C-PASS"]["pass"])
            self.assertEqual(items_by_id["C-FAIL"]["duration"], 901.0)
            self.assertFalse(items_by_id["C-FAIL"]["pass"])
            self.assertEqual(result["violations"]["max_seconds"], 901.0)
            self.assertFalse(result["violations"]["pass"])

    def test_cross_end_cleared_after_end_pass_at_900(self):
        """Violation starting inside window and clearing after end clips duration to end - first_seen."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0

            fs = end - 900.0
            cleared_at = end + 500.0  # clears after window
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": cleared_at,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": fs,
                "cleared_at": cleared_at,
                "seconds_open": cleared_at - fs,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            item = viol_items[0]
            self.assertEqual(item["first_seen"], fs)
            self.assertEqual(item["cleared_at"], cleared_at)
            # Duration clipped to end - fs = 900
            self.assertEqual(item["duration"], 900.0)
            self.assertTrue(item["pass"])
            self.assertEqual(result["violations"]["max_seconds"], 900.0)
            self.assertTrue(result["violations"]["pass"])

    def test_cross_end_cleared_after_end_fails_at_901(self):
        """Violation starting inside window and clearing after end with 901s in-window overlap fails."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0

            fs = end - 901.0
            cleared_at = end + 500.0
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": cleared_at,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": fs,
                "cleared_at": cleared_at,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            self.assertEqual(viol_items[0]["duration"], 901.0)
            self.assertFalse(viol_items[0]["pass"])
            self.assertEqual(result["violations"]["max_seconds"], 901.0)
            self.assertFalse(result["violations"]["pass"])

    def test_cross_end_open_violation_pass_at_900(self):
        """Still-open violation cross-end (now >= end): duration = end - first_seen == 900 -> passes."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0

            fs = end - 900.0
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": fs,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            item = viol_items[0]
            self.assertEqual(item["first_seen"], fs)
            self.assertIsNone(item["cleared_at"])
            self.assertEqual(item["duration"], 900.0)
            self.assertTrue(item["pass"])
            self.assertEqual(result["violations"]["max_seconds"], 900.0)
            self.assertTrue(result["violations"]["pass"])

    def test_cross_end_open_violation_fails_at_901(self):
        """Still-open violation cross-end (now >= end): duration = end - first_seen == 901 -> fails."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0

            fs = end - 901.0
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": fs,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            self.assertEqual(viol_items[0]["duration"], 901.0)
            self.assertFalse(viol_items[0]["pass"])
            self.assertFalse(result["violations"]["pass"])

    def test_ongoing_open_violation_now_cap_pass_at_900(self):
        """Ongoing window (now < end): open violation duration capped at now - first_seen == 900 -> passes."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 30000.0
            now = start + 2000.0  # now < end

            fs = now - 900.0
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": fs,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            self.assertEqual(viol_items[0]["duration"], 900.0)
            self.assertTrue(viol_items[0]["pass"])
            self.assertEqual(result["violations"]["max_seconds"], 900.0)
            self.assertTrue(result["violations"]["pass"])

    def test_ongoing_open_violation_now_cap_fails_at_901(self):
        """Ongoing window (now < end): open violation duration capped at now - first_seen == 901 -> fails."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 30000.0
            now = start + 2000.0

            fs = now - 901.0
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": fs,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            self.assertEqual(viol_items[0]["duration"], 901.0)
            self.assertFalse(viol_items[0]["pass"])
            self.assertEqual(result["violations"]["max_seconds"], 901.0)
            self.assertFalse(result["violations"]["pass"])

    def test_ongoing_cross_start_open_violation_now_cap_pass_at_900(self):
        """Ongoing window (now < end) with cross-start open violation younger than 900: now - first_seen == 900 -> passes."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 30000.0
            now = start + 600.0

            fs = start - 300.0  # began 300s before start; lifetime at now is 900s
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": fs,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            item = viol_items[0]
            self.assertEqual(item["first_seen"], fs)
            self.assertIsNone(item["cleared_at"])
            self.assertEqual(item["duration"], 900.0)
            self.assertTrue(item["pass"])
            self.assertTrue(result["violations"]["pass"])

    def test_ongoing_cross_start_open_violation_now_cap_fails_at_901(self):
        """Ongoing window (now < end) with cross-start open violation lifetime 901: now - first_seen == 901 -> fails."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 30000.0
            now = start + 601.0

            fs = start - 300.0
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": fs,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            self.assertEqual(viol_items[0]["duration"], 901.0)
            self.assertFalse(viol_items[0]["pass"])
            self.assertFalse(result["violations"]["pass"])

    def test_ongoing_cross_start_open_violation_already_old_fails_short_overlap(self):
        """Cross-start open violation already older than 900s before start fails even if in-window overlap is small."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 30000.0
            now = start + 50.0

            fs = start - 1000.0  # already 1000s old before start
            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": fs,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            item = viol_items[0]
            self.assertEqual(item["first_seen"], fs)
            self.assertIsNone(item["cleared_at"])
            self.assertEqual(item["duration"], 1050.0)
            self.assertFalse(item["pass"])
            self.assertFalse(result["violations"]["pass"])

    def test_cross_entire_window_end_capped_with_preserved_first_seen_duration(self):
        """Violation begins before start and clears after end: right edge capped at end, duration = end - first_seen."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 1200.0  # window length 1200s
            now = end + 3600.0

            fs = start - 500.0
            cleared_at = end + 500.0
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            anomaly = {
                "ts": cleared_at,
                "type": "DEAD_PID",
                "item": "C-0001",
                "first_seen": fs,
                "cleared_at": cleared_at,
            }
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(anomaly) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            item = viol_items[0]
            self.assertEqual(item["first_seen"], fs)
            self.assertEqual(item["cleared_at"], cleared_at)
            # Duration = end - fs = 1200 + 500 = 1700.0
            self.assertEqual(item["duration"], 1700.0)
            self.assertFalse(item["pass"])

    def test_disposal_evidence_exact_contract_boundaries(self):
        """Disposal evidence contract requires cleared_at in [start, end]."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0
            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")

            # Boundary cases:
            # 1. cleared_at == start -> counts
            # 2. cleared_at == end -> counts
            # 3. cleared_at == start - 0.5 -> does NOT count
            # 4. cleared_at == end + 0.5 -> does NOT count
            records = [
                {"type": "DEAD_PID", "item": "C-0001", "first_seen": start - 200, "cleared_at": start},
                {"type": "DEAD_PID", "item": "C-0002", "first_seen": end - 200, "cleared_at": end},
                {"type": "DEAD_PID", "item": "C-0003", "first_seen": start - 500, "cleared_at": start - 0.5},
                {"type": "DEAD_PID", "item": "C-0004", "first_seen": end - 100, "cleared_at": end + 0.5},
            ]
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                for r in records:
                    fh.write(json.dumps(r) + "\n")

            _, result = _run_verdict(out_dir, start, end, now=now)
            # Only C-0001 (at start) and C-0002 (at end) count
            self.assertEqual(result["disposal_evidence"]["count"], 2)
            self.assertTrue(result["disposal_evidence"]["pass"])

    def test_deduplication_between_anomalies_and_violations_json(self):
        """Violation present in both anomalies.jsonl and violations.json deduplicated by (type, item, fs)."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0
            now = end + 3600.0
            fs = start + 100.0

            anomalies_path = os.path.join(out_dir, "anomalies.jsonl")
            with open(anomalies_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "type": "DEAD_PID", "item": "C-0001", "first_seen": fs, "cleared_at": start + 400.0,
                }) + "\n")

            viols_path = os.path.join(out_dir, "violations.json")
            with open(viols_path, "w", encoding="utf-8") as fh:
                json.dump({
                    "updated": now,
                    "violations": [{
                        "type": "DEAD_PID", "item": "C-0001", "first_seen": fs,
                    }]
                }, fh)

            _, result = _run_verdict(out_dir, start, end, now=now)
            viol_items = result["violations"]["items"]
            self.assertEqual(len(viol_items), 1)
            self.assertEqual(viol_items[0]["item"], "C-0001")
            self.assertEqual(viol_items[0]["cleared_at"], start + 400.0)

    def test_verdict_read_only_and_malformed_lines_preserved(self):
        """Verdict mode is strictly read-only and bad JSON lines are counted in bad_lines."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = tmp + "/out"
            os.makedirs(out_dir)
            start = 10000.0
            end = start + 3000.0

            obs_path = os.path.join(out_dir, "observations.jsonl")
            with open(obs_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(_make_obs_record(start + 1)) + "\n")
                fh.write("{corrupt json 1\n")
                fh.write("[\"not\", \"a\", \"dict\"]\n")

            anom_path = os.path.join(out_dir, "anomalies.jsonl")
            with open(anom_path, "w", encoding="utf-8") as fh:
                fh.write("{corrupt json 2\n")

            # Capture file state before verdict run
            files_before = set(os.listdir(out_dir))
            hashes_before = {}
            for fname in files_before:
                with open(os.path.join(out_dir, fname), "rb") as fh:
                    hashes_before[fname] = hashlib.sha256(fh.read()).hexdigest()

            _, result = _run_verdict(out_dir, start, end)

            # Files must be completely unchanged (no writes)
            files_after = set(os.listdir(out_dir))
            self.assertEqual(files_before, files_after)
            for fname in files_before:
                with open(os.path.join(out_dir, fname), "rb") as fh:
                    h = hashlib.sha256(fh.read()).hexdigest()
                self.assertEqual(h, hashes_before[fname], f"File {fname} was modified during verdict")

            # 3 malformed lines counted
            self.assertEqual(result["bad_lines"], 3)


