"""Real headless-Chrome acceptance for synthetic multi-ledger dashboard views."""
import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

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


def assert_view(dom,*,ledger_label,symbol,exact_value):
    if 'data-layout-overflow="false"' not in dom:
        raise AssertionError('root viewport overflow detected or layout evidence missing')
    if ledger_label+' · 唯讀帳本' not in dom:
        raise AssertionError('selected ledger label not rendered')
    if symbol not in dom:
        raise AssertionError('selected ledger trade/symbol not rendered')
    if f'data-exact="{exact_value}"' not in dom:
        raise AssertionError('exact high-precision value is not accessible in DOM')
    if 'id="ledgerSelect"' not in dom:
        raise AssertionError('configured ledger selector missing')


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv);args.output.mkdir(parents=True,exist_ok=True)
    chrome=chrome_binary()
    with tempfile.TemporaryDirectory(prefix='issue3-browser-') as td:
        root=Path(td);nested=root/'nested';nested.mkdir()
        alpha=fresh(ledger_snapshot(capital='250',symbol='BTCUSDT',window_start=500,deadline=10000))
        beta=fresh(ledger_snapshot(capital='1000.123456789012345678901234567890',
                                   symbol='SOLUSDT',window_start=1500,deadline=22000,high_precision=True))
        alpha_path=root/'alpha.json';beta_path=nested/'beta.json'
        alpha_path.write_text(json.dumps(alpha));beta_path.write_text(json.dumps(beta))
        config=root/'ledgers.json'
        config.write_text(json.dumps(dict(schema_version=1,root=str(root.resolve()),default='alpha',
            ledgers=[dict(id='alpha',label='Alpha 250',status='alpha.json'),
                     dict(id='beta',label='Beta high precision',status='nested/beta.json')])))
        # Keep unrelated observer panels deterministic and public-safe.
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
            # Real browser navigation alpha -> beta -> alpha demonstrates configured selection paths.
            alpha_dom_1=chrome_dom(chrome,base+'?ledger=alpha',1366,768)
            beta_desktop=chrome_dom(chrome,base+'?ledger=beta',1366,768)
            beta_mobile=chrome_dom(chrome,base+'?ledger=beta',390,844)
            alpha_dom_2=chrome_dom(chrome,base+'?ledger=alpha',1366,768)
            assert_view(alpha_dom_1,ledger_label='Alpha 250',symbol='BTCUSDT',exact_value='250')
            assert_view(beta_desktop,ledger_label='Beta high precision',symbol='SOLUSDT',exact_value=exact)
            assert_view(beta_mobile,ledger_label='Beta high precision',symbol='SOLUSDT',exact_value=exact)
            assert_view(alpha_dom_2,ledger_label='Alpha 250',symbol='BTCUSDT',exact_value='250')
            desktop=args.output/'desktop-beta.png';mobile=args.output/'mobile-beta.png'
            chrome_shot(chrome,base+'?ledger=beta',1366,768,desktop)
            chrome_shot(chrome,base+'?ledger=beta',390,844,mobile)
            evidence=dict(
                schema_version=1,classification='SYNTHETIC_BROWSER_ACCEPTANCE_NOT_STRATEGY_PERFORMANCE',
                chrome=Path(chrome).name,
                sequence=['alpha','beta','alpha'],
                desktop=dict(viewport=[1366,768],png=list(png_size(desktop)),
                             sha256=hashlib.sha256(desktop.read_bytes()).hexdigest(),
                             layout_overflow=False,ledger='beta'),
                mobile=dict(viewport=[390,844],png=list(png_size(mobile)),
                            sha256=hashlib.sha256(mobile.read_bytes()).hexdigest(),
                            layout_overflow=False,ledger='beta'),
                exact_value_accessible=True,source='synthetic configured ledgers only')
            (args.output/'evidence.json').write_text(json.dumps(evidence,sort_keys=True,indent=2)+'\n')
            print('ISSUE3_BROWSER_EVIDENCE '+json.dumps(evidence,sort_keys=True,separators=(',',':')))
        finally:
            server.shutdown();server.server_close();thread.join()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
