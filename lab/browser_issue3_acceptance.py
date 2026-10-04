"""Real headless-Chrome acceptance for synthetic multi-ledger dashboard views."""
import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

LAB=Path(__file__).resolve().parent
TESTS=LAB/'tests'
sys.path.insert(0,str(LAB));sys.path.insert(0,str(TESTS))
import dashboard
from test_multi_ledger_dashboard import ledger_snapshot


def png_size(path):
    raw=Path(path).read_bytes()
    if raw[:8]!=b'\x89PNG\r\n\x1a\n':
        raise RuntimeError('browser screenshot is not PNG')
    return struct.unpack('>II',raw[16:24])


def fresh(snapshot):
    now=datetime.now(timezone.utc);ms=int(now.timestamp()*1000)
    snapshot['updated_at']=now.isoformat()
    snapshot['feed']['connected']=True;snapshot['feed']['last_success_at']=now.isoformat()
    for market in snapshot['markets']:
        market['last_received_at']=now.isoformat()
        market['source_timestamps_ms']=dict.fromkeys(('bookTicker','depth5','premiumIndex'),ms)
    return snapshot


def chrome_binary():
    for name in ('google-chrome','google-chrome-stable','chromium','chromium-browser'):
        found=shutil.which(name)
        if found:return found
    raise RuntimeError('real Chrome/Chromium binary unavailable on acceptance runner')


def chrome_dom(chrome,url,width,height):
    cmd=[chrome,'--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
         '--virtual-time-budget=5000',f'--window-size={width},{height}','--dump-dom',url]
    result=subprocess.run(cmd,text=True,capture_output=True,timeout=30)
    if result.returncode:
        raise RuntimeError('Chrome DOM run failed: '+result.stderr[-500:])
    return result.stdout


def chrome_shot(chrome,url,width,height,path):
    cmd=[chrome,'--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
         '--hide-scrollbars','--virtual-time-budget=5000',f'--window-size={width},{height}',
         f'--screenshot={path}',url]
    result=subprocess.run(cmd,text=True,capture_output=True,timeout=30)
    if result.returncode:
        raise RuntimeError('Chrome screenshot failed: '+result.stderr[-500:])
    if not Path(path).is_file() or Path(path).stat().st_size<1000:
        raise RuntimeError('Chrome screenshot missing or empty')


def inspect_view(dom,*,ledger_label,symbol,exact_values):
    """Return explicit readiness/layout diagnostics; never collapse missing into overflow."""
    def attr(name):
        match=re.search(r'\b'+re.escape(name)+r'="([^"]*)"',dom)
        return match.group(1) if match else None
    ready=attr('data-render-ready')
    overflow=attr('data-layout-overflow')
    scroll=attr('data-layout-scroll-width')
    client=attr('data-layout-client-width')
    phase=attr('data-layout-phase')
    failures=[]
    if ready!='true':
        failures.append('render-not-ready')
    if overflow is None or scroll is None or client is None:
        failures.append('layout-measurement-missing')
    elif overflow!='false':
        failures.append('root-layout-overflow')
    if ledger_label+' · 唯讀帳本' not in dom:
        failures.append('selected-ledger-label-missing')
    if symbol not in dom:
        failures.append('selected-ledger-symbol-missing')
    for exact_value in exact_values:
        if f'data-exact="{exact_value}"' not in dom:
            failures.append('exact-value-missing:'+exact_value)
    if 'id="ledgerSelect"' not in dom:
        failures.append('ledger-selector-missing')
    return dict(ready=ready,overflow=overflow,scroll_width=scroll,client_width=client,
                phase=phase,failures=failures)


