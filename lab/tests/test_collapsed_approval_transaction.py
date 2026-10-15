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
