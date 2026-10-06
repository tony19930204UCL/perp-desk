"""Real-Chrome fixture acceptance for Issue #32 observer console."""
import argparse,json,hashlib,tempfile,threading,time,sys
from datetime import datetime,timezone,timedelta
from pathlib import Path

LAB=Path(__file__).resolve().parent
TESTS=LAB/'tests'
sys.path.insert(0,str(LAB));sys.path.insert(0,str(TESTS))
import dashboard
from browser_issue3_acceptance import CdpSession,chrome_binary,png_size
from test_multi_ledger_dashboard import ledger_snapshot
from test_research_dashboard import ResearchFixture

def rgb(value):
    nums=[int(x) for x in value[value.find('(')+1:value.find(')')].split(',')[:3]]
    return [x/255 for x in nums]
def lum(c):
    vals=[x/12.92 if x<=.04045 else ((x+.055)/1.055)**2.4 for x in c]
    return .2126*vals[0]+.7152*vals[1]+.0722*vals[2]
def contrast(a,b):
    x,y=sorted((lum(rgb(a)),lum(rgb(b))),reverse=True);return (x+.05)/(y+.05)

def work_fixture(now):
    old=(now-timedelta(minutes=20)).isoformat()
    return dict(schema_version=1,tasks=[
        dict(id='active',title='目前 evidence review',state='running',current_step='read report',next_step='verify',
             updated_at=old,started_at=old,evidence=['artifact:fixture']),
        dict(id='done',title='歷史交付',state='completed',current_step='done',next_step='none',
             updated_at=old,started_at=old,evidence=['ci:fixture'])])
def health_fixture(now):
    old=(now-timedelta(hours=2)).isoformat()
    return dict(schema_version=1,available=True,mode='paper',operational_healthy=False,engineering_resolved=False,
                checked_at=old,incidents=[dict(id='historical-storage-hold',status='recovered_monitoring')],
                faults={},pending_work=[])

def wait_ready(s):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        d=s.evaluate("""(()=>({ready:document.documentElement.dataset.renderReady,
          research:document.getElementById('currentResearchName')?.textContent||'',
          account:document.getElementById('accountName')?.textContent||''}))()""")
        if d and d.get('ready')=='true' and d.get('research')=='ETH-DISCOVERY-LAB-001' and '唯讀帳本' in d.get('account',''):return d
        time.sleep(.05)
    raise AssertionError('dashboard did not render')

