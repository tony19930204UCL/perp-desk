/* Static read-only dashboard. Source values are never parsed as markup. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const node = (tag, value, parent) => {
    const el = document.createElement(tag);
    if (value !== undefined) el.textContent = value;
    if (parent) parent.appendChild(el);
    return el;
  };
  const clear = id => { const el = $(id); el.replaceChildren(); return el; };
  const text = value => typeof value === 'string' && value.length ? value : '未知';
  const finiteDay = value => {
    if (typeof value === 'number') return Number.isFinite(value) && value >= 0 ? value : null;
    if (typeof value !== 'string' || !/^(?:\d+)(?:\.\d+)?$/.test(value)) return null;
    const num = Number(value);
    return Number.isFinite(num) && num >= 0 ? num : null;
  };
  const day = value => {
    const n = finiteDay(value);
    if (n === null) return { display:'未知', exact:null };
    const compact = n.toFixed(2).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1');
    return { display:compact, exact:String(value) };
  };
  const count = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : '未知';
  const money = value => value && typeof value === 'object' &&
    typeof value.exact === 'string' && typeof value.display === 'string'
    ? {display:value.display, exact:value.exact} : {display:'未知', exact:null};
  const field = (parent, label, value) => {
    const wrapper = node('div', undefined, parent);
    node('dt', label, wrapper);
    const val = node('dd', value.display, wrapper);
    if (value.exact !== null) val.title = value.exact;
  };
  const plain = value => ({display:value, exact:null});
  const enums = {
    OPEN:'未結', IN_PROGRESS:'進行中', PENDING:'待處理', BLOCKED:'受阻',
    VERIFIED:'已驗證', DROPPED:'已撤銷', PASS:'通過', FAIL:'未通過',
    CONFIRMED:'已確認', REJECTED:'已拒絕', OBSERVATION:'觀察',
    EXPERIMENT:'實驗', DECISION:'決策', PREDICTION:'預測',
    known:'新鮮', unknown:'未知'
  };
  const enumText = value => typeof value === 'string' && Object.hasOwn(enums,value) ? enums[value] : '未知';
  const valid = m => m && typeof m === 'object' && !Array.isArray(m) &&
    ['known','unknown'].includes(m.scorecard_state) &&
    (m.scorecard_at === null || typeof m.scorecard_at === 'string') &&
    Array.isArray(m.reasons) && m.reasons.every(v => typeof v === 'string') &&
    Array.isArray(m.arms) && Array.isArray(m.journal) && Array.isArray(m.commitments) &&
    m.health && typeof m.health === 'object' && !Array.isArray(m.health) &&
    ['liveness','clock','scheduler'].every(k => m.health[k] === 'unknown') &&
    Array.isArray(m.health.reasons) && m.health.reasons.every(v => typeof v === 'string') &&
    (m.scorecard_state !== 'unknown' || m.arms.length === 0) &&
    m.arms.every(a => a && typeof a === 'object' && !Array.isArray(a)) &&
    m.journal.every(j => j && typeof j === 'object' && !Array.isArray(j)) &&
    m.commitments.every(c => c && typeof c === 'object' && !Array.isArray(c));
  let requestId = 0;
  function unknown(reason) {
    $('overall').textContent = '未知 · 無法確認目前資料';
    $('freshness').textContent = '未知';
    const reasons = clear('reasons');
    node('li',reason,reasons);
    for (const id of ['arms','journal','commitments']) node('p','未知',clear(id));
    const health = clear('health');
    [['服務存活','缺少獨立服務存活證據'],['時鐘','缺少獨立時鐘驗證'],
     ['排程','缺少獨立排程證據']].forEach(([name,why]) => {
      const div = node('div',undefined,health);
      node('strong',name+'：未知',div);
      node('small',why,div);
    });
    return false;
  }
  function renderDashboard(model) {
    if (!valid(model)) return unknown('資料格式無效');
    $('overall').textContent = '服務、時鐘與排程狀態：未知（無獨立證據）';
    $('freshness').textContent = '記分卡：'+enumText(model.scorecard_state)+
      ' · 時間：'+text(model.scorecard_at);
    const reasons = clear('reasons');
    if (model.reasons.length === 0) node('li','未回報資料異常（不代表系統健康）',reasons);
    else model.reasons.forEach(s => node('li',s,reasons));
    const health = clear('health');
    [['liveness','服務存活'],['clock','時鐘'],['scheduler','排程']].forEach(([key,label],i) => {
      const box = node('div',undefined,health);
      node('strong',label+'：'+enumText(model.health[key]),box);
      node('small',text(model.health.reasons[i]),box);
    });
    const arms = clear('arms');
    if (!model.arms.length) node('p',model.scorecard_state === 'unknown' ? '未知' : '沒有臂資料',arms);
    const fields = [['days_in','已經天數','day'],['days_left','剩餘天數','day'],
      ['signals','訊號','count'],['blocked','阻擋','count'],['fills','成交','count'],
      ['round_trips','完整來回','count'],['pnl','淨損益','money'],
      ['fees','手續費','money'],['max_dd','最大回撤','money'],
      ['open_positions','未平倉','count'],['feed_errors','行情來源錯誤','count']];
    model.arms.forEach(a => {
      const card = node('article',undefined,arms);
      card.className = 'panel';
      node('h3',text(a.arm),card);
      const dl = node('dl',undefined,card);
      fields.forEach(([key,label,type]) => field(dl,label,type === 'day' ? day(a[key]) :
        type === 'money' ? money(a[key]) : plain(count(a[key]))));
    });
    const journal = clear('journal');
    if (!model.journal.length) node('p','沒有日誌資料',journal);
    model.journal.slice(0,5).forEach(j => {
      const item = node('article',undefined,journal);
      item.className = 'entry';
      node('h3',text(j.title),item);
      node('p',enumText(j.kind)+' · 判定：'+enumText(j.verdict)+' · '+text(j.at),item);
      const details = node('details',undefined,item);
      node('summary','查看全文 · '+text(j.id),details);
      node('p',text(j.body),details);
    });
    const commitments = clear('commitments');
    if (!model.commitments.length) node('p','沒有未結承諾',commitments);
    model.commitments.forEach(c => {
      const item = node('article',undefined,commitments);
      item.className = 'entry';
      node('h3',text(c.id)+' · '+enumText(c.state),item);
      node('p','到期：'+text(c.due),item);
      node('p',text(c.text),item);
    });
    return true;
  }
  async function refreshDashboard() {
    const id = ++requestId;
    try {
      const response = await fetch('/api/dashboard',{cache:'no-store'});
      if (!response.ok) throw new Error('status');
      const result = await response.json();
      if (id === requestId) return renderDashboard(result);
    } catch (_) {
      if (id === requestId) return unknown('讀取失敗或回應無效');
    }
    return false;
  }
  window.renderDashboard = model => { ++requestId; return renderDashboard(model); };
  window.refreshDashboard = refreshDashboard;
  document.addEventListener('DOMContentLoaded', () => {
    unknown('尚未讀取');
    $('refresh').addEventListener('click', refreshDashboard);
    refreshDashboard();
  });
})();
