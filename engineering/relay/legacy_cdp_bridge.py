#!/usr/bin/env python3
"""Portable stdin/stdout wrapper for a scoped Windows PowerShell CDP adapter."""
import argparse,fcntl,json,subprocess,sys,time
from pathlib import Path
SNAPSHOT='''JSON.stringify({url:location.href,text:document.body.innerText,user_messages:[...document.querySelectorAll('[data-user-message-bubble] .whitespace-pre-wrap,[data-message-author-role="user"] .whitespace-pre-wrap')].map(e=>e.innerText),composer_text:[...document.querySelectorAll('#prompt-textarea,[contenteditable="true"][role="textbox"]')].filter(e=>e.getClientRects().length).map(e=>e.innerText).join(""),busy:[...document.querySelectorAll('button')].some(e=>e.getClientRects().length&&(/^(停止|停止回應|Stop|Stop generating)$/i.test(e.getAttribute('aria-label')||'')||e.getAttribute('data-testid')==='stop-button')),buttons:[...document.querySelectorAll('button')].map(e=>({text:e.innerText,aria:e.getAttribute('aria-label')}))})'''
def main():
    p=argparse.ArgumentParser()
    for key in ['adapter','request-file','request-file-windows','chat-url','profile-dir']: p.add_argument('--'+key,required=True)
    p.add_argument('--port',type=int,required=True); p.add_argument('--single-page',action='store_true'); a=p.parse_args(); req=json.load(sys.stdin)
    if req.get('op')=='snapshot': expression=SNAPSHOT
    elif req.get('op')=='evaluate' and isinstance(req.get('expression'),str): expression=req['expression']
    else: raise ValueError('UNSUPPORTED_TRANSPORT_OPERATION')
    path=Path(a.request_file); path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.lock').open('a+') as lease:
        deadline=time.time()+25   # wait for the other caller instead of failing: several watchers share one browser
        while True:
            try: fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.time()>deadline: raise
                time.sleep(0.2)
        path.write_text(json.dumps({'id':1,'method':'Runtime.evaluate','params':{'expression':expression,'returnByValue':True}}),encoding='utf-8')
        command=['powershell.exe','-NoProfile','-File',a.adapter,'-RequestFile',a.request_file_windows,'-ChatUrl',a.chat_url,'-Port',str(a.port),'-ProfileDir',a.profile_dir]+(['-SingleDedicatedPage'] if a.single_page else [])
        r=subprocess.run(command,capture_output=True,text=True,timeout=30)
        if r.returncode: raise RuntimeError(r.stderr[:500])
        value=json.loads(r.stdout.lstrip('\ufeff'))
        if isinstance(value,str):
            try: value=json.loads(value)
            except json.JSONDecodeError: pass
        print(json.dumps(value,ensure_ascii=False))
if __name__=='__main__': main()
