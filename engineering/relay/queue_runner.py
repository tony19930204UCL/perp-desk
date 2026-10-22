#!/usr/bin/env python3
"""Agent-agnostic backlog driver on top of profile_runner.Relay.

`step` is stateless and idempotent: call it in any loop/cron. It returns exactly one status:
  DISPATCHED | WAITING | REVIEW_REQUIRED | ALL_DONE | BLOCKED_AUTH | BLOCKED_OWNER
The ONLY agent-specific part is the review: on REVIEW_REQUIRED the calling agent inspects the
pinned diff/tests itself, writes a verdict file, and calls `step --verdict FILE`.
"""
from __future__ import annotations
import argparse,fcntl,json,sys
from pathlib import Path
from profile_runner import Relay,job_at,read
from main_verdict import atomic

def backlog_path(r): return r.root/'backlog.json'
def load(r):
    b=read(backlog_path(r)); ids=[i['id'] for i in b['items']]
    if len(ids)!=len(set(ids)): raise ValueError('BACKLOG_DUPLICATE_ID')
    for i in b['items']:
        if not i.get('instruction') or not i.get('acceptance'): raise ValueError('ITEM_NEEDS_INSTRUCTION_AND_ACCEPTANCE:'+i['id'])
        i.setdefault('status','PENDING'); i.setdefault('attempts',0); i.setdefault('max_revisions',3)
    return b
def save(r,b): atomic(backlog_path(r),b)
def nxt(b):
    done={i['id'] for i in b['items'] if i['status']=='DONE'}
    for i in b['items']:
        if i['status']=='PENDING' and set(i.get('depends_on',[]))<=done: return i
def active(b): return next((i for i in b['items'] if i['status']=='ACTIVE'),None)
def instr_id(item): return '%s-r%d'%(item['id'],item['attempts'])
def start(r,b,item,text=None):
    item['status']='ACTIVE'; rec=r.dispatch(instr_id(item),text or item['instruction']+'\n\nACCEPTANCE: '+item['acceptance'])
    if rec['status']!='SENT_CONFIRMED': item['status']='PENDING'; save(r,b); return {'status':'BLOCKED_OWNER','reason':'DISPATCH_'+rec['status']}
    save(r,b); return {'status':'DISPATCHED','item':item['id'],'instruction_id':instr_id(item)}

def step(r,verdict=None):
    b=load(r); cur=active(b)
    if verdict:
        if not cur: raise RuntimeError('NO_ACTIVE_ITEM')
        if verdict['status']=='ACCEPTED_SCOPE':
            cur['status']='DONE'; n=nxt(b)
            verdict.setdefault('next_instruction',(n['instruction']+'\n\nACCEPTANCE: '+n['acceptance']) if n else '')
            r.verdict(verdict)
            if not n: save(r,b); return {'status':'ALL_DONE','done':[i['id'] for i in b['items'] if i['status']=='DONE']}
            n['status']='ACTIVE'; res=r.advance(instr_id(n)); save(r,b)
            return {'status':'DISPATCHED' if res['status']=='DISPATCH_CONFIRMED' else 'BLOCKED_OWNER','item':n['id'],'detail':res['status']}
        if verdict['status']=='REVISE':
            cur['attempts']+=1
            if cur['attempts']>cur['max_revisions']: cur['status']='BLOCKED'; save(r,b); return {'status':'BLOCKED_OWNER','reason':'MAX_REVISIONS','item':cur['id']}
            r.verdict(verdict); res=r.advance(instr_id(cur)); save(r,b)
            return {'status':'DISPATCHED' if res['status']=='DISPATCH_CONFIRMED' else 'BLOCKED_OWNER','item':cur['id'],'detail':res['status']}
        cur['status']='BLOCKED'; r.verdict(verdict); save(r,b); return {'status':'BLOCKED_OWNER','reason':verdict.get('reason'),'item':cur['id']}
    if not cur:
        n=nxt(b)
        if not n: return {'status':'ALL_DONE' if all(i['status']=='DONE' for i in b['items']) else 'BLOCKED_OWNER'}
        return start(r,b,n)
    w=r.watch()
    if w.get('status')=='BLOCKED_AUTH': return {'status':'BLOCKED_AUTH','kind':w['kind'],'notify':w['notify_client'],'chat_url':w['chat_url']}
    if w.get('status')=='HANDBACK_OBSERVED':
        packet={'item':cur,'head_sha':w['head_sha'],'mailbox_blob':w['blob_sha'],'payload':w['payload'],
          'reviewer_must':['read-check HEAD/blob still current (gh api pulls/N)','compare source_head_sha..head: commits, parents, files within write_allowlist',
            'run/inspect the real tests against the item acceptance; never trust ChatGPT PASS text',
            'write verdict JSON {head_sha,mailbox_blob,status:ACCEPTED_SCOPE|REVISE|BLOCKED,reason,evidence_paths,remaining_work,next_instruction(REVISE only)}',
            'call: queue_runner.py step --job JOB --verdict FILE']}
        atomic(r.root/'review_packet.json',packet); return {'status':'REVIEW_REQUIRED','packet':str(r.root/'review_packet.json')}
    return {'status':'WAITING','detail':w.get('status'),'auto_approved':w.get('auto_approved')}

def main():
    p=argparse.ArgumentParser(); p.add_argument('command',choices=['step','status']); p.add_argument('--job',required=True); p.add_argument('--verdict'); a=p.parse_args()
    r=Relay(job_at(a.job))
    with (r.root/'profile_relay.lock').open('a+') as lease:
        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        out=step(r,read(a.verdict) if a.verdict else None) if a.command=='step' else {i['id']:i['status'] for i in load(r)['items']}
    print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
if __name__=='__main__': main()