def http_json(url):
    with urlopen(url,timeout=5) as response:
        return response.status,json.loads(response.read())


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=3)
    args=parser.parse_args(argv)
    if not 2<=args.rounds<=5:
        parser.error('--rounds must be 2..5 for bounded repeatability')
    args.output.mkdir(parents=True,exist_ok=True)
    chrome=chrome_binary()
    evidence=dict(schema_version=2,
                  classification='SYNTHETIC_BROWSER_ACCEPTANCE_NOT_STRATEGY_PERFORMANCE',
                  chrome=Path(chrome).name,rounds_requested=args.rounds,rounds=[],failures=[],
                  source='synthetic configured ledgers only')
    with tempfile.TemporaryDirectory(prefix='issue3-browser-') as td:
        root=Path(td);nested=root/'nested';nested.mkdir()
        alpha=fresh(ledger_snapshot(capital='250',symbol='BTCUSDT',window_start=500,deadline=10000))
        beta=fresh(ledger_snapshot(capital='1000.123456789012345678901234567890',
                                   symbol='SOLUSDT',window_start=750,deadline=22000,high_precision=True))
        alpha_path=root/'alpha.json';beta_path=nested/'beta.json'
        alpha_path.write_text(json.dumps(alpha));beta_path.write_text(json.dumps(beta))
        config=root/'ledgers.json'
        config.write_text(json.dumps(dict(schema_version=1,root=str(root.resolve()),default='alpha',
            ledgers=[dict(id='alpha',label='Alpha 250',status='alpha.json'),
                     dict(id='beta',label='Beta high precision',status='nested/beta.json')])))
        (root/'work_status.json').write_text(json.dumps(dict(schema_version=1,tasks=[])))
        health=dict(schema_version=1,available=True,mode='paper',checked_at=datetime.now(timezone.utc).isoformat(),
                    operational_healthy=True,engineering_resolved=False,incidents=[],faults={},pending_work=[],
                    service_restart_limitation='synthetic browser acceptance')
        (root/'health_status.json').write_text(json.dumps(health))
        server=dashboard.make_server(0,alpha_path,LAB/'dashboard.html',config)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}/'
            exact=beta['initial_equity_usdt']
            status_a,a1=http_json(base+'api/status?ledger=alpha')
            status_b,b=http_json(base+'api/status?ledger=beta')
            status_a2,a2=http_json(base+'api/status?ledger=alpha')
            if [status_a,status_b,status_a2]!=[200,200,200]:
                raise AssertionError('configured HTTP selection did not remain available')
            if [a1['ledger_view']['id'],b['ledger_view']['id'],a2['ledger_view']['id']]!=['alpha','beta','alpha']:
                raise AssertionError('HTTP ledger switch sequence mismatch')
            if a1['initial_equity_usdt']!='250' or a2['initial_equity_usdt']!='250':
                raise AssertionError('alpha capital changed across switch')
            if b['initial_equity_usdt']!=exact or b['observer']['closed_trades'][0]['symbol']!='SOLUSDT':
                raise AssertionError('beta ledger evidence mixed or missing')
            ratio=b['observer']['statistics']['fee_to_gross_percent']
            if ratio is None or len(ratio)<16:
                raise AssertionError('synthetic beta ratio did not exercise high precision')
            evidence['http_switch']=dict(sequence=['alpha','beta','alpha'],
                                         alpha_capital=a1['initial_equity_usdt'],
                                         beta_capital=b['initial_equity_usdt'],
                                         beta_symbol=b['observer']['closed_trades'][0]['symbol'],
                                         beta_fee_to_gross_percent=ratio)
            for round_no in range(1,args.rounds+1):
                round_ev=dict(round=round_no,sequence=['alpha','beta','alpha'],views={})
                alpha_dom_1=chrome_dom(chrome,base+'?ledger=alpha',1366,768)
                beta_desktop=chrome_dom(chrome,base+'?ledger=beta',1366,768)
                beta_mobile=chrome_dom(chrome,base+'?ledger=beta',390,844)
                alpha_dom_2=chrome_dom(chrome,base+'?ledger=alpha',1366,768)
                views=[
                    ('alpha_before',alpha_dom_1,'Alpha 250','BTCUSDT',['250'],1366,768),
                    ('beta_desktop',beta_desktop,'Beta high precision','SOLUSDT',[exact,ratio+'%'],1366,768),
                    ('beta_mobile',beta_mobile,'Beta high precision','SOLUSDT',[exact,ratio+'%'],390,844),
                    ('alpha_after',alpha_dom_2,'Alpha 250','BTCUSDT',['250'],1366,768)]
                for name,dom,label,symbol,exact_values,width,height in views:
                    diag=inspect_view(dom,ledger_label=label,symbol=symbol,exact_values=exact_values)
                    diag['viewport']=[width,height]
                    round_ev['views'][name]=diag
                    if diag['failures']:
                        evidence['failures'].append(dict(round=round_no,view=name,reasons=diag['failures'],
                                                         ready=diag['ready'],overflow=diag['overflow'],
                                                         scroll_width=diag['scroll_width'],
                                                         client_width=diag['client_width'],phase=diag['phase']))
                        (args.output/f'failure-round-{round_no}-{name}.html').write_text(dom,encoding='utf-8')
                for name,url,width,height in [
                    ('desktop',base+'?ledger=beta',1366,768),
                    ('mobile',base+'?ledger=beta',390,844)]:
                    shot=args.output/f'round-{round_no}-{name}-beta.png'
                    chrome_shot(chrome,url,width,height,shot)
                    round_ev[name]=dict(viewport=[width,height],png=list(png_size(shot)),
                                        sha256=hashlib.sha256(shot.read_bytes()).hexdigest())
                evidence['rounds'].append(round_ev)
            evidence['exact_value_accessible']=not any(
                any(reason.startswith('exact-value-missing:') for reason in failure['reasons'])
                for failure in evidence['failures'])
        except Exception as exc:
            evidence['fatal_error']=type(exc).__name__+': '+str(exc)
            raise
        finally:
            evidence['rounds_completed']=len(evidence['rounds'])
            (args.output/'evidence.json').write_text(json.dumps(evidence,sort_keys=True,indent=2)+'\n')
            print('ISSUE3_BROWSER_EVIDENCE '+json.dumps(evidence,sort_keys=True,separators=(',',':')))
            server.shutdown();server.server_close();thread.join()
    if evidence['failures']:
        first=evidence['failures'][0]
        raise AssertionError('bounded browser acceptance failed: '+json.dumps(first,sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
