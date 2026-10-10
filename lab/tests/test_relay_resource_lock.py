import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from relay_resource_lock import acquire, inspect, release, renew, ResourceBusyError


def contender(root, start, queue):
    start.wait()
    try:
        acquire("chrome:9222", root, str(os.getpid()), 60)
        queue.put("won")
    except ResourceBusyError:
        queue.put("busy")


class ResourceLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name

    def take(self, owner="a", now=100):
        return acquire("chrome:9222", self.root, owner, 30, now=now,
                       pid_alive=lambda pid: True)

    def test_second_owner_refused(self):
        self.take()
        with self.assertRaisesRegex(ResourceBusyError, "a"):
            self.take("b")

    def test_dead_pid_recovery(self):
        self.take()
        rec = acquire("chrome:9222", self.root, "b", 30, now=101,
                      pid_alive=lambda pid: False)
        self.assertEqual(rec["previous_owner"], "a")
        self.assertTrue(rec["recovered_from_dead_owner"])

    def test_expired_lease_recovery(self):
        self.take()
        rec = self.take("b", now=131)
        self.assertEqual(rec["previous_owner"], "a")
        self.assertFalse(rec["recovered_from_dead_owner"])

    def test_nonowner_release_refused(self):
        rec = self.take()
        with self.assertRaises(ResourceBusyError):
            release("chrome:9222", self.root, "b", rec["token"], now=101)
        self.assertEqual(inspect("chrome:9222", self.root, now=101)["owner_id"], "a")

    def test_release_reacquire(self):
        rec = self.take()
        self.assertTrue(release("chrome:9222", self.root, "a", rec["token"], now=101))
        self.assertEqual(self.take("b")["owner_id"], "b")

    def test_renew_extends(self):
        rec = self.take()
        newer = renew("chrome:9222", self.root, "a", rec["token"], 40, now=120)
        self.assertEqual(newer["lease_until"], 160)
        with self.assertRaises(ResourceBusyError):
            self.take("b", now=140)

    def test_corrupt_fails_closed(self):
        self.take()
        next(Path(self.root).glob("*.json")).write_text("{broken")
        with self.assertRaises(ResourceBusyError):
            self.take("b", now=200)
        with self.assertRaises(ResourceBusyError):
            inspect("chrome:9222", self.root)

    def test_inspect_truthful(self):
        self.take()
        self.assertTrue(inspect("chrome:9222", self.root, now=101,
                                pid_alive=lambda pid: True)["live"])
        self.assertTrue(inspect("chrome:9222", self.root, now=131,
                                pid_alive=lambda pid: True)["stale"])

    def test_token_prevents_aba_release(self):
        rec = self.take()
        self.take("b", now=131)
        with self.assertRaises(ResourceBusyError):
            release("chrome:9222", self.root, "a", rec["token"], now=132)

    def test_real_process_race(self):
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        queue = ctx.Queue()
        processes = [ctx.Process(target=contender, args=(self.root, start, queue))
                     for _ in range(2)]
        for proc in processes:
            proc.start()
        start.set()
        results = [queue.get(timeout=15) for _ in processes]
        for proc in processes:
            proc.join(15)
            self.assertEqual(proc.exitcode, 0)
        self.assertCountEqual(results, ["won", "busy"])


if __name__ == "__main__":
    unittest.main()
