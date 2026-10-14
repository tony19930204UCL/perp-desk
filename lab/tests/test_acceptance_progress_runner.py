"""Acceptance tests for progress-aware acceptance runner.

Covers real subprocess executions against bounded fixture suites:
- Slow progress passing across soft checkpoints with identical worker PID
- Legitimate long test within item budget surviving soft checkpoint
- Inactivity / sleeping stall diagnostics with thread stack dump
- Busy loop not mistaken for progress under explicit item budget
- Test output buffering without lifecycle suppression
- Zero stdout execution tracked via JSONL lifecycle
- Fixture descendant process cleanup
- Signal cancellation via SIGTERM (clean termination without zombie delay)
- Flock deduplication and completed exact-key result reuse
- Changed head rerunning rather than silently reusing
- Partial suite rejection (incomplete execution cannot pass)
- SKIP accounting distinct from pass and fail
- Real test failure detection and reporting
- Concurrent same key reuses active handle
- Regression: long test without stdout surviving soft checkpoint and completing cleanly
- Regression: long test without item budget exceeding hard deadline -> TIMEOUT with diagnostics
- Orphan recovery: SIGKILL owner while worker alive refuses duplicate launch and fails closed
- Empty suite or missing lifecycle events rejected from PASS
- Subtest failure lifecycle reporting
- Rejection of unsupported discover / unittest arguments
- Support and propagation of unittest --failfast option
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

LAB_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = LAB_DIR.parent
RUNNER = REPO_ROOT / "engineering/acceptance/progress_runner.py"


def make_sha(seed: str) -> str:
    """Assemble token-like hex SHA at runtime without embedding string literals."""
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


class AcceptanceProgressRunnerTests(unittest.TestCase):
    """Real subprocess acceptance tests for progress_runner."""

    def test_01_slow_progress_passes_across_soft_checkpoint_same_pid(self):
        """Soft checkpoint extends observation when progress occurs; worker PID remains identical."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-slow-progress"),
                "--suite",
                "engineering.acceptance.fixture_suites.SlowProgressSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "0.3",
                "--checkpoint-seconds",
                "0.25",
                "--hard-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}",
            )
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "PASS")
            self.assertEqual(data["verdict"], "PASS")
            self.assertGreaterEqual(
                len(data["checkpoints"]),
                1,
                "Expected at least one soft checkpoint extension",
            )
            worker_pid = data["child_pid"]
            self.assertIsNotNone(worker_pid)
            for chk in data["checkpoints"]:
                self.assertEqual(
                    chk["child_pid"],
                    worker_pid,
                    "PID must remain identical across checkpoints",
                )
                self.assertEqual(chk["reason"], "progress_observed")

    def test_02_legitimate_long_single_test_within_budget(self):
        """A long single test stays within item-seconds budget and is not killed by soft checkpoint."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-legit-long"),
                "--suite",
                "engineering.acceptance.fixture_suites.LegitimateLongSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "0.2",
                "--checkpoint-seconds",
                "0.2",
                "--item-seconds",
                "1.0",
                "--hard-seconds",
                "4.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}",
            )
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "PASS")
            self.assertEqual(data["tests"]["ran"], 1)
            self.assertEqual(data["tests"]["passed"], 1)
            self.assertGreaterEqual(len(data["checkpoints"]), 1)
            self.assertEqual(
                data["checkpoints"][0]["reason"],
                "legitimate_long_test_within_budget",
            )

    def test_03_sleeping_stall_diagnostics(self):
        """A test stalling due to sleep triggers STALL with stack and wait evidence."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-sleeping-stall"),
                "--suite",
                "engineering.acceptance.fixture_suites.SleepingStallSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--item-seconds",
                "0.3",
                "--hard-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 3)  # EXIT_STALL
            data = json.loads(out_file.read_text())
            self.assertIn(data["status"], ("STALL", "STALL_SUSPECTED"))
            self.assertIn(
                "SleepingStallSuite",
                data["diagnostics"].get("current_test", ""),
            )
            self.assertTrue(
                "sleep" in data["diagnostics"].get("stack", "").lower()
                or "wait_evidence" in data["diagnostics"]
                or "elapsed_in_test" in data["diagnostics"]
            )

    def test_04_busy_loop_not_mistaken_for_progress(self):
        """High CPU alone is not mistaken for progress and terminates with STALL under explicit item budget."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-busy-loop"),
                "--suite",
                "engineering.acceptance.fixture_suites.BusyLoopSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--item-seconds",
                "0.3",
                "--hard-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 3)  # EXIT_STALL
            data = json.loads(out_file.read_text())
            self.assertIn(data["status"], ("STALL", "STALL_SUSPECTED"))
            self.assertIn(
                "BusyLoopSuite",
                data["diagnostics"].get("current_test", ""),
            )
            self.assertEqual(data["tests"]["passed"], 0)

    def test_05_output_buffer(self):
        """Unittest buffering does not suppress JSONL lifecycle event delivery."""
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-output-buffer"),
                "--suite",
                "engineering.acceptance.fixture_suites.OutputBufferSuite -b",
                "--state-dir",
                str(state_dir),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 0)
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "PASS")
            events_file = state_dir / "events.jsonl"
            self.assertTrue(events_file.exists())
            events = [
                json.loads(l)
                for l in events_file.read_text().splitlines()
                if l.strip()
            ]
            types = [e["event"] for e in events]
            self.assertIn("discovered", types)
            self.assertIn("start", types)
            self.assertIn("end", types)
            self.assertIn("suite_completed", types)

    def test_06_no_stdout_with_lifecycle(self):
        """Silent test suites write zero stdout but progress is fully verified via lifecycle."""
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-no-stdout"),
                "--suite",
                "engineering.acceptance.fixture_suites.NoStdoutSuite",
                "--state-dir",
                str(state_dir),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 0)
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "PASS")
            self.assertEqual(data["completed_count"], 2)
            self.assertEqual(data["tests"]["passed"], 2)

    def test_07_descendant_cleanup(self):
        """Descendant background processes spawned by fixtures are terminated."""
        with tempfile.TemporaryDirectory() as td:
            pid_file = Path(td) / "descendant.pid"
            env = os.environ.copy()
            env["DESCENDANT_PID_FILE"] = str(pid_file)
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-descendants"),
                "--suite",
                "engineering.acceptance.fixture_suites.DescendantSpawningSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, env=env, timeout=10
            )
            self.assertEqual(proc.returncode, 0)
            self.assertTrue(pid_file.exists())
            descendant_pid = int(pid_file.read_text().strip())
            time.sleep(0.1)
            self.assertFalse(
                Path(f"/proc/{descendant_pid}").exists(),
                f"Descendant process {descendant_pid} survived test completion",
            )

    def test_08_cancel_via_sigterm(self):
        """SIGTERM cleanly cancels runner and owned child process group without zombie polling delay."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-cancel"),
                "--suite",
                "engineering.acceptance.fixture_suites.LongRunningSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "10.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            state_file = Path(td) / "state/state.json"
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if state_file.exists():
                    try:
                        st = json.loads(state_file.read_text())
                        if st.get("child_pid"):
                            break
                    except Exception:
                        pass
                time.sleep(0.02)
            time.sleep(0.05)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=5)
            self.assertEqual(
                proc.returncode,
                4,
                f"Expected EXIT_CANCELLED (4), got {proc.returncode}\nstdout: {stdout}\nstderr: {stderr}",
            )
            self.assertTrue(out_file.exists())
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "CANCELLED")
            self.assertEqual(data["verdict"], "CANCELLED")
            child_pid = data.get("child_pid")
            if child_pid:
                self.assertFalse(Path(f"/proc/{child_pid}").exists())

    def test_09_restart_and_dedupe(self):
        """Identical invocation reuses completed result without re-executing."""
        with tempfile.TemporaryDirectory() as td:
            head_val = make_sha("head-dedupe")
            state_dir = Path(td) / "state"
            out_1 = Path(td) / "out1.json"
            out_2 = Path(td) / "out2.json"
            cmd_base = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                head_val,
                "--suite",
                "engineering.acceptance.fixture_suites.QuickPassSuite",
                "--state-dir",
                str(state_dir),
                "--soft-seconds",
                "5.0",
            ]
            proc1 = subprocess.run(
                cmd_base + ["--out", str(out_1)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc1.returncode, 0)
            data1 = json.loads(out_1.read_text())
            self.assertEqual(data1["status"], "PASS")

            t0 = time.monotonic()
            proc2 = subprocess.run(
                cmd_base + ["--out", str(out_2)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            elapsed = time.monotonic() - t0
            self.assertEqual(proc2.returncode, 0)
            data2 = json.loads(out_2.read_text())
            self.assertEqual(data2["status"], "PASS")
            self.assertEqual(data1["config_key"], data2["config_key"])
            self.assertEqual(data1["child_pid"], data2["child_pid"])
            self.assertLess(
                elapsed,
                1.5,
                "Result reuse must be fast without re-running test suite",
            )

    def test_10_changed_head_reruns(self):
        """Changed pinned head SHA causes a fresh rerun and does not silently reuse old state."""
        with tempfile.TemporaryDirectory() as td:
            head_a = make_sha("head-alpha")
            head_b = make_sha("head-beta")
            state_dir = Path(td) / "state"
            out_a = Path(td) / "out_a.json"
            out_b = Path(td) / "out_b.json"

            cmd_a = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                head_a,
                "--suite",
                "engineering.acceptance.fixture_suites.QuickPassSuite",
                "--state-dir",
                str(state_dir),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_a),
            ]
            proc_a = subprocess.run(
                cmd_a, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc_a.returncode, 0)
            data_a = json.loads(out_a.read_text())

            cmd_b = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                head_b,
                "--suite",
                "engineering.acceptance.fixture_suites.QuickPassSuite",
                "--state-dir",
                str(state_dir),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_b),
            ]
            proc_b = subprocess.run(
                cmd_b, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc_b.returncode, 0)
            data_b = json.loads(out_b.read_text())

            self.assertNotEqual(data_a["config_key"], data_b["config_key"])
            self.assertEqual(data_b["head"], head_b)
            self.assertNotEqual(data_a["child_pid"], data_b["child_pid"])

    def test_11_partial_suite_cannot_pass(self):
        """If a test process terminates prematurely, discovered vs completed ID mismatch rejects PASS."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-partial-suite"),
                "--suite",
                "engineering.acceptance.fixture_suites.PartialSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 1)  # EXIT_FAIL
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "FAIL")
            self.assertEqual(data["verdict"], "FAIL")
            self.assertFalse(data.get("tests_ok", True))
            self.assertEqual(data["discovered_count"], 2)
            self.assertEqual(data["completed_count"], 0)
            recon = data.get("diagnostics", {}).get("reconciliation", {})
            self.assertIn(
                recon.get("error"),
                ("missing_suite_completed_lifecycle", "incomplete_execution"),
            )
            self.assertEqual(recon.get("discovered_count"), 2)
            self.assertEqual(recon.get("completed_count"), 0)

    def test_12_skip_distinct(self):
        """Skipped tests are accounted for distinctly from pass and fail."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-skip-distinct"),
                "--suite",
                "engineering.acceptance.fixture_suites.SkipSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 0)
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "PASS")
            self.assertEqual(data["tests"]["skipped"], 1)
            self.assertEqual(data["tests"]["passed"], 1)
            self.assertEqual(len(data["skipped"]), 1)
            self.assertIn("demonstrating distinct", data["skipped"][0]["reason"])

    def test_13_real_test_failure(self):
        """Assertion failures in tests are detected and reported with exit code 1."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-real-failure"),
                "--suite",
                "engineering.acceptance.fixture_suites.RealFailureSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 1)  # EXIT_FAIL
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "FAIL")
            self.assertEqual(data["verdict"], "FAIL")
            self.assertGreaterEqual(data["tests"]["failed"], 1)

    def test_14_concurrent_same_key_reuses_handle(self):
        """Concurrent invocation with identical configuration key waits and reuses the active handle."""
        with tempfile.TemporaryDirectory() as td:
            head_val = make_sha("head-concurrent")
            state_dir = Path(td) / "state"
            out_1 = Path(td) / "out1.json"
            out_2 = Path(td) / "out2.json"
            cmd_base = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                head_val,
                "--suite",
                "engineering.acceptance.fixture_suites.SlowProgressSuite",
                "--state-dir",
                str(state_dir),
                "--soft-seconds",
                "5.0",
            ]
            p1 = subprocess.Popen(
                cmd_base + ["--out", str(out_1)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.08)
            p2 = subprocess.Popen(
                cmd_base + ["--out", str(out_2)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            out1_text, err1_text = p1.communicate(timeout=10)
            out2_text, err2_text = p2.communicate(timeout=10)
            self.assertEqual(p1.returncode, 0, f"p1 stderr: {err1_text}")
            self.assertEqual(p2.returncode, 0, f"p2 stderr: {err2_text}")
            d1 = json.loads(out_1.read_text())
            d2 = json.loads(out_2.read_text())
            self.assertEqual(d1["status"], "PASS")
            self.assertEqual(d2["status"], "PASS")
            self.assertEqual(d1["config_key"], d2["config_key"])
            self.assertEqual(d1["child_pid"], d2["child_pid"])

    def test_15_uncertainty_no_stdout_long_item_survives_soft_then_completes(self):
        """Long test with no stdout and no item limit survives soft checkpoint, records observation, and PASSes."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-silent-survivor"),
                "--suite",
                "engineering.acceptance.fixture_suites.LongSilentTestSurvivesSoftSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "0.2",
                "--checkpoint-seconds",
                "0.2",
                "--hard-seconds",
                "4.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}",
            )
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "PASS")
            self.assertEqual(data["tests"]["passed"], 1)
            self.assertGreaterEqual(
                len(data["checkpoints"]),
                1,
                "Expected at least one soft checkpoint observation",
            )
            self.assertEqual(
                data["checkpoints"][0]["reason"], "stall_suspected"
            )
            # Child PID remains constant throughout
            worker_pid = data["child_pid"]
            self.assertEqual(data["checkpoints"][0]["child_pid"], worker_pid)

    def test_16_uncertainty_beyond_hard_results_in_timeout_with_diagnostics(self):
        """Test with no item budget exceeding hard suite deadline results in TIMEOUT with diagnostics."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-hard-timeout"),
                "--suite",
                "engineering.acceptance.fixture_suites.LongRunningSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "0.2",
                "--checkpoint-seconds",
                "0.2",
                "--hard-seconds",
                "0.6",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 2)  # EXIT_TIMEOUT
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "TIMEOUT")
            self.assertEqual(data["verdict"], "TIMEOUT")
            self.assertEqual(
                data["diagnostics"].get("reason"), "hard_deadline_exceeded"
            )
            self.assertGreaterEqual(len(data["checkpoints"]), 1)
            self.assertIn("current_test", data["diagnostics"])

    def test_17_sigkill_owner_orphan_refuses_duplicate(self):
        """SIGKILL owner while worker is alive causes subsequent runner to detect orphan and fail closed."""
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            head_val = make_sha("head-orphan-refuse")
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                head_val,
                "--suite",
                "engineering.acceptance.fixture_suites.LongRunningSuite",
                "--state-dir",
                str(state_dir),
                "--soft-seconds",
                "10.0",
                "--hard-seconds",
                "20.0",
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            state_file = state_dir / "state.json"
            deadline = time.monotonic() + 5.0
            child_pid = None
            while time.monotonic() < deadline:
                if state_file.exists():
                    try:
                        st = json.loads(state_file.read_text())
                        child_pid = st.get("child_pid")
                        if child_pid and Path(f"/proc/{child_pid}").exists():
                            break
                    except Exception:
                        pass
                time.sleep(0.05)

            self.assertIsNotNone(child_pid, "Worker child_pid was not recorded")
            # SIGKILL the owner process while the worker child continues running in its own session
            proc.kill()
            proc.wait(timeout=2.0)

            try:
                self.assertTrue(
                    Path(f"/proc/{child_pid}").exists(),
                    "Worker child should still be alive after owner SIGKILL",
                )

                # Attempt duplicate launch while orphan worker is still running
                proc_dup = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(
                    proc_dup.returncode,
                    1,
                    f"Duplicate runner must fail closed (exit 1), got {proc_dup.returncode}",
                )
                self.assertIn(
                    "orphan",
                    proc_dup.stderr.lower(),
                    f"Expected orphan warning in stderr: {proc_dup.stderr}",
                )
            finally:
                # Clean up orphaned child process
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except OSError:
                    pass

    def test_18_empty_or_no_lifecycle_suite_rejected(self):
        """Empty test suite with no discovered tests cannot PASS; fails closed."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-empty-suite"),
                "--suite",
                "engineering.acceptance.fixture_suites.EmptySuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 1)  # EXIT_FAIL
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "FAIL")
            self.assertIn(
                "no_tests_discovered",
                str(data["diagnostics"].get("reconciliation", {})),
            )

    def test_19_subtest_failure_lifecycle(self):
        """Failing subtest within a test method is tracked and reported as FAIL."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-subtest-fail"),
                "--suite",
                "engineering.acceptance.fixture_suites.SubtestFailureSuite",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 1)  # EXIT_FAIL
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "FAIL")
            self.assertEqual(data["verdict"], "FAIL")
            self.assertGreaterEqual(data["tests"]["failed"], 1)

    def test_20_unsupported_unittest_args_rejected(self):
        """Unsupported command line options for discover are rejected with exit code 1."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-unsupported-args"),
                "--suite",
                "discover --bogus-unsupported-option",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 1)  # EXIT_FAIL
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "FAIL")

    def test_21_failfast_option_respected(self):
        """Unittest -f / --failfast stops execution immediately on the first failure."""
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out.json"
            cmd = [
                sys.executable,
                str(RUNNER),
                "--cwd",
                str(REPO_ROOT),
                "--head",
                make_sha("head-failfast"),
                "--suite",
                "engineering.acceptance.fixture_suites.FailfastSuite -f",
                "--state-dir",
                str(Path(td) / "state"),
                "--soft-seconds",
                "5.0",
                "--out",
                str(out_file),
            ]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10
            )
            self.assertEqual(proc.returncode, 1)  # EXIT_FAIL
            data = json.loads(out_file.read_text())
            self.assertEqual(data["status"], "FAIL")
            self.assertEqual(data["tests"]["ran"], 1)
            self.assertEqual(data["tests"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
