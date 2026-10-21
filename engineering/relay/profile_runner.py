#!/usr/bin/env python3
"""Profile-local coordinator. Main verdicts are local, never mailbox instructions."""
from __future__ import annotations
import argparse,fcntl,json,re,subprocess
from pathlib import Path
from urllib.parse import urlparse
from datetime import datetime,timezone
from engineering_instruction_carrier import InstructionCarrier
import product_dispatch as pd
from engineering_github_inbox import GitHubInboxObserver
from main_verdict import atomic,folder_for,record_verdict,validate_result,MainHandbackError

def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def gh(endpoint):
    p=subprocess.run(['gh','api',endpoint],capture_output=True,text=True,timeout=60)
    if p.returncode: raise RuntimeError(p.stderr[:500])
    return json.loads(p.stdout)

def job_at(path):
    job=read(path)
    job.setdefault('conversation_mode','new')
    if job['conversation_mode'] not in ('new','reuse'): raise ValueError('CONVERSATION_MODE')
    required=['repository','pr_number','branch','mailbox','task_id','state_dir','transport_command','write_allowlist','valid_until']+(['chat_url'] if job['conversation_mode']=='reuse' else [])
    if any(not job.get(k) for k in required): raise ValueError('JOB_REQUIRED_FIELDS')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',job['repository']): raise ValueError('REPOSITORY_FORMAT')
    if type(job['pr_number']) is not int or job['pr_number']<=0: raise ValueError('PR_FORMAT')
    if not Path(job['state_dir']).is_absolute(): raise ValueError('ABSOLUTE_STATE_REQUIRED')
    if job['conversation_mode']=='reuse' and not re.fullmatch(r'https://chatgpt\.com/c/[A-Za-z0-9-]+',job['chat_url']): raise ValueError('PINNED_CHAT_REQUIRED')
    if job['mailbox'] not in job['write_allowlist']: raise ValueError('MAILBOX_NOT_AUTHORIZED')
    if not isinstance(job['transport_command'],list): raise ValueError('TRANSPORT_ARGV_REQUIRED')
    deadline=datetime.fromisoformat(job['valid_until'])
    if deadline.tzinfo is None: raise ValueError('TIMEZONE_REQUIRED')
    return job

