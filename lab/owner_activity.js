/* Safe, read-only owner activity UI. All external strings use textContent. */
(() => {
'use strict';
const $ = id => document.getElementById(id);
const put=(tag,text,parent,cls)=>{const el=document.createElement(tag);if(text!==undefined)el.textContent=text;if(cls)el.className=cls;if(parent)parent.appendChild(el);return el};
const wipe=id=>{const el=$(id);el.replaceChildren();return el};
const str=v=>typeof v==='string'&&v.length?v:'未確認';
const enumName={running_verified:'可驗證執行',activity_unconfirmed:'活動未確認',blocked:'受阻',scheduled_idle:'排程閒置',missed:'預期執行逾期',failed:'失敗',success_without_output:'成功但缺成果',result_proven:'有成果證據',engineering_accepted:'工程已驗收',completed_unaccepted:'執行結束·未驗收',dropped:'取消',planned:'計畫中',disabled:'已停用',unknown:'未知'};
let model=null,selected=null,seq=0;
const status=v=>enumName[v]||str(v);
function detail(task){
 selected=task.id;const target=wipe('detail');
 put('h2',str(task.id)+' · '+str(task.title),target);
 put('p',status(task.state),target,'status');
 const rows=[
 ['原承諾',task.commitment],['目的',task.why],['驗收條件',task.acceptance],
 ['目前步驟',task.step],['下一步',task.next],['活動核驗',status(task.activity)],
 ['執行持有人',task.owner],['開始時間',task.started_at],['最後進度',task.progress_at],
 ['來源更新',task.updated_at],['狀態來源',task.source_keys?.join(', ')],
 ['階段',task.stage],['階段狀態',task.stage_status],['依賴',task.dependencies?.join(', ')],
 ['未解除阻擋',task.blockers?.join(', ')],['阻擋分類',task.blocker_details?.map(b=>b.label+' ('+b.resolution+')').join(' · ')],['延後原因',task.defer_reason],['延後狀態',task.deferral_state],
 ['重新檢查',task.recheck_at],['期限',task.due],
 ['工程驗收',task.engineering_accepted?'已驗收':'未獲驗收證據'],
 ['已部署',str(task.deployed)],['無人值守閉環',str(task.unattended)],
 ['可讀證據',task.evidence?.join(' · ')],['警示',task.warnings?.join(' · ')]
 ];
 if(task.schedule){const s=task.schedule;rows.push(
 ['排程',s.cadence],['執行結果',status(s.run_state)],
 ['最近預期派送',s.expected_at],['最近實際執行',s.last_run_at],
 ['下一次預定',s.next_run_at],['最近回報',s.last_status],
 ['成果引用',s.result_ref],['成果時間',s.output_at]);}
 const dl=put('dl',undefined,target);
 rows.forEach(([name,value])=>{const cell=put('div',undefined,dl);put('dt',name,cell);put('dd',str(value),cell)});
 if(task.history?.length){put('h3','更新紀錄',target);task.history.forEach(e=>put('p',[e.at,e.state,e.note].filter(Boolean).join(' · '),target,'meta'))}
}
function render(){
 if(!model||!Array.isArray(model.tasks))return;
 const tasks=model.tasks,query=$('search').value.trim().toLowerCase(),filter=$('stateFilter').value,stage=$('stageFilter').value;
 const shown=tasks.filter(t=>(!filter||t.state===filter)&&(!stage||t.stage===stage)&&
 (!query||String(t.id||'').toLowerCase().includes(query)||String(t.title||'').toLowerCase().includes(query)));
 $('visibleCount').textContent=shown.length+' / '+tasks.length+' 項';
 const list=wipe('tasks');
 shown.forEach(t=>{const b=put('button',undefined,list,'task');b.type='button';b.dataset.selected=String(t.id===selected);
 put('strong',str(t.id)+' · '+str(t.title),b);put('span',status(t.state),b,'pill'+(t.warnings?.length?' warning':t.state==='running_verified'?' ok':''));
 put('span',[str(t.stage),t.next||t.step||'下一步未確認'].join(' · '),b,'meta');
 b.addEventListener('click',()=>{detail(t);render()});});
 if(!shown.length)put('p','沒有符合篩選的工作；這不代表來源為空或健康。',list,'subtle');
 if(selected){const t=tasks.find(t=>t.id===selected);if(t)detail(t);else {selected=null;const box=wipe('detail');put('h2','任務詳情',box);put('p','先前選取的任務已不存在於最新來源；舊內容已清除。',box,'subtle');}}
}
function show(data){
 if(!data||!Array.isArray(data.tasks)||!data.sources||!data.totals)throw Error('invalid model');
 model=data;
 const abnormal=Object.entries(data.sources).filter(([,v])=>v.state!=='available');
 $('status').textContent=(abnormal.length?'來源狀態不完整：'+abnormal.map(([k,v])=>k+' '+v.state).join('，'):'已載入唯讀資料，未證實任何服務或執行器持續運作')+' · 更新：'+str(data.generated_at);
 const m=wipe('metrics');
 const tiles=[['承諾來源',data.totals.ledger],['工作總數',data.totals.union],
 ['核實中的 worker',data.tasks.filter(t=>t.state==='running_verified').length],
 ['警示工作',data.tasks.filter(t=>t.warnings?.length).length]];
 tiles.forEach(([label,n])=>{const card=put('div',undefined,m,'metric');put('strong',Number.isInteger(n)?String(n):'未知',card);put('span',label,card)});
 const source=wipe('sources');
 Object.entries(data.sources).forEach(([k,v])=>{const box=put('div',undefined,source,'source-row');put('strong',k+' · '+str(v.state),box);put('p','更新：'+str(v.updated_at),box,'meta')});
 const stages=wipe('stages');
 (data.stages||[]).forEach(s=>{const box=put('div',undefined,stages,'stage');put('h3',str(s.title)+' / '+str(s.id),box);
 put('p','退出門檻：'+str(s.exit_gate)+' · 目標日期：'+str(s.target_date),box,'meta')});
 if(!(data.stages||[]).length)put('p','階段資料未提供',stages,'subtle');
 const events=wipe('events');
 (data.events||[]).slice(-8).reverse().forEach(e=>{const box=put('div',undefined,events,'event');put('p',e.summary,box);put('p',[e.at,e.kind,e.verdict,e.proof_ref].filter(Boolean).join(' · '),box,'meta')});
 if(!(data.events||[]).length)put('p','未提供可安全公開的產出摘要',events,'subtle');
 const selector=$('stageFilter'),prior=selector.value;
 while(selector.options.length>1)selector.remove(1);
 [...new Set(data.tasks.map(t=>t.stage))].sort().forEach(x=>{const o=document.createElement('option');o.value=x;o.textContent=x;selector.appendChild(o)});
 selector.value=[...selector.options].some(o=>o.value===prior)?prior:'';
 render();
}
function unavailable(){model=null;selected=null;$('status').textContent='資料無法確認：API 讀取失敗或格式錯誤';
 for(const key of ['tasks','metrics','stages','sources','events','detail']){const box=wipe(key);put('p','未知／未能取得資料',box,'subtle')}}
async function refresh(){
 const current=++seq;
 try{const res=await fetch('/api/owner-activity',{cache:'no-store'});if(!res.ok)throw Error('HTTP');const data=await res.json();if(current===seq)show(data)}
 catch(_){if(current===seq)unavailable()}
}
window.renderOwnerActivity=data=>{++seq;show(data)};
window.refreshOwnerActivity=refresh;
document.addEventListener('DOMContentLoaded',()=>{
 $('refresh').addEventListener('click',refresh);
 ['search','stateFilter','stageFilter'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',render));
 refresh();setInterval(refresh,30000);
});
})();
/* Separate 3-second Metro poll: nonoverlapping, monotonic, last-known on error. */
(() => {
'use strict';
const get = id => document.getElementById(id);
const add = (parent,tag,value,cls) => {
  const el=document.createElement(tag);
  if(value!==undefined&&value!==null)el.textContent=String(value);
  if(cls)el.className=cls;
  parent.appendChild(el);return el;
};
const clear=id=>{const el=get(id);el.replaceChildren();return el};
let snapshot=null, pending=false, cycle=0, lastOk=0;
function render(data) {
  if(!data||!Array.isArray(data.routes)||!data.kpis||!data.sources)throw Error('invalid metro');
  snapshot=data;lastOk=Date.now();
  const wasOpen=!!document.querySelector('#metroKpis details')?.open;
  const k=clear('metroKpis');
  [['核驗中程序 / PIDs',data.kpis.processes],['Maker baseline',String(data.kpis.maker_cells??'NA')+'/48'],
   ['清算事件',data.kpis.liquidations],['待人工決策',data.kpis.pending_decisions]].forEach(([label,v])=>{
    const box=add(k,'div',undefined,'metric');
    add(box,'strong',v===null||v===undefined?'NA':v);
    add(box,'span',label);
  });
  // Accessible read-only disclosure: no action or approval endpoint.
  const tiles=k.children;
  if(tiles.length===4){
    const decision=tiles[3];
    const label=decision.textContent;
    decision.replaceChildren();
    const details=add(decision,'details');
    details.open=wasOpen;
    const summary=add(details,'summary',label+' · 展開唯讀清單');
    summary.setAttribute('role','button');
    const entries=data.kpis.pending_list;
    if(Array.isArray(entries))add(summary,'span',' · 清單 '+entries.length+' 項');
    const list=add(details,'div');
    const taipei=sec=>{
      if(typeof sec!=='number'||!Number.isFinite(sec))return 'NA';
      return new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',
        dateStyle:'medium',timeStyle:'short'}).format(new Date(sec*1000));
    };
    if(!Array.isArray(entries))add(list,'p','資料無法確認（NA）');
    else if(!entries.length)add(list,'p','目前沒有需要你決定的事');
    else entries.forEach(item=>{
      const line=add(list,'p');
      add(line,'strong',item.id+' · '+item.question+' · ');
      add(line,'span',item.reason+' · ');
      const stamp=add(line,'time',taipei(item.created));
      if(typeof item.created==='number')
        stamp.title=new Date(item.created*1000).toISOString();
    });
  }
  const recent=clear('metroRecent');
  const fmt=ms=>new Intl.DateTimeFormat('zh-TW',{timeZone:'Asia/Taipei',
    dateStyle:'medium',timeStyle:'short'}).format(new Date(ms));
  (data.recent||[]).forEach(e=>add(recent,'p',fmt(e.at)+' · '+e.kind+
    ' · '+e.id+' · '+e.text+(e.alive?' · '+e.alive:''),'meta'));
  if(!(data.recent||[]).length)add(recent,'p','目前沒有近況','meta');
  const routes=clear('metroRoutes');
  data.routes.forEach(route=>{
    const card=add(routes,'section',undefined,'metro-route '+route.color);
    add(card,'h3',route.title);
    const line=add(card,'div',undefined,'metro-line');
    (route.steps||[]).forEach(step=>{
      const stop=add(line,'div',undefined,'metro-stop '+step.state);
      add(stop,'span',step.state==='done'?'✓':'', 'dot');
      add(stop,'span',step.title+' · '+({done:'已核驗',active:'運作中',waiting:'等待中',next:'下一站',retest:'失敗重測中',user:'待使用者核准',failed:'已失敗'}[step.state]||'未知'));
    });
    (route.steps||[]).filter(x=>x.note||x.item).forEach(x=>
      add(card,'p',[x.item,x.note].filter(Boolean).join(' · '),'route-note'));
    add(card,'p','現在：'+(route.current||'未確認')+' · 下一步：'+(route.next||'未確認'),'route-note');
    add(card,'p','最後核驗：'+(route.verified_at||'未知'),'route-note');
    (route.steps||[]).filter(x=>x.evidence).forEach(x=>
      add(card,'p',x.title+' · '+x.evidence,'route-note'));
  });
  const spot=clear('metroSpot'),maker=data.maker||{},results=maker.results||{};
  add(spot,'p','階段：'+(maker.phase||'未確認'));
  add(spot,'p','產品程序：'+(data.processes||[]).filter(p=>p.product==='maker'&&p.state==='verified')
    .map(p=>p.kind+(p.identity==='managed'?' · cron 管理':'')+' PID '+p.pid+' · '+(p.elapsed_s??'NA')+' s · RSS '+(p.rss_bytes??'NA')+' bytes').join('；') || '沒有核實的程序');
  add(spot,'p','清算 heartbeat：'+(data.heartbeat?.at||'NA')+
    ' · age '+(data.heartbeat?.age_ms??'NA')+' ms');
  add(spot,'p',results.count>0?'初步結果 '+results.count+'/48，尚未全局驗收':'尚無有效 baseline cell');
  add(spot,'p','租約：'+(maker.lease||'未知')+' · 輸出：'+(maker.output||'未知'));
  add(spot,'p','研究結果：'+(results.state||'未知')+'；未推論策略 PASS');
  const rows=Array.isArray(results.metrics)?results.metrics:[];
  add(spot,'p','X1 / X2 / X3 有效格數：'+['X1','X2','X3'].map(
    x=>x+' '+(results.by_exit?.[x]??'NA')).join(' · '));
  rows.slice(0,12).forEach(m=>{
    add(spot,'p',m.cell+' · Gross '+(m.gross_bp??'NA')+' bp · Net '+(m.net_bp??'NA')+
      ' bp · PF '+(m.profit_factor??'NA')+' · TP/(TP+SL+SL_GAP) '+
      (m.conditional_win_rate??'NA')+' (n='+m.risk_denominator+') · TIME '+(m.actual_time_share??'NA'),'meta');
  });
  const health=clear('metroHealth');
  Object.entries(data.sources).forEach(([name,v])=>{
    const age=v.age_ms==null?'':(' · '+Math.floor(v.age_ms/60000)+' 分鐘');
    const label=v.basis==='ledger_match'?'與帳本一致':
      v.state==='stale'?'稍早資料':(v.state||'未知');
    add(health,'p',name+'：'+label+age,'meta');
  });
  if((data.warnings||[]).includes('directive_mismatch'))
    add(health,'p','directive_mismatch · 指令與帳本未能核對','meta');
  get('metroHistoryCount').textContent=String(data.history_count??'未知');
  const history=clear('metroHistoryRows');
  (data.history||[]).forEach(t=>add(history,'p',(t.id||'?')+' · '+(t.title||'未確認')+' · '+(t.state||'未知'),'meta'));
}
async function poll() {
  if(pending)return;
  pending=true;const current=++cycle;
  try {
    const response=await fetch('/api/owner-metro',{cache:'no-store'});
    if(!response.ok)throw Error('HTTP');
    const model=await response.json();
    if(current===cycle)render(model);
  } catch (_) {
    const old=lastOk?Math.max(0,Math.floor((Date.now()-lastOk)/1000))+' 秒前':'無';
    get('metroHealth').textContent='本輪讀取失敗；保留最後已知資料，成功讀取：'+old;
  } finally {pending=false;}
}
window.refreshOwnerMetro=poll;
// Preserve the existing observer's DOMContentLoaded harness/handler.
if(typeof window.addEventListener==='function') {
  window.addEventListener('load',()=>{poll();setInterval(poll,3000);});
}
})();
