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
- Compatibility old CLI flags (--tests, --skip-tests, --out) and external fields
- Pending CI reuse without retesting already completed suite
- Changed exact-head key rerun behavior
- Pretty-printed and single-line REVIEW_REQUIRED packet parsing without code execution
- Live worker identity and 40-minute non-duplication
- Timeout explicit TIMEOUT verdict, never generic FAIL
- CI / scope / head failure fail closed with FAIL
"""
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

LAB_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = LAB_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
RUNNER = REPO_ROOT / "engineering/acceptance/progress_runner.py"
ACCEPT_UNIT = REPO_ROOT / "engineering/acceptance/accept_unit.py"
AUTO_ACCEPT = REPO_ROOT / "engineering/acceptance/auto_accept.py"


def make_sha(seed: str) -> str:
    """Assemble token-like hex SHA at runtime without embedding string literals."""
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def make_fake_gh(bin_dir: Path, state_file: Path):
    """Create an executable fake gh script in bin_dir reading dynamic mock state from state_file."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh_script = bin_dir / "gh"
    gh_content = f"""#!{sys.executable}
import json, os, sys, shutil
from pathlib import Path

state_file = Path({repr(str(state_file))})
state = json.loads(state_file.read_text()) if state_file.exists() else {{}}

args = sys.argv[1:]
if not args:
    sys.exit(0)

if args[0] == "api":
    api_args = [a for a in args[1:] if not a.startswith("-") and not a.startswith("Accept:")]
    api_path = api_args[0] if api_args else ""
    if "ref/heads" in api_path:
        print(state.get("head_sha", "1111111111111111111111111111111111111111"))
    elif "commits" in api_path or "pulls" in api_path:
        print(state.get("base_sha", "0000000000000000000000000000000000000000"))
    elif "compare" in api_path:
        files = state.get("changed_files", ["engineering/acceptance/accept_unit.py"])
        print("\\n".join(files))
    elif "actions/runs" in api_path:
        runs = state.get("workflow_runs", [{{"event": "push", "status": "completed", "conclusion": "success"}}])
        print(json.dumps({{"workflow_runs": runs}}))
    elif "contents" in api_path:
        mb = state.get("mailbox", {{"status": "DELIVERED", "instruction_id": "test-1", "tests": [{{"name": "lab", "result": "passed"}}]}})
        print(json.dumps(mb))
    else:
        print("{{}}")
    sys.exit(0)

if args[0] == "repo" and len(args) > 1 and args[1] == "clone":
    target_dir = Path(args[3])
    source_dir = Path(state.get("clone_source"))
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(source_dir, target_dir)
    sys.exit(0)

sys.exit(0)
"""
    gh_script.write_text(gh_content)
    gh_script.chmod(0o755)


