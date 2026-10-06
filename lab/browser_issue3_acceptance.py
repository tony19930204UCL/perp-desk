"""Real headless-Chrome acceptance for synthetic multi-ledger dashboard views."""
import argparse
import base64
import hashlib
import json
import re
import os
import socket
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlsplit

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



class CdpSession:
    """Minimal stdlib CDP client: one real Chrome page/session, no external driver."""
    def __init__(self,chrome,profile):
        self.stderr_capture=tempfile.TemporaryFile(mode='w+t',encoding='utf-8',errors='replace')
        started=time.monotonic()
        self.proc=subprocess.Popen(
            [chrome,'--headless=new','--no-sandbox','--disable-gpu','--disable-dev-shm-usage',
             '--remote-debugging-port=0','--remote-allow-origins=*',
             '--user-data-dir='+str(profile),'about:blank'],
            stdout=subprocess.DEVNULL,stderr=self.stderr_capture,text=True)
        # Chrome with port=0 publishes the selected port atomically in the profile.
        # Hosted runners can take longer than ten seconds under package/cache pressure,
        # so wait for both the file and the HTTP page target within one finite deadline.
        active_port=Path(profile)/'DevToolsActivePort'
        deadline=started+30;page=None;last_error=None
        while time.monotonic()<deadline:
            if self.proc.poll() is not None: break
            try:
                lines=active_port.read_text().splitlines()
                if lines and lines[0].isdigit():
                    port=int(lines[0])
                    targets=json.loads(urlopen(f'http://127.0.0.1:{port}/json',timeout=1).read())
                    page=next((item for item in targets if item.get('type')=='page'),None)
                    if page: break
            except (FileNotFoundError,OSError,UnicodeError,ValueError) as exc:
                last_error=type(exc).__name__
            time.sleep(.05)
        self.startup_seconds=round(time.monotonic()-started,3)
        if not page:
            exit_code=self.proc.poll()
            try:
                self.stderr_capture.flush();self.stderr_capture.seek(0)
                stderr_tail=self.stderr_capture.read()[-1200:].replace(str(profile),'<profile>')
            except (OSError,ValueError):
                stderr_tail='<unavailable>'
            self.close()
            raise RuntimeError(
                f'Chrome DevTools endpoint unavailable after {self.startup_seconds}s '
                f'(exit={exit_code!r}, last_error={last_error!r}, stderr_tail={stderr_tail!r})')
        self.ws=self._connect(page['webSocketDebuggerUrl'])
        self.next_id=1;self.events=[]

    def _connect(self,url):
        u=urlsplit(url);sock=socket.create_connection((u.hostname,u.port),timeout=10)
        key=base64.b64encode(os.urandom(16)).decode()
        request=(f'GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n'
                 'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                 f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
        sock.sendall(request.encode())
        response=b''
        while b'\r\n\r\n' not in response:
            chunk=sock.recv(4096)
            if not chunk: raise RuntimeError('WebSocket handshake closed')
            response+=chunk
        if b' 101 ' not in response.split(b'\r\n',1)[0]:
            raise RuntimeError('WebSocket handshake failed')
        return sock

    def _send_frame(self,payload):
        data=payload.encode();mask=os.urandom(4);n=len(data)
        head=bytearray([0x81])
        if n<126: head.append(0x80|n)
        elif n<65536: head.extend([0x80|126]);head.extend(struct.pack('>H',n))
        else: head.extend([0x80|127]);head.extend(struct.pack('>Q',n))
        masked=bytes(b^mask[i%4] for i,b in enumerate(data))
        self.ws.sendall(bytes(head)+mask+masked)

    def _recv_exact(self,n):
        out=b''
        while len(out)<n:
            chunk=self.ws.recv(n-len(out))
            if not chunk: raise RuntimeError('WebSocket closed')
            out+=chunk
        return out

    def _recv_frame(self):
        first,second=self._recv_exact(2);opcode=first&0x0f;n=second&0x7f
        if n==126:n=struct.unpack('>H',self._recv_exact(2))[0]
        elif n==127:n=struct.unpack('>Q',self._recv_exact(8))[0]
        masked=bool(second&0x80);mask=self._recv_exact(4) if masked else None
        data=self._recv_exact(n)
        if masked:data=bytes(b^mask[i%4] for i,b in enumerate(data))
        if opcode==0x9:
            # Server ping. Reply pong, preserving payload.
            frame=bytearray([0x8A]);ln=len(data);frame.append(0x80|ln);m=os.urandom(4)
            self.ws.sendall(bytes(frame)+m+bytes(b^m[i%4] for i,b in enumerate(data)))
            return self._recv_frame()
        if opcode==0x8: raise RuntimeError('WebSocket closed by Chrome')
        return data.decode()

    def call(self,method,params=None,timeout=10):
        call_id=self.next_id;self.next_id+=1
        self._send_frame(json.dumps(dict(id=call_id,method=method,params=params or {}),separators=(',',':')))
        self.ws.settimeout(timeout)
        while True:
            message=json.loads(self._recv_frame())
            if message.get('id')==call_id:
                if 'error' in message:raise RuntimeError(method+': '+json.dumps(message['error']))
                return message.get('result',{})
            self.events.append(message)

    def evaluate(self,expression):
        result=self.call('Runtime.evaluate',dict(expression=expression,returnByValue=True,awaitPromise=True))
        remote=result.get('result',{})
        if remote.get('subtype')=='error':
            raise RuntimeError('Runtime.evaluate failed: '+remote.get('description','error'))
        return remote.get('value')

    def navigate(self,url):
        self.call('Page.navigate',{'url':url})
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            state=self.evaluate('document.readyState')
            if state=='complete': return
            time.sleep(.05)
        raise RuntimeError('navigation did not reach document.complete')

    def set_mobile_metrics(self,width=390,height=844,dpr=1):
        self.call('Emulation.setDeviceMetricsOverride',dict(
            width=width,height=height,deviceScaleFactor=dpr,mobile=True,
            screenWidth=width,screenHeight=height,screenOrientation={'type':'portraitPrimary','angle':0}))

    def wait_dashboard(self,ledger_label,symbol,exact_values,timeout=8):
        exact_json=json.dumps(exact_values)
        label_json=json.dumps(ledger_label+' · 唯讀帳本')
        symbol_json=json.dumps(symbol)
        expression=f"""(()=>{{
          const root=document.documentElement;
          const exacts={exact_json};
          const exactSeen=exacts.map(v=>!!document.querySelector('[data-exact="'+CSS.escape(v)+'"]'));
          return {{
            ready:root.dataset.renderReady||null,
            phase:root.dataset.layoutPhase||null,
            innerWidth:window.innerWidth,innerHeight:window.innerHeight,
            clientWidth:root.clientWidth,clientHeight:root.clientHeight,
            scrollWidth:root.scrollWidth,scrollHeight:root.scrollHeight,
            devicePixelRatio:window.devicePixelRatio,
            overflow:root.scrollWidth>root.clientWidth,
            declaredOverflow:root.dataset.layoutOverflow||null,
            declaredClientWidth:root.dataset.layoutClientWidth||null,
            declaredScrollWidth:root.dataset.layoutScrollWidth||null,
            label:document.getElementById('accountName')?.textContent||'',
            bodyText:document.body.innerText,
            exactSeen:exactSeen
          }};
        }})()"""
        deadline=time.monotonic()+timeout;last=None
        while time.monotonic()<deadline:
            last=self.evaluate(expression)
            if (last and last.get('ready')=='true' and last.get('label')==ledger_label+' · 唯讀帳本'
                    and symbol in last.get('bodyText','') and all(last.get('exactSeen',[]))):
                return last
            time.sleep(.05)
        raise AssertionError('mobile dashboard readiness timeout: '+json.dumps(last,sort_keys=True))

    def screenshot(self,path):
        shot=self.call('Page.captureScreenshot',{'format':'png','fromSurface':True,'captureBeyondViewport':False})
        raw=base64.b64decode(shot['data']);Path(path).write_bytes(raw)
        return png_size(path)

    def close(self):
        try:
            if getattr(self,'ws',None): self.ws.close()
        except Exception:pass
        if getattr(self,'proc',None) and self.proc.poll() is None:
            self.proc.terminate()
            try:self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill();self.proc.wait(timeout=5)
        try:
            if getattr(self,'stderr_capture',None):self.stderr_capture.close()
        except Exception:pass


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



def mobile_rounds_same_session(chrome,base,output,rounds,exact,ratio):
    """One browser process/page/session, fixed 390x844 CSS device metrics, bounded rounds."""
    profile=Path(tempfile.mkdtemp(prefix='issue3-cdp-profile-'))
    session=None;round_evidence=[]
    try:
        session=CdpSession(chrome,profile)
        session.call('Page.enable');session.call('Runtime.enable')
        session.set_mobile_metrics(390,844,1)
        for round_no in range(1,rounds+1):
            views={}
            for name,url,label,symbol,exact_values in [
                ('alpha_before',base+'?ledger=alpha','Alpha 250','BTCUSDT',['250']),
                ('beta_mobile',base+'?ledger=beta','Beta high precision','SOLUSDT',[exact,ratio+'%']),
                ('alpha_after',base+'?ledger=alpha','Alpha 250','BTCUSDT',['250'])]:
                session.navigate(url)
                metrics=session.wait_dashboard(label,symbol,exact_values)
                failures=[]
                if metrics['innerWidth']!=390: failures.append('inner-width-not-390')
                if metrics['clientWidth']!=390: failures.append('document-client-width-not-390')
                if metrics['scrollWidth']!=390: failures.append('root-scroll-width-not-390')
                if metrics['overflow'] is not False: failures.append('root-layout-overflow')
                if float(metrics['devicePixelRatio'])!=1.0: failures.append('unexpected-device-pixel-ratio')
                if failures:
                    raise AssertionError('true-390 mobile metrics failed: '+json.dumps(
                        dict(round=round_no,view=name,failures=failures,metrics=metrics),sort_keys=True))
                views[name]=metrics
                if name=='beta_mobile':
                    shot=output/f'round-{round_no}-mobile-beta-cdp.png'
                    png=session.screenshot(shot)
                    scale=[png[0]/metrics['innerWidth'],png[1]/metrics['innerHeight']]
                    views[name]['screenshot']=dict(
                        png=list(png),sha256=hashlib.sha256(shot.read_bytes()).hexdigest(),
                        css_viewport=[metrics['innerWidth'],metrics['innerHeight']],
                        device_pixel_ratio=metrics['devicePixelRatio'],pixel_per_css=scale)
                    if png[0]!=390:
                        raise AssertionError('mobile screenshot pixel width not 390 in same CDP session: '+json.dumps(views[name]['screenshot']))
            round_evidence.append(dict(round=round_no,sequence=['alpha','beta','alpha'],views=views))
        return round_evidence
    finally:
        if session:session.close()
        shutil.rmtree(profile,ignore_errors=True)


def main(argv=None):
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=3)
    args=parser.parse_args(argv)
    if not 2<=args.rounds<=5:
        parser.error('--rounds must be 2..5 for bounded repeatability')
    args.output.mkdir(parents=True,exist_ok=True)
    chrome=chrome_binary()
    evidence=dict(schema_version=3,
                  classification='SYNTHETIC_BROWSER_ACCEPTANCE_NOT_STRATEGY_PERFORMANCE',
                  chrome=Path(chrome).name,rounds_requested=args.rounds,rounds=[],failures=[],
                  source='synthetic configured ledgers only',previous_mobile_measurement=dict(requested_window=[390,844],observed_client_width=485,observed_scroll_width=485,png=[390,844],status='preserved prior harness mismatch; not treated as 390 CSS viewport'))
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
            # Desktop keeps the existing real-Chrome command-line acceptance.
            for round_no in range(1,args.rounds+1):
                round_ev=dict(round=round_no,sequence=['alpha','beta','alpha'],views={})
                alpha_dom_1=chrome_dom(chrome,base+'?ledger=alpha',1366,768)
                beta_desktop=chrome_dom(chrome,base+'?ledger=beta',1366,768)
                alpha_dom_2=chrome_dom(chrome,base+'?ledger=alpha',1366,768)
                views=[
                    ('alpha_before',alpha_dom_1,'Alpha 250','BTCUSDT',['250'],1366,768),
                    ('beta_desktop',beta_desktop,'Beta high precision','SOLUSDT',[exact,ratio+'%'],1366,768),
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
                shot=args.output/f'round-{round_no}-desktop-beta.png'
                chrome_shot(chrome,base+'?ledger=beta',1366,768,shot)
                round_ev['desktop']=dict(requested_window=[1366,768],png=list(png_size(shot)),
                                         sha256=hashlib.sha256(shot.read_bytes()).hexdigest())
                evidence['rounds'].append(round_ev)

            # Mobile evidence is a single real Chrome browser/page session with an
            # explicit 390x844 CSS device-metrics override. No relabeling of
            # desktop --window-size output is accepted.
            evidence['mobile_cdp_rounds']=mobile_rounds_same_session(
                chrome,base,args.output,args.rounds,exact,ratio)
            evidence['exact_value_accessible']=not any(
                any(reason.startswith('exact-value-missing:') for reason in failure['reasons'])
                for failure in evidence['failures'])
            evidence['true_mobile_css_viewport_verified']=all(
                view['innerWidth']==390 and view['clientWidth']==390 and view['scrollWidth']==390
                and view['overflow'] is False and float(view['devicePixelRatio'])==1.0
                and view['screenshot']['png'][0]==390 and view['screenshot']['pixel_per_css'][0]==1.0
                for round_ev in evidence['mobile_cdp_rounds']
                for name,view in round_ev['views'].items() if name=='beta_mobile')
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
