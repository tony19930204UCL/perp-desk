"""Node DOM interaction regression for actual Monitor JS; real screenshots not claimed."""
import shutil
import subprocess
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
 const old=window.refreshOwnerActivity(),newer=window.refreshOwnerActivity();
 pending[2].resolve({ok:true,json:async()=>({...model,tasks:[task('NEW','running_verified')]})});await newer;
 pending[1].resolve({ok:true,json:async()=>({...model,tasks:[task('OLD','blocked')]})});await old;
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


if __name__ == '__main__':
    unittest.main()