def metrics(s):
    return s.evaluate("""(()=>{const r=document.documentElement,b=document.body;return {
      innerWidth,innerHeight,clientWidth:r.clientWidth,clientHeight:r.clientHeight,
      scrollWidth:r.scrollWidth,scrollHeight:r.scrollHeight,devicePixelRatio,
      overflow:r.scrollWidth>r.clientWidth,
      theme:getComputedStyle(r).getPropertyValue('--bg').trim(),
      text:getComputedStyle(b).color,background:getComputedStyle(b).backgroundColor,
      muted:getComputedStyle(document.querySelector('.muted')).color,
      controlColor:getComputedStyle(document.getElementById('themeToggle')).color,
      controlBg:getComputedStyle(document.getElementById('themeToggle')).backgroundColor,
      controlBorder:getComputedStyle(document.getElementById('themeToggle')).borderTopColor,
      surface:getComputedStyle(document.querySelector('.panel')).backgroundColor,
      focusOutline:getComputedStyle(document.getElementById('themeToggle')).getPropertyValue('outline-style'),
      bodyFont:getComputedStyle(b).fontFamily,
      cjkFontAvailable:document.fonts.check('14px "Noto Sans CJK TC"','觀察員目前研究歷史帳戶工作證據')
    }})()""")

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args(argv);a.output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        f=ResearchFixture(td);root=Path(td);now=datetime.now(timezone.utc)
        status=ledger_snapshot(capital='250',symbol='BTCUSDT',window_start=500,deadline=10000,high_precision=True)
        status['updated_at']=now.isoformat();status['feed']['connected']=True;status['feed']['last_success_at']=now.isoformat()
        ms=int(now.timestamp()*1000)
        for m in status['markets']:
            m['last_received_at']=now.isoformat();m['source_timestamps_ms']=dict.fromkeys(('bookTicker','depth5','premiumIndex'),ms)
        status_path=root/'status.json';status_path.write_text(json.dumps(status))
        (root/'work_status.json').write_text(json.dumps(work_fixture(now)))
        (root/'health_status.json').write_text(json.dumps(health_fixture(now)))
        server=dashboard.make_server(0,status_path,LAB/'dashboard.html',None,f.cfg.resolve())
        t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
        chrome=chrome_binary();session=None;profile=root/'chrome-profile';evidence={'label':'ISSUE32_FIXTURE_BROWSER_NOT_LIVE','views':{}}
        try:
            session=CdpSession(chrome,profile);session.call('Page.enable');session.call('Runtime.enable')
            base=f'http://127.0.0.1:{server.server_port}/'
            for width,height in ((390,844),(768,900),(1280,800)):
                session.call('Emulation.setDeviceMetricsOverride',dict(width=width,height=height,deviceScaleFactor=1,mobile=width<=390,screenWidth=width,screenHeight=height))
                session.navigate(base);wait_ready(session)
                # Current-research DOM, separate arms and diagnostic.
                session.evaluate("document.getElementById('tab-research').click()")
                research=session.evaluate("""(()=>({hidden:document.getElementById('view-research').hidden,
                  text:document.getElementById('view-research').innerText,
                  arms:[...document.querySelectorAll('#armsTable tr')].map(r=>r.innerText)}))()""")
                if research['hidden'] or len(research['arms'])!=3 or '2x diagnostic' not in research['text']:
                    raise AssertionError('research view missing three-arm/2x separation')
                if not all(('Arm '+x) in '\n'.join(research['arms']) for x in 'ABC'):
                    raise AssertionError('arm rows missing')
                # Historical account remains distinct and exact-value nodes survive.
                session.evaluate("document.getElementById('tab-account').click()")
                hist=session.evaluate("""(()=>({text:document.getElementById('view-account').innerText,
                  exact:[...document.querySelectorAll('#view-account [data-exact]')].map(x=>x.dataset.exact)}))()""")
                if '歷史' not in hist['text'] or status['initial_equity_usdt'] not in hist['exact']:
                    raise AssertionError('historical account exact binding missing')
                # Work and historical health remain separate.
                session.evaluate("document.getElementById('tab-work').click()")
                work=session.evaluate("document.getElementById('view-work').innerText")
                if '目前 evidence review' not in work or '歷史交付' not in work or 'historical-storage-hold' not in work:
                    raise AssertionError('work/incident hierarchy missing')
                # Refresh must retain view, focus and scroll.
                session.evaluate("document.getElementById('tab-research').click();document.getElementById('tab-research').focus();scrollTo(0,200)")
                before=session.evaluate("({view:sessionStorage.getItem('perpdesk-view'),focus:document.activeElement.id,y:scrollY})")
                session.evaluate("refresh()")
                time.sleep(.4)
                after=session.evaluate("({view:sessionStorage.getItem('perpdesk-view'),focus:document.activeElement.id,y:scrollY,hidden:document.getElementById('view-research').hidden})")
                if (after['view']!='research' or after['focus']!='tab-research' or after['hidden']
                        or abs(after['y']-before['y'])>1):
                    raise AssertionError('refresh lost selected view/focus/scroll position')
                session.call('Input.dispatchKeyEvent',dict(type='keyDown',key='Shift',code='ShiftLeft',windowsVirtualKeyCode=16,nativeVirtualKeyCode=16))
                session.evaluate("document.getElementById('themeToggle').focus()")
                session.call('Input.dispatchKeyEvent',dict(type='keyUp',key='Shift',code='ShiftLeft',windowsVirtualKeyCode=16,nativeVirtualKeyCode=16))
                focus_id=session.evaluate("document.activeElement?.id||''")
                if focus_id!='themeToggle':raise AssertionError('keyboard-modality focus missing on theme control: '+focus_id)
                m=metrics(session)
                if m['innerWidth']!=width or m['scrollWidth']>m['clientWidth'] or m['overflow']:
                    raise AssertionError('viewport/root overflow mismatch '+json.dumps(m))
                if m['focusOutline']=='none':
                    raise AssertionError('focus-visible outline missing '+json.dumps(m))
                if not m['cjkFontAvailable']:
                    raise AssertionError('Traditional Chinese fixture font unavailable '+json.dumps(m))
                normal=contrast(m['text'],m['background']);muted=contrast(m['muted'],m['background']);control=contrast(m['controlColor'],m['controlBg'])
                boundary=contrast(m['controlBorder'],m['controlBg'])
                if min(normal,muted)<4.5 or control<4.5:raise AssertionError('text contrast below 4.5')
                if boundary<3:raise AssertionError('non-text control contrast below 3')
                shot=a.output/f'issue32-{width}.png';size=session.screenshot(shot)
                evidence['views'][str(width)]=dict(metrics=m,screenshot_size=size,screenshot_sha256=hashlib.sha256(shot.read_bytes()).hexdigest(),
                                                   contrast=dict(normal=normal,muted=muted,control=control,boundary=boundary),refresh_before=before,refresh_after=after)
            # Real-DOM blocked-state acceptance: source gap + storage stop + inactive
            # process proof must remain visibly blocked/unconfirmed after an ordinary refresh.
            blocked=json.loads(f.report.read_text())
            blocked['runner']['source_failure']=True
            blocked['source_gaps']=2
            blocked['storage_used_bytes']=31_500_000
            f.report.write_text(json.dumps(blocked))
            f.proof.write_text(json.dumps(dict(schema_version=1,active=False,
                                               observed_at_ms=int(datetime.now(timezone.utc).timestamp()*1000),
                                               proof_kind='owned_process')))
            session.evaluate("document.getElementById('tab-overview').click();refresh()")
            deadline=time.monotonic()+4
            blocked_dom=None
            while time.monotonic()<deadline:
                blocked_dom=session.evaluate("""(()=>({
                  state:document.getElementById('researchState')?.textContent||'',
                  blockers:document.getElementById('currentBlockers')?.innerText||'',
                  process:document.getElementById('processProof')?.textContent||'',
                  processAge:document.getElementById('processProofAge')?.textContent||''
                }))()""")
                if ('source gap' in blocked_dom.get('state','')
                        and 'source_gaps_recorded' in blocked_dom.get('blockers','')
                        and 'storage_entry_stop' in blocked_dom.get('blockers','')
                        and 'process_unconfirmed' in blocked_dom.get('blockers','')
                        and blocked_dom.get('process')=='未確認'):
                    break
                time.sleep(.05)
            else:
                raise AssertionError('blocked research state not truthfully rendered '+json.dumps(blocked_dom,ensure_ascii=False))
            evidence['blocked_dom']=blocked_dom
            # Keyboard tab navigation is explicit, not color-only.
            session.evaluate("document.getElementById('tab-overview').focus();document.getElementById('tab-overview').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}))")
            key=session.evaluate("({focus:document.activeElement.id,selected:document.querySelector('.tab[aria-selected=true]').id})")
            if key!=dict(focus='tab-research',selected='tab-research'):raise AssertionError('keyboard tab navigation failed')
            evidence['keyboard']=key
            from urllib.request import urlopen
            api_research=json.loads(urlopen(base+'api/research',timeout=5).read())
            api_ledgers=json.loads(urlopen(base+'api/ledgers',timeout=5).read())
            api=dict(research=dict(id=api_research.get('id'),label=api_research.get('label'),
                                   state=api_research.get('state'),capital_pooled=api_research.get('capital_pooled'),
                                   arms={a:dict(net_ledger_usdt=api_research['arms'][a]['net_ledger_usdt'],
                                                positions=len(api_research['arms'][a]['account']['positions']),
                                                pending=api_research['arms'][a]['account']['pending_orders'])
                                         for a in 'ABC'}),
                     ledgers=api_ledgers)
            (a.output/'api-evidence.json').write_text(json.dumps(api,indent=2,sort_keys=True))
            (a.output/'evidence.json').write_text(json.dumps(evidence,indent=2,sort_keys=True))
        finally:
            if session:session.close()
            server.shutdown();server.server_close();t.join()
    print(json.dumps(evidence,sort_keys=True))
    return 0

if __name__=='__main__':raise SystemExit(main())
