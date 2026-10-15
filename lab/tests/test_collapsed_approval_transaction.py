"""Offline tests for policy and admission; C01-C28 browser acceptance remains pending."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engineering" / "relay"))
from approval_transaction import admission, AdmissionError, strict_policy, diff_policy, PolicyRejected, InfraError

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.j={"repository":"example/repo","branch":"feature/only",
                "write_allowlist":["path.txt"]}
        self.op={"path":"github/update_file",
                 "args":{"repository_full_name":"example/repo","branch":"feature/only",
                         "path":"path.txt","content":"safe"}}
    def test_strict_valid(self):
        self.assertEqual(strict_policy(self.op,self.j)["tool"],"update_file")
    def test_wrong_repository(self):
        self.op["args"]["repository_full_name"]="other/repo"
        with self.assertRaises(PolicyRejected): strict_policy(self.op,self.j)
    def test_wrong_branch(self):
        self.op["args"]["branch"]="main"
        with self.assertRaises(PolicyRejected): strict_policy(self.op,self.j)
    def test_outside_allowlist(self):
        self.op["args"]["path"]="different.txt"
        with self.assertRaises(PolicyRejected): strict_policy(self.op,self.j)
    def test_overlarge_content(self):
        self.op["args"]["content"]="x"*180001
        with self.assertRaises(PolicyRejected): strict_policy(self.op,self.j)
    def test_diff_valid(self):
        self.op["args"]["content"]="two"
        self.assertEqual(diff_policy(self.op,self.j,fetch_base=lambda j,p:"one")["changed_lines"],2)
    def test_diff_empty(self):
        with self.assertRaises(PolicyRejected):
            diff_policy(self.op,self.j,fetch_base=lambda j,p:"safe")
    def test_diff_base_empty_infra(self):
        with self.assertRaises(InfraError):
            diff_policy(self.op,self.j,fetch_base=lambda j,p:"")
    def test_create_cannot_diff(self):
        self.op["path"]="github/create_file"
        with self.assertRaises(PolicyRejected): diff_policy(self.op,self.j,fetch_base=lambda j,p:"base")

class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p=Path(self.temp.name)
        self.chat="https://chatgpt.com/c/synthetic-100"
        self.job={"task_id":"t1","repository":"example/repo","branch":"test",
                  "write_allowlist":["path.txt"],"chat_url":self.chat,
                  "valid_until":"2099-01-01T00:00:00+00:00"}
        self.write("job.json",self.job)
        self.write("active_instruction.json",{"task_id":"t1","instruction_id":"t1-r0","chat_url":self.chat})
        self.write("send_t1-r0.json",{"status":"CONFIRMED","chat_url":self.chat})
    def write(self,n,v):
        (self.p/n).write_text(json.dumps(v))
    def test_valid(self):
        self.assertEqual(admission(self.p)[1][1],"t1-r0")
    def test_naive_expiry(self):
        self.job["valid_until"]="2099-01-01T00:00:00"
        self.write("job.json",self.job)
        with self.assertRaises(AdmissionError): admission(self.p)
    def test_expired(self):
        self.job["valid_until"]="2000-01-01T00:00:00+00:00"
        self.write("job.json",self.job)
        with self.assertRaises(AdmissionError): admission(self.p)
    def test_homepage(self):
        self.write("active_instruction.json",{"task_id":"t1","instruction_id":"t1-r0","chat_url":"https://chatgpt.com/"})
        with self.assertRaises(AdmissionError): admission(self.p)
    def test_unknown_send(self):
        self.write("send_t1-r0.json",{"status":"SEND_UNKNOWN_NO_RETRY","chat_url":self.chat})
        with self.assertRaises(AdmissionError): admission(self.p)
    def test_wrong_active_task(self):
        self.write("active_instruction.json",{"task_id":"t2","instruction_id":"t1-r0","chat_url":self.chat})
        with self.assertRaises(AdmissionError): admission(self.p)

if __name__=="__main__": unittest.main()


# Browser-backed, isolated fixture tests execute the actual candidate DOM_JS.
# These remain candidate tests until the owner runs them on a clean clone.
class CandidateDOMTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest("playwright unavailable; C01-C28 NOT VERIFIED") from exc
        cls._playwright_context=sync_playwright()
        cls._playwright=cls._playwright_context.start()
        try:
            cls.browser=cls._playwright.chromium.launch(headless=True, executable_path="/usr/bin/chromium",
                args=["--no-sandbox"])
        except Exception as exc:
            cls._playwright_context.stop()
            raise unittest.SkipTest("isolated Chromium unavailable; C01-C28 NOT VERIFIED: "+str(exc)) from exc

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._playwright_context.stop()

    def setUp(self):
        import approval_transaction as ap
        self.ap=ap
        self.ctx=self.browser.new_context()
        self.page=self.ctx.new_page()
        self.page.goto(Path(__file__).resolve().parent.joinpath("fixtures/collapsed_approval_dom.html").as_uri())
        self.page.evaluate("""() => {document.getElementById('operation-dialog').innerText='call_tool\\n'+JSON.stringify({
            path:'github/update_file',
            args:{repository_full_name:'example/repo',branch:'test',path:'path.txt',content:'safe'}
        })}""")
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p=Path(self.temp.name)
        self.chat="https://chatgpt.com/c/synthetic-100"
        self.job={"task_id":"t1","repository":"example/repo","branch":"test",
            "write_allowlist":["path.txt"],"chat_url":"https://chatgpt.com/",
            "conversation_mode":"new","valid_until":"2099-01-01T00:00:00+00:00"}
        self.write("job.json",self.job)
        self.write("active_instruction.json",{"task_id":"t1","instruction_id":"t1-r0","chat_url":self.chat})
        self.write("send_t1-r0.json",{"status":"CONFIRMED","chat_url":self.chat})
        self._home=self.p
        self._previous=self.ap.profile_root
        self.ap.profile_root=lambda p:self._home
        self.addCleanup(lambda:setattr(self.ap,"profile_root",self._previous))
        self.token="candidate"
        self.original_draft=self.page.locator("#prompt-textarea").inner_text()

    def tearDown(self):
        self.ctx.close()

    def write(self,name,value):
        (self.p/name).write_text(json.dumps(value))

    def js(self,action):
        code=self.ap.DOM_JS.replace("__TOKEN__",json.dumps(self.token)).replace("__ACTION__",json.dumps(action))
        return self.page.evaluate(code)

    def clicks(self):
        return self.page.evaluate("window.syntheticClicks")

    def mutate(self,expr):
        self.page.evaluate(expr)

    def send(self,job,req):
        if req["op"]=="snapshot":
            return {"url":self.chat,"text":self.page.locator("body").inner_text()}
        if req["op"]=="evaluate":
            return self.page.evaluate(req["expression"])
        raise AssertionError("unsupported")

    def approval(self,mode="strict",**kw):
        return self.ap.approve(self.p,mode=mode,send=self.send,**kw)

    def test_C01_collapsed_card(self):
        self.assertEqual(self.approval()["status"],"APPROVED")
        self.assertEqual(self.clicks().count("expand:activity"),1)
        self.assertEqual(self.clicks().count("once:card"),1)

    def test_C02_expanded_card(self):
        self.mutate("document.getElementById('activity').click()")
        self.page.evaluate("window.syntheticClicks=[]")
        self.assertEqual(self.approval()["status"],"APPROVED")
        self.assertNotIn("expand:activity",self.clicks())

    def test_C03_completed_collapsed(self):
        self.mutate("document.querySelector('.once').disabled=true;document.getElementById('card').dataset.approvalState='completed'")
        self.assertEqual(self.approval()["status"],"NO_PENDING")

    def test_C04_completed_expanded(self):
        self.mutate("document.getElementById('activity').click();document.querySelector('.once').disabled=true;document.getElementById('card').dataset.approvalState='completed'")
        self.assertEqual(self.approval()["status"],"NO_PENDING")

    def test_C05_no_control_or_card(self):
        self.mutate("document.getElementById('current-turn').innerHTML=''")
        self.assertEqual(self.approval()["status"],"NO_PENDING")
        self.assertEqual(self.clicks(),[])

    def test_C06_ambiguous_controls(self):
        self.mutate("document.getElementById('activity').after(document.getElementById('activity').cloneNode(true))")
        self.assertEqual(self.approval()["status"],"INFRA")
        self.assertEqual(self.clicks(),[])

    def test_C07_completed_and_pending(self):
        self.mutate("""document.getElementById('activity').click();
            let older=document.getElementById('card').cloneNode(true);older.id='old-card';
            older.dataset.approvalState='completed';older.querySelector('.once').disabled=true;
            document.getElementById('fold').prepend(older);""")
        self.assertEqual(self.approval()["status"],"APPROVED")
        self.assertEqual(self.clicks().count("once:card"),1)

    def test_C08_homepage_origin_draft(self):
        self.chat="https://chatgpt.com/"
        self.assertEqual(self.approval()["status"],"REJECTED")
        self.assertEqual(self.page.locator("#prompt-textarea").inner_text(),self.original_draft)

    def test_C09_unknown_dispatch(self):
        self.write("send_t1-r0.json",{"status":"SEND_UNKNOWN_NO_RETRY","chat_url":self.chat})
        self.assertEqual(self.approval()["status"],"REJECTED")
        self.assertEqual(self.clicks(),[])

    def test_C10_naive_expiry(self):
        self.job["valid_until"]="2099-01-01"
        self.write("job.json",self.job)
        self.assertEqual(self.approval()["status"],"REJECTED")

    def test_C11_expire_after_details(self):
        calls=[0]
        def clock():
            calls[0]+=1
            return dt.datetime(2100 if calls[0]>5 else 2026,1,1,tzinfo=dt.timezone.utc)
        self.assertEqual(self.approval(clock=clock)["status"],"REJECTED")
        self.assertNotIn("once:card",self.clicks())

    def test_C12_repo_scope(self):
        self.job["repository"]="another/repo";self.write("job.json",self.job)
        self.assertEqual(self.approval()["status"],"REJECTED")

    def test_C13_strict_token(self):
        self.mutate("""document.getElementById('operation-dialog').innerText=
            'call_tool\\n'+JSON.stringify({path:'github/update_file',args:{
            repository_full_name:'example/repo',branch:'test',path:'path.txt',
            content:'ghp_'+'a'.repeat(25)}})""")
        self.assertEqual(self.approval(mode="strict")["status"],"REJECTED")

    def test_C14_diff_empty(self):
        self.assertEqual(self.approval(mode="diff",fetch_base=lambda j,p:"safe")["status"],"REJECTED")

    def test_C15_both_policies_refuse(self):
        self.mutate("""document.getElementById('operation-dialog').innerText=
            'call_tool\\n'+JSON.stringify({path:'github/update_file',args:{
            repository_full_name:'example/repo',branch:'test',path:'path.txt',
            content:'ghp_'+'a'.repeat(25)}})""")
        self.assertEqual(self.approval(mode="auto",fetch_base=lambda j,p:"safe")["status"],"REJECTED")

    def test_C16_disabled_activity(self):
        self.mutate("document.getElementById('activity').disabled=true")
        self.assertEqual(self.approval()["status"],"INFRA")
        self.assertEqual(self.clicks(),[])

    def test_C17_unknown_activity_label(self):
        self.mutate("document.getElementById('activity').innerText='Other activity'")
        self.assertEqual(self.approval()["status"],"NO_PENDING")

    def test_C18_multiple_operations(self):
        self.mutate("""const d=document.getElementById('operation-dialog');
            d.innerText+='\\ncall_tool\\n{}'""")
        self.assertEqual(self.approval()["status"],"REJECTED")

    def test_C19_navigation_after_details(self):
        called=[False]
        def send(j,r):
            out=self.send(j,r)
            if r["op"]=="evaluate" and '"details"' in r["expression"] and not called[0]:
                called[0]=True;self.chat="https://chatgpt.com/c/another-conversation"
            return out
        self.assertEqual(self.ap.approve(self.p,send=send)["status"],"REJECTED")
        self.assertNotIn("once:card",self.clicks())

    def test_C20_content_changes(self):
        called=[False]
        def send(j,r):
            out=self.send(j,r)
            if r["op"]=="evaluate" and '"details"' in r["expression"] and not called[0]:
                called[0]=True
                self.mutate("""document.getElementById('operation-dialog').innerText=
                    document.getElementById('operation-dialog').innerText.replace('"safe"','"mutated"')""")
            return out
        self.assertNotEqual(self.ap.approve(self.p,send=send)["status"],"APPROVED")
        self.assertNotIn("once:card",self.clicks())

    def test_C21_detached_card(self):
        self.assertEqual(self.js("inspect")["state"],"COLLAPSED")
        self.js("expand");self.js("bind")
        self.mutate("document.getElementById('card').remove()")
        with self.assertRaises(Exception): self.js("details")

    def test_C22_lock_busy(self):
        lock=self.ap.profile_root(self.p)/"cache"/"approval-transaction.lock"
        lock.parent.mkdir(parents=True,exist_ok=True)
        import fcntl
        with lock.open("a+") as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.approval(lock_timeout=.01)["status"],"INFRA")

    def test_C23_persisted_invocation_reentry(self):
        self.assertEqual(self.approval()["status"],"APPROVED")
        # If the DOM mistakenly shows the same invocation pending again, the
        # persistent ledger still refuses a second physical click.
        self.mutate("document.getElementById('card').dataset.approvalState='pending';document.querySelector('.once').disabled=false")
        self.assertEqual(self.approval()["status"],"UNKNOWN")
        self.assertEqual(self.clicks().count("once:card"),1)

    def test_C24_transport_error(self):
        def fail(j,r): raise self.ap.InfraError("synthetic transport timeout")
        self.assertEqual(self.ap.approve(self.p,send=fail)["status"],"INFRA")

    def test_C25_readback_unknown_persistent(self):
        def send(j,r):
            if r["op"]=="evaluate" and '"readback"' in r["expression"]:
                raise self.ap.InfraError("synthetic unknown readback")
            return self.send(j,r)
        self.assertEqual(self.ap.approve(self.p,send=send)["status"],"UNKNOWN")
        self.mutate("document.getElementById('card').dataset.approvalState='pending';document.querySelector('.once').disabled=false")
        self.assertEqual(self.approval()["status"],"UNKNOWN")
        self.assertEqual(self.clicks().count("once:card"),1)

    def test_C26_conversation_wide_not_clicked(self):
        self.assertEqual(self.approval()["status"],"APPROVED")
        self.assertFalse(any("WIDE" in click for click in self.clicks()))

    def test_C27_valid_diff(self):
        self.mutate("""document.getElementById('operation-dialog').innerText=
            'call_tool\\n'+JSON.stringify({path:'github/update_file',args:{
            repository_full_name:'example/repo',branch:'test',path:'path.txt',content:'new'}})""")
        self.assertEqual(self.approval(mode="diff",fetch_base=lambda j,p:"old")["status"],"APPROVED")

    def test_C28_drafts_unchanged(self):
        self.assertEqual(self.approval()["status"],"APPROVED")
        self.assertEqual(self.page.locator("#prompt-textarea").inner_text(),self.original_draft)
        self.assertEqual(self.page.locator("#old-editor").inner_text(),"Old user message")
        self.assertEqual(self.clicks().count("once:card"),1)
