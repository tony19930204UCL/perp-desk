"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const txt = (id, value) => { $(id).textContent = value; };
  const valid = v => typeof v === "number" && Number.isFinite(v);
  const money = v => valid(v) ? v.toFixed(2) + " U" : "尚無資料";
  const stamp = v => valid(v) && v > 0
    ? new Date(v).toLocaleString("zh-TW", {timeZone:"UTC",hour12:false}) + " UTC"
    : "來源時間未知";
  const val = v => v === null || v === undefined || v === "" ? "未知" : String(v);
  const list = (id, values, describe, empty) => {
    const node = $(id); node.replaceChildren();
    if (!Array.isArray(values)) {
      const li = document.createElement("li"); li.textContent = "來源尚未取得"; node.append(li); return;
    }
    if (!values.length) {
      const li = document.createElement("li"); li.textContent = empty; node.append(li); return;
    }
    values.forEach(v => {
      const li = document.createElement("li");
      li.textContent = describe(v || {}); node.append(li);
    });
  };
  const bookLine = (parent, label, value) => {
    const p = document.createElement("p");
    const name = document.createElement("span");
    const result = document.createElement("b");
    name.textContent = label + "："; result.textContent = value;
    p.append(name, result); parent.append(p);
  };
  const draw = d => {
    if (!d || d.schema !== "boss-dashboard.v1") throw Error("SCHEMA");
    const t = d.tactical || {}, r = d.radar || {}, p = d.prediction || {};
    const w = d.weekly || {};
    txt("site-status", "唯讀快照；頁面取得時間不代表資料新鮮。來源狀態請見各戰場原始時間。");
    txt("tactical-time", "戰術來源：" + stamp(t.observed_ms));
    txt("tactical-state", "狀態：" + val(t.state) + "｜估值：" + val(t.valuation_state));
    ["equity","available","realized","cash","fees","funding"].forEach(k =>
      txt(k, money(t[({available:"available_margin"})[k] || k])));
    txt("unrealized", t.flat_confirmed === true && t.unrealized === 0
      ? "0.00 U（確認空倉）" : valid(t.unrealized) ? money(t.unrealized) : "尚無估值");
    const pos = t.position;
    txt("position", t.position_known !== true ? "持倉未知" : !pos ? "確認空倉"
      : [val(pos.symbol),val(pos.side),"數量 " + val(pos.qty)].join(" / "));
    txt("tactical-note", val(t.available_basis) + "。"
      + val(t.risk_note));
    list("orders", t.orders, x => [val(x.symbol),val(x.side),
      "入場 " + money(x.entry),"停損 " + money(x.stop),
      "TP1 " + money(x.tp1),"TP2 " + money(x.tp2),
      "有效至 " + stamp(x.expires_ms)].join(" · "), "目前沒有有效指令（已核對）");
    txt("radar-time", "雷達來源：" + stamp(r.observed_ms)
      + (r.waiting ? "（等待新資料）" : ""));
    list("top3", Array.isArray(r.top3) ? r.top3.slice(0,3) : null,
      x => [val(x.symbol),val(x.absorption || x.branch),
        "OI4h " + (valid(x.oi4h) ? x.oi4h : "未知"),
        "funding " + (valid(x.funding) ? x.funding : "未知"),
        stamp(x.observed_ms)].join(" · "), "目前無雷達候選（來源有效）");
    list("trades", t.trades, x => [val(x.kind),val(x.symbol),
      "數量 " + val(x.qty),"價格 " + money(x.price),
      "已計淨額 " + money(x.pnl_after_exit_fee),
      "事件時間 " + stamp(x.t)].join(" · "), "帳本尚無事件（已核對）");
    txt("prediction-time", "產品來源：" + stamp(p.observed_ms));
    txt("prediction-state", val(p.state)
      + "；官方市場、strike、真實帳戶與結算規則仍未驗證。");
    txt("prediction-stake", valid(p.bankroll) && valid(p.stake)
      ? money(p.bankroll) + " / " + money(p.stake) : "尚無資料");
    txt("prediction-pnl", money(p.net_pnl));
    txt("prediction-win", valid(p.official_win_rate)
      ? (p.official_win_rate * 100).toFixed(1) + "%" : "尚無資料");
    txt("weekly-time", "週期來源：" + stamp(w.observed_ms)
      + (w.waiting ? "（待更新）" : ""));
    const books = Array.isArray(w.books) ? w.books : [];
    for (let i=0;i<4;i++) {
      const node = $("book-" + i).querySelector(".book-data");
      node.replaceChildren();
      const b = books[i];
      if (!b) {bookLine(node,"資料","尚未建立／來源未知"); continue;}
      bookLine(node,"策略",val(b.name));
      bookLine(node,"批次進場日",stamp(b.entry_day));
      bookLine(node,"實際觀測起點",stamp(b.observed_start_ms));
      bookLine(node,"實際觀測終點",stamp(b.observed_end_ms));
      bookLine(node,"預定退出日",stamp(b.exit_day));
      bookLine(node,"多方 PnL",money(b.long_pnl));
      bookLine(node,"空方 PnL",money(b.short_pnl));
      bookLine(node,"資金費",money(b.funding));
      bookLine(node,"費用假設",money(b.fee_assumption));
      bookLine(node,"淨值變動（非權益）",money(b.net));
    }
    txt("weekly-settlements", w.settlements === null || w.settlements === undefined
      ? "歷史結算檔未確認／尚未建立，不能推定零結算。"
      : Array.isArray(w.settlements) && w.settlements.length === 0
        ? "結算檔已讀取，目前無歷史列；不推定真實零盈虧。"
        : "歷史結算來源已有 " + (Array.isArray(w.settlements) ? w.settlements.length : "未知") + " 筆可檢視列。");
    txt("engineering-data", JSON.stringify(d.engineering || {},null,2));
  };
  async function refresh() {
    try {
      const res = await fetch("/api/boss-dashboard", {cache:"no-store"});
      if (!res.ok) throw Error("HTTP_" + res.status);
      draw(await res.json());
    } catch (e) {
      txt("site-status","唯讀資料無法讀取（" + String(e.message).slice(0,80) + "）；舊值不可信，請重新整理。");
      // Never carry a previously successful monetary value across a failed read.
      ["equity","available","realized","cash","fees","funding",
        "prediction-stake","prediction-pnl","prediction-win"].forEach(k => txt(k,"尚無資料"));
      txt("unrealized","尚無估值");
    }
  }
  refresh();
  document.addEventListener("visibilitychange", () => {if (!document.hidden) refresh();});
})();
