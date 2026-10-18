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
        # Existing stdlib CDP client, no Playwright/installation dependency.
        import importlib.util, os, shutil
        from pathlib import Path
        source=Path(__file__).resolve().parents[1]/"browser_issue3_acceptance.py"
        spec=importlib.util.spec_from_file_location("browser_issue3_acceptance",source)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.CdpSession=module.CdpSession
        cls.chrome=os.environ.get("COLLAPSED_APPROVAL_CHROMIUM") or module.chrome_binary()
        if not Path(cls.chrome).exists() and not shutil.which(cls.chrome):
            raise RuntimeError("CHROMIUM_REQUIRED_NO_SKIP")

    def _eval(self, expression):
        return self.session.evaluate(expression)

    class _Locator:
        def __init__(self, session, selector):
            self.session=session;self.selector=selector
        def inner_text(self):
            return self.session.evaluate("document.querySelector("+json.dumps(self.selector)+").innerText")

    class _Page:
        def __init__(self,session):self.session=session
        def evaluate(self,expression):return self.session.evaluate(expression)
        def locator(self,selector):return CandidateDOMTests._Locator(self.session,selector)
        def goto(self,url):self.session.navigate(url)

    def setUp(self):
        import approval_transaction as ap
        self.ap=ap
        self.chrome_tmp=tempfile.TemporaryDirectory()
        self.session=self.CdpSession(self.chrome,Path(self.chrome_tmp.name))
        self.page=self._Page(self.session)
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
        self.session.close()
        self.chrome_tmp.cleanup()

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

    def active_action(self,req):
        if req.get("op")!="evaluate": return None
        import re
        m=re.search(r'const token = [^,]+, action = ("[^"]+");',req["expression"])
        return json.loads(m.group(1)) if m else None

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
            if r["op"]=="evaluate" and self.active_action(r)=="details" and not called[0]:
                called[0]=True;self.chat="https://chatgpt.com/c/another-conversation"
            return out
        self.assertEqual(self.ap.approve(self.p,send=send)["status"],"REJECTED")
        self.assertNotIn("once:card",self.clicks())

    def test_C20_content_changes(self):
        called=[False]
        def send(j,r):
            out=self.send(j,r)
            if r["op"]=="evaluate" and self.active_action(r)=="details" and not called[0]:
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
            if r["op"]=="evaluate" and self.active_action(r)=="readback":
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


