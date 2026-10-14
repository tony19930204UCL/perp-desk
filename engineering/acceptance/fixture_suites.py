#!/usr/bin/env python3
"""Dedicated test suites used as real subprocess fixtures for progress runner testing.

Designed with small, bounded sleep intervals (sub-second) so testing is fast
and does not materially slow down the lab test suite.
"""
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest


class SlowProgressSuite(unittest.TestCase):
    """Suite with tests progressing steadily across soft checkpoints."""

    def test_step_1(self):
        time.sleep(0.08)
        self.assertTrue(True)

    def test_step_2(self):
        time.sleep(0.08)
        self.assertTrue(True)

    def test_step_3(self):
        time.sleep(0.08)
        self.assertTrue(True)

    def test_step_4(self):
        time.sleep(0.08)
        self.assertTrue(True)

    def test_step_5(self):
        time.sleep(0.08)
        self.assertTrue(True)


class LegitimateLongSuite(unittest.TestCase):
    """Suite with a single long test that stays within explicit item budget."""

    def test_long_operation(self):
        time.sleep(0.35)
        self.assertTrue(True)


class SleepingStallSuite(unittest.TestCase):
    """Suite that stalls due to sleeping without making progress."""

    def test_sleep_forever(self):
        time.sleep(10.0)


class BusyLoopSuite(unittest.TestCase):
    """Suite that loops on CPU without making progress."""

    def test_busy_loop(self):
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            pass


class OutputBufferSuite(unittest.TestCase):
    """Suite verifying unittest buffer option does not suppress lifecycle."""

    def test_buffered_output(self):
        sys.stdout.write("FIXTURE_STDOUT_LINE_1\n")
        sys.stderr.write("FIXTURE_STDERR_LINE_1\n")
        sys.stdout.flush()
        sys.stderr.flush()
        self.assertTrue(True)


class NoStdoutSuite(unittest.TestCase):
    """Suite producing zero stdout/stderr, verified via lifecycle JSONL."""

    def test_silent_1(self):
        self.assertTrue(True)

    def test_silent_2(self):
        self.assertTrue(True)


class DescendantSpawningSuite(unittest.TestCase):
    """Suite spawning a background process that must be cleaned up."""

    def test_spawn_child(self):
        pid_file = os.environ.get("DESCENDANT_PID_FILE")
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        if pid_file:
            Path(pid_file).write_text(str(p.pid))
        self.assertTrue(True)


class LongRunningSuite(unittest.TestCase):
    """Suite intended to be interrupted/cancelled by SIGTERM."""

    def test_long_wait(self):
        time.sleep(15.0)


class QuickPassSuite(unittest.TestCase):
    """Fast clean suite used for deduplication and recovery tests."""

    def test_fast_alpha(self):
        self.assertEqual(1, 1)

    def test_fast_beta(self):
        self.assertEqual(2, 2)


class PartialSuite(unittest.TestCase):
    """Suite where first test calls os._exit, leaving suite incomplete."""

    def test_unclean_exit(self):
        # Abruptly exit process, preventing test_unreachable from running
        os._exit(0)

    def test_unreachable(self):
        self.assertTrue(True)


class SkipSuite(unittest.TestCase):
    """Suite with explicit skips distinct from pass/fail."""

    def test_passing_item(self):
        self.assertTrue(True)

    @unittest.skip("demonstrating distinct skipped test accounting")
    def test_skipped_item(self):
        self.assertTrue(False)


class RealFailureSuite(unittest.TestCase):
    """Suite with an actual assertion failure."""

    def test_intentional_failure(self):
        self.assertEqual(1, 2, "intentional fixture assertion failure")


class LongSilentTestSurvivesSoftSuite(unittest.TestCase):
    """Single test with no stdout taking ~0.35s to survive soft checkpoint and complete."""

    def test_silent_survivor(self):
        time.sleep(0.35)
        self.assertTrue(True)


class SubtestFailureSuite(unittest.TestCase):
    """Suite with subtests where one subtest fails."""

    def test_subtest_lifecycle(self):
        with self.subTest(case="alpha_pass"):
            self.assertEqual(1, 1)
        with self.subTest(case="beta_fail"):
            self.assertEqual(1, 2, "intentional subtest assertion failure")
        with self.subTest(case="gamma_pass"):
            self.assertEqual(3, 3)


class FailfastSuite(unittest.TestCase):
    """Suite verifying --failfast stops execution immediately after the first failure."""

    def test_first_failing(self):
        self.assertEqual(10, 20, "failfast deliberate failure")

    def test_second_skipped_by_failfast(self):
        self.assertTrue(True)


class EmptySuite(unittest.TestCase):
    """Suite containing no tests, verifying empty discovery rejection."""
    pass


if __name__ == "__main__":
    unittest.main()
