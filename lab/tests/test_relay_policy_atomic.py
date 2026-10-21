"""Offline preliminary atomic dispatch contract tests, stdlib-only.

Not a substitute for browser/CDP integration or installed producer acceptance.
"""
import datetime
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[2] / "engineering" / "relay" / "product_dispatch.py"
spec = importlib.util.spec_from_file_location("product_dispatch", SOURCE)
dispatch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)


class AtomicDispatchTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.base = Path(self.scratch.name)
        self.job = {"repository": "synthetic/example", "branch": "test-branch",
                    "pr_number": 123, "write_allowlist": ["synthetic.py"],
                    "valid_until": (datetime.datetime.now(datetime.timezone.utc) +
                                    datetime.timedelta(hours=1)).isoformat(),
                    "dispatch_chat_url": "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
                    "dispatch_lock_root": str(self.base / "shared")}
        self.first = self.base / "job-a"
        self.second = self.base / "job-b"
        for jobdir in (self.first, self.second):
            jobdir.mkdir()
            (jobdir / "job.json").write_text(json.dumps(self.job), encoding="utf-8")

    def test_shared_product_claim_winner_truth(self):
        owner = dispatch.claim(self.job, self.first, "one")
        with self.assertRaises(dispatch.DispatchBlocked) as result:
            dispatch.claim(self.job, self.second, "two")
        self.assertEqual(result.exception.winner, owner["owner"])
        self.assertEqual(dispatch.check(self.job, self.first, "one")["phase"], "PREPARED")

    def test_separate_product_is_independent(self):
        one = dispatch.claim(self.job, self.first, "one")
        second = dict(self.job, pr_number=124)
        (self.second / "job.json").write_text(json.dumps(second), encoding="utf-8")
        two = dispatch.claim(second, self.second, "two")
        self.assertNotEqual(one["generation"], two["generation"])

    def test_scope_change_aborts(self):
        dispatch.claim(self.job, self.first, "one")
        changed = dict(self.job, write_allowlist=["other.py"])
        (self.first / "job.json").write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.check(self.job, self.first, "one", phase="send")

    def test_unknown_send_does_not_reopen(self):
        dispatch.claim(self.job, self.first, "one")
        dispatch.phase(self.job, self.first, "one", "SEND_INTENT")
        dispatch.phase(self.job, self.first, "one", "SEND_UNKNOWN")
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.claim(self.job, self.second, "two")
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.check(self.job, self.first, "one", phase="send")

    def test_no_implicit_configuration(self):
        job = dict(self.job)
        job.pop("dispatch_lock_root")
        with self.assertRaisesRegex(dispatch.DispatchBlocked, "UNCONFIGURED"):
            dispatch.claim(job, self.first, "one")

    def test_real_competing_subprocess_winner(self):
        script = (
            "import importlib.util,json,sys,time\n"
            "s=importlib.util.spec_from_file_location('d',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n"
            "j=json.loads(sys.argv[2])\n"
            "try:\n"
            " r=m.claim(j,sys.argv[3],sys.argv[4]);print(json.dumps({'ok':True,'owner':r['owner']}))\n"
            "except m.DispatchBlocked as e:\n"
            " print(json.dumps({'ok':False,'reason':str(e),'winner':e.winner}))\n")
        processes = [
            subprocess.Popen([sys.executable, "-c", script, str(SOURCE),
                              json.dumps(self.job), str(jd), name],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for jd, name in ((self.first, "one"), (self.second, "two"))]
        values = []
        try:
            for process in processes:
                out, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, err)
                values.append(json.loads(out))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
        self.assertEqual(sum(value["ok"] for value in values), 1)
        winner = next(value["owner"] for value in values if value["ok"])
        self.assertEqual(next(value["winner"] for value in values if not value["ok"]), winner)


if __name__ == "__main__":
    unittest.main()
