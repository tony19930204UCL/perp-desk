#!/usr/bin/env python3
"""Opt-in real Chromium acceptance for synthetic multiarm SQLite snapshots."""
import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import browser_dashboard_v2_acceptance as cd
import dashboard_multiarm_server as srv

LAB=Path(__file__).resolve().parent
FIXTURE=LAB/'tests/fixtures/dashboard_multiarm_browser.json'
PROBES=('MA-BROWSER-POPULATED-1280-390','MA-BROWSER-ALL-TABS',
        'MA-BROWSER-UNKNOWN-VS-ZERO','MA-BROWSER-HOSTILE-TEXT',
        'MA-BROWSER-STALE-FETCH-CLEARING','MA-BROWSER-NO-EXTERNAL-REQUESTS',
        'MA-BROWSER-READONLY')
FILES=[LAB/x for x in ('dashboard_multiarm_model.py','dashboard_multiarm_reader.py',
       'dashboard_multiarm_server.py','dashboard_multiarm.html','dashboard_multiarm_ui.js')]
FILES.append(FIXTURE)
DOM=r"""(()=>({width:innerWidth,client:document.documentElement.clientWidth,
scroll:document.documentElement.scrollWidth,body:document.body.scrollWidth,
title:document.getElementById('chosen').textContent,
reason:document.getElementById('reason').textContent,
account:document.getElementById('account').textContent,
table:document.getElementById('tbody').textContent,
tabs:[...document.querySelectorAll('#tabs button')].map(x=>x.textContent),
arms:[...document.querySelectorAll('#arms button')].map(x=>x.textContent),
bad:document.querySelectorAll('img,iframe,object,script[src^="http"]').length
})())"""


def allowed(url,origin):
    a,b=urlsplit(url),urlsplit(origin)
    return a.scheme=='http' and a.hostname==b.hostname=='127.0.0.1' and a.port==b.port and a.path in ('/','/dashboard_multiarm.html','/dashboard_multiarm_ui.js','/api/multiarm') and not a.query and not a.fragment


def write_db(folder,payload):
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/'broker.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sim_broker_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
        db.execute('INSERT INTO sim_broker_state VALUES(1,?)',(json.dumps(payload,ensure_ascii=False),))
    return path


def stat(paths):
    return {p.name+':'+str(i):{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'mtime_ns':p.stat().st_mtime_ns,'size':p.stat().st_size} for i,p in enumerate(paths)}


def probe(report,name,fun):
    started=time.monotonic()
    item=report['probes'][name]
    try:
        item['evidence']=fun()
        item['status']='PASS'
    except cd.AcceptanceTimeout as exc:
        item.update(status='TIMEOUT',evidence=str(exc)[:250])
    except Exception as exc:
        item.update(status='FAIL',evidence=(type(exc).__name__+': '+str(exc))[:250])
    item['duration_ms']=round((time.monotonic()-started)*1000,2)


def assert_true(value,message):
    if not value:
        raise AssertionError(message)


def run(cdp,report,paths):
    def populated():
        result={}
        for w,h in ((1280,800),(390,844)):
            cdp.viewport(w,h);cdp.navigate()
            snap=cdp.wait(DOM,lambda x:x and 'w1-xrpusdt' in x['arms'] and len(x['tabs'])==5,'page')
            assert_true(snap['width']==w and snap['client']==w,'viewport width mismatch')
            assert_true(snap['scroll']<=snap['client'] and snap['body']<=snap['client'],'horizontal overflow')
            shot=cdp.screenshot(Path(report['output'])/('multiarm-%s.png'%w))
            assert_true(shot['pixel_size']==[w,h],'screenshot resolution')
            report['artifacts'].append(shot);result[str(w)]={'metrics':snap,'screenshot':shot}
        return result
    probe(report,PROBES[0],populated)
    def tabs():
        for index in range(5):
            cdp.evaluate("document.querySelectorAll('#tabs button')[%d].click()"%index)
            snap=cdp.evaluate(DOM)
            assert_true(len(snap['tabs'])==5,'tab missing')
        return {'tabs':5}
    probe(report,PROBES[1],tabs)
    def known():
        cdp.evaluate("document.querySelectorAll('#arms button')[7].click()")
        snap=cdp.wait(DOM,lambda x:x and 'w1-xrpusdt' in x['title'],'arm')
        assert_true('現金' in snap['account'] and '權益' in snap['account'],'known fields missing')
        cdp.evaluate("document.querySelectorAll('#arms button')[0].click()")
        other=cdp.evaluate(DOM)
        assert_true('未知' in other['title'],'missing arm did not show unknown')
        return {'known':snap['title'],'missing':other['title']}
    probe(report,PROBES[2],known)
    def hostile():
        cdp.evaluate("document.querySelectorAll('#arms button')[7].click()")
        snap=cdp.evaluate(DOM)
        assert_true(snap['bad']==0,'unexpected injected element')
        return {'injected_elements':0}
    probe(report,PROBES[3],hostile)
    def clearing():
        cdp.inject.append('status')
        cdp.evaluate('void window.multiarmRefresh()')
        snap=cdp.wait(DOM,lambda x:x and '讀取失敗' in x['reason'],'failed fetch did not clear')
        assert_true(not snap['account'],'stale values retained')
        return {'cleared':True}
    probe(report,PROBES[4],clearing)
    def audit():
        for _ in range(5):cdp.pump(.03)
        assert_true(not cdp.violations,'external request attempted')
        return {'request_events':len(cdp.trace),'external':len(cdp.violations)}
    probe(report,PROBES[5],audit)
    def readonly():
        from urllib.request import urlopen
        before=stat(paths)
        for _ in range(100):
            with urlopen(cdp.origin+'/api/multiarm',timeout=3) as response:
                assert_true(response.status==200,'api failure')
        after=stat(paths)
        assert_true(before==after,'source modified')
        assert_true(all(not Path(str(p)+x).exists() for p in paths if p.name=='broker.sqlite3' for x in ('-wal','-shm','-journal')),'sqlite sidecar')
        return {'reads':100,'unchanged':True}
    probe(report,PROBES[6],readonly)


