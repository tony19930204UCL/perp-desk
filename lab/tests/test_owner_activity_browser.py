"""Node DOM interaction regression for actual Monitor JS; real screenshots not claimed."""
import copy
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / 'owner_activity.js'
HTML = ROOT / 'owner_activity.html'
HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Node {
 constructor(tag){this.tagName=tag;this.children=[];this._text='';this.listeners={};this.value='';this.options=[];this.dataset={};}
 set textContent(value){this._text=String(value);this.children=[]}
 get textContent(){return this._text+this.children.map(x=>x.textContent).join('')}
 appendChild(child){this.children.push(child);if(this.tagName==='select'&&child.tagName==='option')this.options.push(child);return child}
 replaceChildren(...children){this._text='';this.children=children}
 addEventListener(key,callback){this.listeners[key]=callback}
 remove(i){this.options.splice(i,1);this.children.splice(i,1)}
}
const ids={};for(let id of ['search','stateFilter','stageFilter','tasks','metrics','stages','sources','events','detail','status','visibleCount','refresh'])ids[id]=new Node(id);
ids.stageFilter.options=[{value:''}];
const document={getElementById:id=>ids[id],createElement:tag=>new Node(tag),
 addEventListener:(name,cb)=>{if(name==='DOMContentLoaded')document.ready=cb}};
const pending=[];const fetch=(url,options)=>new Promise(resolve=>pending.push({url,options,resolve}));
const window={},context={document,window,fetch,setInterval:()=>0,console,Object,String,Number,Array,Set};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
const task=(id,state)=>({id,title:'safe '+id,state,stage:'paper',warnings:[],source_keys:['ledger'],dependencies:[],blockers:[],evidence:[],history:[],activity:'not_proven'});
const model={schema_version:1,observer_only:true,generated_at:'2026-10-15',sources:{ledger:{state:'available',updated_at:null}},
 totals:{ledger:2,union:2},stages:[{id:'paper',title:'Paper reliability',exit_gate:'unsupported'}],
 tasks:[task('C-0026','scheduled_idle'),task('C-0028','blocked')],events:[]};
