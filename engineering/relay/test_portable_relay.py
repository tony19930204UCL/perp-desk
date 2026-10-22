#!/usr/bin/env python3
"""Portable integration tests; all remote calls mocked explicitly."""
import base64,json,os,re,tempfile,unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from profile_runner import Relay,job_at
from main_verdict import MainHandbackError
class PortableRelayTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'));self.root=Path(self.tmp.name)
  self.job={'repository':'example/repo','pr_number':7,'branch':'engineer','mailbox':'docs/HANDOFF.json','task_id':'task-alpha','state_dir':str(self.root/'profile-alpha'),'conversation_mode':'reuse','chat_url':'https://chatgpt.com/c/test-chat','transport_command':['false'],'write_allowlist':['docs/HANDOFF.json'],'valid_until':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
  self.url='https://chatgpt.com/c/test-chat';self.page_text='';self.head='a'*40;self.blob='b'*40;self.users=[];self.composer='';self.sends=0
  self.payload={'schema_version':2,'repository':'example/repo','pr_number':7,'branch':'engineer','task_id':'task-alpha','instruction_id':'first','status':'READY_FOR_REVIEW','source_head_sha':'c'*40,'summary':'mocked test report','changed_files':['docs/HANDOFF.json'],'tests':[],'remaining_work':[],'next_step_reminder':{'to':'Main CIO','verify_now':'test','if_pass':'not automatically executed','if_fail':'not automatically executed','blockers':[]}}
  self.job['dispatch_lock_root']=str(self.root/'locks')
  self.job['dispatch_chat_url']=self.job['chat_url']
  self.r=Relay(self.job,self.gh,self.transport)
 def tearDown(self):self.tmp.cleanup()
 def gh(self,api):
  if '/files?' in api:return [{'filename':'docs/HANDOFF.json'}]
  if '/pulls/' in api:return {'state':'open','merged':False,'head':{'sha':self.head,'ref':'engineer','repo':{'full_name':'example/repo'}}}
  if '/contents/' in api:return {'sha':self.blob,'content':base64.b64encode(json.dumps(self.payload).encode()).decode()}
  if '/compare/' in api:return {'status':'ahead'}
  raise AssertionError(api)
 def transport(self,req):
  if req['op']=='snapshot':return {'url':self.url,'busy':False,'user_messages':list(self.users),'composer_text':self.composer,'text':self.page_text}
  x=req['expression']
  if "location.href='https://chatgpt.com/c/" in x:
   self.url=re.search(r"location.href='([^']+)'",x).group(1);self.users=[];return True
  if 'Allow once' in x:return json.dumps({'found':False})
  if 'insertText' in x:
   self.composer=json.loads(re.search(r"insertText',false,(.*?)\);const norm",x).group(1));return True
  if 'b[0].click()' in x:
   self.users.append(self.composer);self.composer='';self.sends+=1
   if self.job.get('conversation_mode')=='new':
    self.job['dispatch_chat_url']='https://chatgpt.com/c/new-%d'%(self.sends+1)
   return True
  raise AssertionError(x)
 def prepared(self,status='ACCEPTED_SCOPE',action='next bounded action'):
  self.assertEqual('SENT_CONFIRMED',self.r.dispatch('first','initial bounded instruction')['status'])
  obs=self.r.observe();self.assertEqual('HANDBACK_OBSERVED',obs['status'])
  ev=self.root/'real-local-test.log';ev.write_text('mock-test independent evidence')
  self.r.verdict({'head_sha':self.head,'mailbox_blob':self.blob,'status':status,'reason':'independent fixture verification','evidence_paths':[str(ev)],'remaining_work':[{'status':'ACTIONABLE_NOW','item':'bounded next'}] if action else [],'next_instruction':action})
  return ev
 def test_pass_dispatches_next_and_confirms_receipt(self):
  self.prepared();result=self.r.advance('second');self.assertEqual('DISPATCH_CONFIRMED',result['status']);self.assertEqual(2,self.sends)
  self.assertIn('next bounded action',self.users[-1]);self.assertNotIn('not automatically executed',self.users[-1])
 def test_revise_dispatches_repair_not_just_pending_file(self):
  self.prepared('REVISE','repair reproduced defect');result=self.r.advance('repair');self.assertEqual('DISPATCH_CONFIRMED',result['status']);self.assertIn('repair reproduced defect',self.users[-1])
 def test_terminal_scope_does_not_dispatch_unrequested_third_message(self):
  self.prepared(action='');self.assertEqual('ACCEPTED_SCOPE_STOP',self.r.advance('unused')['status']);self.assertEqual(1,self.sends)
 def test_mutated_independent_evidence_rejects_dispatch(self):
  ev=self.prepared();ev.write_text('tampered');self.assertRaises(MainHandbackError,self.r.advance,'second');self.assertEqual(1,self.sends)
 def test_wrong_instruction_rejected(self):
  self.r.dispatch('first','initial');self.payload['instruction_id']='foreign';self.assertEqual('INCOMPLETE_HANDBACK',self.r.observe()['status'])
 def test_expiry_blocks_A_not_B(self):
  self.prepared();self.job['valid_until']='2000-01-01T00:00:00+00:00';self.assertEqual('HANDBACK_OBSERVED',self.r.observe()['status']);self.assertRaises(RuntimeError,self.r.advance,'second');self.assertEqual(1,self.sends)
 def test_profiles_and_jobs_do_not_share_receipts(self):
  self.r.dispatch('first','initial');other=dict(self.job,state_dir=str(self.root/'profile-beta'));r2=Relay(other,self.gh,self.transport)
  self.assertFalse((r2.root/'active_instruction.json').exists());self.assertNotEqual(self.r.root,r2.root)
 def test_job_template_loads_without_private_state(self):
  p=self.root/'job.json';p.write_text(json.dumps(self.job));self.assertEqual('example/repo',job_at(p)['repository'])
 def test_stale_head_cannot_reuse_previous_verdict(self):
  self.prepared();self.head='d'*40;self.assertRaises(FileNotFoundError,self.r.advance,'second');self.assertEqual(1,self.sends)
 def new_relay(self):
  self.job['conversation_mode']='new';self.job.pop('chat_url');self.job['dispatch_chat_url']='https://chatgpt.com/c/new-1';self.url='https://chatgpt.com/c/old-conversation'
  self.r=Relay(self.job,self.gh,self.transport);return self.r
 def test_new_mode_opens_fresh_chat_each_dispatch_and_records_url(self):
  import time;orig=time.sleep;time.sleep=lambda *_:None
  try:
   r=self.new_relay();self.assertEqual('SENT_CONFIRMED',r.dispatch('first','one')['status'])
   a=json.loads((r.root/'active_instruction.json').read_text());self.assertEqual('https://chatgpt.com/c/new-1',a['chat_url'])
   self.assertEqual('SENT_CONFIRMED',r.dispatch('second','two')['status'])
   b=json.loads((r.root/'active_instruction.json').read_text());self.assertEqual('https://chatgpt.com/c/new-2',b['chat_url']);self.assertNotEqual(a['chat_url'],b['chat_url'])
  finally:time.sleep=orig
 def test_new_mode_needs_no_chat_url(self):
  p=self.root/'j.json';self.job['conversation_mode']='new';self.job.pop('chat_url');p.write_text(json.dumps(self.job));self.assertEqual('new',job_at(p)['conversation_mode'])
 def test_github_connector_block_notifies_exactly_once(self):
  self.r.dispatch('first','x');self.payload['instruction_id']='foreign';self.page_text='Please Connect GitHub to continue'
  one=self.r.watch();two=self.r.watch()
  self.assertEqual('BLOCKED_AUTH',one['status']);self.assertEqual('GITHUB_CONNECTOR',one['kind']);self.assertTrue(one['notify_client']);self.assertFalse(two['notify_client'])
 def test_login_block_detected_and_plain_wait_is_not_block(self):
  self.r.dispatch('first','x');self.payload['instruction_id']='foreign';self.page_text='Working on your task';self.assertEqual('INCOMPLETE_HANDBACK',self.r.watch()['status'])
  self.page_text='Welcome back\nLog in or Sign up';self.assertEqual('CHATGPT_LOGIN',self.r.watch()['kind'])
 def test_queue_runs_backlog_to_all_done_and_revise_path(self):
  import queue_runner as q
  r=self.new_relay();import time;orig=time.sleep;time.sleep=lambda *_:None
  try:
   json.dump({'items':[{'id':'A1','instruction':'do one','acceptance':'ok1'},{'id':'A2','instruction':'do two','acceptance':'ok2','depends_on':['A1']}]},open(r.root/'backlog.json','w'))
   self.assertEqual('DISPATCHED',q.step(r)['status']);self.payload['instruction_id']='A1-r0'
   rv=q.step(r);self.assertEqual('REVIEW_REQUIRED',rv['status'])
   ev=self.root/'e.log';ev.write_text('x')
   v=lambda st,**k:dict({'head_sha':self.head,'mailbox_blob':self.blob,'status':st,'reason':'r','evidence_paths':[str(ev)],'remaining_work':[{'status':'ACTIONABLE_NOW','item':'n'}]},**k)
   out=q.step(r,v('REVISE',next_instruction='fix it'));self.assertEqual('DISPATCHED',out['status']);self.assertEqual('A1-r1',q.active(q.load(r))['id']+'-r1')
   self.payload['instruction_id']='A1-r1';self.head='d'*40;self.blob='e'*40
   self.assertEqual('REVIEW_REQUIRED',q.step(r)['status'])
   out=q.step(r,v('ACCEPTED_SCOPE'));self.assertEqual('DISPATCHED',out['status']);self.assertEqual('A2',out['item'])
   self.payload['instruction_id']='A2-r0';self.head='f'*40;self.blob='1'*40
   self.assertEqual('REVIEW_REQUIRED',q.step(r)['status'])
   out=q.step(r,v('ACCEPTED_SCOPE',remaining_work=[{'status':'DONE','item':'n'}]));self.assertEqual('ALL_DONE',out['status'])
  finally:time.sleep=orig
if __name__=='__main__':unittest.main(verbosity=2)
