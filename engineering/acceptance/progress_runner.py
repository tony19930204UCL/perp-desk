#!/usr/bin/env python3
"""Progress-aware acceptance test runner.

Monitors unittest discovery and execution lifecycle in real time.
Features:
- Pinned head and exact configuration keying with flock concurrency protection.
- Soft checkpoint (default 840s) that extends observation when progress is observed (same PID/identity).
- Observational stall/inactivity suspicion capturing stack and kernel wait channel evidence without killing progressing tests.
- Hard deadline (default 2400s) enforcing strict total time limits.
- Process group bounded termination with SIGTERM and fallback SIGKILL, reaping owned child Popen during grace.
- Explicit item budget (--item-seconds) and inactivity detection (--stall-seconds).
- Faulthandler-based thread diagnostics on stall or inactivity.
- Discovered test ID vs completed ID reconciliation; empty or incomplete executions cannot PASS.
- Continuous unbuffered stdout/stderr and JSONL lifecycle persistence.
- Output JSON format compatible with acceptance requirements.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_TIMEOUT = 2
EXIT_STALL = 3
EXIT_CANCELLED = 4


def get_proc_identity(pid: int):
    """Return (pid, starttime) tuple uniquely identifying a Linux process instance."""
    if not pid or pid <= 1:
        return None
    stat_file = Path(f"/proc/{pid}/stat")
    if not stat_file.exists():
        return None
    try:
        raw = stat_file.read_text()
        rparen = raw.rfind(")")
        if rparen != -1:
            rest = raw[rparen + 1 :].split()
            # rest[19] is field 22 (starttime)
            return (pid, int(rest[19]))
    except Exception:
        pass
    try:
        return (pid, Path(f"/proc/{pid}").stat().st_mtime_ns)
    except Exception:
        return None


def is_proc_alive(pid: int, expected_ident=None) -> bool:
    """Check if process pid is currently running and matches expected_ident if provided."""
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    if expected_ident is not None:
        curr = get_proc_identity(pid)
        if curr is not None and tuple(expected_ident) == curr:
            return True
        if curr is not None:
            # PID exists but identity changed (PID was recycled)
            return False
    return True


def stop_process_group(pgid: int, proc: subprocess.Popen = None, timeout: float = 5.0):
    """Gracefully terminate a process group with SIGTERM, falling back to SIGKILL.

    Reaps owned child Popen during grace period so it does not remain an unreaped zombie,
    which would otherwise cause killpg(pgid, 0) to poll until timeout even when the child
    has already terminated. Also preserves TERM-resistant descendant handling.
    """
    if isinstance(proc, (int, float)):
        timeout = float(proc)
        proc = None

    if not pgid or pgid <= 1:
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        if proc is not None and proc.poll() is None:
            try:
                proc.poll()
            except Exception:
                pass
        return

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Reap owned Popen so it doesn't linger as a zombie holding pgid alive
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=0.05)
            except (subprocess.TimeoutExpired, Exception):
                pass

        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, OSError):
            # Process group is completely gone
            if proc is not None and proc.poll() is None:
                try:
                    proc.wait(timeout=0.1)
                except Exception:
                    pass
            return
        time.sleep(0.05)

    # Fallback to SIGKILL for any TERM-resistant descendants
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass

    if proc is not None and proc.poll() is None:
        try:
            proc.wait(timeout=0.5)
        except Exception:
            pass


def gather_stack_trace(pid: int):
    """Signal worker via SIGUSR1 so faulthandler dumps tracebacks to stderr."""
    if not pid or pid <= 1:
        return
    try:
        os.kill(pid, signal.SIGUSR1)
        time.sleep(0.15)
    except OSError:
        pass


def get_wait_evidence(pid: int) -> dict:
    """Capture kernel wait channel if readable in /proc."""
    evidence = {}
    if not pid or pid <= 1:
        return evidence
    try:
        wchan = Path(f"/proc/{pid}/wchan")
        if wchan.exists():
            evidence["wchan"] = wchan.read_text().strip()
    except Exception:
        pass
    return evidence


def read_tail(file_path: Path, max_bytes: int = 4096) -> str:
    """Read trailing bytes from a file safely in binary mode."""
    if not file_path.exists():
        return ""
    try:
        size = file_path.stat().st_size
        with open(file_path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read()
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def read_tail_lines(file_path: Path, count: int = 5) -> list:
    """Read last N lines from a file."""
    content = read_tail(file_path, max_bytes=8192)
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return lines[-count:] if lines else []


def save_state(path: Path, data: dict):
    """Atomically write state dict to file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def compute_config_key(config_dict: dict) -> str:
    """Compute sha256 hash of canonical sorted JSON."""
    return hashlib.sha256(
        json.dumps(config_dict, sort_keys=True).encode("utf-8")
    ).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Progress-aware acceptance test runner"
    )
    parser.add_argument(
        "--cwd", default=".", help="Working directory for test suite"
    )
    parser.add_argument(
        "--head", required=True, help="Pinned SHA for git commit/head"
    )
    parser.add_argument(
        "--suite", default="discover -s tests", help="Unittest suite arguments"
    )
    parser.add_argument(
        "--state-dir",
        default=".progress_runner",
        help="State directory for progress tracking and logs",
    )
    parser.add_argument(
        "--soft-seconds",
        type=float,
        default=840.0,
        help="Soft checkpoint budget in seconds (default: 840)",
    )
    parser.add_argument(
        "--checkpoint-seconds",
        type=float,
        default=300.0,
        help="Checkpoint interval in seconds (default: 300)",
    )
    parser.add_argument(
        "--hard-seconds",
        type=float,
        default=2400.0,
        help="Absolute hard deadline in seconds (default: 2400)",
    )
    parser.add_argument(
        "--item-seconds",
        type=float,
        default=None,
        help="Optional explicit budget for a single test item",
    )
    parser.add_argument(
        "--stall-seconds",
        type=float,
        default=None,
        help="Optional inactivity budget before suspecting stall",
    )
    parser.add_argument(
        "--out", default=None, help="Path to write final JSON report"
    )
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)

    config_dict = {
        "checkpoint_seconds": float(args.checkpoint_seconds),
        "cwd": str(Path(args.cwd).resolve()),
        "hard_seconds": float(args.hard_seconds),
        "head": str(args.head).strip(),
        "item_seconds": (
            float(args.item_seconds) if args.item_seconds is not None else None
        ),
        "soft_seconds": float(args.soft_seconds),
        "stall_seconds": (
            float(args.stall_seconds)
            if args.stall_seconds is not None
            else None
        ),
        "suite": str(args.suite).strip(),
    }
    config_key = compute_config_key(config_dict)

    lock_path = state_dir / "runner.lock"
    state_path = state_dir / "state.json"
    stdout_path = state_dir / "stdout.log"
    stderr_path = state_dir / "stderr.log"
    events_path = state_dir / "events.jsonl"

    lock_file = open(lock_path, "a+")

    # Try non-blocking flock
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked_by_other = False
    except (BlockingIOError, OSError):
        locked_by_other = True

    if locked_by_other:
        # Check if existing runner has the exact same configuration key
        if state_path.exists():
            try:
                existing_state = json.loads(state_path.read_text())
            except Exception:
                existing_state = {}
            if existing_state.get("config_key") != config_key:
                sys.stderr.write(
                    f"Conflict: state-dir {state_dir} is locked by another runner with a different configuration.\n"
                )
                return EXIT_FAIL
        # Concurrent same key reuses existing handle: wait for completion
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if state_path.exists():
            result = json.loads(state_path.read_text())
            if result.get("config_key") != config_key:
                sys.stderr.write(
                    f"Conflict: state-dir {state_dir} completed with different configuration key.\n"
                )
                return EXIT_FAIL
            if args.out:
                Path(args.out).write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
            return result.get("exit_code", EXIT_PASS)
        return EXIT_FAIL

    remaining_hard_seconds = args.hard_seconds

    # Lock acquired; check existing state in state_dir
    if state_path.exists():
        try:
            existing_state = json.loads(state_path.read_text())
        except Exception:
            existing_state = {}

        if existing_state.get("config_key") == config_key:
            existing_status = existing_state.get("status")
            if existing_status in (
                "PASS",
                "FAIL",
                "TIMEOUT",
                "STALL",
                "STALL_SUSPECTED",
                "CANCELLED",
            ):
                # Completed exact-key result reused
                if args.out:
                    Path(args.out).write_text(
                        json.dumps(existing_state, indent=2)
                    )
                print(json.dumps(existing_state, indent=2))
                return existing_state.get("exit_code", EXIT_PASS)

            if existing_status == "RUNNING":
                old_child_pid = existing_state.get("child_pid")
                old_child_ident = existing_state.get("child_identity")
                old_owner_pid = existing_state.get("owner_pid")
                old_owner_ident = existing_state.get("owner_identity")

                if (old_child_pid and is_proc_alive(old_child_pid, old_child_ident)) or (
                    old_owner_pid and is_proc_alive(old_owner_pid, old_owner_ident)
                ):
                    sys.stderr.write(
                        "Fail closed: previous child process is still live (uncertain orphan).\n"
                    )
                    return EXIT_FAIL

                # Dead orphan: retain run elapsed deadline, never reset clock
                prev_start_epoch = existing_state.get(
                    "start_time_epoch", time.time()
                )
                elapsed_since_start = time.time() - prev_start_epoch
                if elapsed_since_start >= args.hard_seconds:
                    existing_state["status"] = "TIMEOUT"
                    existing_state["verdict"] = "TIMEOUT"
                    existing_state["exit_code"] = EXIT_TIMEOUT
                    save_state(state_path, existing_state)
                    if args.out:
                        Path(args.out).write_text(
                            json.dumps(existing_state, indent=2)
                        )
                    print(json.dumps(existing_state, indent=2))
                    return EXIT_TIMEOUT
                remaining_hard_seconds = max(
                    0.1, args.hard_seconds - elapsed_since_start
                )
        else:
            # Changed head/suite/budget never silently reuses
            old_child_pid = existing_state.get("child_pid")
            old_child_ident = existing_state.get("child_identity")
            if old_child_pid and is_proc_alive(old_child_pid, old_child_ident):
                sys.stderr.write(
                    "Fail closed: previous child process from different configuration still running.\n"
                )
                return EXIT_FAIL

    # Prepare fresh execution logs
    events_path.unlink(missing_ok=True)
    stdout_path.unlink(missing_ok=True)
    stderr_path.unlink(missing_ok=True)

    cancel_event = threading.Event()

    def handle_signal(signum, frame):
        cancel_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    worker_script = (
        Path(__file__).resolve().with_name("unittest_worker.py")
    )
    worker_cmd = [
        sys.executable,
        "-u",
        str(worker_script),
        "--suite",
        args.suite,
        "--events",
        str(events_path),
        "--cwd",
        str(Path(args.cwd).resolve()),
    ]

    stdout_f = open(stdout_path, "ab")
    stderr_f = open(stderr_path, "ab")

    start_monotonic = time.monotonic()
    start_epoch = time.time()
    hard_deadline = start_monotonic + remaining_hard_seconds
    observation_deadline = start_monotonic + args.soft_seconds

    child_proc = subprocess.Popen(
        worker_cmd,
        cwd=str(Path(args.cwd).resolve()),
        stdout=stdout_f,
        stderr=stderr_f,
        preexec_fn=os.setsid,
    )
    child_pid = child_proc.pid
    child_ident = get_proc_identity(child_pid)
    owner_pid = os.getpid()
    owner_ident = get_proc_identity(owner_pid)

    current_state = {
        "head": str(args.head).strip(),
        "suite": str(args.suite).strip(),
        "cwd": str(Path(args.cwd).resolve()),
        "config_key": config_key,
        "config": config_dict,
        "owner_pid": owner_pid,
        "owner_identity": owner_ident,
        "child_pid": child_pid,
        "child_identity": child_ident,
        "status": "RUNNING",
        "verdict": "RUNNING",
        "start_time_epoch": start_epoch,
        "start_monotonic": start_monotonic,
        "last_progress_monotonic": start_monotonic,
        "last_completion_time": None,
        "soft_seconds": args.soft_seconds,
        "checkpoint_seconds": args.checkpoint_seconds,
        "hard_seconds": args.hard_seconds,
        "item_seconds": args.item_seconds,
        "stall_seconds": args.stall_seconds,
        "checkpoints": [],
        "discovered_ids": [],
        "completed_ids": [],
        "last_completed_id": None,
        "current_id": None,
        "diagnostics": {},
    }
    save_state(state_path, current_state)

    discovered_ids = []
    discovered_event_seen = False
    completed_tests = {}
    current_test_id = None
    current_test_start = None
    last_completed_id = None
    last_progress_monotonic = start_monotonic
    last_checkpoint_completed_count = 0
    checkpoints = []
    diagnostics = {}
    final_status = None
    events_pos = 0
    suite_completed_event = None
    event_reader_errors = []

    def process_new_events():
        nonlocal events_pos, discovered_ids, discovered_event_seen
        nonlocal completed_tests, current_test_id, current_test_start
        nonlocal last_completed_id, last_progress_monotonic
        nonlocal suite_completed_event, event_reader_errors
        if not events_path.exists():
            return
        state_updated = False
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                f.seek(events_pos)
                while True:
                    line_start = f.tell()
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        f.seek(line_start)
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception as json_err:
                        event_reader_errors.append(
                            f"malformed_event_line: {json_err}"
                        )
                        continue

                    ev_type = ev.get("event")
                    if ev_type == "discovered":
                        discovered_event_seen = True
                        discovered_ids = ev.get("ids", [])
                        last_progress_monotonic = time.monotonic()
                        current_state["discovered_ids"] = list(discovered_ids)
                        current_state["last_progress_monotonic"] = last_progress_monotonic
                        state_updated = True
                    elif ev_type == "start":
                        current_test_id = ev.get("id")
                        current_test_start = ev.get(
                            "monotonic", time.monotonic()
                        )
                        last_progress_monotonic = time.monotonic()
                        current_state["current_id"] = current_test_id
                        current_state["last_progress_monotonic"] = last_progress_monotonic
                        state_updated = True
                    elif ev_type == "end":
                        tid = ev.get("id")
                        # Preserve failure result if duplicate ID occurred
                        prev_res = completed_tests.get(tid, {}).get("result")
                        if (
                            prev_res in ("FAIL", "ERROR")
                            and ev.get("result") == "PASS"
                        ):
                            pass  # Retain prior failure
                        else:
                            completed_tests[tid] = ev
                        last_completed_id = tid
                        if current_test_id == tid:
                            current_test_id = None
                            current_test_start = None
                        last_progress_monotonic = time.monotonic()
                        current_state["last_completed_id"] = last_completed_id
                        current_state["last_completion_time"] = ev.get(
                            "time", time.time()
                        )
                        current_state["completed_ids"] = list(
                            completed_tests.keys()
                        )
                        current_state["current_id"] = current_test_id
                        current_state["last_progress_monotonic"] = last_progress_monotonic
                        state_updated = True
                    elif ev_type == "subtest":
                        if ev.get("result") == "FAIL":
                            tid = ev.get("id")
                            if tid in completed_tests:
                                completed_tests[tid]["result"] = "FAIL"
                    elif ev_type == "suite_completed":
                        suite_completed_event = ev
                        last_progress_monotonic = time.monotonic()
                    elif ev_type == "load_error":
                        event_reader_errors.append(
                            f"worker_load_error: {ev.get('error')}"
                        )
                events_pos = f.tell()
        except Exception as read_err:
            event_reader_errors.append(f"event_read_exception: {read_err}")

        if state_updated:
            save_state(state_path, current_state)

    # Main monitoring loop
    while True:
        process_new_events()

        if cancel_event.is_set():
            final_status = "CANCELLED"
            diagnostics["reason"] = "cancelled_by_signal"
            break

        if child_proc.poll() is not None:
            # Child exited naturally
            break

        now = time.monotonic()

        # Hard suite deadline: absolute upper bound
        if now >= hard_deadline:
            gather_stack_trace(child_pid)
            final_status = "TIMEOUT"
            diagnostics["reason"] = "hard_deadline_exceeded"
            diagnostics["elapsed_seconds"] = round(now - start_monotonic, 3)
            diagnostics["hard_seconds"] = args.hard_seconds
            diagnostics["current_test"] = current_test_id
            diagnostics["wait_evidence"] = get_wait_evidence(child_pid)
            diagnostics["stack"] = read_tail(stderr_path)
            break

        # Explicit per-item budget: only terminates when explicit limit is given
        if (
            args.item_seconds is not None
            and current_test_id
            and current_test_start
        ):
            elapsed_in_test = now - current_test_start
            if elapsed_in_test > args.item_seconds:
                gather_stack_trace(child_pid)
                final_status = "STALL"
                diagnostics["reason"] = (
                    f"item_budget_exceeded:{current_test_id}"
                )
                diagnostics["current_test"] = current_test_id
                diagnostics["elapsed_in_test"] = round(elapsed_in_test, 3)
                diagnostics["item_seconds"] = args.item_seconds
                diagnostics["wait_evidence"] = get_wait_evidence(child_pid)
                diagnostics["stack"] = read_tail(stderr_path)
                break

        # Inactivity stall observation: observational only, does not terminate
        if args.stall_seconds is not None:
            elapsed_since_progress = now - last_progress_monotonic
            if elapsed_since_progress > args.stall_seconds:
                gather_stack_trace(child_pid)
                diagnostics["reason"] = "inactivity_stall_suspected"
                diagnostics["current_test"] = current_test_id
                diagnostics["elapsed_inactive"] = round(
                    elapsed_since_progress, 3
                )
                diagnostics["stall_seconds"] = args.stall_seconds
                diagnostics["wait_evidence"] = get_wait_evidence(child_pid)
                diagnostics["stack"] = read_tail(stderr_path)

        # Soft checkpoint observation: does NOT kill progressing or indeterminate tests
        if now >= observation_deadline:
            curr_completed = len(completed_tests)
            old_obs = observation_deadline - start_monotonic
            observation_deadline = now + args.checkpoint_seconds
            new_obs = observation_deadline - start_monotonic

            if curr_completed > last_checkpoint_completed_count:
                reason = "progress_observed"
            elif (
                current_test_id
                and args.item_seconds
                and current_test_start
                and (now - current_test_start) <= args.item_seconds
            ):
                reason = "legitimate_long_test_within_budget"
            else:
                # Observational suspicion only: capture evidence, keep SAME child running
                gather_stack_trace(child_pid)
                reason = "stall_suspected"
                diagnostics["reason"] = "no_progress_at_checkpoint"
                diagnostics["current_test"] = current_test_id
                diagnostics["elapsed_in_test"] = round(
                    now - (current_test_start or now), 3
                )
                diagnostics["wait_evidence"] = get_wait_evidence(child_pid)
                diagnostics["stack"] = read_tail(stderr_path)

            chk = {
                "checkpoint_number": len(checkpoints) + 1,
                "reason": reason,
                "old_observation_deadline": round(old_obs, 3),
                "new_observation_deadline": round(new_obs, 3),
                "child_pid": child_pid,
                "child_identity": child_ident,
                "last_completed_id": last_completed_id,
                "current_id": current_test_id,
                "completed_count": curr_completed,
                "discovered_count": len(discovered_ids),
                "elapsed_seconds": round(now - start_monotonic, 3),
                "hard_deadline_remaining": round(
                    max(0.0, hard_deadline - now), 3
                ),
            }
            if reason == "stall_suspected":
                chk["wait_evidence"] = diagnostics.get("wait_evidence", {})
                chk["current_test"] = current_test_id
                chk["stack"] = diagnostics.get("stack", "")

            checkpoints.append(chk)
            last_checkpoint_completed_count = curr_completed
            current_state["checkpoints"] = checkpoints
            current_state["last_completed_id"] = last_completed_id
            current_state["current_id"] = current_test_id
            current_state["completed_ids"] = list(completed_tests.keys())
            current_state["diagnostics"] = diagnostics
            save_state(state_path, current_state)

        time.sleep(0.05)

    # If stopped due to timeout/stall/cancel, terminate the process group cleanly
    if final_status is not None or child_proc.poll() is None:
        stop_process_group(child_pid, proc=child_proc)

    stdout_f.close()
    stderr_f.close()

    # Ingest any remaining lifecycle events emitted before termination
    process_new_events()

    # Reconcile test IDs and determine final verdict
    if final_status is None:
        child_rc = child_proc.returncode
        discovered_set = set(discovered_ids)
        completed_set = set(completed_tests.keys())
        missing_ids = discovered_set - completed_set

        # Empty/no-lifecycle successful child is NOT enough to PASS;
        # require discovered and end-of-suite lifecycle plus completed ID reconciliation.
        if event_reader_errors:
            final_status = "FAIL"
            diagnostics["event_reader_errors"] = event_reader_errors
        elif not discovered_event_seen or not discovered_ids:
            final_status = "FAIL"
            diagnostics["reconciliation"] = {
                "error": "no_tests_discovered_or_no_lifecycle",
                "discovered_count": len(discovered_ids),
                "completed_count": len(completed_tests),
            }
        elif suite_completed_event is None:
            final_status = "FAIL"
            diagnostics["reconciliation"] = {
                "error": "missing_suite_completed_lifecycle",
                "discovered_count": len(discovered_ids),
                "completed_count": len(completed_tests),
            }
        elif missing_ids:
            final_status = "FAIL"
            diagnostics["reconciliation"] = {
                "error": "incomplete_execution",
                "discovered_count": len(discovered_ids),
                "completed_count": len(completed_tests),
                "missing_ids": sorted(list(missing_ids)),
            }
        else:
            any_fail = any(
                ev.get("result") in ("FAIL", "ERROR", "UNEXPECTED_SUCCESS")
                for ev in completed_tests.values()
            )
            suite_not_successful = (
                suite_completed_event is not None
                and not suite_completed_event.get("was_successful", True)
            )
            if any_fail or child_rc != 0 or suite_not_successful:
                final_status = "FAIL"
            else:
                final_status = "PASS"

    if final_status == "PASS":
        exit_code = EXIT_PASS
    elif final_status == "TIMEOUT":
        exit_code = EXIT_TIMEOUT
    elif final_status in ("STALL", "STALL_SUSPECTED"):
        exit_code = EXIT_STALL
    elif final_status == "CANCELLED":
        exit_code = EXIT_CANCELLED
    else:
        exit_code = EXIT_FAIL

    passed_count = sum(
        1 for ev in completed_tests.values() if ev.get("result") == "PASS"
    )
    failed_count = sum(
        1
        for ev in completed_tests.values()
        if ev.get("result") in ("FAIL", "ERROR", "UNEXPECTED_SUCCESS")
    )
    skipped_count = sum(
        1 for ev in completed_tests.values() if ev.get("result") == "SKIP"
    )

    skipped_details = [
        {"id": k, "reason": ev.get("message")}
        for k, ev in completed_tests.items()
        if ev.get("result") == "SKIP"
    ]

    tail_lines = read_tail_lines(stderr_path, 5) or read_tail_lines(
        stdout_path, 5
    )

    total_seconds = round(time.monotonic() - start_monotonic, 3)

    result_report = {
        "status": final_status,
        "verdict": final_status,
        "exit_code": exit_code,
        "head": str(args.head).strip(),
        "suite": str(args.suite).strip(),
        "cwd": str(Path(args.cwd).resolve()),
        "total_seconds": total_seconds,
        "discovered_count": len(discovered_ids),
        "completed_count": len(completed_tests),
        "discovered_ids": list(discovered_ids),
        "completed_ids": list(completed_tests.keys()),
        "last_completed_id": last_completed_id,
        "current_id": current_test_id,
        "tests": {
            "ran": len(completed_tests),
            "passed": passed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "ok": final_status == "PASS",
            "tail": tail_lines,
        },
        "tests_ok": final_status == "PASS",
        "checkpoints": checkpoints,
        "diagnostics": diagnostics,
        "skipped": skipped_details,
        "child_pid": child_pid,
        "child_identity": child_ident,
        "owner_pid": owner_pid,
        "owner_identity": owner_ident,
        "config_key": config_key,
    }

    save_state(state_path, result_report)

    out_json = json.dumps(result_report, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(out_json + "\n")
    print(out_json)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
