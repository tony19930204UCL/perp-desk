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
sys.modules['product_dispatch'] = dispatch
sys.path.insert(0, str(SOURCE.parent))
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

    def test_claim_winner(self):
        owner = dispatch.claim(self.job, self.first, "one")
        with self.assertRaises(dispatch.DispatchBlocked) as result:
            dispatch.claim(self.job, self.second, "two")
        self.assertEqual(result.exception.winner, owner["owner"])
        self.assertEqual(dispatch.check(self.job, self.first, "one")["phase"], "PREPARED")

    def test_distinct_products(self):
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

    def test_unknown_hold(self):
        dispatch.claim(self.job, self.first, "one")
        dispatch.phase(self.job, self.first, "one", "SEND_INTENT")
        dispatch.phase(self.job, self.first, "one", "SEND_UNKNOWN")
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.claim(self.job, self.second, "two")
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.check(self.job, self.first, "one", phase="send")

    def test_no_default_root(self):
        job = dict(self.job)
        job.pop("dispatch_lock_root")
        with self.assertRaisesRegex(dispatch.DispatchBlocked, "UNCONFIGURED"):
            dispatch.claim(job, self.first, "one")

    def test_two_proc_claim(self):
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
        import approval_transaction as tx
        tx._fetch_base = lambda job, path: 'old = 0\n'
        # Test admission on the public API before any lifecycle effect.
        positive = self._card("wire")
        negative = dict(positive, path="mcp__GitHub__update_file")
        args = (self.job["repository"], self.job["branch"],
                "synthetic.py", {"synthetic.py"}, self.job["pr_number"])
        self.assertTrue(carrier.approve_operation(positive, *args))
        self.assertFalse(carrier.approve_operation(negative, *args))
        return carrier

    def test_wire_gate(self):
        """Sanitized slash operation admitted; malformed MCP path refused."""
        carrier = self._carrier()
        operation = self._card("admit")
        malformed = dict(operation, path="mcp__GitHub__update_file")
        args = (self.job["repository"], self.job["branch"],
                "synthetic.py", {"synthetic.py"}, self.job["pr_number"])
        self.assertTrue(carrier.approve_operation(operation, *args))
        self.assertFalse(carrier.approve_operation(malformed, *args))

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

    def test_carrier_once(self):
        call, clicks, _ = self._sent()
        call()
        self.assertEqual(clicks, ["send"])
        self.assertEqual(dispatch.inspect(self.job, self.first, "one")["phase"],
                         "SENT_CONFIRMED")
        with self.assertRaises(dispatch.DispatchBlocked):
            call()
        self.assertEqual(clicks, ["send"])

    def test_unknown_no_retry(self):
        call, clicks, _ = self._sent(unknown=True)
        with self.assertRaises(RuntimeError):
            call()
        self.assertEqual(clicks, ["send"])
        self.assertEqual(dispatch.inspect(self.job, self.first, "one")["phase"],
                         "SEND_UNKNOWN")
        with self.assertRaises(dispatch.DispatchBlocked):
            call()
        self.assertEqual(clicks, ["send"])

    def test_scope_before_click(self):
        carrier = self._carrier()
        call, clicks, save = self._sent()
        call()
        op = {"path": "/connector_sanitized/link_sanitized/update_file",
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
                dispatch_job=self.job, dispatch_dir=self.first, instruction_id="one",
                event_fn=lambda *a,**kw: None)
        self.assertEqual(effects, [])
        self.assertEqual(dispatch.inspect(dict(self.job, write_allowlist=["changed.py"]),
                                          self.first, "one")["phase"], "SENT_CONFIRMED")

    def test_no_source_click(self):
        carrier = self._carrier()
        dispatch.claim(self.job, self.first, "one")
        op = {"path": "/connector_sanitized/link_sanitized/update_file",
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
                dispatch_job=self.job, dispatch_dir=self.first, instruction_id="one",
                event_fn=lambda *a,**kw: None)
        self.assertFalse(clicked)



    def test_card_once(self):
        carrier = self._carrier()
        call, sent, save = self._sent()
        call()
        self.assertEqual(sent, ["send"])
        op = {"path": "/connector_sanitized/link_sanitized/update_file",
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
                      instruction_id="one", event_fn=lambda *a,**kw: None)
        self.assertTrue(carrier.permission(
            {"text": "要允許 ChatGPT 使用 GitHub 嗎？"}, **params))
        self.assertEqual(approvals, [True])
        self.assertEqual([x["phase"] for x in dispatch.inspect(self.job, self.first, "one")["cards"].values()], ["APPROVED"])
        with self.assertRaises(dispatch.DispatchBlocked):
            carrier.permission({"text": "要允許 ChatGPT 使用 GitHub 嗎？"}, **params)
        self.assertEqual(approvals, [True])



    def _permit_card(self, card, callback=None):
        carrier = self._carrier()
        clicks = []
        def cdp(js):
            if "JSON.stringify" in js:
                return ["call_tool\n" + json.dumps(card)]
            if "允許一次" in js:
                clicks.append(True)
                if callback:
                    callback()
            return True
        return carrier.permission({"text": "要允許 ChatGPT 使用 GitHub 嗎？"},
            cdp_fn=cdp, snapshot_fn=lambda: {"text": ""},
            repo=self.job["repository"], branch=self.job["branch"],
            mailbox="synthetic.py", allowed={"synthetic.py"},
            pr_number=self.job["pr_number"],
            save_fn=lambda name, data:(self.first / name).write_text(json.dumps(data)),
            event_fn=lambda *a, **kw: None, dispatch_job=self.job,
            dispatch_dir=self.first, instruction_id="one"), clicks

    def _card(self, value):
        return {"path":"/connector_sanitized/link_sanitized/update_file",
                "args":{"repository_full_name":self.job["repository"],
                        "branch":self.job["branch"],"path":"synthetic.py",
                        "content":"value = " + value}}

    def test_two_cards(self):
        call, _, _ = self._sent()
        call()
        self.assertEqual(self._permit_card(self._card("1")), (True, [True]))
        self.assertEqual(self._permit_card(self._card("2")), (True, [True]))
        self.assertEqual(len(dispatch.inspect(self.job, self.first, "one")["cards"]), 2)
        with self.assertRaisesRegex(dispatch.DispatchBlocked, "CARD_ALREADY_ATTEMPTED"):
            self._permit_card(self._card("1"))

    def test_card_unknown_hold(self):
        call, _, _ = self._sent()
        call()
        effects = []
        def unknown():
            effects.append(True)
            raise RuntimeError("SYNTHETIC_UNKNOWN")
        with self.assertRaisesRegex(RuntimeError, "SYNTHETIC_UNKNOWN"):
            self._permit_card(self._card("3"), unknown)
        with self.assertRaises(dispatch.DispatchBlocked):
            self._permit_card(self._card("3"), unknown)
        self.assertEqual(effects, [True])

    def test_adopt_send_source(self):
        call, _, _ = self._sent()
        call()
        prior = dispatch.inspect(self.job, self.first, "one")
        other = dict(self.job, dispatch_lock_root=str(self.base / "isolated-import"))
        (self.first / "job.json").write_text(json.dumps(other))
        scope = dispatch._scope(other, self.first, "one")[2]
        proof = {"receipt_sha256":prior["send_receipt_sha256"],
                 "repository":scope["repo"],"pr_number":scope["pr"],
                 "branch":scope["branch"],"chat":scope["chat"],
                 "instruction_id":"one","scope_sha256":dispatch._sha(scope)}
        other["dispatch_adopt_source"] = proof
        self.assertEqual(dispatch.adopt_send(other, self.first, "one")["phase"],
                         "SENT_CONFIRMED")
        self.job = other
        self.assertEqual(self._permit_card(self._card("4")), (True, [True]))

    def test_market_adopt_denied(self):
        dispatch.adopt_existing(self.job, self.first, "one", dispatch._identity())
        (self.first / "send_one.json").write_text(json.dumps({
            "status":"CONFIRMED","instruction_id":"one",
            "chat_url":self.job["dispatch_chat_url"],
            "evidence_kind":"EXACT_RENDERED_USER_BUBBLE","text_hash":"a"*64}))
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.approval_once(self.job, self.first, "one", "a"*64)



    def _check(self):
        import approval_transaction as tx
        return tx

    def _write_op(self, content, mode="create_file", path="safe.txt"):
        return {"path": "/connector_sanitized/link_sanitized/" + mode,
                "args": {"repository_full_name": self.job["repository"],
                         "branch": self.job["branch"], "path": path, "content": content}}

    def test_bound_create(self):
        tx = self._check()
        job = dict(self.job, write_allowlist=["safe.txt"])
        for n in (180, 200, 238, 600):
            self.assertEqual(tx.changed_policy(self._write_op("x\n" * n), job)["changed_lines"], n)
        with self.assertRaises(tx.PolicyRejected):
            tx.changed_policy(self._write_op("x\n" * 601), job)
        with self.assertRaises(tx.PolicyRejected):
            tx.changed_policy(self._write_op("x\n"), job, 601)

    def test_bound_update(self):
        tx = self._check()
        job = dict(self.job, write_allowlist=["safe.txt"])
        for n in (180, 200, 238, 600):
            op = self._write_op("x\n" * (n-1), "update_file")
            self.assertEqual(tx.changed_policy(op, job, fetch_base=lambda *a:"old\n")["changed_lines"], n)
        with self.assertRaises(tx.PolicyRejected):
            tx.changed_policy(self._write_op("x\n"*600,"update_file"), job,
                              fetch_base=lambda *a:"old\n")

    def test_syntax_names(self):
        tx = self._check()
        job = dict(self.job, write_allowlist=["safe.py"])
        name = "test_" + "meaningful_case_"*3
        const = "DIAGNOSTIC_" + "NAME_PART_"*3
        source = const + " = 1\n" + "def " + name + "():\n    return " + const + "\n"
        self.assertTrue(tx.changed_policy(self._write_op(source, path="safe.py"), job))
        for bad in ("x = " + repr(name), "# " + name,
                    "x = " + repr(const), "def " + name + "(:\n    pass\n"):
            with self.subTest(value=bad):
                with self.assertRaises(tx.PolicyRejected):
                    tx.changed_policy(self._write_op(bad, path="safe.py"), job)

    def test_secret_first(self):
        tx = self._check()
        job = dict(self.job, write_allowlist=["safe.py", ".env"])
        fragments = ["sk-"+"x"*22, "ghp_"+"z"*25, "-----BEGIN "+"PRIVATE KEY-----", ".env"]
        for value in fragments:
            with self.subTest(value=value[:6]):
                with self.assertRaises(tx.PolicyRejected):
                    tx.changed_policy(self._write_op("x = "+repr(value), path="safe.py"), job)
        with self.assertRaises(tx.PolicyRejected):
            tx.changed_policy(self._write_op("x\n", path=".env"), job)

    def test_prompt_template(self):
        template = (SOURCE.parent / "instruction-template.txt").read_text(encoding="utf-8")
        filled = template.format(repository="synthetic/example", pr_number=123,
                                 branch="test-branch", chat_url=self.job["dispatch_chat_url"],
                                 instruction_id="one", write_allowlist="['synthetic.py']",
                                 valid_until=self.job["valid_until"])
        self.assertIn("<=180", filled)
        self.assertIn("<=25", filled)
        self.assertIn("600", filled)
        runner = (SOURCE.parent / "profile_runner.py").read_text(encoding="utf-8")
        self.assertIn("instruction-template.txt", runner)


if __name__ == "__main__":
    unittest.main()
