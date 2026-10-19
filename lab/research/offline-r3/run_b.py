#!/usr/bin/env python3
"""RESEARCH-R3 B 組：稀有大事件反轉／續航 (4 幣 1m, 6 格)。輸出 results/b.json、results/b_events.csv"""
import json, os, random, statistics, sys, csv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r3b import *
from statistics import NormalDist

N_TESTS = 12
Z = NormalDist().inv_cdf(1 - 0.05 / (2 * N_TESTS))
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
RES = os.path.join(ROOT, "results"); os.makedirs(RES, exist_ok=True)


def stats(trs, key="net"):
    n = len(trs)
    if n == 0:
        return {"n": 0}
    net = [t[key] for t in trs]
    m = sum(net) / n
    tc, ncl = cluster_t(trs, val=key)
    w = [x for x in net if x > 0]; ls = [x for x in net if x <= 0]
    return {"n": n, "win_pct": round(100 * len(w) / n, 1), "gross_mean_bp": round(sum(t["gross"] for t in trs) / n, 2),
            "funding_mean_bp": round(sum(t["funding"] for t in trs) / n, 3), "net_mean_bp": round(m, 2),
            "net_median_bp": round(statistics.median(net), 2), "t_cluster_day": round(tc, 2) if tc is not None else None,
            "n_day_clusters": ncl, "avg_win_bp": round(sum(w) / len(w), 1) if w else None, "avg_loss_bp": round(sum(ls) / len(ls), 1) if ls else None,
            "payoff": round((sum(w) / len(w)) / abs(sum(ls) / len(ls)), 2) if w and ls else None,
            "worst_bp": round(min(net), 1), "best_bp": round(max(net), 1)}