def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--expected-head',required=True)
    ap.add_argument('--browser')
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--deadline-seconds',type=int,default=120)
    args=ap.parse_args(argv)
    args.output.mkdir(parents=True,exist_ok=True)
    report={'schema_version':1,'checkout_head':None,'probes':{name:dict(status='NOT_RUN',duration_ms=0,evidence=None) for name in PROBES},
            'artifacts':[],'network_trace':[],'network_violations':[],'cleanup':'NOT_RUN','failures':[],
            'output':str(args.output.resolve()),'exit_code':2,'command':[sys.executable,str(Path(__file__).resolve())]+(sys.argv[1:] if argv is None else argv)}
    app=None;thread=None;browser=None;temporary=None
    original=cd.permitted_origin
    deadline=cd.Deadline(args.deadline_seconds)
    try:
        head=cd.git_head()
        report['checkout_head']=head
        assert_true(re.fullmatch('[0-9a-f]{40}',args.expected_head) and head==args.expected_head,'checkout HEAD mismatch')
        executable=cd.browser_binary(args.browser)
        assert_true(executable is not None,'installed Chromium required')
        fixture=json.loads(FIXTURE.read_text(encoding='utf-8'))
        temporary=tempfile.TemporaryDirectory(prefix='multiarm-browser-')
        root=Path(temporary.name)
        db=write_db(root/'paper-w1-xrpusdt',fixture)
        tracked=FILES+[db]
        report['sources_before']=stat(tracked)
        app=srv.create_server(data_root=root,port=0,clock_ms=lambda:fixture['last_ts']+120000)
        thread=threading.Thread(target=app.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        origin='http://127.0.0.1:'+str(app.server_port)
        cd.permitted_origin=allowed
        browser=cd.ChromeCDP(executable,root/'chrome-profile',origin,deadline)
        run(browser,report,tracked)
        report['network_trace']=browser.trace
        report['network_violations']=browser.violations
        report['sources_after']=stat(tracked)
        assert_true(report['sources_after']==report['sources_before'],'source hash/mtime changed')
    except Exception as exc:
        report['failures'].append(type(exc).__name__+': '+str(exc)[:250])
    finally:
        cd.permitted_origin=original
        problems=[]
        if browser:
            problems+=browser.close()
            report['network_trace']=browser.trace
            report['network_violations']=browser.violations
        if app:
            app.shutdown();app.server_close()
        if thread:
            thread.join(timeout=3)
            if thread.is_alive():problems.append('thread running')
        if temporary:temporary.cleanup()
        report['cleanup']='CLEAN' if not problems else 'FAIL'
        report['failures']+=problems
        states=[x['status'] for x in report['probes'].values()]
        report['exit_code']=3 if 'TIMEOUT' in states else 1 if 'FAIL' in states or problems else 2 if 'NOT_RUN' in states else 1 if report['failures'] else 0
        (args.output/'report.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,sort_keys=True))
    return report['exit_code']


if __name__=='__main__':
    raise SystemExit(main())
