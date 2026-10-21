"""Offline preliminary atomic dispatch contract tests, stdlib-only.

Not a substitute for browser/CDP integration or installed producer acceptance.
"""
import datetime
import importlib.util
import hashlib
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


    def _carrier(self):
        sys.path.insert(0, str(SOURCE.parent))
        import engineering_instruction_carrier as carrier
        return carrier

    def _sent(self, *, unknown=False):
        """Real carrier.send, injected only offline browser effects."""
        carrier = self._carrier()
        key = "one"
        text = "SYNTHETIC_OFFLINE_INSTRUCTION"
        self.first.mkdir(exist_ok=True)
        snap = {"url": self.job["dispatch_chat_url"],
                "user_messages": [], "composer_text": "", "busy": False}
        clicks = []
        def cdp(js):
            if "Send button unavailable" in js:
                clicks.append("send")
                if unknown:
                    raise RuntimeError("OFFLINE_SEND_UNKNOWN")
            return True
        def save(name, obj):
            (self.first / name).write_text(json.dumps(obj), encoding="utf-8")
        def after():
            return {"url": self.job["dispatch_chat_url"], "user_messages": [text],
                    "composer_text": "", "busy": False}
        dispatch.claim(self.job, self.first, key)
        def call():
            return carrier.send(text, key, snap, root=self.first,
                 chat_path="/c/11111111-2222-3333-4444-555555555555",
                 cdp_fn=cdp, snapshot_fn=after, save_fn=save,
                 valid_until=self.job["valid_until"], dispatch_job=self.job)
        return call, clicks, save

    def test_carrier_effect_exactly_once(self):
        call, clicks, _ = self._sent()
        call()
        self.assertEqual(clicks, ["send"])
        self.assertEqual(dispatch.inspect(self.job, self.first, "one")["phase"],
                         "SENT_CONFIRMED")
        with self.assertRaises(dispatch.DispatchBlocked):
            call()
        self.assertEqual(clicks, ["send"])

    def test_carrier_unknown_never_retry(self):
        call, clicks, _ = self._sent(unknown=True)
        with self.assertRaises(RuntimeError):
            call()
        self.assertEqual(clicks, ["send"])
        self.assertEqual(dispatch.inspect(self.job, self.first, "one")["phase"],
                         "SEND_UNKNOWN")
        with self.assertRaises(dispatch.DispatchBlocked):
            call()
        self.assertEqual(clicks, ["send"])

    def test_permission_source_and_scope_mutation(self):
        carrier = self._carrier()
        call, clicks, save = self._sent()
        call()
        op = {"path": "mcp__GitHub__update_file",
              "args": {"repository_full_name": self.job["repository"],
                       "branch": self.job["branch"], "path": "synthetic.py",
                       "content": "safe = 1"}}
        effects = []
        card = {"text": "要允許 ChatGPT 使用 GitHub 嗎？"}
        def cdp(js):
            if "JSON.stringify" in js:
                return ["call_tool\n" + json.dumps(op)]
            if "關閉對話框" in js:
                changed = dict(self.job, write_allowlist=["changed.py"])
                (self.first / "job.json").write_text(json.dumps(changed))
            if "允許一次" in js:
                effects.append("approval")
            return True
        with self.assertRaises(dispatch.DispatchBlocked):
            carrier.permission(card, cdp_fn=cdp, snapshot_fn=lambda: {"text": ""},
                repo=self.job["repository"], branch=self.job["branch"],
                mailbox="synthetic.py", allowed={"synthetic.py"},
                pr_number=self.job["pr_number"], save_fn=save,
                dispatch_job=self.job, dispatch_dir=self.first, instruction_id="one")
        self.assertEqual(effects, [])
        self.assertEqual(dispatch.inspect(dict(self.job, write_allowlist=["changed.py"]),
                                          self.first, "one")["phase"], "APPROVE_INTENT")

    def test_permission_requires_confirmed_source(self):
        carrier = self._carrier()
        dispatch.claim(self.job, self.first, "one")
        op = {"path": "mcp__GitHub__update_file",
              "args": {"repository_full_name": self.job["repository"],
                       "branch": self.job["branch"], "path": "synthetic.py",
                       "content": "value = 1"}}
        clicked = []
        def cdp(js):
            if "JSON.stringify" in js:
                return ["call_tool\n" + json.dumps(op)]
            if "允許一次" in js:
                clicked.append(True)
            return True
        with self.assertRaises(dispatch.DispatchBlocked):
            carrier.permission({"text":"要允許 ChatGPT 使用 GitHub 嗎？"},
                cdp_fn=cdp, snapshot_fn=lambda: {"text":""},
                repo=self.job["repository"], branch=self.job["branch"],
                mailbox="synthetic.py", allowed={"synthetic.py"},
                pr_number=self.job["pr_number"],
                save_fn=lambda name,obj:(self.first/name).write_text(json.dumps(obj)),
                dispatch_job=self.job, dispatch_dir=self.first, instruction_id="one")
        self.assertFalse(clicked)



    def test_permission_once_effect(self):
        carrier = self._carrier()
        call, sent, save = self._sent()
        call()
        self.assertEqual(sent, ["send"])
        op = {"path": "mcp__GitHub__update_file",
              "args": {"repository_full_name": self.job["repository"],
                       "branch": self.job["branch"], "path": "synthetic.py",
                       "content": "safe = 1"}}
        approvals = []
        def cdp(js):
            if "JSON.stringify" in js:
                return ["call_tool\n" + json.dumps(op)]
            if "允許一次" in js:
                approvals.append(True)
            return True
        params = dict(cdp_fn=cdp, snapshot_fn=lambda: {"text": ""},
                      repo=self.job["repository"], branch=self.job["branch"],
                      mailbox="synthetic.py", allowed={"synthetic.py"},
                      pr_number=self.job["pr_number"], save_fn=save,
                      dispatch_job=self.job, dispatch_dir=self.first,
                      instruction_id="one")
        self.assertTrue(carrier.permission(
            {"text": "要允許 ChatGPT 使用 GitHub 嗎？"}, **params))
        self.assertEqual(approvals, [True])
        self.assertEqual(dispatch.inspect(self.job, self.first, "one")["phase"], "APPROVED")
        with self.assertRaises(dispatch.DispatchBlocked):
            carrier.permission({"text": "要允許 ChatGPT 使用 GitHub 嗎？"}, **params)
        self.assertEqual(approvals, [True])



if __name__ == "__main__":
    unittest.main()
