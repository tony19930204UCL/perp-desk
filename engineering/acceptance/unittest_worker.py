#!/usr/bin/env python3
"""Unbuffered lifecycle-aware unittest worker for progress-aware acceptance.

Discovers unittest suites, emits real-time JSONL lifecycle events (flush & fsync)
with monotonic times and actual test outcomes, maintains raw unbuffered stdout/stderr,
enables faulthandler thread diagnostics on SIGUSR1/crash, and cleans up fixture descendants.
"""
import argparse
import atexit
import faulthandler
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import time
import unittest

# Ensure workspace root and current working directory are on sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


def setup_diagnostics():
    """Enable faulthandler on stderr and register SIGUSR1 for diagnostic stack dumping."""
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except Exception:
        pass
    try:
        faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)
    except Exception:
        pass

    # Unbuffered stream flushing
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(line_buffering=True)
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass


def find_descendants(pid: int) -> set:
    """Find all descendant process IDs under pid using Linux /proc."""
    descendants = set()
    to_check = [pid]
    while to_check:
        curr = to_check.pop(0)
        children_file = Path(f"/proc/{curr}/task/{curr}/children")
        if children_file.exists():
            try:
                kids = [int(x) for x in children_file.read_text().split()]
                for k in kids:
                    if k not in descendants and k != pid:
                        descendants.add(k)
                        to_check.append(k)
            except Exception:
                pass
    return descendants


def cleanup_descendants():
    """Terminate and clean up any remaining descendant processes spawned by fixtures."""
    pid = os.getpid()
    descendants = find_descendants(pid)
    for p in descendants:
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    if descendants:
        time.sleep(0.05)
        for p in descendants:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


atexit.register(cleanup_descendants)