def main():
    funds = {s: r1.Fund(s) for s in COINS}
    data = {}; events = {}
    for s in COINS:
        T = load_1m(s)
        t = T[0]
        assert len(t) == 1604160 and all(t[i + 1] - t[i] == MIN for i in range(len(t) - 1)), s
        data[s] = T
        events[s] = detect_events(*T)
    # 事件特徵
    feat = {}
    rows_out = []
    for s in COINS:
        t, o, h, l, c, qv = data[s]
        ev = events[s]
        gaps, rng_ratio, vr, zz = [], [], [], []
        path = {m: [] for m in (1, 5, 15, 30, 60)}
        # 正常 bar 振幅: 事件前 1440 分鐘平均 (h-l)/o
        for e in ev:
            i = e["i"]
            gap = (o[i + 1] / c[i] - 1) * e["s"] * 1e4          # >0: 下一根開盤繼續朝事件方向 (對 fade 不利)
            base = sum((h[q] - l[q]) / o[q] for q in range(i - 1440, i)) / 1440 * 1e4
            nxt = (h[i + 1] - l[i + 1]) / o[i + 1] * 1e4
            gaps.append(gap); rng_ratio.append(nxt / base if base > 0 else None)
            vr.append(e["vratio"]); zz.append(abs(e["r5"]) / e["sigma"])
            for m in path:
                if i + 1 + m < len(t):
                    path[m].append(e["s"] * (o[i + 1 + m] / o[i + 1] - 1) * 1e4)
            rows_out.append([s, t[i], e["s"], round(e["r5"] * 1e4, 2), round(e["sigma"] * 1e4, 3), round(e["vratio"], 2), round(gap, 2)])
        rr = [x for x in rng_ratio if x is not None]
        feat[s] = {"n_events": len(ev), "events_per_year": round(len(ev) / (len(t) - 43200 - 11 * 1440) * 525600, 1),
                   "n_down": sum(1 for e in ev if e["s"] < 0), "n_up": sum(1 for e in ev if e["s"] > 0),
                   "mean_abs_r5_bp": round(sum(abs(e["r5"]) for e in ev) / len(ev) * 1e4, 1),
                   "median_z": round(statistics.median(zz), 2), "median_vratio": round(statistics.median(vr), 2),
                   "next_bar_gap_bp_mean(正=續朝事件方向,對反轉不利)": round(sum(gaps) / len(gaps), 2),
                   "next_bar_gap_abs_median_bp": round(statistics.median([abs(x) for x in gaps]), 2),
                   "next_bar_range_over_prev1440_mean_ratio_mean": round(sum(rr) / len(rr), 2),
                   "next_bar_range_ratio_median": round(statistics.median(rr), 2),
                   "mean_continuation_path_bp(事件方向, 自 t+1 開盤起算 m 分鐘)": {str(m): round(sum(v) / len(v), 2) for m, v in path.items()}}
    with open(os.path.join(RES, "b_events.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["sym", "decision_bar_open_ms", "dir_of_move", "r5_bp", "sigma5_bp", "vol_ratio", "next_open_gap_bp_in_move_dir"]); w.writerows(rows_out)

    # 交易與格
    trades = {}
    for dirn, sign in (("fade", -1), ("follow", +1)):
        for hm in HORIZONS:
            trs = []
            for s in COINS:
                t, o = data[s][0], data[s][1]
                for e in events[s]:
                    x = trade(t, o, e["i"], sign * e["s"], hm, funds[s])
                    if x is None:
                        continue
                    x["sym"] = s; x["i"] = e["i"]; trs.append(x)
            trades[(dirn, hm)] = trs
    OUT = {"n_tests_registered_total": N_TESTS, "z_bonferroni": round(Z, 4), "split_ms": SPLIT, "events_feature": feat,
           "n_events_total": sum(len(v) for v in events.values()), "cells": {}}
    for (dirn, hm), trs in trades.items():
        tr = [x for x in trs if x["t_entry"] < SPLIT]; va = [x for x in trs if x["t_entry"] >= SPLIT]
        cell = {"all": stats(trs), "train": stats(tr), "validation": stats(va),
                "validation_cost_2x": stats(va, "net_2x"), "train_cost_2x": stats(tr, "net_2x")}
        # 驗證半段分幣
        cell["validation_per_coin"] = {s: stats([x for x in va if x["sym"] == s]) for s in COINS}
        # 去掉驗證半段貢獻最大的單一事件日
        byday = defaultdict(float)
        for x in va: byday[x["t_entry"] // DAY] += x["net"]
        topd = max(byday, key=byday.get) if byday else None
        va_ex = [x for x in va if x["t_entry"] // DAY != topd]
        cell["validation_ex_top_event_day"] = {"day_ms": topd * DAY if topd is not None else None, "day_sum_bp": round(byday[topd], 1) if topd is not None else None, **stats(va_ex)}
        # 安慰劑: 同幣同日隨機時點、隨機方向、同 h
        rnd = random.Random(20261019 + hm + (0 if dirn == "fade" else 1))
        pl = []
        for x in va:
            s = x["sym"]; t, o = data[s][0], data[s][1]
            day0 = (t[x["i"]] // DAY) * DAY
            i0 = (day0 - t[0]) // MIN
            for _ in range(20):
                ip = i0 + rnd.randrange(0, 1440)
                if ip < 43210 or ip + 1 + hm >= len(t):
                    continue
                y = trade(t, o, ip, rnd.choice((-1, 1)), hm, funds[s])
                if y: pl.append(y)
        cell["validation_placebo_random_time_dir"] = {"n": len(pl), "gross_mean_bp": round(sum(p["gross"] for p in pl) / len(pl), 3),
                                                       "gross_sd_bp": round(statistics.pstdev([p["gross"] for p in pl]), 2),
                                                       "net_mean_bp": round(sum(p["net"] for p in pl) / len(pl), 2),
                                                       "se_gross_mean_bp(依事件數計)": round(statistics.pstdev([p["gross"] for p in pl]) / math.sqrt(len(va)), 2)}
        # 判定
        nv = cell["validation"].get("n", 0)
        ok_a = cell["train"].get("net_mean_bp", -1) > 0 and cell["validation"].get("net_mean_bp", -1) > 0
        tv = cell["validation"].get("t_cluster_day")
        ok_b = tv is not None and tv > Z
        ok_c = cell["validation_cost_2x"].get("net_mean_bp", -1) > 0
        ok_d = nv >= 30
        ok_e = cell["validation_ex_top_event_day"].get("net_mean_bp", -1) > 0
        ok_f = sum(1 for s in COINS if cell["validation_per_coin"][s].get("net_mean_bp", -1) > 0) >= 3
        cell["criteria"] = {"a_train_val_both_pos": ok_a, "b_val_t_gt_z": ok_b, "c_2x_cost_val_pos": ok_c, "d_n_ge_30": ok_d, "e_ex_top_day_pos": ok_e, "f_ge3_coins_pos": ok_f}
        cell["verdict"] = "PASS" if all((ok_a, ok_b, ok_c, ok_d, ok_e, ok_f)) else ("WEAK_LEAD" if ok_a and ok_c and ok_d and tv is not None and tv > 2.0 else ("INSUFFICIENT_SAMPLE" if not ok_d else "FAIL"))
        OUT["cells"][f"{dirn}_h{hm}"] = cell
    json.dump(OUT, open(os.path.join(RES, "b.json"), "w"), indent=1, ensure_ascii=False)
    print("Z", round(Z, 4), "events", OUT["n_events_total"])
    for s in COINS: print(s, json.dumps(feat[s], ensure_ascii=False))
    for k, c in OUT["cells"].items():
        tr_, va_, v2 = c["train"], c["validation"], c["validation_cost_2x"]
        print(f"{k:11s} {c['verdict']:8s} | train n {tr_['n']} net {tr_['net_mean_bp']} gross {tr_['gross_mean_bp']} | val n {va_['n']} win {va_['win_pct']}% gross {va_['gross_mean_bp']} net {va_['net_mean_bp']} t {va_['t_cluster_day']} med {va_['net_median_bp']} payoff {va_['payoff']} | 2x {v2['net_mean_bp']} | exTopDay {c['validation_ex_top_event_day']['net_mean_bp']} | coins>0 {sum(1 for s in COINS if c['validation_per_coin'][s].get('net_mean_bp',-1)>0)} | placebo gross {c['validation_placebo_random_time_dir']['gross_mean_bp']}")


if __name__ == "__main__":
    main()