def create_test_git_repo(repo_path: Path, test_content: str = None) -> str:
    """Create a minimal real local git repository for testing without network."""
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(repo_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Test"], check=True, capture_output=True)

    eng_dir = repo_path / "engineering" / "acceptance"
    eng_dir.mkdir(parents=True, exist_ok=True)
    (repo_path / "engineering" / "__init__.py").write_text("")
    (eng_dir / "__init__.py").write_text("")
    shutil.copy(REPO_ROOT / "engineering/acceptance/fixture_suites.py", eng_dir / "fixture_suites.py")

    lab_dir = repo_path / "lab"
    lab_dir.mkdir(parents=True, exist_ok=True)
    (lab_dir / "__init__.py").write_text("")

    tc = test_content or (
        "import unittest\n\n"
        "class LocalSampleTest(unittest.TestCase):\n"
        "    def test_passing_item(self):\n"
        "        self.assertTrue(True)\n"
    )
    (lab_dir / "test_sample.py").write_text(tc)

    subprocess.run(["git", "-C", str(repo_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "init"], check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return head


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

    def test_22_compatibility_old_cli_flags_and_external_fields(self):
        """accept_unit preserves legacy CLI flags (--tests, --skip-tests, --out) and external contract fields."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bin_dir = td_path / "bin"
            state_file = td_path / "gh_state.json"
            template_repo = td_path / "template_repo"
            head_sha = create_test_git_repo(template_repo)

            make_fake_gh(bin_dir, state_file)
            state_file.write_text(json.dumps({
                "head_sha": head_sha,
                "base_sha": make_sha("base-sha-22"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-22", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))

            job_dir = td_path / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "job.json").write_text(json.dumps({
                "repository": "test/repo",
                "branch": "feat-compat",
                "mailbox": "docs/mailbox.json",
                "write_allowlist": ["engineering/acceptance/accept_unit.py"],
                "pr_number": 1,
            }))
            (job_dir / "review_packet.json").write_text(json.dumps({
                "head_sha": head_sha
            }))

            profile_dir = td_path / "profile"
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
            env["HERMES_HOME"] = str(profile_dir)

            out_file = td_path / "out.json"
            cmd = [
                sys.executable,
                str(ACCEPT_UNIT),
                str(job_dir),
                "--tests", "discover -s . -p test_*.py",
                "--out", str(out_file),
                "--soft-seconds", "5.0",
                "--hard-seconds", "20.0",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(proc.returncode, 0, f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
            self.assertTrue(out_file.exists())
            data = json.loads(out_file.read_text())

            # Verify external contract fields
            for f in ("job", "repository", "branch", "head", "head_ok", "scope_ok", "ci_ok", "tests_ok", "verdict", "mailbox", "ci_runs", "tests"):
                self.assertIn(f, data, f"Required contract field '{f}' missing from output")

            self.assertEqual(data["head_ok"], True)
            self.assertEqual(data["scope_ok"], True)
            self.assertEqual(data["ci_ok"], True)
            self.assertEqual(data["tests_ok"], True)
            self.assertEqual(data["verdict"], "PASS")

            # Verify tests dictionary fields
            t = data["tests"]
            for f in ("ran", "seconds", "ok", "failed", "skipped", "tail"):
                self.assertIn(f, t, f"Required tests field '{f}' missing")
            self.assertEqual(t["ok"], True)
            self.assertEqual(t["failed"], False)
            self.assertGreaterEqual(t["ran"], 1)

            # Test --skip-tests flag compatibility
            out_skip = td_path / "out_skip.json"
            cmd_skip = [
                sys.executable,
                str(ACCEPT_UNIT),
                str(job_dir),
                "--skip-tests",
                "--out", str(out_skip),
            ]
            proc_skip = subprocess.run(cmd_skip, capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(proc_skip.returncode, 0)
            data_skip = json.loads(out_skip.read_text())
            self.assertIsNone(data_skip["tests_ok"])
            self.assertEqual(data_skip["verdict"], "PASS_WITHOUT_TESTS")

    def test_23_pending_ci_reuse_without_retesting_completed_suite(self):
        """accept_unit produces PENDING_CI when CI is running, and reuses completed test results on subsequent tick."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bin_dir = td_path / "bin"
            state_file = td_path / "gh_state.json"
            template_repo = td_path / "template_repo"
            head_sha = create_test_git_repo(template_repo)

            make_fake_gh(bin_dir, state_file)
            state_file.write_text(json.dumps({
                "head_sha": head_sha,
                "base_sha": make_sha("base-sha-23"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                # CI run is in_progress
                "workflow_runs": [{"event": "push", "status": "in_progress", "conclusion": None}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-23", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))

            job_dir = td_path / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "job.json").write_text(json.dumps({
                "repository": "test/repo",
                "branch": "feat-pending-ci",
                "mailbox": "docs/mailbox.json",
                "write_allowlist": ["engineering/acceptance/accept_unit.py"],
                "pr_number": 2,
            }))
            (job_dir / "review_packet.json").write_text(json.dumps({
                "head_sha": head_sha
            }))

            profile_dir = td_path / "profile"
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
            env["HERMES_HOME"] = str(profile_dir)

            # First run: CI pending, but tests execute and pass
            out_1 = td_path / "out1.json"
            cmd = [
                sys.executable,
                str(ACCEPT_UNIT),
                str(job_dir),
                "--tests", "discover -s . -p test_*.py",
                "--out", str(out_1),
                "--soft-seconds", "5.0",
                "--hard-seconds", "20.0",
            ]
            p1 = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(p1.returncode, 0, f"stdout: {p1.stdout}\nstderr: {p1.stderr}")
            d1 = json.loads(out_1.read_text())
            self.assertEqual(d1["verdict"], "PENDING_CI")
            self.assertEqual(d1["tests_ok"], True)
            self.assertEqual(d1["ci_pending"], True)

            # State dir must exist and have recorded completed tests
            state_runner = job_dir / ".progress_runner" / "state.json"
            self.assertTrue(state_runner.exists())
            saved_state = json.loads(state_runner.read_text())
            self.assertEqual(saved_state["status"], "PASS")

            # Second tick: CI is now completed successfully
            state_file.write_text(json.dumps({
                "head_sha": head_sha,
                "base_sha": make_sha("base-sha-23"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-23", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))

            out_2 = td_path / "out2.json"
            cmd2 = [
                sys.executable,
                str(ACCEPT_UNIT),
                str(job_dir),
                "--tests", "discover -s . -p test_*.py",
                "--out", str(out_2),
                "--soft-seconds", "5.0",
                "--hard-seconds", "20.0",
            ]
            t_start = time.monotonic()
            p2 = subprocess.run(cmd2, capture_output=True, text=True, env=env, timeout=15)
            elapsed = time.monotonic() - t_start
            self.assertEqual(p2.returncode, 0, f"stdout: {p2.stdout}\nstderr: {p2.stderr}")
            d2 = json.loads(out_2.read_text())
            self.assertEqual(d2["verdict"], "PASS")
            self.assertEqual(d2["tests_ok"], True)
            self.assertEqual(d2["ci_ok"], True)
            self.assertEqual(d2["ci_pending"], False)
            self.assertLess(elapsed, 2.0, "Second run should reuse completed test suite quickly without retesting")

    def test_24_exact_head_key_changed_behavior(self):
        """accept_unit reruns tests when head changes and never reuses previous head's result."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bin_dir = td_path / "bin"
            state_file = td_path / "gh_state.json"
            template_repo = td_path / "template_repo"
            head_a = create_test_git_repo(template_repo)

            make_fake_gh(bin_dir, state_file)
            state_file.write_text(json.dumps({
                "head_sha": head_a,
                "base_sha": make_sha("base-sha-24"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-24", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))

            job_dir = td_path / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "job.json").write_text(json.dumps({
                "repository": "test/repo",
                "branch": "feat-head-change",
                "mailbox": "docs/mailbox.json",
                "write_allowlist": ["engineering/acceptance/accept_unit.py"],
                "pr_number": 3,
            }))
            (job_dir / "review_packet.json").write_text(json.dumps({
                "head_sha": head_a
            }))

            profile_dir = td_path / "profile"
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
            env["HERMES_HOME"] = str(profile_dir)

            out_a = td_path / "out_a.json"
            p_a = subprocess.run([
                sys.executable, str(ACCEPT_UNIT), str(job_dir),
                "--tests", "discover -s . -p test_*.py",
                "--out", str(out_a),
                "--soft-seconds", "5.0", "--hard-seconds", "20.0"
            ], capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(p_a.returncode, 0)
            d_a = json.loads(out_a.read_text())
            self.assertEqual(d_a["head"], head_a)
            self.assertEqual(d_a["verdict"], "PASS")

            # Create commit B on template repo
            (template_repo / "lab" / "test_sample.py").write_text(
                "import unittest\nclass TestB(unittest.TestCase):\n    def test_b(self):\n        self.assertEqual(2, 2)\n"
            )
            subprocess.run(["git", "-C", str(template_repo), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(template_repo), "commit", "-m", "commit B"], check=True, capture_output=True)
            head_b = subprocess.run(["git", "-C", str(template_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            self.assertNotEqual(head_a, head_b)

            # Update fake gh and review packet to head B
            state_file.write_text(json.dumps({
                "head_sha": head_b,
                "base_sha": make_sha("base-sha-24"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-24", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))
            (job_dir / "review_packet.json").write_text(json.dumps({
                "head_sha": head_b
            }))

            out_b = td_path / "out_b.json"
            p_b = subprocess.run([
                sys.executable, str(ACCEPT_UNIT), str(job_dir),
                "--tests", "discover -s . -p test_*.py",
                "--out", str(out_b),
                "--soft-seconds", "5.0", "--hard-seconds", "20.0"
            ], capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(p_b.returncode, 0)
            d_b = json.loads(out_b.read_text())
            self.assertEqual(d_b["head"], head_b)
            self.assertEqual(d_b["clone_head_matches"], True)
            self.assertEqual(d_b["verdict"], "PASS")

    def test_25_pretty_packet_parsing(self):
        """auto_accept correctly parses review packet path from single-line and pretty-printed logs without code execution."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            runs_dir = home_dir / "commitments" / "runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            job_dir = home_dir / "job_alpha"
            job_dir.mkdir(parents=True, exist_ok=True)
            packet_path = job_dir / "review_packet.json"
            packet_path.write_text('{"head_sha": "abc"}')

            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                # 1. Single-line log
                log1 = runs_dir / "item1.log"
                log1.write_text(f'2026-10-14 12:00:00 REVIEW_REQUIRED: {{"packet": "{packet_path}"}}\n')
                jd1, _ = aa.job_dir_from_log("item1")
                self.assertEqual(jd1, job_dir)

                # 2. Pretty-printed multi-line log
                log2 = runs_dir / "item2.log"
                log2.write_text(f"""2026-10-14 12:00:00 TASK_COMPLETE
REVIEW_REQUIRED
{{
  "packet": "{packet_path}",
  "verdict": "READY"
}}
""")
                jd2, _ = aa.job_dir_from_log("item2")
                self.assertEqual(jd2, job_dir)

                # 3. Log with arbitrary non-executed text/code
                log3 = runs_dir / "item3.log"
                log3.write_text(f"""REVIEW_REQUIRED
{{
  "packet": "{packet_path}"
}}
import os; os.system("invalid")
""")
                jd3, _ = aa.job_dir_from_log("item3")
                self.assertEqual(jd3, job_dir)
            finally:
                aa.HOME = old_home

    def test_26_live_worker_identity_and_40min_budget_non_duplication(self):
        """auto_accept respects live worker start identity and 40-minute budget without duplicate launches."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    pid = dummy.pid
                    ident = aa.get_proc_identity(pid)
                    self.assertIsNotNone(ident)

                    # is_proc_alive with correct ident returns True
                    self.assertTrue(aa.is_proc_alive(pid, ident))

                    # is_proc_alive with mismatched start identity returns False (recycled PID simulation)
                    bogus_ident = (pid, ident[1] + 999999)
                    self.assertFalse(aa.is_proc_alive(pid, bogus_ident))

                    # Dead PID returns False
                    self.assertFalse(aa.is_proc_alive(9999999))

                    # Set up auto_accept environment
                    journal_dir = home_dir / "lab" / "journal"
                    journal_dir.mkdir(parents=True, exist_ok=True)
                    runs_dir = home_dir / "commitments" / "runs"
                    runs_dir.mkdir(parents=True, exist_ok=True)
                    job_dir = home_dir / "job_beta"
                    job_dir.mkdir(parents=True, exist_ok=True)
                    (job_dir / "job.json").write_text('{"repository": "repo", "branch": "b", "mailbox": "m", "write_allowlist": []}')
                    packet_path = job_dir / "review_packet.json"
                    packet_path.write_text('{"head_sha": "abc"}')
                    (runs_dir / "beta_item.log").write_text(f'REVIEW_REQUIRED {{"packet": "{packet_path}"}}')

                    running_file = job_dir / "auto_accept-beta_item.running"
                    # Simulate running worker at 1500s elapsed (> 1200s, within 2400s)
                    start_epoch = int(time.time() - 1500)
                    running_file.write_text(f"{pid} {ident[1]} {start_epoch} 2026-10-14 12:00:00")

                    # Mock commit module in aa
                    class MockCommit:
                        DISPATCHED = "DISPATCHED"
                        @staticmethod
                        def handle_status(h):
                            return ("finished", None)
                        class Ledger:
                            def __init__(self):
                                self.data = {"items": [{"id": "beta_item", "state": "DISPATCHED", "handle": "h1"}]}
                            def __enter__(self):
                                return self
                            def __exit__(self, *args):
                                pass

                    old_commit = aa.c
                    aa.c = MockCommit()
                    try:
                        # main() must not launch a new worker because dummy worker is alive within 2400s budget!
                        err_file = job_dir / "auto_accept-beta_item.err"
                        self.assertFalse(err_file.exists())
                        aa.main()
                        # Verify no new launch occurred (err file not created)
                        self.assertFalse(err_file.exists(), "auto_accept must not start duplicate worker while valid 40min run is active")
                        self.assertTrue(running_file.exists())
                    finally:
                        aa.c = old_commit
                finally:
                    dummy.kill()
                    dummy.wait()

                # Now dummy is dead: main() should detect dead process, clean up old running file,
                # and launch helper via Popen. Test via mock Popen asserting exact argv/creation/identity.
                fake_proc_dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    fake_ident = aa.get_proc_identity(fake_proc_dummy.pid)
                    fake_popen_calls = []

                    def fake_popen(*args, **kwargs):
                        fake_popen_calls.append((args, kwargs))
                        return fake_proc_dummy

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen
                    aa.c = MockCommit()
                    try:
                        aa.main()
                        self.assertFalse(running_file.exists() and f"{pid}" in running_file.read_text())
                        self.assertEqual(len(fake_popen_calls), 1)
                        call_args, call_kwargs = fake_popen_calls[0]
                        expected_argv = [
                            sys.executable,
                            str(aa.ACCEPT),
                            str(job_dir),
                            "--out",
                            str(job_dir / "auto_accept-beta_item.json"),
                        ]
                        self.assertEqual(call_args[0], expected_argv)
                        self.assertTrue(call_kwargs.get("start_new_session"))
                        self.assertEqual(call_kwargs.get("stdout"), subprocess.DEVNULL)
                        self.assertTrue(running_file.exists())
                        parts = running_file.read_text().split()
                        self.assertEqual(int(parts[0]), fake_proc_dummy.pid)
                        self.assertEqual(int(parts[1]), fake_ident[1])
                    finally:
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    fake_proc_dummy.kill()
                    fake_proc_dummy.wait()
            finally:
                aa.HOME = old_home

    def test_27_timeout_verdict_not_generic_fail(self):
        """accept_unit returns TIMEOUT verdict on deadline expiry, NEVER generic FAIL."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bin_dir = td_path / "bin"
            state_file = td_path / "gh_state.json"
            template_repo = td_path / "template_repo"
            sleep_test = (
                "import unittest, time\n"
                "class SlowTest(unittest.TestCase):\n"
                "    def test_sleep(self):\n"
                "        time.sleep(15.0)\n"
            )
            head_sha = create_test_git_repo(template_repo, test_content=sleep_test)

            make_fake_gh(bin_dir, state_file)
            state_file.write_text(json.dumps({
                "head_sha": head_sha,
                "base_sha": make_sha("base-sha-27"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-27", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))

            job_dir = td_path / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "job.json").write_text(json.dumps({
                "repository": "test/repo",
                "branch": "feat-timeout",
                "mailbox": "docs/mailbox.json",
                "write_allowlist": ["engineering/acceptance/accept_unit.py"],
                "pr_number": 4,
            }))
            (job_dir / "review_packet.json").write_text(json.dumps({
                "head_sha": head_sha
            }))

            profile_dir = td_path / "profile"
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
            env["HERMES_HOME"] = str(profile_dir)

            out_file = td_path / "out_timeout.json"
            cmd = [
                sys.executable,
                str(ACCEPT_UNIT),
                str(job_dir),
                "--tests", "discover -s . -p test_*.py",
                "--out", str(out_file),
                "--soft-seconds", "0.2",
                "--checkpoint-seconds", "0.2",
                "--hard-seconds", "0.6",
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(proc.returncode, 0)
            self.assertTrue(out_file.exists())
            data = json.loads(out_file.read_text())

            # Verdict MUST be TIMEOUT, not generic FAIL
            self.assertEqual(data["verdict"], "TIMEOUT")
            self.assertEqual(data["tests_ok"], False)
            self.assertIn("error", data["tests"])

    def test_28_ci_scope_head_failure_fail_closed(self):
        """accept_unit fails closed with FAIL verdict on scope, head or CI failures."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bin_dir = td_path / "bin"
            state_file = td_path / "gh_state.json"
            template_repo = td_path / "template_repo"
            head_sha = create_test_git_repo(template_repo)

            make_fake_gh(bin_dir, state_file)
            job_dir = td_path / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "job.json").write_text(json.dumps({
                "repository": "test/repo",
                "branch": "feat-fail-closed",
                "mailbox": "docs/mailbox.json",
                "write_allowlist": ["engineering/acceptance/accept_unit.py"],
                "pr_number": 5,
            }))
            (job_dir / "review_packet.json").write_text(json.dumps({
                "head_sha": head_sha
            }))

            profile_dir = td_path / "profile"
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
            env["HERMES_HOME"] = str(profile_dir)

            # Case A: Scope failure (file outside allowlist)
            state_file.write_text(json.dumps({
                "head_sha": head_sha,
                "base_sha": make_sha("base-sha-28"),
                "changed_files": ["engineering/acceptance/accept_unit.py", "unauthorized_file.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-28", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))
            out_scope = td_path / "out_scope.json"
            subprocess.run([
                sys.executable, str(ACCEPT_UNIT), str(job_dir),
                "--skip-tests", "--out", str(out_scope)
            ], capture_output=True, text=True, env=env, timeout=10)
            d_scope = json.loads(out_scope.read_text())
            self.assertEqual(d_scope["scope_ok"], False)
            self.assertEqual(d_scope["verdict"], "FAIL")

            # Case B: CI failure (workflow completed with failure conclusion)
            state_file.write_text(json.dumps({
                "head_sha": head_sha,
                "base_sha": make_sha("base-sha-28"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "failure"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-28", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))
            out_ci = td_path / "out_ci.json"
            subprocess.run([
                sys.executable, str(ACCEPT_UNIT), str(job_dir),
                "--skip-tests", "--out", str(out_ci)
            ], capture_output=True, text=True, env=env, timeout=10)
            d_ci = json.loads(out_ci.read_text())
            self.assertEqual(d_ci["ci_ok"], False)
            self.assertEqual(d_ci["verdict"], "FAIL")

            # Case C: Head mismatch with review packet
            state_file.write_text(json.dumps({
                "head_sha": head_sha,
                "base_sha": make_sha("base-sha-28"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-28", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))
            (job_dir / "review_packet.json").write_text(json.dumps({
                "head_sha": make_sha("mismatch-head-sha")
            }))
            out_head = td_path / "out_head.json"
            subprocess.run([
                sys.executable, str(ACCEPT_UNIT), str(job_dir),
                "--skip-tests", "--out", str(out_head)
            ], capture_output=True, text=True, env=env, timeout=10)
            d_head = json.loads(out_head.read_text())
            self.assertEqual(d_head["head_ok"], False)
            self.assertEqual(d_head["verdict"], "FAIL")

    def test_29_live_marker_2401s_no_duplicate(self):
        """auto_accept does not launch duplicate when worker PID is still alive even after 2400s elapsed."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    pid = dummy.pid
                    ident = aa.get_proc_identity(pid)
                    self.assertIsNotNone(ident)

                    (home_dir / "lab" / "journal").mkdir(parents=True, exist_ok=True)
                    runs_dir = home_dir / "commitments" / "runs"
                    runs_dir.mkdir(parents=True, exist_ok=True)
                    job_dir = home_dir / "job_gamma"
                    job_dir.mkdir(parents=True, exist_ok=True)
                    (job_dir / "job.json").write_text('{"repository": "repo", "branch": "b", "mailbox": "m", "write_allowlist": []}')
                    packet_path = job_dir / "review_packet.json"
                    packet_path.write_text('{"head_sha": "abc"}')
                    (runs_dir / "gamma_item.log").write_text(f'REVIEW_REQUIRED {{"packet": "{packet_path}"}}')

                    running_file = job_dir / "auto_accept-gamma_item.running"
                    # Start epoch set to 2401s ago (> 2400s hard ceiling)
                    start_epoch = int(time.time() - 2401)
                    running_file.write_text(f"{pid} {ident[1]} {start_epoch} 2026-10-14 12:00:00")

                    class MockCommit:
                        DISPATCHED = "DISPATCHED"
                        @staticmethod
                        def handle_status(h):
                            return ("finished", None)
                        class Ledger:
                            def __init__(self):
                                self.data = {"items": [{"id": "gamma_item", "state": "DISPATCHED", "handle": "h1"}]}
                            def __enter__(self):
                                return self
                            def __exit__(self, *args):
                                pass

                    old_commit = aa.c
                    aa.c = MockCommit()
                    fake_popen_called = False
                    def fake_popen(*args, **kwargs):
                        nonlocal fake_popen_called
                        fake_popen_called = True
                        return dummy

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen
                    try:
                        aa.main()
                        # Live exact PID/start remains blocker even after 2400s elapsed
                        self.assertFalse(fake_popen_called, "auto_accept must not start duplicate worker while live PID/start is active, even at 2401s")
                        self.assertTrue(running_file.exists())
                    finally:
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    dummy.kill()
                    dummy.wait()
            finally:
                aa.HOME = old_home

    def test_30_recycled_pid_cleanup_and_launch(self):
        """auto_accept detects recycled PID via start identity mismatch and allows new launch."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    pid = dummy.pid
                    ident = aa.get_proc_identity(pid)
                    self.assertIsNotNone(ident)

                    (home_dir / "lab" / "journal").mkdir(parents=True, exist_ok=True)
                    runs_dir = home_dir / "commitments" / "runs"
                    runs_dir.mkdir(parents=True, exist_ok=True)
                    job_dir = home_dir / "job_delta"
                    job_dir.mkdir(parents=True, exist_ok=True)
                    (job_dir / "job.json").write_text('{"repository": "repo", "branch": "b", "mailbox": "m", "write_allowlist": []}')
                    packet_path = job_dir / "review_packet.json"
                    packet_path.write_text('{"head_sha": "abc"}')
                    (runs_dir / "delta_item.log").write_text(f'REVIEW_REQUIRED {{"packet": "{packet_path}"}}')

                    running_file = job_dir / "auto_accept-delta_item.running"
                    # Write mismatched start identity for live PID (recycled PID simulation)
                    bogus_starttime = ident[1] + 888888
                    running_file.write_text(f"{pid} {bogus_starttime} {int(time.time() - 100)} 2026-10-14 12:00:00")

                    class MockCommit:
                        DISPATCHED = "DISPATCHED"
                        @staticmethod
                        def handle_status(h):
                            return ("finished", None)
                        class Ledger:
                            def __init__(self):
                                self.data = {"items": [{"id": "delta_item", "state": "DISPATCHED", "handle": "h1"}]}
                            def __enter__(self):
                                return self
                            def __exit__(self, *args):
                                pass

                    old_commit = aa.c
                    aa.c = MockCommit()
                    fake_popen_calls = []
                    def fake_popen(*args, **kwargs):
                        fake_popen_calls.append((args, kwargs))
                        return dummy

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen
                    try:
                        aa.main()
                        # Recycled PID marker should be cleaned up and new worker launched
                        self.assertEqual(len(fake_popen_calls), 1)
                        self.assertTrue(running_file.exists())
                        parts = running_file.read_text().split()
                        self.assertEqual(int(parts[0]), pid)
                        self.assertEqual(int(parts[1]), ident[1], "New running marker must reflect real start identity")
                    finally:
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    dummy.kill()
                    dummy.wait()
            finally:
                aa.HOME = old_home

    def test_31_legacy_marker_handling(self):
        """auto_accept blocks duplicate when legacy marker PID is alive, cleans up and launches when dead."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    pid = dummy.pid
                    (home_dir / "lab" / "journal").mkdir(parents=True, exist_ok=True)
                    runs_dir = home_dir / "commitments" / "runs"
                    runs_dir.mkdir(parents=True, exist_ok=True)
                    job_dir = home_dir / "job_legacy"
                    job_dir.mkdir(parents=True, exist_ok=True)
                    (job_dir / "job.json").write_text('{"repository": "repo", "branch": "b", "mailbox": "m", "write_allowlist": []}')
                    packet_path = job_dir / "review_packet.json"
                    packet_path.write_text('{"head_sha": "abc"}')
                    (runs_dir / "legacy_item.log").write_text(f'REVIEW_REQUIRED {{"packet": "{packet_path}"}}')

                    running_file = job_dir / "auto_accept-legacy_item.running"
                    # Legacy marker format: only PID and timestamp
                    running_file.write_text(f"{pid} 2026-10-14 12:00:00")

                    class MockCommit:
                        DISPATCHED = "DISPATCHED"
                        @staticmethod
                        def handle_status(h):
                            return ("finished", None)
                        class Ledger:
                            def __init__(self):
                                self.data = {"items": [{"id": "legacy_item", "state": "DISPATCHED", "handle": "h1"}]}
                            def __enter__(self):
                                return self
                            def __exit__(self, *args):
                                pass

                    old_commit = aa.c
                    aa.c = MockCommit()
                    fake_popen_called = False
                    def fake_popen(*args, **kwargs):
                        nonlocal fake_popen_called
                        fake_popen_called = True
                        return dummy

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen
                    try:
                        # Case A: Live legacy PID blocks duplicate launch
                        aa.main()
                        self.assertFalse(fake_popen_called, "Live legacy PID must block duplicate launch")
                        self.assertTrue(running_file.exists())
                    finally:
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    dummy.kill()
                    dummy.wait()

                # Case B: Dead legacy PID cleans up and launches
                fake_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    fake_popen_calls = []
                    def fake_popen2(*args, **kwargs):
                        fake_popen_calls.append((args, kwargs))
                        return fake_proc

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen2
                    aa.c = MockCommit()
                    try:
                        aa.main()
                        self.assertEqual(len(fake_popen_calls), 1)
                    finally:
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    fake_proc.kill()
                    fake_proc.wait()
            finally:
                aa.HOME = old_home

    def test_32_two_jobs_reversed_ledger_order_global_serialization(self):
        """auto_accept globally scans all registered job markers so second job's active marker blocks first job."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    pid = dummy.pid
                    ident = aa.get_proc_identity(pid)
                    self.assertIsNotNone(ident)

                    (home_dir / "lab" / "journal").mkdir(parents=True, exist_ok=True)
                    runs_dir = home_dir / "commitments" / "runs"
                    runs_dir.mkdir(parents=True, exist_ok=True)

                    # Job 1: no running marker yet
                    job1_dir = home_dir / "job_one"
                    job1_dir.mkdir(parents=True, exist_ok=True)
                    (job1_dir / "job.json").write_text('{"repository": "repo", "branch": "b1", "mailbox": "m1", "write_allowlist": []}')
                    p1 = job1_dir / "review_packet.json"
                    p1.write_text('{"head_sha": "sha1"}')
                    (runs_dir / "item_1.log").write_text(f'REVIEW_REQUIRED {{"packet": "{p1}"}}')

                    # Job 2: active running marker with live dummy worker
                    job2_dir = home_dir / "job_two"
                    job2_dir.mkdir(parents=True, exist_ok=True)
                    (job2_dir / "job.json").write_text('{"repository": "repo", "branch": "b2", "mailbox": "m2", "write_allowlist": []}')
                    p2 = job2_dir / "review_packet.json"
                    p2.write_text('{"head_sha": "sha2"}')
                    (runs_dir / "item_2.log").write_text(f'REVIEW_REQUIRED {{"packet": "{p2}"}}')
                    running_file_2 = job2_dir / "auto_accept-item_2.running"
                    running_file_2.write_text(f"{pid} {ident[1]} {int(time.time() - 300)} 2026-10-14 12:00:00")

                    # Ledger has item_1 first, item_2 second
                    class MockCommit:
                        DISPATCHED = "DISPATCHED"
                        @staticmethod
                        def handle_status(h):
                            return ("finished", None)
                        class Ledger:
                            def __init__(self):
                                self.data = {"items": [
                                    {"id": "item_1", "state": "DISPATCHED", "handle": "h1"},
                                    {"id": "item_2", "state": "DISPATCHED", "handle": "h2"},
                                ]}
                            def __enter__(self):
                                return self
                            def __exit__(self, *args):
                                pass

                    old_commit = aa.c
                    aa.c = MockCommit()
                    fake_popen_called = False
                    def fake_popen(*args, **kwargs):
                        nonlocal fake_popen_called
                        fake_popen_called = True
                        return dummy

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen
                    try:
                        aa.main()
                        # Because item_2 has an active worker, global scan must block item_1 from launching
                        self.assertFalse(fake_popen_called, "Global serialization must prevent launch of item_1 when item_2 is active")
                        self.assertFalse((job1_dir / "auto_accept-item_1.running").exists())
                    finally:
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    dummy.kill()
                    dummy.wait()
            finally:
                aa.HOME = old_home

    def test_33_accept_unit_strict_runner_parsing_and_reconciliation(self):
        """accept_unit strictly validates runner artifacts: missing JSON, head mismatch, reconciliation errors fail closed."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bin_dir = td_path / "bin"
            state_file = td_path / "gh_state.json"
            template_repo = td_path / "template_repo"
            head_sha = create_test_git_repo(template_repo)

            make_fake_gh(bin_dir, state_file)
            state_file.write_text(json.dumps({
                "head_sha": head_sha,
                "base_sha": make_sha("base-sha-33"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-33", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))

            job_dir = td_path / "job"
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "job.json").write_text(json.dumps({
                "repository": "test/repo",
                "branch": "feat-strict",
                "mailbox": "docs/mailbox.json",
                "write_allowlist": ["engineering/acceptance/accept_unit.py"],
                "pr_number": 99,
            }))
            (job_dir / "review_packet.json").write_text(json.dumps({
                "head_sha": head_sha
            }))

            profile_dir = td_path / "profile"
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
            env["HERMES_HOME"] = str(profile_dir)

            state_dir = td_path / "runner_state"
            state_dir.mkdir(parents=True, exist_ok=True)

            # Plant stale runner_out from previous old head
            stale_out = state_dir / "runner_out.json"
            stale_out.write_text(json.dumps({
                "status": "PASS",
                "verdict": "PASS",
                "head": make_sha("old-stale-head"),
                "cwd": str(td_path),
                "suite": "discover",
                "discovered_count": 1,
                "completed_count": 1,
                "discovered_ids": ["test1"],
                "completed_ids": ["test1"],
                "tests": {"ran": 1, "ok": True, "failed": 0},
                "tests_ok": True,
            }))

            out_file = td_path / "out_strict.json"
            # Run with non-existent test suite that fails discovery
            p = subprocess.run([
                sys.executable, str(ACCEPT_UNIT), str(job_dir),
                "--tests", "discover -s non_existent_dir",
                "--out", str(out_file),
                "--state-dir", str(state_dir),
                "--soft-seconds", "5.0",
                "--hard-seconds", "20.0",
            ], capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(p.returncode, 0)
            data = json.loads(out_file.read_text())
            # Stale runner_out must not have produced PASS, verdict must be FAIL
            self.assertEqual(data["verdict"], "FAIL")
            self.assertEqual(data["tests_ok"], False)

            # Executable regression injections of runner subprocess result for each case
            from engineering.acceptance.progress_runner import compute_config_key

            repo_head = f"test/repo@{head_sha}"
            clone_key = hashlib.sha256(repo_head.encode("utf-8")).hexdigest()[:16]
            scratch_base = profile_dir / "cache" / "scratch"
            clone_dir = scratch_base / f"accept-{clone_key}" / "clone"
            test_cwd = clone_dir / "lab"

            expected_config = {
                "checkpoint_seconds": 300.0,
                "cwd": str(test_cwd.resolve()),
                "hard_seconds": 2400.0,
                "head": head_sha,
                "item_seconds": None,
                "soft_seconds": 840.0,
                "stall_seconds": None,
                "suite": "discover -s tests",
            }
            valid_cfg_key = compute_config_key(expected_config)

            baseline_valid = {
                "status": "PASS",
                "verdict": "PASS",
                "exit_code": 0,
                "head": head_sha,
                "cwd": str(test_cwd.resolve()),
                "suite": "discover -s tests",
                "total_seconds": 1.5,
                "discovered_count": 1,
                "completed_count": 1,
                "discovered_ids": ["test_sample.LocalSampleTest.test_passing_item"],
                "completed_ids": ["test_sample.LocalSampleTest.test_passing_item"],
                "last_completed_id": "test_sample.LocalSampleTest.test_passing_item",
                "current_id": None,
                "suite_completed_event": {
                    "event": "suite_completed",
                    "was_successful": True,
                    "ran": 1,
                    "monotonic": 100.0,
                },
                "tests": {
                    "ran": 1,
                    "passed": 1,
                    "failed": 0,
                    "skipped": 0,
                    "ok": True,
                    "tail": [],
                },
                "tests_ok": True,
                "config_key": valid_cfg_key,
            }

            def run_with_runner_data(runner_payload, rc=0, raw_payload=None):
                out_path = td_path / "injected_out.json"
                if out_path.exists():
                    out_path.unlink()

                real_run = subprocess.run
                def mock_run(cmd, *a_args, **kw):
                    cmd_strs = [str(x) for x in cmd] if isinstance(cmd, (list, tuple)) else str(cmd).split()
                    if any("progress_runner.py" in x for x in cmd_strs):
                        if raw_payload is not None:
                            content = raw_payload
                        elif runner_payload is not None:
                            content = json.dumps(runner_payload)
                        else:
                            content = None
                        for i, arg in enumerate(cmd_strs):
                            if arg == "--out" and i + 1 < len(cmd_strs):
                                if content is not None:
                                    Path(cmd_strs[i + 1]).write_text(content)
                        out_str = content if content is not None else ""
                        return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=out_str, stderr="")
                    return real_run(cmd, *a_args, **kw)

                orig_argv = sys.argv
                orig_env = os.environ.copy()
                try:
                    os.environ["PATH"] = str(bin_dir) + ":" + os.environ.get("PATH", "")
                    os.environ["HERMES_HOME"] = str(profile_dir)
                    sys.argv = [
                        str(ACCEPT_UNIT),
                        str(job_dir),
                        "--tests", "discover -s tests",
                        "--out", str(out_path),
                        "--soft-seconds", "840.0",
                        "--hard-seconds", "2400.0",
                    ]
                    with patch("subprocess.run", side_effect=mock_run):
                        runpy.run_path(str(ACCEPT_UNIT), run_name="__main__")
                finally:
                    sys.argv = orig_argv
                    os.environ.clear()
                    os.environ.update(orig_env)

                self.assertTrue(out_path.exists())
                return json.loads(out_path.read_text())

            # 1. Baseline valid runner report passes completely
            res_valid = run_with_runner_data(baseline_valid)
            self.assertEqual(res_valid["verdict"], "PASS")
            self.assertEqual(res_valid["tests_ok"], True)
            self.assertEqual(res_valid["tests"]["ran"], 1)

            # 2. Operator reproduced defect: status PASS, discovered_count 1, completed_count 1,
            # tests {ok: True, failed: 0}, tests_ok True, but omit config_key, discovered_ids, completed_ids, suite_completed proof, tests.ran
            defect_payload = {
                "status": "PASS",
                "verdict": "PASS",
                "exit_code": 0,
                "head": head_sha,
                "cwd": str(test_cwd.resolve()),
                "suite": "discover -s tests",
                "discovered_count": 1,
                "completed_count": 1,
                "tests": {"ok": True, "failed": 0},
                "tests_ok": True,
            }
            res_defect = run_with_runner_data(defect_payload)
            self.assertEqual(res_defect["verdict"], "FAIL", "Defect payload must FAIL not PASS with tests.ran 0")
            self.assertEqual(res_defect["tests_ok"], False)

            # 3. Omit config_key -> FAIL
            d_no_key = copy.deepcopy(baseline_valid)
            del d_no_key["config_key"]
            self.assertEqual(run_with_runner_data(d_no_key)["verdict"], "FAIL")

            # 4. Wrong config_key -> FAIL
            d_bad_key = copy.deepcopy(baseline_valid)
            d_bad_key["config_key"] = "0" * 64
            self.assertEqual(run_with_runner_data(d_bad_key)["verdict"], "FAIL")

            # 5. Omit discovered_ids -> FAIL
            d_no_disc = copy.deepcopy(baseline_valid)
            del d_no_disc["discovered_ids"]
            self.assertEqual(run_with_runner_data(d_no_disc)["verdict"], "FAIL")

            # 6. Omit completed_ids -> FAIL
            d_no_comp = copy.deepcopy(baseline_valid)
            del d_no_comp["completed_ids"]
            self.assertEqual(run_with_runner_data(d_no_comp)["verdict"], "FAIL")

            # 7. Non-unique discovered_ids -> FAIL
            d_dup_disc = copy.deepcopy(baseline_valid)
            d_dup_disc["discovered_ids"] = ["item1", "item1"]
            d_dup_disc["discovered_count"] = 2
            d_dup_disc["completed_ids"] = ["item1", "item1"]
            d_dup_disc["completed_count"] = 2
            d_dup_disc["tests"]["ran"] = 2
            d_dup_disc["tests"]["passed"] = 2
            d_dup_disc["suite_completed_event"]["ran"] = 2
            self.assertEqual(run_with_runner_data(d_dup_disc)["verdict"], "FAIL")

            # 8. Empty string ID -> FAIL
            d_empty_id = copy.deepcopy(baseline_valid)
            d_empty_id["discovered_ids"] = [""]
            d_empty_id["completed_ids"] = [""]
            self.assertEqual(run_with_runner_data(d_empty_id)["verdict"], "FAIL")

            # 9. Count length mismatch -> FAIL
            d_cnt_mismatch = copy.deepcopy(baseline_valid)
            d_cnt_mismatch["discovered_count"] = 5
            self.assertEqual(run_with_runner_data(d_cnt_mismatch)["verdict"], "FAIL")

            # 10. Discovered vs completed reconciliation mismatch -> FAIL
            d_reconcile = copy.deepcopy(baseline_valid)
            d_reconcile["completed_ids"] = ["different_test_id"]
            self.assertEqual(run_with_runner_data(d_reconcile)["verdict"], "FAIL")

            # 11. Omit tests.ran or tests.ran != completed_count -> FAIL
            d_no_ran = copy.deepcopy(baseline_valid)
            del d_no_ran["tests"]["ran"]
            self.assertEqual(run_with_runner_data(d_no_ran)["verdict"], "FAIL")
            d_bad_ran = copy.deepcopy(baseline_valid)
            d_bad_ran["tests"]["ran"] = 0
            self.assertEqual(run_with_runner_data(d_bad_ran)["verdict"], "FAIL")

            # 12. passed + failed + skipped != completed_count -> FAIL
            d_reconcile_tests = copy.deepcopy(baseline_valid)
            d_reconcile_tests["tests"]["passed"] = 0
            self.assertEqual(run_with_runner_data(d_reconcile_tests)["verdict"], "FAIL")

            # 13. Missing suite_completed_event -> FAIL
            d_no_suite = copy.deepcopy(baseline_valid)
            del d_no_suite["suite_completed_event"]
            self.assertEqual(run_with_runner_data(d_no_suite)["verdict"], "FAIL")

            # 14. suite_completed_event was_successful = False -> FAIL
            d_suite_fail = copy.deepcopy(baseline_valid)
            d_suite_fail["suite_completed_event"]["was_successful"] = False
            self.assertEqual(run_with_runner_data(d_suite_fail)["verdict"], "FAIL")

            # 15. suite_completed_event ran != completed_count -> FAIL
            d_suite_ran = copy.deepcopy(baseline_valid)
            d_suite_ran["suite_completed_event"]["ran"] = 99
            self.assertEqual(run_with_runner_data(d_suite_ran)["verdict"], "FAIL")

            # 16. RC 0 but status FAIL -> FAIL
            d_status_fail = copy.deepcopy(baseline_valid)
            d_status_fail["status"] = "FAIL"
            self.assertEqual(run_with_runner_data(d_status_fail)["verdict"], "FAIL")

            # 17. RC 1 with status PASS -> FAIL
            self.assertEqual(run_with_runner_data(baseline_valid, rc=1)["verdict"], "FAIL")

            # 18. TIMEOUT remains TIMEOUT
            d_timeout = copy.deepcopy(baseline_valid)
            d_timeout["status"] = "TIMEOUT"
            d_timeout["exit_code"] = 2
            d_timeout["diagnostics"] = {"reason": "hard_deadline_exceeded"}
            res_timeout = run_with_runner_data(d_timeout, rc=2)
            self.assertEqual(res_timeout["verdict"], "TIMEOUT")
            self.assertEqual(res_timeout["tests_ok"], False)

            # 19. Missing/empty runner artifacts -> FAIL
            self.assertEqual(run_with_runner_data(None, rc=0)["verdict"], "FAIL")

            # 20. Injected malformed top-level JSON cases: [] / None / string / numeric => FAIL with JSON no exception
            # Injected top-level list []
            res_list = run_with_runner_data([], rc=0)
            self.assertEqual(res_list["verdict"], "FAIL")
            self.assertFalse(res_list["tests_ok"])
            self.assertEqual(res_list["tests"]["error"], "missing or malformed runner artifacts")

            res_list_nonempty = run_with_runner_data(["malformed", "list"], rc=0)
            self.assertEqual(res_list_nonempty["verdict"], "FAIL")
            self.assertFalse(res_list_nonempty["tests_ok"])
            self.assertEqual(res_list_nonempty["tests"]["error"], "missing or malformed runner artifacts")

            # Injected top-level null (JSON None)
            res_null = run_with_runner_data(None, rc=0, raw_payload="null")
            self.assertEqual(res_null["verdict"], "FAIL")
            self.assertFalse(res_null["tests_ok"])
            self.assertEqual(res_null["tests"]["error"], "missing or malformed runner artifacts")

            # Injected top-level string
            res_str = run_with_runner_data("malformed_json_string", rc=0)
            self.assertEqual(res_str["verdict"], "FAIL")
            self.assertFalse(res_str["tests_ok"])
            self.assertEqual(res_str["tests"]["error"], "missing or malformed runner artifacts")

            # Injected top-level numeric (int / float)
            res_int = run_with_runner_data(12345, rc=0)
            self.assertEqual(res_int["verdict"], "FAIL")
            self.assertFalse(res_int["tests_ok"])
            self.assertEqual(res_int["tests"]["error"], "missing or malformed runner artifacts")

            res_float = run_with_runner_data(98.6, rc=0)
            self.assertEqual(res_float["verdict"], "FAIL")
            self.assertFalse(res_float["tests_ok"])
            self.assertEqual(res_float["tests"]["error"], "missing or malformed runner artifacts")

    def test_34_accept_unit_clone_immutability_and_scratch_path(self):
        """accept_unit ensures clone immutability: Head A clone and live worker survive Head B acceptance;
        cached clone mismatch fails closed without deleting or mutating existing clone; no /tmp fallback.
        """
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            bin_dir = td_path / "bin"
            state_file = td_path / "gh_state.json"
            template_repo = td_path / "template_repo"
            head_a = create_test_git_repo(template_repo)

            make_fake_gh(bin_dir, state_file)
            state_file.write_text(json.dumps({
                "head_sha": head_a,
                "base_sha": make_sha("base-sha-34-a"),
                "changed_files": ["engineering/acceptance/accept_unit.py"],
                "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                "mailbox": {"status": "DELIVERED", "instruction_id": "test-34-a", "tests": [{"name": "lab", "result": "passed"}]},
                "clone_source": str(template_repo),
            }))

            job_a = td_path / "job_a"
            job_a.mkdir(parents=True, exist_ok=True)
            (job_a / "job.json").write_text(json.dumps({
                "repository": "test/repo",
                "branch": "feat-head-a",
                "mailbox": "docs/mailbox.json",
                "write_allowlist": ["engineering/acceptance/accept_unit.py"],
                "pr_number": 1,
            }))
            (job_a / "review_packet.json").write_text(json.dumps({"head_sha": head_a}))

            profile_dir = td_path / "profile"
            scratch_base = profile_dir / "cache" / "scratch"
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + ":" + env.get("PATH", "")
            env["HERMES_HOME"] = str(profile_dir)

            # Step 1: Run accept_unit for Job A (Head A)
            out_a = td_path / "out_a.json"
            p_a = subprocess.run([
                sys.executable, str(ACCEPT_UNIT), str(job_a),
                "--tests", "discover -s . -p test_*.py",
                "--out", str(out_a),
                "--soft-seconds", "5.0", "--hard-seconds", "20.0"
            ], capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(p_a.returncode, 0)
            d_a = json.loads(out_a.read_text())
            self.assertEqual(d_a["verdict"], "PASS")

            # Verify Head A clone exists
            key_a = hashlib.sha256(("test/repo@%s" % head_a).encode("utf-8")).hexdigest()[:16]
            clone_a_dir = scratch_base / f"accept-{key_a}" / "clone"
            self.assertTrue(clone_a_dir.exists())

            # Step 2: Plant canary and spawn live slow worker in Head A's clone
            canary_a = clone_a_dir / "canary_a.txt"
            canary_a.write_text("head_a_canary_bytes")
            slow_worker = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(15)"],
                cwd=str(clone_a_dir)
            )
            try:
                self.assertIsNone(slow_worker.poll(), "Worker in clone A must be running")

                # Step 3: Create Head B in template repo
                (template_repo / "lab" / "test_sample.py").write_text(
                    "import unittest\nclass TestB(unittest.TestCase):\n    def test_b(self):\n        self.assertTrue(True)\n"
                )
                subprocess.run(["git", "-C", str(template_repo), "add", "."], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(template_repo), "commit", "-m", "commit B"], check=True, capture_output=True)
                head_b = subprocess.run(["git", "-C", str(template_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
                self.assertNotEqual(head_a, head_b)

                # Configure Job B on separate job state
                job_b = td_path / "job_b"
                job_b.mkdir(parents=True, exist_ok=True)
                (job_b / "job.json").write_text(json.dumps({
                    "repository": "test/repo",
                    "branch": "feat-head-b",
                    "mailbox": "docs/mailbox.json",
                    "write_allowlist": ["engineering/acceptance/accept_unit.py"],
                    "pr_number": 2,
                }))
                (job_b / "review_packet.json").write_text(json.dumps({"head_sha": head_b}))
                state_file.write_text(json.dumps({
                    "head_sha": head_b,
                    "base_sha": make_sha("base-sha-34-b"),
                    "changed_files": ["engineering/acceptance/accept_unit.py"],
                    "workflow_runs": [{"event": "push", "status": "completed", "conclusion": "success"}],
                    "mailbox": {"status": "DELIVERED", "instruction_id": "test-34-b", "tests": [{"name": "lab", "result": "passed"}]},
                    "clone_source": str(template_repo),
                }))

                out_b = td_path / "out_b.json"
                p_b = subprocess.run([
                    sys.executable, str(ACCEPT_UNIT), str(job_b),
                    "--tests", "discover -s . -p test_*.py",
                    "--out", str(out_b),
                    "--soft-seconds", "5.0", "--hard-seconds", "20.0"
                ], capture_output=True, text=True, env=env, timeout=15)
                self.assertEqual(p_b.returncode, 0)
                d_b = json.loads(out_b.read_text())
                self.assertEqual(d_b["verdict"], "PASS")

                # Step 4: Verify Head A clone and slow worker survived completely untouched
                self.assertIsNone(slow_worker.poll(), "Slow worker in clone A must survive Head B acceptance")
                self.assertTrue(clone_a_dir.exists(), "Clone A directory must not be deleted")
                self.assertTrue(canary_a.exists(), "Canary file in clone A must remain intact")
                self.assertEqual(canary_a.read_text(), "head_a_canary_bytes")
                head_in_a = subprocess.run(["git", "-C", str(clone_a_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
                self.assertEqual(head_in_a, head_a, "Head in clone A must remain head A")
            finally:
                slow_worker.kill()
                slow_worker.wait()

            # Step 5: Test existing exact-head cached clone with mismatch fails closed without deleting/mutating
            key_b = hashlib.sha256(("test/repo@%s" % head_b).encode("utf-8")).hexdigest()[:16]
            clone_b_dir = scratch_base / f"accept-{key_b}" / "clone"
            self.assertTrue(clone_b_dir.exists())
            canary_b = clone_b_dir / "canary_b.txt"
            canary_b.write_text("head_b_canary_bytes")

            # Corrupt HEAD in cached clone B
            subprocess.run(["git", "-C", str(clone_b_dir), "commit", "--allow-empty", "-m", "corrupt"], check=True, capture_output=True)
            corrupt_head = subprocess.run(["git", "-C", str(clone_b_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            self.assertNotEqual(corrupt_head, head_b)

            out_mismatch = td_path / "out_mismatch.json"
            p_mismatch = subprocess.run([
                sys.executable, str(ACCEPT_UNIT), str(job_b),
                "--tests", "discover -s . -p test_*.py",
                "--out", str(out_mismatch),
                "--soft-seconds", "5.0", "--hard-seconds", "20.0"
            ], capture_output=True, text=True, env=env, timeout=15)
            self.assertEqual(p_mismatch.returncode, 0)
            d_mismatch = json.loads(out_mismatch.read_text())
            self.assertEqual(d_mismatch["verdict"], "FAIL", "Cached clone mismatch must fail closed")
            self.assertEqual(d_mismatch["clone_head_matches"], False)
            self.assertTrue(clone_b_dir.exists(), "Existing clone must NOT be rmtree'd")
            self.assertTrue(canary_b.exists(), "Existing clone files must NOT be deleted")
            self.assertEqual(canary_b.read_text(), "head_b_canary_bytes")
            self.assertEqual(
                subprocess.run(["git", "-C", str(clone_b_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip(),
                corrupt_head,
                "Existing clone must NOT be mutated"
            )

            # Step 6: Scratch path inaccessible fails closed without global temp fallback
            blocked_file = td_path / "blocked_file"
            blocked_file.write_text("not a directory")
            env_blocked = os.environ.copy()
            env_blocked["HERMES_HOME"] = str(blocked_file)
            env_blocked["PATH"] = str(bin_dir) + ":" + env_blocked.get("PATH", "")
            out_blocked = td_path / "out_blocked.json"

            p_blocked = subprocess.run([
                sys.executable, str(ACCEPT_UNIT), str(job_b),
                "--out", str(out_blocked),
                "--soft-seconds", "5.0",
                "--hard-seconds", "20.0",
            ], capture_output=True, text=True, env=env_blocked, timeout=10)
            self.assertEqual(p_blocked.returncode, 0)
            d_blocked = json.loads(out_blocked.read_text())
            self.assertEqual(d_blocked["verdict"], "FAIL")
            self.assertIn("inaccessible", d_blocked["tests"]["error"].lower())

    def test_35_unknown_identity_none_with_live_pid_preserves_and_blocks(self):
        """auto_accept fails closed when identity read returns None for live PID: preserves marker and blocks dispatch."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    pid = dummy.pid
                    (home_dir / "lab" / "journal").mkdir(parents=True, exist_ok=True)
                    runs_dir = home_dir / "commitments" / "runs"
                    runs_dir.mkdir(parents=True, exist_ok=True)
                    job_dir = home_dir / "job_unknown_ident"
                    job_dir.mkdir(parents=True, exist_ok=True)
                    (job_dir / "job.json").write_text('{"repository": "repo", "branch": "b", "mailbox": "m", "write_allowlist": []}')
                    packet_path = job_dir / "review_packet.json"
                    packet_path.write_text('{"head_sha": "abc"}')
                    (runs_dir / "unknown_item.log").write_text(f'REVIEW_REQUIRED {{"packet": "{packet_path}"}}')

                    running_file = job_dir / "auto_accept-unknown_item.running"
                    # Marker with recorded starttime
                    running_file.write_text(f"{pid} 1234567 {int(time.time() - 100)} 2026-10-14 12:00:00")

                    # Mock get_proc_identity returning None while os.kill(pid, 0) succeeds
                    old_get_ident = aa.get_proc_identity
                    aa.get_proc_identity = lambda p: None

                    class MockCommit:
                        DISPATCHED = "DISPATCHED"
                        @staticmethod
                        def handle_status(h):
                            return ("finished", None)
                        class Ledger:
                            def __init__(self):
                                self.data = {"items": [{"id": "unknown_item", "state": "DISPATCHED", "handle": "h1"}]}
                            def __enter__(self):
                                return self
                            def __exit__(self, *args):
                                pass

                    old_commit = aa.c
                    aa.c = MockCommit()
                    fake_popen_called = False
                    def fake_popen(*args, **kwargs):
                        nonlocal fake_popen_called
                        fake_popen_called = True
                        return dummy

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen
                    try:
                        # 1. check_marker_liveness directly: must return True and NOT unlink
                        self.assertTrue(aa.check_marker_liveness(running_file), "check_marker_liveness must return True for live PID with unknown identity")
                        self.assertTrue(running_file.exists(), "running_file must NOT be unlinked on unknown identity read failure")

                        # 2. main(): must block duplicate launch
                        aa.main()
                        self.assertFalse(fake_popen_called, "main must NOT dispatch duplicate worker when identity read fails for live PID")
                        self.assertTrue(running_file.exists())
                    finally:
                        aa.get_proc_identity = old_get_ident
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    dummy.kill()
                    dummy.wait()
            finally:
                aa.HOME = old_home

    def test_36_malformed_marker_preserves_and_blocks(self):
        """auto_accept fails closed on malformed markers: preserves marker and blocks dispatch."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                (home_dir / "lab" / "journal").mkdir(parents=True, exist_ok=True)
                runs_dir = home_dir / "commitments" / "runs"
                runs_dir.mkdir(parents=True, exist_ok=True)
                job_dir = home_dir / "job_malformed"
                job_dir.mkdir(parents=True, exist_ok=True)
                (job_dir / "job.json").write_text('{"repository": "repo", "branch": "b", "mailbox": "m", "write_allowlist": []}')
                packet_path = job_dir / "review_packet.json"
                packet_path.write_text('{"head_sha": "abc"}')
                (runs_dir / "malformed_item.log").write_text(f'REVIEW_REQUIRED {{"packet": "{packet_path}"}}')

                running_file = job_dir / "auto_accept-malformed_item.running"

                class MockCommit:
                    DISPATCHED = "DISPATCHED"
                    @staticmethod
                    def handle_status(h):
                        return ("finished", None)
                    class Ledger:
                        def __init__(self):
                            self.data = {"items": [{"id": "malformed_item", "state": "DISPATCHED", "handle": "h1"}]}
                        def __enter__(self):
                            return self
                        def __exit__(self, *args):
                            pass

                old_commit = aa.c
                aa.c = MockCommit()
                fake_popen_called = False
                def fake_popen(*args, **kwargs):
                    nonlocal fake_popen_called
                    fake_popen_called = True
                    return None

                old_popen = aa.subprocess.Popen
                aa.subprocess.Popen = fake_popen
                try:
                    malformed_cases = [
                        "",  # empty
                        "not_a_pid_text",
                        "0 12345 67890",
                        "-1 12345 67890",
                        "corrupt data garbage",
                    ]
                    for bad_raw in malformed_cases:
                        running_file.write_text(bad_raw)
                        # check_marker_liveness must preserve and block
                        self.assertTrue(aa.check_marker_liveness(running_file), f"Malformed marker '{bad_raw}' must return True (block)")
                        self.assertTrue(running_file.exists(), f"Malformed marker '{bad_raw}' must be preserved")

                        fake_popen_called = False
                        aa.main()
                        self.assertFalse(fake_popen_called, f"Malformed marker '{bad_raw}' must block launch in main")
                        self.assertTrue(running_file.exists())
                finally:
                    aa.subprocess.Popen = old_popen
                    aa.c = old_commit
            finally:
                aa.HOME = old_home

    def test_37_launch_get_proc_identity_failure_retains_quarantine_marker(self):
        """auto_accept retains quarantine marker when get_proc_identity fails at launch, preventing repeat launches."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    pid = dummy.pid
                    (home_dir / "lab" / "journal").mkdir(parents=True, exist_ok=True)
                    runs_dir = home_dir / "commitments" / "runs"
                    runs_dir.mkdir(parents=True, exist_ok=True)
                    job_dir = home_dir / "job_quarantine"
                    job_dir.mkdir(parents=True, exist_ok=True)
                    (job_dir / "job.json").write_text('{"repository": "repo", "branch": "b", "mailbox": "m", "write_allowlist": []}')
                    packet_path = job_dir / "review_packet.json"
                    packet_path.write_text('{"head_sha": "abc"}')
                    (runs_dir / "quarantine_item.log").write_text(f'REVIEW_REQUIRED {{"packet": "{packet_path}"}}')

                    running_file = job_dir / "auto_accept-quarantine_item.running"

                    class MockCommit:
                        DISPATCHED = "DISPATCHED"
                        @staticmethod
                        def handle_status(h):
                            return ("finished", None)
                        class Ledger:
                            def __init__(self):
                                self.data = {"items": [{"id": "quarantine_item", "state": "DISPATCHED", "handle": "h1"}]}
                            def __enter__(self):
                                return self
                            def __exit__(self, *args):
                                pass

                    old_commit = aa.c
                    aa.c = MockCommit()

                    # Mock get_proc_identity to return None (launch identity failure)
                    old_get_ident = aa.get_proc_identity
                    aa.get_proc_identity = lambda p: None

                    fake_popen_count = 0
                    def fake_popen(*args, **kwargs):
                        nonlocal fake_popen_count
                        fake_popen_count += 1
                        return dummy

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen
                    try:
                        # Tick 1: Launches worker, but identity read fails
                        aa.main()
                        self.assertEqual(fake_popen_count, 1)
                        self.assertTrue(running_file.exists())
                        marker_text = running_file.read_text()
                        self.assertIn("quarantine", marker_text, "Marker must record quarantine status")
                        self.assertNotIn(f"{pid} 0 ", marker_text, "Must NOT write starttime 0 which would lead to repeat launch")

                        # Tick 2: Quarantine marker is retained and blocks repeat launch
                        aa.main()
                        self.assertEqual(fake_popen_count, 1, "Must NOT launch duplicate worker on next tick")
                        self.assertTrue(running_file.exists(), "Quarantine marker must be retained")
                    finally:
                        aa.get_proc_identity = old_get_ident
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    dummy.kill()
                    dummy.wait()
            finally:
                aa.HOME = old_home

    def test_38_global_scan_mixed_states_and_reversed_ledger_order(self):
        """auto_accept global scan checks registered job markers across all items even when state is VERIFIED/OPEN/etc."""
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
                try:
                    pid = dummy.pid
                    ident = aa.get_proc_identity(pid)
                    self.assertIsNotNone(ident)

                    (home_dir / "lab" / "journal").mkdir(parents=True, exist_ok=True)
                    runs_dir = home_dir / "commitments" / "runs"
                    runs_dir.mkdir(parents=True, exist_ok=True)

                    # Job 1: VERIFIED state in ledger, but has live running worker marker!
                    job1_dir = home_dir / "job_verified"
                    job1_dir.mkdir(parents=True, exist_ok=True)
                    (job1_dir / "job.json").write_text('{"repository": "repo", "branch": "b1", "mailbox": "m1", "write_allowlist": []}')
                    p1 = job1_dir / "review_packet.json"
                    p1.write_text('{"head_sha": "sha1"}')
                    (runs_dir / "item_verified.log").write_text(f'REVIEW_REQUIRED {{"packet": "{p1}"}}')
                    running_1 = job1_dir / "auto_accept-item_verified.running"
                    running_1.write_text(f"{pid} {ident[1]} {int(time.time() - 200)} 2026-10-14 12:00:00")

                    # Job 2: DISPATCHED state, finished handle, ready to launch
                    job2_dir = home_dir / "job_dispatched"
                    job2_dir.mkdir(parents=True, exist_ok=True)
                    (job2_dir / "job.json").write_text('{"repository": "repo", "branch": "b2", "mailbox": "m2", "write_allowlist": []}')
                    p2 = job2_dir / "review_packet.json"
                    p2.write_text('{"head_sha": "sha2"}')
                    (runs_dir / "item_dispatched.log").write_text(f'REVIEW_REQUIRED {{"packet": "{p2}"}}')

                    # Reversed ledger order: DISPATCHED candidate is first, VERIFIED item with live worker is second!
                    class MockCommit:
                        DISPATCHED = "DISPATCHED"
                        VERIFIED = "VERIFIED"
                        OPEN = "OPEN"
                        @staticmethod
                        def handle_status(h):
                            return ("finished", None)
                        class Ledger:
                            def __init__(self, item_order):
                                self.data = {"items": item_order}
                            def __enter__(self):
                                return self
                            def __exit__(self, *args):
                                pass

                    old_commit = aa.c
                    fake_popen_called = False
                    def fake_popen(*args, **kwargs):
                        nonlocal fake_popen_called
                        fake_popen_called = True
                        return dummy

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen
                    orig_ledger_cls = MockCommit.Ledger
                    try:
                        # Case A: item_dispatched first, item_verified second
                        aa.c = MockCommit
                        aa.c.Ledger = lambda: orig_ledger_cls([
                            {"id": "item_dispatched", "state": "DISPATCHED", "handle": "h_disp"},
                            {"id": "item_verified", "state": "VERIFIED", "handle": "h_ver"},
                        ])
                        fake_popen_called = False
                        aa.main()
                        self.assertFalse(fake_popen_called, "Live worker in VERIFIED item must block DISPATCHED item even in reversed order")

                        # Case B: item_verified has state OPEN
                        aa.c.Ledger = lambda: orig_ledger_cls([
                            {"id": "item_dispatched", "state": "DISPATCHED", "handle": "h_disp"},
                            {"id": "item_verified", "state": "OPEN", "handle": "h_ver"},
                        ])
                        fake_popen_called = False
                        aa.main()
                        self.assertFalse(fake_popen_called, "Live worker in OPEN item must block DISPATCHED item")
                    finally:
                        MockCommit.Ledger = orig_ledger_cls
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    dummy.kill()
                    dummy.wait()
            finally:
                aa.HOME = old_home

    def test_39_marker_liveness_eperm_and_unknown_errno_preserves_marker(self):
        """auto_accept fails closed when os.kill raises EPERM (PermissionError) or unknown OSError:
        marker is preserved and blocks dispatch. Only ESRCH / ProcessLookupError proves process is dead.
        """
        import engineering.acceptance.auto_accept as aa
        with tempfile.TemporaryDirectory() as td:
            home_dir = Path(td)
            old_home = aa.HOME
            aa.HOME = home_dir
            try:
                (home_dir / "lab" / "journal").mkdir(parents=True, exist_ok=True)
                runs_dir = home_dir / "commitments" / "runs"
                runs_dir.mkdir(parents=True, exist_ok=True)
                job_dir = home_dir / "job_eperm"
                job_dir.mkdir(parents=True, exist_ok=True)
                (job_dir / "job.json").write_text('{"repository": "repo", "branch": "b", "mailbox": "m", "write_allowlist": []}')
                packet_path = job_dir / "review_packet.json"
                packet_path.write_text('{"head_sha": "abc"}')
                (runs_dir / "eperm_item.log").write_text(f'REVIEW_REQUIRED {{"packet": "{packet_path}"}}')

                running_file = job_dir / "auto_accept-eperm_item.running"
                test_pid = 99999
                test_start = int(time.time() - 50)
                marker_content = f"{test_pid} 12345 {test_start} 2026-10-14 12:00:00"

                old_kill = aa.os.kill
                old_ident = aa.get_proc_identity
                try:
                    # 1. EPERM (PermissionError): must preserve marker and return True (block)
                    running_file.write_text(marker_content)
                    def mock_kill_eperm(pid, sig):
                        raise PermissionError(errno.EPERM, "Operation not permitted")
                    aa.os.kill = mock_kill_eperm

                    self.assertTrue(aa.check_marker_liveness(running_file), "EPERM does not prove dead: must return True")
                    self.assertTrue(running_file.exists(), "EPERM must NOT delete running marker")
                    self.assertFalse(aa.is_proc_alive(test_pid), "is_proc_alive may return False for unverified PID")
                    self.assertTrue(running_file.exists(), "is_proc_alive must not perform any cleanup")

                    # 2. Unknown errno (e.g. EINVAL): must preserve marker and return True (block)
                    running_file.write_text(marker_content)
                    def mock_kill_unknown(pid, sig):
                        err = OSError()
                        err.errno = errno.EINVAL
                        err.strerror = "Invalid argument"
                        raise err
                    aa.os.kill = mock_kill_unknown

                    self.assertTrue(aa.check_marker_liveness(running_file), "Unknown OSError does not prove dead: must return True")
                    self.assertTrue(running_file.exists(), "Unknown OSError must NOT delete running marker")

                    # 3. Definitely DEAD via ProcessLookupError: cleans up and returns False
                    running_file.write_text(marker_content)
                    def mock_kill_ple(pid, sig):
                        raise ProcessLookupError(errno.ESRCH, "No such process")
                    aa.os.kill = mock_kill_ple

                    self.assertFalse(aa.check_marker_liveness(running_file), "ProcessLookupError proves dead: must return False")
                    self.assertFalse(running_file.exists(), "ProcessLookupError must clean up running marker")

                    # 4. Definitely DEAD via OSError with errno == ESRCH: cleans up and returns False
                    running_file.write_text(marker_content)
                    def mock_kill_oserror_esrch(pid, sig):
                        err = OSError()
                        err.errno = errno.ESRCH
                        err.strerror = "No such process"
                        raise err
                    aa.os.kill = mock_kill_oserror_esrch

                    self.assertFalse(aa.check_marker_liveness(running_file), "ESRCH proves dead: must return False")
                    self.assertFalse(running_file.exists(), "ESRCH must clean up running marker")

                    # 5. Recycled PID (PID alive but identity mismatch): cleans up and returns False
                    running_file.write_text(marker_content)
                    aa.os.kill = lambda p, s: None
                    aa.get_proc_identity = lambda p: (p, 999999)

                    self.assertFalse(aa.check_marker_liveness(running_file), "Recycled PID must return False")
                    self.assertFalse(running_file.exists(), "Recycled PID must clean up running marker")

                    # 6. Full main() integration test under EPERM: must block duplicate launch and preserve marker
                    running_file.write_text(marker_content)
                    aa.os.kill = mock_kill_eperm
                    aa.get_proc_identity = old_ident

                    class MockCommit:
                        DISPATCHED = "DISPATCHED"
                        @staticmethod
                        def handle_status(h):
                            return ("finished", None)
                        class Ledger:
                            def __init__(self):
                                self.data = {"items": [{"id": "eperm_item", "state": "DISPATCHED", "handle": "h1"}]}
                            def __enter__(self):
                                return self
                            def __exit__(self, *args):
                                pass

                    old_commit = aa.c
                    aa.c = MockCommit()
                    fake_popen_called = False
                    def fake_popen(*args, **kwargs):
                        nonlocal fake_popen_called
                        fake_popen_called = True
                        return None

                    old_popen = aa.subprocess.Popen
                    aa.subprocess.Popen = fake_popen
                    try:
                        aa.main()
                        self.assertFalse(fake_popen_called, "main must NOT dispatch duplicate worker when marker has EPERM")
                        self.assertTrue(running_file.exists(), "main must preserve marker when EPERM")

                        # Under unknown errno, main() also preserves and blocks
                        running_file.write_text(marker_content)
                        aa.os.kill = mock_kill_unknown
                        fake_popen_called = False
                        aa.main()
                        self.assertFalse(fake_popen_called, "main must NOT dispatch duplicate worker when marker has unknown errno")
                        self.assertTrue(running_file.exists(), "main must preserve marker when unknown errno")
                    finally:
                        aa.subprocess.Popen = old_popen
                        aa.c = old_commit
                finally:
                    aa.os.kill = old_kill
                    aa.get_proc_identity = old_ident
            finally:
                aa.HOME = old_home


if __name__ == "__main__":
    unittest.main()