const find=(el,tag)=>el.children.flatMap(child=>[...(child.tagName===tag?[child]:[]),...find(child,tag)]);
(async()=>{
 document.ready();assert.equal(pending.length,1);
 assert.equal(pending[0].url,'/api/owner-activity');assert.equal(pending[0].options.cache,'no-store');
 pending[0].resolve({ok:true,json:async()=>model});await new Promise(resolve=>setImmediate(resolve));
 assert(ids.tasks.textContent.includes('C-0026'));assert(ids.metrics.textContent.includes('2'));
 ids.search.value='C-0028';ids.search.listeners.input();
 assert(!ids.tasks.textContent.includes('C-0026'));assert(ids.tasks.textContent.includes('C-0028'));
 const buttons=find(ids.tasks,'button');assert.equal(buttons.length,1);buttons[0].listeners.click();
 assert(ids.detail.textContent.includes('C-0028'));
 ids.search.value='';ids.search.listeners.input();
 ids.stateFilter.value='scheduled_idle';ids.stateFilter.listeners.change();
 assert(!ids.tasks.textContent.includes('C-0028'));
 ids.stateFilter.value='';ids.stateFilter.listeners.change();
 const hostile='<img src=x onerror=alert(1)>';const changed={...model,tasks:[{...task('C-0026','activity_unconfirmed'),title:hostile}]};
 window.renderOwnerActivity(changed);
 assert(ids.tasks.textContent.includes(hostile));assert.equal(find(ids.tasks,'img').length,0);
 // Selecting a task and removing it on refresh must clear the former detail.
 window.renderOwnerActivity({...model,tasks:[task('C-0030','failed')]});
 find(ids.tasks,'button')[0].listeners.click();
 assert(ids.detail.textContent.includes('C-0030'));
 const removed=window.refreshOwnerActivity();
 pending[1].resolve({ok:true,json:async()=>({...model,tasks:[task('C-0026','scheduled_idle')]})});
 await removed;
 assert(!ids.detail.textContent.includes('C-0030'));
 assert(ids.detail.textContent.includes('已不存在'));
 // A filtered-out but still existing task remains selectable only as current model data.
 window.renderOwnerActivity(model);
 ids.search.value='C-0028';ids.search.listeners.input();
 find(ids.tasks,'button')[0].listeners.click();
 ids.search.value='C-0026';ids.search.listeners.input();
 assert(ids.detail.textContent.includes('C-0028'));
 ids.search.value='';ids.search.listeners.input();
 const old=window.refreshOwnerActivity(),newer=window.refreshOwnerActivity();
 pending[3].resolve({ok:true,json:async()=>({...model,tasks:[task('NEW','running_verified')]})});await newer;
 pending[2].resolve({ok:true,json:async()=>({...model,tasks:[task('OLD','blocked')]})});await old;
 assert(ids.tasks.textContent.includes('NEW'));assert(!ids.tasks.textContent.includes('OLD'));
 console.log('Node DOM interactions PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
"""


class BrowserContract(unittest.TestCase):
    def test_html_responsive_and_safe(self):
        html = HTML.read_text(encoding='utf-8')
        self.assertIn('width=device-width', html)
        self.assertIn('@media(max-width:430px)', html)
        self.assertIn('aria-live=', html)
        self.assertIn('owner_activity.js', html)
        for forbidden in ('<iframe', 'onclick=', 'https://'):
            self.assertNotIn(forbidden, html)
        js = JS.read_text(encoding='utf-8')
        for forbidden in ('innerHTML', 'outerHTML', 'document.write', 'eval('):
            self.assertNotIn(forbidden, js)

    @unittest.skipUnless(shutil.which('node'), 'node unavailable')
    def test_real_js_search_filter_detail_refresh_race(self):
        run = subprocess.run(['node', '-e', HARNESS, str(JS)], capture_output=True,
                             text=True, timeout=15, check=False)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


    def test_real_chromium_300_task_mobile_layout(self):
        """True DOM measurements; synthetic records only, never a live profile."""
        from browser_issue3_acceptance import CdpSession, chrome_binary
        from dashboard_v2_server import create_server
        try:
            chrome = chrome_binary()
        except RuntimeError:
            self.skipTest('Chromium not installed: real viewport measurement unavailable')
        original = json.loads((ROOT / 'tests/fixtures/owner_activity_sanitized.json').read_text())
        self.assertTrue(original['synthetic'])
        long_name = ('跨階段研究驗證_調查任務-' + 'SYNTHETICUNBROKENSTATUSNAME' * 8 +
                     '-多語文依賴情境未確認・Αλφα・العربية')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for folder in ('commitments', 'cron', 'lab/journal', 'lab/shared'):
                (root / folder).mkdir(parents=True, exist_ok=True)
            ledger = copy.deepcopy(original['ledger'])
            for number in range(5, 300):
                ledger['items'].append({
                    'id': 'C-SYNTH-%04d' % number,
                    'text': long_name + ('-' + str(number)),
                    'state': 'BLOCKED' if number % 7 == 0 else 'OPEN',
                    'blocked_by': 'item:C-UNCONFIRMED' if number % 7 == 0 else None,
                    'updated': 1792062000, 'due': 1792234800,
                    'reason': '多語文_原因_' + long_name,
                    'accept': '驗收依據_' + long_name})
            roadmap = copy.deepcopy(original['roadmap'])
            roadmap['goals'][0]['title'] = long_name
            work = copy.deepcopy(original['work'])
            work['tasks'][0]['current_step'] = long_name
            work['tasks'][0]['next_step'] = long_name
            sources = {
                'commitments/ledger.json': ledger,
                'cron/jobs.json': original['cron'],
                'lab/journal/roadmap.json': roadmap,
                'lab/shared/work_status.json': work,
            }
            for rel, obj in sources.items():
                (root / rel).write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
            (root / 'lab/journal/entries.jsonl').write_text(
                json.dumps(original['journal'][0], ensure_ascii=False) + '\n', encoding='utf-8')
            server = create_server(scorecard_path=str(root / 'score'),
                                   journal_path=str(root / 'journal'),
                                   ledger_path=str(root / 'account_ledger'),
                                   port=0, clock_ms=lambda: 1792065600000,
                                   max_bytes=1048576)
            server.configure_monitor(profile_root=str(root))
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={'poll_interval': .01}, daemon=True)
            thread.start()
            session = None
            try:
                session = CdpSession(chrome, root / 'chrome-profile')
                session.call('Page.enable')
                session.call('Runtime.enable')
                base = 'http://127.0.0.1:%d/owner_activity.html' % server.server_port
                for width, height, mobile in ((390, 844, True), (1440, 900, False)):
                    with self.subTest(width=width):
                        session.call('Emulation.setDeviceMetricsOverride', {
                            'width': width, 'height': height,
                            'deviceScaleFactor': 1, 'mobile': mobile,
                            'screenWidth': width, 'screenHeight': height})
                        session.navigate(base)
                        deadline = time.monotonic() + 8
                        metrics = None
                        while time.monotonic() < deadline:
                            metrics = session.evaluate("""(()=>({
                                innerWidth, dpr:devicePixelRatio,
                                clientWidth:document.documentElement.clientWidth,
                                scrollWidth:document.documentElement.scrollWidth,
                                count:document.getElementById('visibleCount').textContent,
                                tail:document.getElementById('tasks').textContent.includes('C-SYNTH-0299'),
                                overflowing:[...document.querySelectorAll('body *')].filter(e=>{
                                    const r=e.getBoundingClientRect();
                                    return r.width>0 && (r.right>document.documentElement.clientWidth+.1 || r.left<-.1);
                                }).slice(0,8).map(e=>({tag:e.tagName,css:e.className,
                                    scroll:e.scrollWidth,client:e.clientWidth}))
                            }))()""")
                            if metrics and metrics['count'] == '300 / 300 項' and metrics['tail']:
                                break
                            time.sleep(.05)
                        self.assertIsNotNone(metrics)
                        self.assertEqual(metrics['count'], '300 / 300 項', metrics)
                        self.assertTrue(metrics['tail'], metrics)
                        self.assertEqual(metrics['innerWidth'], width, metrics)
                        self.assertEqual(metrics['dpr'], 1, metrics)
                        self.assertLessEqual(metrics['scrollWidth'], metrics['clientWidth'], metrics)
                        # Real click and expanded detail, not a static initial-layout check.
                        detail = session.evaluate("""(()=>{
                            const rows=document.querySelectorAll('#tasks button.task');
                            rows[rows.length-1].click();
                            return {visible:rows.length,
                                text:document.getElementById('detail').textContent,
                                scroll:document.documentElement.scrollWidth,
                                client:document.documentElement.clientWidth};
                        })()""")
                        self.assertEqual(detail['visible'], 300)
                        self.assertIn('C-SYNTH-0299', detail['text'])
                        self.assertLessEqual(detail['scroll'], detail['client'], detail)
            finally:
                if session is not None:
                    session.close()
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
