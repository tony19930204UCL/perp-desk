"""Deterministic tests exercising the actual browser JS with a minimal DOM fake."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / 'dashboard_v2.html'
JS = ROOT / 'dashboard_v2_ui.js'
HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
class Element {
  constructor(tag) {
    this.tagName=tag; this.children=[]; this._text=''; this.title='';
    this.className=''; this.handlers={};
  }
  set textContent(s) { this._text=String(s); this.children=[]; }
  get textContent() { return this._text+this.children.map(c=>c.textContent).join(''); }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...children) { this._text=''; this.children=[...children]; }
  addEventListener(kind,callback) { this.handlers[kind]=callback; }
}
const ids={};
for (const k of ['overall','freshness','reasons','health','arms','journal','commitments','refresh'])
  ids[k]=new Element(k);
const document = {
  getElementById: k => ids[k],
  createElement: k => new Element(k),
  addEventListener: (k,f) => { if(k==='DOMContentLoaded') document.ready=f; }
};
const pending=[];
const fetch = (url,options) => new Promise((resolve,reject)=>pending.push({url,options,resolve,reject}));
const window = {};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),
  {document,window,fetch,Number,Object,String,Array,RegExp,console});
const arm = {arm:'A',days_in:1.07,days_left:'19.930',signals:3,blocked:0,
  fills:2,round_trips:1,pnl:{exact:'123.123456789',display:'123.12'},
  fees:{exact:'0.00001',display:'0.00'},max_dd:{exact:'-1.005',display:'-1.01'},
  open_positions:0,feed_errors:5};
const model = {scorecard_state:'known',scorecard_at:'2026-10-12T11:33:20+00:00',
  reasons:[],arms:[arm],journal:[],commitments:[],
  health:{liveness:'unknown',clock:'unknown',scheduler:'unknown',
    reasons:['沒有服務證據','沒有時鐘證據','沒有排程證據']}};
const text = id => ids[id].textContent;
const descendants = (node,tag) => {
  let found = [];
  for(const c of node.children) {
    if(c.tagName===tag) found.push(c);
    found=found.concat(descendants(c,tag));
  }
  return found;
};
(async () => {
  const test = process.argv[2];
  if(test==='render') {
    assert.equal(window.renderDashboard(model),true);
    for(const s of ['A','1.07','19.93','123.12','0.00','-1.01','3','5'])
      assert(text('arms').includes(s),s);
  } else if(test==='money') {
    window.renderDashboard(model);
    const cells=descendants(ids.arms,'dd');
    assert(cells.some(c=>c.title==='123.123456789' && c.textContent==='123.12'));
    assert(cells.some(c=>c.title==='0.00001' && c.textContent==='0.00'));
  } else if(test==='days') {
    window.renderDashboard(model);
    const cells=descendants(ids.arms,'dd');
    assert(cells.some(c=>c.title==='19.930' && c.textContent==='19.93'));
    assert(cells.some(c=>c.title==='1.07'));
  } else if(test==='null') {
    model.arms[0].pnl={exact:null,display:'未知'};
    model.arms[0].fills=null;
    model.arms[0].blocked=true;
    model.arms[0].days_in=null;
    window.renderDashboard(model);
    const cells=descendants(ids.arms,'dd');
    assert(cells.filter(c=>c.textContent==='未知').length>=4);
    assert(!cells.some(c=>c.title==='null'));
  } else if(test==='health') {
    window.renderDashboard(model);
    assert(text('health').includes('服務存活：未知'));
    assert(text('health').includes('時鐘：未知'));
    assert(text('health').includes('排程：未知'));
    assert(!text('health').includes('正常'));
  } else if(test==='hostile') {
    const bad='</script><img onerror=evil()>';
    model.arms[0].arm=bad;
    model.journal=[{id:bad,title:bad,kind:bad,body:bad,verdict:bad,at:bad}];
    model.commitments=[{id:bad,state:bad,due:bad,text:bad}];
    window.renderDashboard(model);
    assert(text('arms').includes(bad) && text('journal').includes(bad));
    assert(text('commitments').includes(bad));
    assert.equal(descendants(ids.journal,'img').length,0);
    assert.equal(descendants(ids.arms,'script').length,0);
  } else if(test==='journal') {
    model.journal=Array.from({length:7},(_,i)=>({id:'J'+i,kind:'PREDICTION',
      title:'標題'+i,verdict:'PASS',body:'正文'+i,at:'date'}));
    window.renderDashboard(model);
    assert.equal(descendants(ids.journal,'details').length,5);
    assert(text('journal').includes('正文4'));
    assert(!text('journal').includes('正文5'));
    assert(text('journal').includes('預測'));
  } else if(test==='commitments') {
    model.commitments=[{id:'C-1',state:'OPEN',due:'tomorrow',text:'待處理文字'}];
    window.renderDashboard(model);
    assert(text('commitments').includes('C-1'));
    assert(text('commitments').includes('未結'));
    assert(text('commitments').includes('tomorrow'));
  } else if(test==='invalid') {
    window.renderDashboard(model);
    assert.equal(window.renderDashboard({arms:[]}),false);
    assert.equal(text('arms'),'未知');
    assert(text('reasons').includes('資料格式無效'));
  } else if(test==='fail') {
    window.renderDashboard(model);
    const promise=window.refreshDashboard();
    assert.equal(pending[0].url,'/api/dashboard');
    assert.equal(pending[0].options.cache,'no-store');
    pending[0].resolve({ok:false});
    await promise;
    assert.equal(text('arms'),'未知');
    assert(text('reasons').includes('讀取失敗'));
  } else if(test==='jsonerror') {
    window.renderDashboard(model);
    const promise=window.refreshDashboard();
    pending[0].resolve({ok:true,json:()=>Promise.reject(Error('bad json'))});
    await promise;
    assert.equal(text('arms'),'未知');
  } else if(test==='race') {
    const older=window.refreshDashboard();
    const newer=window.refreshDashboard();
    const recent={...model,arms:[{...arm,arm:'NEW'}]};
    pending[1].resolve({ok:true,json:async()=>recent});
    await newer;
    pending[0].resolve({ok:true,json:async()=>({...model,arms:[{...arm,arm:'OLD'}]})});
    await older;
    assert(text('arms').includes('NEW') && !text('arms').includes('OLD'));
  } else if(test==='ready') {
    document.ready();
    assert.equal(pending.length,1);
    assert.equal(typeof ids.refresh.handlers.click,'function');
  } else if(test==='unknown') {
    model.scorecard_state='unknown'; model.arms=[];
    model.reasons=['記分卡已過期'];
    window.renderDashboard(model);
    assert.equal(text('arms'),'未知');
    assert(text('reasons').includes('記分卡已過期'));
  } else throw Error(test);
})().catch(e=>{console.error(e);process.exitCode=1});
"""