class MatrixVariantTests(unittest.TestCase):
    """Additional variants and multiprocess locks; not substitutes for browser tests."""

    def setUp(self):
        import approval_transaction as ap
        self.ap=ap
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.job={"repository":"example/repo","branch":"test","write_allowlist":["path.txt"]}
        self.args={"repository_full_name":"example/repo","branch":"test","path":"path.txt","content":"safe"}
        self.op={"path":"github/update_file","args":self.args}

    def reject(self,content):
        self.args["content"]=content
        with self.assertRaises(self.ap.PolicyRejected):
            self.ap.strict_policy(self.op,self.job)

    def test_C13_all_inherited_patterns(self):
        # Assemble patterns at runtime; none is a credential.
        patterns=[
            "sk_"+"live_"+"a"*22,"sk_"+"test_"+"a"*22,
            "xox"+"b-"+"a"*22,"xox"+"a-"+"a"*22,
            "AKIA"+"A"*15,"-----"+"BEGIN"+" SYNTHETIC KEY",
            "sk-"+"a"*23,"ghp_"+"a"*25,
            "PRIVATE "+"KEY", 'api_key="'+'a'*25+'"',
            "A"*32,"12345678901234567890123456789012345",
        ]
        for ix,token in enumerate(patterns):
            with self.subTest(pattern=ix): self.reject(token)
        for nonstring in (None, [], {}, 42):
            with self.subTest(type=type(nonstring).__name__): self.reject(nonstring)
        self.reject("a"*180001)

    def test_C14_all_diff_policy_variants(self):
        for sample in ["sk-"+"a"*25,"ghp_"+"a"*25,
                       "github_pat_"+"a"*22,
                       "PRIVATE "+"KEY","A"*32]:
            with self.subTest(kind=sample[:6]):
                self.args["content"]=sample
                with self.assertRaises(self.ap.PolicyRejected):
                    self.ap.diff_policy(self.op,self.job,fetch_base=lambda j,p:"old")
        self.args["content"]="base"
        with self.assertRaises(self.ap.PolicyRejected):
            self.ap.diff_policy(self.op,self.job,fetch_base=lambda j,p:"base")
        self.args["content"]="\n".join("changed"+str(i) for i in range(201))
        with self.assertRaises(self.ap.PolicyRejected):
            self.ap.diff_policy(self.op,self.job,200,fetch_base=lambda j,p:"old")
        with self.assertRaises(self.ap.InfraError):
            self.ap.diff_policy(self.op,self.job,200,fetch_base=lambda j,p:"")
        with self.assertRaises(self.ap.InfraError):
            self.ap.diff_policy(self.op,self.job,200,fetch_base=lambda j,p:None)

    def test_C18_parser_requires_one_complete_object(self):
        valid="call_tool\n"+json.dumps(self.op)
        self.assertEqual(self.ap.parse_operation(valid),self.op)
        for invalid in ("call_tool\n","call_tool\n{}",valid+"\n{}",
                        valid+"\ncall_tool\n"+json.dumps(self.op),
                        valid+" unexpected", "call_tool\nnull"):
            with self.subTest(input=invalid[:40]):
                with self.assertRaises(self.ap.PolicyRejected): self.ap.parse_operation(invalid)

    def test_C22_multiprocess_lock_retained_during_fallback(self):
        import subprocess, os, time
        lock=self.root/"cache"/"approval-transaction.lock"
        lock.parent.mkdir(parents=True,exist_ok=True)
        child_code=("import fcntl,sys,time;f=open(sys.argv[1],'a+');"
                    "fcntl.flock(f,fcntl.LOCK_EX); print('LOCKED',flush=True);time.sleep(2)")
        p=subprocess.Popen([sys.executable,"-c",child_code,str(lock)],
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(p.stdout.readline().strip(),"LOCKED")
            with lock.open("a+") as fd:
                import fcntl
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertIsNone(p.poll())
        finally:
            p.terminate()
            p.communicate(timeout=5)

    def test_C24_real_transport_failures(self):
        import subprocess
        cases=[
            [sys.executable,"-c","import sys;sys.exit(2)"],
            [sys.executable,"-c","print('')"],
            [sys.executable,"-c","print('not json')"],
        ]
        for cmd in cases:
            with self.subTest(cmd=cmd[-1]):
                j={"transport_command":cmd}
                with self.assertRaises(self.ap.InfraError):
                    self.ap.transport(j,{"op":"snapshot"})
        with self.assertRaises(self.ap.InfraError):
            self.ap.evaluate({}, "tok","inspect",send=lambda j,r:(_ for _ in ()).throw(RuntimeError("synthetic JS error")))


class AdmissionVariantTests(unittest.TestCase):
    def setUp(self):
        import approval_transaction as ap
        self.ap=ap
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.chat="https://chatgpt.com/c/fake-current-turn"
        self.j={"task_id":"synthetic","conversation_mode":"new",
                "chat_url":"https://chatgpt.com/",
                "repository":"example/repo","branch":"test","write_allowlist":["path.txt"],
                "valid_until":"2099-01-01T00:00:00+00:00"}
        self.a={"task_id":"synthetic","instruction_id":"synthetic-r2","chat_url":self.chat}
        self.r={"status":"CONFIRMED","chat_url":self.chat,"chat_path":"/"}
        self.save()

    def save(self):
        for name,obj in [("job.json",self.j),("active_instruction.json",self.a),
                         ("send_synthetic-r2.json",self.r)]:
            (self.root/name).write_text(json.dumps(obj))

    def test_C08_wrong_chat_and_origin_variants(self):
        for bad in ("https://other.example/c/fake-current-turn",
                    "http://chatgpt.com/c/fake-current-turn",
                    "https://chatgpt.com/",
                    "https://chatgpt.com/c/different"):
            with self.subTest(url=bad):
                self.a["chat_url"]=bad;self.save()
                with self.assertRaises(self.ap.AdmissionError):self.ap.admission(self.root)

    def test_C09_wrong_task_instruction_or_receipt(self):
        for data in ({"task_id":"wrong"},{"instruction_id":"else-r2"},
                     {"chat_url":None}):
            with self.subTest(data=data):
                self.a={**self.a,**data};self.save()
                with self.assertRaises(self.ap.AdmissionError):self.ap.admission(self.root)
                self.a={"task_id":"synthetic","instruction_id":"synthetic-r2","chat_url":self.chat}
        for status in ("SEND_UNKNOWN_NO_RETRY","BROWSER_BUSY_NOT_SENT",""):
            with self.subTest(status=status):
                self.r["status"]=status;self.save()
                with self.assertRaises(self.ap.AdmissionError):self.ap.admission(self.root)

    def test_C10_expiry_variants(self):
        for value in (None,"","not a date","2000-01-01T00:00:00+00:00",
                      "2099-01-01T00:00:00"):
            with self.subTest(expiry=value):
                self.j["valid_until"]=value;self.save()
                with self.assertRaises(self.ap.AdmissionError):self.ap.admission(self.root)

    def test_C12_all_tool_scopes(self):
        for mutation in ({"path":"github/delete_file"},
                         {"args":{"repository_full_name":"other/repo"}},
                         {"args":{"branch":"main"}},
                         {"args":{"path":"not-allowed"}}):
            op={"path":"github/update_file",
                "args":{"repository_full_name":"example/repo","branch":"test",
                        "path":"path.txt","content":"safe"}}
            op["path"]=mutation.get("path",op["path"])
            op["args"].update(mutation.get("args",{}))
            with self.subTest(mutation=mutation):
                with self.assertRaises(self.ap.PolicyRejected):
                    self.ap.strict_policy(op,self.j)
        op={"path":"github/create_file","args":{"repository_full_name":"example/repo",
            "branch":"test","path":"path.txt","content":"safe"}}
        with self.assertRaises(self.ap.PolicyRejected):
            self.ap.diff_policy(op,self.j,fetch_base=lambda j,p:"old")

    def test_C20_identity_fingerprint_all_fields(self):
        original={"path":"github/update_file","args":{
            "repository_full_name":"example/repo","branch":"test",
            "path":"path.txt","content":"safe"}}
        a=self.ap.fingerprint(original,"invocation-A")
        self.assertNotEqual(a,self.ap.fingerprint(original,"invocation-B"))
        for key,new in (("repository_full_name","changed/repo"),
                        ("branch","other"),("path","other.txt"),("content","different")):
            op=json.loads(json.dumps(original))
            op["args"][key]=new
            with self.subTest(key=key):
                self.assertNotEqual(a,self.ap.fingerprint(op,"invocation-A"))
        op=json.loads(json.dumps(original));op["path"]="github/create_file"
        self.assertNotEqual(a,self.ap.fingerprint(op,"invocation-A"))


class EntrypointProcessTests(unittest.TestCase):
    """Real direct helpers and auto_allow --once via synthetic no-card transport."""

    def test_C23_real_cli_no_card_isolation(self):
        import os, subprocess, concurrent.futures
        root=Path(__file__).resolve().parents[2]
        scripts=root/"engineering"/"relay"
        with tempfile.TemporaryDirectory() as d:
            home=Path(d)
            jd=home/"cache"/"scratch"/"relay-synthetic"
            jd.mkdir(parents=True)
            transport=home/"synthetic_transport.py"
            log=home/"transport_calls.jsonl"
            transport.write_text("""import json,sys,os
from pathlib import Path
request=json.load(sys.stdin)
with Path(os.environ['SYNTHETIC_TRANSPORT_LOG']).open('a') as out:
 out.write(json.dumps({'op':request['op']})+'\\n')
if request['op']=='snapshot':
 result={'url':'https://chatgpt.com/c/synthetic-test','text':'No pending card','busy':False}
elif request['op']=='evaluate':
 result={'state':'NO_PENDING'}
else: sys.exit(4)
print(json.dumps({'adapter':'trusted_cdp_v1',
 'browser':{'url':'https://chatgpt.com/c/synthetic-test','target_id':'synthetic-target'},
 'result':result}))
""")
            j={"task_id":"synthetic","instruction_id":"synthetic-r2",
               "repository":"example/repo","branch":"test","write_allowlist":["path.txt"],
               "valid_until":"2099-01-01T00:00:00+00:00",
               "conversation_mode":"new","chat_url":"https://chatgpt.com/",
               "transport_command":[sys.executable,str(transport)],"target_id":"synthetic-target"}
            (jd/"job.json").write_text(json.dumps(j))
            (jd/"active_instruction.json").write_text(json.dumps({
                "task_id":"synthetic","instruction_id":"synthetic-r2",
                "chat_url":"https://chatgpt.com/c/synthetic-test","target_id":"synthetic-target"}))
            (jd/"send_synthetic-r2.json").write_text(json.dumps({
                "status":"CONFIRMED","task_id":"synthetic","instruction_id":"synthetic-r2","target_id":"synthetic-target","chat_url":"https://chatgpt.com/c/synthetic-test"}))
            env={**os.environ,"HERMES_HOME":str(home),
                 "SYNTHETIC_TRANSPORT_LOG":str(log)}
            def run(argv):
                return subprocess.run(argv,env=env,capture_output=True,text=True,
                                      timeout=20,cwd=str(root))
            entrypoints=[
                [sys.executable,str(scripts/"relay-allow-card.py"),str(jd)],
                [sys.executable,str(scripts/"relay-allow-if-diff-clean.py"),str(jd),"200"],
                [sys.executable,str(scripts/"auto_allow.py"),"--once",str(jd),"synthetic-r2","https://chatgpt.com/c/synthetic-test","synthetic-target"]]
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                results=list(pool.map(run,entrypoints))
            for i,result in enumerate(results):
                with self.subTest(entrypoint=entrypoints[i]):
                    self.assertEqual(result.returncode,0,
                        (result.stdout,result.stderr))
            self.assertTrue(log.exists())
            self.assertNotIn('"op": "click"',log.read_text())
            self.assertFalse((home/"cache"/"approval-transaction-ledger.json").exists())


class StructuralDetectorRegressionTests(unittest.TestCase):
    def setUp(self):
        import approval_transaction as ap
        self.ap=ap
        self.job={"repository":"example/repo","branch":"feature","write_allowlist":["detector.py"]}
        self.op={"path":"github/update_file","args":{"repository_full_name":"example/repo",
            "branch":"feature","path":"detector.py","content":""}}

    def test_C13_exact_ast_detector_large_file(self):
        import re
        pattern=self.ap.STRICT_REGEX.pattern
        # Python repr is an exact constant, with no dynamic construction.
        source="import re\nPAT = re.compile("+repr(pattern)+")\n"+"# safe line\n"*220
        self.op["args"]["content"]=source
        self.assertEqual(self.ap.strict_policy(self.op,self.job)["tool"],"update_file")
        with self.assertRaises(self.ap.PolicyRejected):
            self.ap.diff_policy(self.op,self.job,max_lines=200,fetch_base=lambda j,p:"old")

    def test_C13_detector_plus_real_value_rejected(self):
        pattern=self.ap.STRICT_REGEX.pattern
        source="import re\nPAT = re.compile("+repr(pattern)+")\n"
        self.op["args"]["content"]=source+"value = "+repr("ghp_"+"a"*25)+"\n"
        with self.assertRaises(self.ap.PolicyRejected):
            self.ap.strict_policy(self.op,self.job)

    def test_C13_changed_detector_and_secret_comment_not_exempt(self):
        pattern=self.ap.STRICT_REGEX.pattern
        for source in (
            "import re\nPAT = re.compile("+repr(pattern+"X")+")\n",
            "import re\nPAT = re.compile(prefix)\nvalue = "+repr("ghp_"+"a"*25)+"\n",
            "# SECRET detection rule\nvalue = "+repr("ghp_"+"a"*25)+"\n",
            "SECRET = "+repr("ghp_"+"a"*25)+"\n",
        ):
            with self.subTest(source=source[:30]):
                self.op["args"]["content"]=source
                with self.assertRaises(self.ap.PolicyRejected):
                    self.ap.strict_policy(self.op,self.job)


class MissingAdmissionRecordTests(unittest.TestCase):
    def test_C09_missing_send_is_admission_failure(self):
        import approval_transaction as ap
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            chat="https://chatgpt.com/c/synthetic-current"
            (p/"job.json").write_text(json.dumps({"task_id":"t","repository":"a/b",
                "branch":"fix","write_allowlist":["a"],"valid_until":"2099-01-01T00:00:00+00:00"}))
            (p/"active_instruction.json").write_text(json.dumps({
                "task_id":"t","instruction_id":"different-r2","chat_url":chat}))
            with self.assertRaises(ap.AdmissionError):
                ap.admission(p)
            (p/"active_instruction.json").write_text("{not json")
            with self.assertRaises(ap.AdmissionError):
                ap.admission(p)


class CrossProcessFallbackRaceTests(unittest.TestCase):
    def test_C22_competing_process_blocked_through_diff_and_readback(self):
        """Synthetic bridge, genuine independent Python processes and flock."""
        import os, subprocess, time
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            work=root/"cache"/"scratch"/"relay-race"
            work.mkdir(parents=True)
            chat="https://chatgpt.com/c/synthetic-race"
            job={"task_id":"race","conversation_mode":"new",
                 "chat_url":"https://chatgpt.com/",
                 "repository":"example/repo","branch":"feature",
                 "write_allowlist":["sample.py"],
                 "valid_until":"2099-01-01T00:00:00+00:00"}
            (work/"job.json").write_text(json.dumps(job))
            (work/"active_instruction.json").write_text(json.dumps({
                "task_id":"race","instruction_id":"race-r3","chat_url":chat}))
            (work/"send_race-r3.json").write_text(json.dumps({
                "status":"CONFIRMED","chat_url":chat}))
            bridge=root/"process_bridge.py"
            bridge.write_text(StringBridgeSource)
            ready=root/"at-fallback"
            release=root/"release-fallback"
            click=root/"clicked"
            script=root/"worker.py"
            script.write_text("""import json,os,sys,time
from pathlib import Path
sys.path.insert(0,os.environ['APPROVAL_MODULE_PATH'])
import approval_transaction as a
home=Path(os.environ['HERMES_HOME'])
job=Path(sys.argv[1])
def send(_job,req):
 if req['op']=='snapshot':return {'url':'https://chatgpt.com/c/synthetic-race','text':''}
 import re
 action=json.loads(re.search(r'const token = [^,]+, action = ("[^"]+");',req['expression']).group(1))
 if action in ('inspect','bind'):return {'state':'PENDING','invocation':'single-operation'}
 if action=='details':
  return {'details':'call_tool\\n'+json.dumps({'path':'github/update_file','args':{
      'repository_full_name':'example/repo','branch':'feature',
      'path':'sample.py','content':'alpha_1234567890_beta_1234567890_gamma'}}),'invocation':'single-operation'}
 if action=='click':
  with (home/'clicked').open('a') as f:f.write('click\\n')
  return {'clicked':True,'invocation':'single-operation'}
 if action=='readback':return {'same':True,'completed':True,'invocation':'single-operation'}
 raise RuntimeError(action)
def base(j,p):
 (home/'at-fallback').write_text('ready')
 deadline=time.monotonic()+8
 while not (home/'release-fallback').exists() and time.monotonic()<deadline:time.sleep(.02)
 if not (home/'release-fallback').exists():raise RuntimeError('barrier timeout')
 return 'old'
r=a.approve(job,mode='auto',send=send,fetch_base=base,lock_timeout=.25)
print(json.dumps(r),flush=True)
""")
            env={**os.environ,"HERMES_HOME":str(root),
                 "APPROVAL_MODULE_PATH":str(Path(__file__).resolve().parents[2]/"engineering"/"relay")}
            first=subprocess.Popen([sys.executable,str(script),str(work)],env=env,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                until=time.monotonic()+6
                while not ready.exists() and time.monotonic()<until:
                    if first.poll() is not None:break
                    time.sleep(.02)
                self.assertTrue(ready.exists(),"first process never reached fallback")
                second=subprocess.run([sys.executable,str(script),str(work)],env=env,
                    capture_output=True,text=True,timeout=5)
                self.assertIn('"LOCK_BUSY"',second.stdout,second.stderr)
                self.assertFalse(click.exists(),"other process interleaved before readback")
                release.write_text("go")
                stdout,stderr=first.communicate(timeout=8)
                self.assertEqual(first.returncode,0,stderr)
                self.assertIn('"APPROVED"',stdout)
                self.assertEqual(click.read_text().count("click"),1)
            finally:
                release.write_text("go")
                if first.poll() is None:
                    first.terminate()
                    first.communicate(timeout=5)


# Kept as a string so fixture imports remain pure stdlib. The process bridge
# never navigates or opens Chrome; DOM behavior is tested separately.
StringBridgeSource = "# no external transport used for C22\\n"