def emit_event(events_file_path: Path, event_dict: dict):
    """Write an event JSONL line and fsync to disk immediately."""
    line = json.dumps(event_dict, ensure_ascii=False) + "\n"
    try:
        with open(events_file_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except Exception as e:
        sys.stderr.write(f"Failed to emit lifecycle event: {e}\n")
        sys.stderr.flush()


def flatten_suite(suite):
    """Recursively collect individual TestCase instances from a TestSuite."""
    tests = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            tests.extend(flatten_suite(item))
        else:
            tests.append(item)
    return tests


class LifecycleTestResult(unittest.TextTestResult):
    """TestResult that emits flush/fsync lifecycle events for each test and subtest."""

    def __init__(self, stream, descriptions, verbosity, events_path: Path):
        super().__init__(stream, descriptions, verbosity)
        self.events_path = events_path
        self._current_test = None
        self._current_start_monotonic = None
        self._current_result = "PASS"
        self._current_message = None

    def startTest(self, test):
        super().startTest(test)
        self._current_test = test
        self._current_start_monotonic = time.monotonic()
        self._current_result = "PASS"
        self._current_message = None
        emit_event(
            self.events_path,
            {
                "event": "start",
                "id": test.id(),
                "time": time.time(),
                "monotonic": self._current_start_monotonic,
                "pid": os.getpid(),
            },
        )

    def addSuccess(self, test):
        super().addSuccess(test)
        if self._current_result == "PASS":
            self._current_result = "PASS"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._current_result = "FAIL"
        self._current_message = self._format_err(err)

    def addError(self, test, err):
        super().addError(test, err)
        self._current_result = "ERROR"
        self._current_message = self._format_err(err)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._current_result = "SKIP"
        self._current_message = str(reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._current_result = "EXPECTED_FAILURE"
        self._current_message = self._format_err(err)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._current_result = "UNEXPECTED_SUCCESS"
        self._current_message = "Unexpected success"

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._current_result = "FAIL"
            formatted = self._format_err(err)
            if self._current_message is None:
                self._current_message = formatted
            else:
                self._current_message += "\n" + formatted
            emit_event(
                self.events_path,
                {
                    "event": "subtest",
                    "id": test.id(),
                    "subtest": str(subtest),
                    "result": "FAIL",
                    "message": formatted,
                    "time": time.time(),
                    "monotonic": time.monotonic(),
                    "pid": os.getpid(),
                },
            )

    def stopTest(self, test):
        super().stopTest(test)
        now_mono = time.monotonic()
        duration = now_mono - (self._current_start_monotonic or now_mono)
        emit_event(
            self.events_path,
            {
                "event": "end",
                "id": test.id(),
                "result": self._current_result,
                "duration": round(duration, 4),
                "message": self._current_message,
                "time": time.time(),
                "monotonic": now_mono,
                "pid": os.getpid(),
            },
        )

    def _format_err(self, err):
        import traceback

        exctype, value, tb = err
        return "".join(traceback.format_exception(exctype, value, tb))


class LifecycleTestRunner(unittest.TextTestRunner):
    """Custom TextTestRunner wired to LifecycleTestResult."""

    def __init__(self, events_path: Path, *args, **kwargs):
        self.events_path = events_path
        super().__init__(*args, **kwargs)

    def _makeResult(self):
        return LifecycleTestResult(
            self.stream, self.descriptions, self.verbosity, self.events_path
        )


def load_suite(loader: unittest.TestLoader, suite_str: str, cwd: Path):
    """Parse suite arguments and load unittest.TestSuite. Returns (suite, buffer_flag, failfast_flag)."""
    parts = shlex.split(suite_str)
    buffer_flag = False
    failfast_flag = False

    filtered = []
    for p in parts:
        if p in ("-b", "--buffer"):
            buffer_flag = True
        elif p in ("-f", "--failfast"):
            failfast_flag = True
        else:
            filtered.append(p)
    parts = filtered

    if not parts:
        return loader.discover(str(cwd)), buffer_flag, failfast_flag

    if parts[0] == "discover":
        parser = argparse.ArgumentParser(prog="discover")
        parser.add_argument(
            "-s", "--start-directory", dest="start_dir", default=str(cwd)
        )
        parser.add_argument("-p", "--pattern", dest="pattern", default="test*.py")
        parser.add_argument(
            "-t", "--top-level-directory", dest="top_level_dir", default=None
        )
        parser.add_argument("-v", "--verbose", action="store_true", default=False)
        parser.add_argument("-q", "--quiet", action="store_true", default=False)
        parser.add_argument("-f", "--failfast", action="store_true", default=False)
        parser.add_argument("-b", "--buffer", action="store_true", default=False)
        parser.add_argument("-k", dest="test_filter", default=None)

        args, unknown = parser.parse_known_args(parts[1:])
        if unknown:
            raise ValueError(
                f"Unsupported unittest discover argument(s): {' '.join(unknown)}"
            )

        if args.buffer:
            buffer_flag = True
        if args.failfast:
            failfast_flag = True

        start_dir = args.start_dir
        if not Path(start_dir).is_absolute():
            start_dir = str(cwd / start_dir)
        top_dir = args.top_level_dir
        if top_dir and not Path(top_dir).is_absolute():
            top_dir = str(cwd / top_dir)
        suite = loader.discover(
            start_dir=start_dir, pattern=args.pattern, top_level_dir=top_dir
        )
        return suite, buffer_flag, failfast_flag

    # For explicit suite names or file paths, reject unexpected options
    for p in parts:
        if p.startswith("-"):
            raise ValueError(f"Unsupported unittest argument: {p}")

    if len(parts) == 1 and parts[0].endswith(".py"):
        file_path = Path(parts[0])
        if not file_path.is_absolute():
            file_path = cwd / file_path
        suite = loader.discover(
            start_dir=str(file_path.parent), pattern=file_path.name
        )
        return suite, buffer_flag, failfast_flag

    try:
        suite = loader.loadTestsFromNames(parts)
        return suite, buffer_flag, failfast_flag
    except Exception:
        suite = unittest.TestSuite()
        for name in parts:
            suite.addTest(loader.loadTestsFromName(name))
        return suite, buffer_flag, failfast_flag


def main():
    setup_diagnostics()

    parser = argparse.ArgumentParser(description="Lifecycle-aware unittest worker")
    parser.add_argument(
        "--suite", required=True, help="Unittest suite arguments or module names"
    )
    parser.add_argument(
        "--events", required=True, help="Path to events.jsonl lifecycle log"
    )
    parser.add_argument(
        "--cwd", default=".", help="Working directory for test execution"
    )
    parser.add_argument(
        "--buffer",
        action="store_true",
        help="Buffer stdout and stderr during test execution",
    )
    parser.add_argument(
        "--failfast",
        action="store_true",
        help="Stop on first fail or error",
    )
    args = parser.parse_args()

    cwd_path = Path(args.cwd).resolve()
    if str(cwd_path) not in sys.path:
        sys.path.insert(0, str(cwd_path))
    os.chdir(str(cwd_path))

    events_path = Path(args.events).resolve()
    events_path.parent.mkdir(parents=True, exist_ok=True)

    loader = unittest.defaultTestLoader
    try:
        suite, suite_buffer, suite_failfast = load_suite(
            loader, args.suite, cwd_path
        )
    except Exception as exc:
        sys.stderr.write(f"Failed to load test suite: {exc}\n")
        sys.stderr.flush()
        emit_event(
            events_path,
            {
                "event": "load_error",
                "error": str(exc),
                "time": time.time(),
                "monotonic": time.monotonic(),
                "pid": os.getpid(),
            },
        )
        return 1

    all_tests = flatten_suite(suite)
    discovered_ids = [t.id() for t in all_tests]

    emit_event(
        events_path,
        {
            "event": "discovered",
            "count": len(discovered_ids),
            "ids": discovered_ids,
            "time": time.time(),
            "monotonic": time.monotonic(),
            "pid": os.getpid(),
        },
    )

    use_buffer = args.buffer or suite_buffer
    use_failfast = args.failfast or suite_failfast
    runner = LifecycleTestRunner(
        events_path=events_path,
        verbosity=2,
        buffer=use_buffer,
        failfast=use_failfast,
    )

    try:
        result = runner.run(suite)
    finally:
        cleanup_descendants()

    emit_event(
        events_path,
        {
            "event": "suite_completed",
            "ran": result.testsRun,
            "failures": len(result.failures),
            "errors": len(result.errors),
            "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
            "was_successful": result.wasSuccessful(),
            "time": time.time(),
            "monotonic": time.monotonic(),
            "pid": os.getpid(),
        },
    )

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