class DashboardV2UITests(unittest.TestCase):
    def run_case(self, case):
        completed = subprocess.run(
            ['node', '-e', HARNESS, str(JS), case],
            capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(completed.returncode, 0,
                         f'{case}: {completed.stderr}\n{completed.stdout}')

    def test_render_values(self): self.run_case('render')
    def test_exact_money_tooltip(self): self.run_case('money')
    def test_fractional_days_tooltip(self): self.run_case('days')
    def test_null_never_zero(self): self.run_case('null')
    def test_fresh_not_health(self): self.run_case('health')
    def test_hostile_text_literal(self): self.run_case('hostile')
    def test_latest_five_details(self): self.run_case('journal')
    def test_open_commitments(self): self.run_case('commitments')
    def test_invalid_model_clears(self): self.run_case('invalid')
    def test_failed_status_clears(self): self.run_case('fail')
    def test_failed_json_clears(self): self.run_case('jsonerror')
    def test_older_reply_ignored(self): self.run_case('race')
    def test_dom_ready_load_once(self): self.run_case('ready')
    def test_expired_scorecard_unknown(self): self.run_case('unknown')

    def test_local_only_html(self):
        html = HTML.read_text(encoding='utf-8')
        self.assertIn('src="dashboard_v2_ui.js"', html)
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)
        self.assertNotIn('<iframe', html)
        self.assertIn('overflow-wrap:anywhere', html)
        self.assertIn('width:min(100%, 1280px)', html)
        self.assertIn('未提供來源，不是 0', html)
        js = JS.read_text(encoding='utf-8')
        for forbidden in ('innerHTML', 'outerHTML', 'insertAdjacentHTML',
                          'document.write', 'localStorage', 'sessionStorage'):
            self.assertNotIn(forbidden, js)


if __name__ == '__main__':
    unittest.main()