class Relay:
    def __init__(self,job,gh_fn=gh,transport_fn=None):
        self.job=job; self.root=Path(job['state_dir']); self.root.mkdir(parents=True,exist_ok=True)
        self.gh=gh_fn; self.transport=transport_fn or self._transport
    def _transport(self,request):
        p=subprocess.run(self.job['transport_command'],input=json.dumps(request),capture_output=True,text=True,timeout=60)
        if p.returncode: raise RuntimeError('TRANSPORT_FAILED:'+p.stderr[:500])
        return json.loads(p.stdout)
    def observer(self):
        return GitHubInboxObserver(repository=self.job['repository'],pr_number=self.job['pr_number'],branch=self.job['branch'],mailbox_path=self.job['mailbox'],gh_fn=self.gh,state_dir=self.root)
    def observe(self):
        active=read(self.root/'active_instruction.json')
        result=self.observer().observe(expected_task_id=self.job['task_id'],expected_instruction_id=active['instruction_id'])
        if result['status']=='HANDBACK_OBSERVED':
            allowed=set(self.job['write_allowlist'])|set(self.job.get('review_allowlist',[]))
            changed=result['payload']['changed_files']
            if not set(changed)<=allowed: raise RuntimeError('REPORTED_FILES_OUTSIDE_SCOPE')
        return result
    def dispatch(self,instruction_id,prompt):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,140}',instruction_id): raise ValueError('INSTRUCTION_ID_FORMAT')
        j=self.job
        pd.claim(j, self.root, instruction_id, chat=j.get('dispatch_chat_url'))
        contract={'repository':j['repository'],'pr_number':j['pr_number'],'branch':j['branch'],'task_id':j['task_id'],'instruction_id':instruction_id,'mailbox':j['mailbox'],'write_allowlist':j['write_allowlist']}
        text=prompt+'\n\nMAIN BOUNDED CONTEXT: '+json.dumps(contract,ensure_ascii=False)+'\nNo merge/deploy/trade or changes outside allowlist. Write schema_version=2 mailbox with these exact IDs, real pre-mailbox source_head_sha, status, summary, changed_files, tests with actual command/result/evidence, remaining_work, and next_step_reminder {to:Main CIO,verify_now,if_pass,if_fail,blockers:[]}. Do not claim tests not run. Main owns independent acceptance.'
        new=j.get('conversation_mode','new')=='new'
        if new: snapshot=self._fresh_chat()
        else:
            snapshot=self.transport({'op':'snapshot'})
            if snapshot.get('url')!=j['chat_url']: raise RuntimeError('PINNED_CHAT_CHANGED')
        carrier=InstructionCarrier(root=self.root,chat_path='/' if new else urlparse(j['chat_url']).path,repo=j['repository'],branch=j['branch'],mailbox=j['mailbox'],allowed=set(j['write_allowlist']),pr_number=j['pr_number'],cdp_fn=lambda expression:self.transport({'op':'evaluate','expression':expression}),snapshot_fn=lambda:self.transport({'op':'snapshot'}),save_fn=lambda name,obj:atomic(self.root/name,obj),event_fn=lambda event,**kw:atomic(self.root/'transport_event_latest.json',{'event':event,**kw}),valid_until=j['valid_until'],dispatch_job=j)
        carrier.send_instruction(text,instruction_id,snapshot)
        path=self.root/('send_'+instruction_id+'.json')
        receipt=read(path) if path.exists() else {'status':'BROWSER_BUSY_NOT_SENT'}
        if receipt.get('status')=='CONFIRMED':
            atomic(self.root/'active_instruction.json',{'task_id':j['task_id'],'instruction_id':instruction_id,'chat_url':receipt.get('chat_url') or j.get('chat_url')})
            return {'status':'SENT_CONFIRMED','receipt':str(path)}
        return {'status':receipt['status'],'receipt':str(path)}
    def _fresh_chat(self):
        import time
        self.transport({'op':'evaluate','expression':"(()=>{location.href='https://chatgpt.com/';return true})()"})
        for _ in range(20):
            time.sleep(1)
            try: snap=self.transport({'op':'snapshot'})
            except Exception: continue
            if snap.get('url')=='https://chatgpt.com/' and not snap.get('user_messages') and not snap.get('busy'): return snap
        raise RuntimeError('NEW_CHAT_NOT_READY')
    AUTH_PATTERNS={'CHATGPT_LOGIN':r'(Log in|Sign up|登入|註冊)\b.{0,40}(Log in|Sign up|登入|註冊)|Welcome back|歡迎回來|verify you are human|確認您是真人',
                   'GITHUB_CONNECTOR':r'(Connect|Authorize|Reconnect|連結|授權|重新連結).{0,30}GitHub|GitHub.{0,30}(authorization|not connected|未連結|需要授權|sign in)'}
    def classify_block(self,snap):
        text=(snap.get('text') or '')[-6000:]
        for kind,pat in self.AUTH_PATTERNS.items():
            if re.search(pat,text,re.I|re.S): return kind
        return None
    PROMPT_JS='''(()=>{const norm=t=>t.replace(/\\s+/g,' ').trim();const btn=[...document.querySelectorAll('button')].find(b=>b.getClientRects().length&&/^(允許一次|Allow once)(\\s|$)/i.test(b.innerText.trim()));if(!btn)return JSON.stringify({found:false});let e=btn;for(let i=0;i<8&&e.parentElement;i++){e=e.parentElement;if(/GitHub/i.test(e.innerText)&&e.innerText.length<1500)break}const card=norm(e.innerText);if(%s){return JSON.stringify({found:true,card:card.slice(0,600),clicked:false})}btn.click();return JSON.stringify({found:true,card:card.slice(0,600),clicked:true})})()'''
    def approve_allow_once(self,dry=False):
        """Click ONLY 'Allow once' on a GitHub tool-permission card that names this job's repository. Never the per-conversation option."""
        repo=json.dumps(self.job['repository'])
        guard=('!(/GitHub/i.test(card))||dry' if self.job.get('client_authorized_github_allow_once') else '!(/GitHub/i.test(card)&&card.includes(%s))||dry'%repo)
        js=self.PROMPT_JS%guard.replace('dry','%s'%('true' if dry else 'false'))
        r=self.transport({'op':'evaluate','expression':js})
        r=json.loads(r) if isinstance(r,str) else r
        if r.get('found') and r.get('clicked'): atomic(self.root/('approved_once_%s.json'%datetime.now(timezone.utc).strftime('%H%M%S')),{'at':datetime.now(timezone.utc).isoformat(),'card':r['card'],'scope':'allow_once'})
        return r
    def watch(self):
        """Observe mailbox; if absent, inspect chat once for auth blocks. Notify exactly once per (instruction,kind)."""
        obs=self.observe()
        if obs['status']=='HANDBACK_OBSERVED': return obs
        active=read(self.root/'active_instruction.json')
        kind=None
        try: kind=self.classify_block(self.transport({'op':'snapshot'}))
        except Exception as e: return {**obs,'transport':'UNAVAILABLE_RETRY','error':str(e)[:200]}
        if not kind:
            ap=self.approve_allow_once()
            if ap.get('found') and ap.get('clicked'): return {**obs,'auto_approved':'ALLOW_ONCE'}
            if ap.get('found'): kind='GITHUB_CONNECTOR'  # card present but not matching this job: do not click
            else: return obs
        elif kind=='GITHUB_CONNECTOR':
            ap=self.approve_allow_once()
            if ap.get('found') and ap.get('clicked'): return {**obs,'auto_approved':'ALLOW_ONCE'}
        flag=self.root/('blocked_auth_'+active['instruction_id']+'_'+kind+'.json')
        first=not flag.exists()
        if first: atomic(flag,{'at':datetime.now(timezone.utc).isoformat(),'kind':kind,'chat_url':active.get('chat_url'),'instruction_id':active['instruction_id']})
        return {'status':'BLOCKED_AUTH','kind':kind,'notify_client':first,'chat_url':active.get('chat_url'),
                'client_action':'Authorize GitHub connector in the ChatGPT conversation' if kind=='GITHUB_CONNECTOR' else 'Log in to ChatGPT in the dedicated browser',
                'after_client_action':'Re-run dispatch with the same instruction text; delivery is idempotent per instruction_id only after a new id, so use a new -rN suffix'}
    def verdict(self,data):
        obs=self.observe()
        if obs['status']!='HANDBACK_OBSERVED': raise RuntimeError(obs['status'])
        if data.get('head_sha')!=obs['head_sha'] or data.get('mailbox_blob')!=obs['blob_sha']: raise RuntimeError('STALE_MAIN_VERDICT')
        request_path=folder_for(self.root,obs['head_sha'],obs['blob_sha'])/'request.json'
        if not request_path.exists():
            atomic(request_path,{'head_sha':obs['head_sha'],'mailbox_blob':obs['blob_sha'],'payload':obs['payload'],'review_kind':'CURRENT_CIO_HANDBACK'})
        return record_verdict(self.root,obs['head_sha'],obs['blob_sha'],status=data['status'],reason=data['reason'],evidence_paths=data['evidence_paths'],remaining_work=data['remaining_work'],next_instruction=data.get('next_instruction',''),source_review=data.get('source_review'))
    def advance(self,instruction_id):
        obs=self.observe()
        if obs['status']!='HANDBACK_OBSERVED': raise RuntimeError(obs['status'])
        head,blob=obs['head_sha'],obs['blob_sha']
        verdict=read(folder_for(self.root,head,blob)/'result.json')
        validate_result(verdict,head,blob)
        if verdict['status']=='BLOCKED': return {'status':'MAIN_BLOCKED_STOP','remaining_work':verdict['remaining_work']}
        action=verdict.get('next_instruction','').strip()
        actionable=any(x.get('status')=='ACTIONABLE_NOW' for x in verdict['remaining_work'])
        if not action:
            if verdict['status']=='REVISE' or actionable: raise MainHandbackError('NEXT_INSTRUCTION_REQUIRED')
            return {'status':'ACCEPTED_SCOPE_STOP','head_sha':head,'mailbox_blob':blob}
        # Re-observe immediately before dispatch; remote reminder is never used.
        check=self.observe()
        if check.get('head_sha')!=head or check.get('blob_sha')!=blob: raise RuntimeError('STALE_HEAD_REQUIRES_REOBSERVATION')
        receipt=self.dispatch(instruction_id,action)
        result={'status':'DISPATCH_CONFIRMED' if receipt['status']=='SENT_CONFIRMED' else 'DISPATCH_PENDING','verdict_status':verdict['status'],'head_sha':head,'mailbox_blob':blob,'transport':receipt}
        atomic(self.root/'main_continuation.json',result)
        return result

def main():
    p=argparse.ArgumentParser(); p.add_argument('command',choices=['dispatch','observe','watch','verdict','advance']); p.add_argument('--job',required=True); p.add_argument('--instruction-id'); p.add_argument('--prompt'); p.add_argument('--input'); a=p.parse_args()
    j=job_at(a.job); r=Relay(j)
    with (r.root/'profile_relay.lock').open('a+') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.command=='dispatch': result=r.dispatch(a.instruction_id,Path(a.prompt).read_text())
        elif a.command=='observe': result=r.observe()
        elif a.command=='watch': result=r.watch()
        elif a.command=='verdict': result=r.verdict(read(a.input))
        else: result=r.advance(a.instruction_id)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
if __name__=='__main__': main()
