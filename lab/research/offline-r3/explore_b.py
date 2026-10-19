#!/usr/bin/env python3
"""RESEARCH-R3 B 組【探索、未登記、不計格、不能用於通過判定】: 事件門檻更嚴時毛優勢是否隨門檻上升 (描述 gross 與成本的比值)。"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r3b import *
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
funds = {s: r1.Fund(s) for s in COINS}
data = {s: load_1m(s) for s in COINS}
out = {"note": "探索、未登記、不計格；只用來描述毛優勢對門檻的敏感度，不能據以挑格或宣稱通過"}
for ks in (4, 6, 8, 10, 12):
    ev = {s: detect_events(*data[s], k_sigma=ks) for s in COINS}
    row = {"n_events": sum(len(v) for v in ev.values())}
    for hm in HORIZONS:
        trs = []
        for s in COINS:
            t, o = data[s][0], data[s][1]
            for e in ev[s]:
                x = trade(t, o, e["i"], -e["s"], hm, funds[s])      # fade 方向 (follow 毛報酬為其相反數)
                if x: x["sym"] = s; trs.append(x)
        va = [x for x in trs if x["t_entry"] >= SPLIT]; al = trs
        def g(v): return round(sum(x["gross"] for x in v) / len(v), 2) if v else None
        row[f"h{hm}"] = {"fade_gross_mean_bp_all": g(al), "fade_gross_mean_bp_validation": g(va), "n_validation": len(va),
                         "fade_net_mean_validation_7bp": round(sum(x["net"] for x in va) / len(va), 2) if va else None,
                         "fade_net_mean_validation_2x": round(sum(x["net_2x"] for x in va) / len(va), 2) if va else None,
                         "fade_win_pct_net_validation": round(100 * sum(1 for x in va if x["net"] > 0) / len(va), 1) if va else None}
    out[f"k_sigma_{ks}"] = row
    print(ks, json.dumps(row, ensure_ascii=False))
json.dump(out, open(os.path.join(ROOT, "results", "b_explore.json"), "w"), indent=1, ensure_ascii=False)
