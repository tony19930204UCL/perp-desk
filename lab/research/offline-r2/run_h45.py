#!/usr/bin/env python3
"""RESEARCH-R2 H4 現貨-永續資金費率套利理論上限(描述, 1 格) + H5 BTC 15m 快進快出可行性 (4 格)。輸出 results/h45.json"""
import csv, json, os, statistics, sys, math
from bisect import bisect_left, insort
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2lib import *

OUT = {"n_tests_registered_total": N_TESTS, "z_bonferroni": round(Z_BONF, 3)}
# ---------------- H4 ----------------
uni = [u["symbol"] for u in json.load(open(os.path.join(DATA2, "universe.json")))["universe"]]
ndays = (T_END - T0) // DAY
daily = {}
for s in uni:
    f = FundF(s)
    daily[s] = [f.sum_between(T0 + d * DAY, T0 + (d + 1) * DAY) * 1e4 for d in range(ndays)]   # bp/日 (多單付正費率 => 空永續收)
basket = [sum(daily[s][d] for s in uni) / len(uni) for d in range(ndays)]
majors = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
maj = [sum(daily[s][d] for s in majors) / 4 for d in range(ndays)]
RT_COST = 2 * ((10 + 2) + (5 + 2))      # 現貨吃單10+滑2, 永續吃單5+滑2, 進出各一次 = 38bp
segs = {"full": (T0, T_END), "train": (T0, SPLIT), "validation": (SPLIT, CONTAM), "recent": (CONTAM, T_END)}
def seg_idx(a, b): return (a - T0) // DAY, (b - T0) // DAY
H4 = {"round_trip_cost_bp_both_legs": RT_COST, "assumptions": "long spot + short perp, collect positive funding; basis PnL, spot borrowing/withdrawal, margin & liquidation risk NOT modeled; no spot data downloaded"}
for name, series in (("BTC", daily["BTCUSDT"]), ("ETH", daily["ETHUSDT"]), ("SOL", daily["SOLUSDT"]), ("XRP", daily["XRPUSDT"]), ("majors4_eq", maj), ("basket20_eq", basket)):
    r = {}
    for sg, (a, b) in segs.items():
        i, j = seg_idx(a, b); x = series[i:j]
        m = sum(x) / len(x)
        r[sg] = {"gross_funding_ann_pct_on_notional": round(m * 365 / 100, 2), "mean_bp_per_day": round(m, 3), "pct_days_negative": round(100 * sum(1 for v in x if v < 0) / len(x), 1)}
    for hold in (30, 90):
        nets = []
        for d0 in range(0, ndays - hold):
            nets.append(sum(series[d0:d0 + hold]) - RT_COST)
        ann = [v / hold * 365 / 100 for v in nets]
        r[f"hold{hold}d_windows"] = {"n": len(nets), "net_ann_pct_on_notional_mean": round(statistics.mean(ann), 2), "median": round(statistics.median(ann), 2),
                                    "worst": round(min(ann), 2), "pct_windows_net_positive": round(100 * sum(1 for v in nets if v > 0) / len(nets), 1),
                                    "net_ann_pct_on_capital_1x_perp": round(statistics.mean(ann) / 2, 2), "net_ann_pct_on_capital_3x_perp": round(statistics.mean(ann) / (1 + 1 / 3), 2)}
    H4[name] = r
OUT["H4_spot_perp_funding_upper_bound"] = H4

# ---------------- H5 ----------------
bars = []
with open(os.path.join(DATA2, "BTCUSDT_15m.csv")) as f:
    rd = csv.reader(f); next(rd)
    for p in rd: bars.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
n = len(bars); M15 = 900000
fund = r1.Fund("BTCUSDT")
T5_0 = ms(2025, 10, 1); T5_S = ms(2026, 4, 10)
P5 = {"train": (T5_0, T5_S), "validation": (T5_S, T_END)}
assert bars[0][0] <= T5_0 + DAY and all(bars[i + 1][0] - bars[i][0] == M15 for i in range(n - 1))
c = [b[4] for b in bars]; absr = [0.0] + [abs(c[i] / c[i - 1] - 1) for i in range(1, n)]
LB = 2880
win = sorted(absr[1:LB + 1])        # 先填初始窗口 absr[1..LB]
trs = {k: [] for k in ("contra_all", "contra_top10", "mom_all", "mom_top10")}
for i in range(LB + 1, n - 1):
    # win 此時 = absr[i-LB .. i-1]
    r = c[i] / c[i - 1] - 1
    top = (bisect_left(win, absr[i]) / LB) >= 0.9
    if r != 0:
        s_contra = -1 if r > 0 else 1
        t_entry = bars[i + 1][0]; t_exit = t_entry + M15
        if t_entry >= T5_0:
            g_unit = (c[i + 1] / c[i] - 1) * 1e4        # 在 close_i 成交(吃單), 持有到 close_{i+1}
            fs = fund.sum_between(t_entry, t_exit) * 1e4
            for key, side, cond in (("contra_all", s_contra, True), ("contra_top10", s_contra, top), ("mom_all", -s_contra, True), ("mom_top10", -s_contra, top)):
                if not cond: continue
                gross = side * g_unit; fcost = side * fs
                trs[key].append({"sym": "BTCUSDT", "t_entry": t_entry, "t_exit": t_exit, "gross": gross, "cost": 14.0, "funding": fcost, "net": gross - 14.0 - fcost, "reason": "time"})
    # 滑動窗口
    old = absr[i - LB]; del win[bisect_left(win, old)]; insort(win, absr[i])
H5 = {}
for key, tl in trs.items():
    r = {}
    for p, (a, b) in P5.items():
        sub = r1.split(tl, a, b); S = r1.summarize(sub)
        r[p] = {k: S.get(k) for k in ("n", "win_pct", "gross_mean_bp", "funding_mean_bp", "net_mean_bp", "net_median_bp", "t_cluster_day", "n_clusters_days", "avg_win_bp", "avg_loss_bp")}
        if sub:
            r[p]["net_if_cost_4bp_maker_opt"] = round(statistics.mean([t["gross"] - 4 - t["funding"] for t in sub]), 2)
            r[p]["net_if_cost_21bp_x1.5"] = round(statistics.mean([t["gross"] - 21 - t["funding"] for t in sub]), 2)
            r[p]["gross_needed_to_break_even_bp"] = 14.0
    tr_, va_ = r["train"], r["validation"]
    ok = (tr_["net_mean_bp"] or -1) > 0 and (va_["net_mean_bp"] or -1) > 0 and (va_["t_cluster_day"] or 0) > Z_BONF and (va_["net_if_cost_21bp_x1.5"]) > 0
    r["verdict"] = "PASS" if ok else "FAIL"
    H5[key] = r
OUT["H5_btc_15m_cells"] = H5
# 描述: 成本相對 15m 波幅
vb0 = [b for b in bars if T5_S <= b[0] < T_END]
rng = sorted((b[2] - b[3]) / b[1] * 1e4 for b in vb0); ar = sorted(abs(b[4] / b[1] - 1) * 1e4 for b in vb0)
q = lambda a, p: round(a[int(p * (len(a) - 1))], 2)
rets = [c[i] / c[i - 1] - 1 for i in range(1, n)]
ac1 = statistics.correlation(rets[:-1], rets[1:])
desc = {"window": "validation half 2026-04-10..2026-10-18, BTCUSDT 15m", "n_bars": len(vb0),
        "range_bp_hl_over_open": {"p10": q(rng, .1), "median": q(rng, .5), "mean": round(sum(rng) / len(rng), 2), "p90": q(rng, .9)},
        "abs_return_open_close_bp": {"median": q(ar, .5), "mean": round(sum(ar) / len(ar), 2), "p90": q(ar, .9)},
        "pct_bars_range_ge_14bp": round(100 * sum(1 for v in rng if v >= 14) / len(rng), 1),
        "pct_bars_abs_ret_ge_14bp": round(100 * sum(1 for v in ar if v >= 14) / len(ar), 1),
        "lag1_autocorr_15m_returns_full_window": round(ac1, 4),
        "roundtrip_cost_bp_taker": 14, "roundtrip_cost_bp_maker_opt": 4,
        "taker_cost_over_mean_abs_ret": round(14 / (sum(ar) / len(ar)), 2),
        "breakeven_win_rate_symmetric_TP_SL": {f"{x}bp": {"taker14": round(0.5 + 14 / (2 * x), 3), "maker4": round(0.5 + 4 / (2 * x), 3)} for x in (10, 14, 20, 30, 50, 100)},
        "note": "對稱止盈止損 x bp 的打平勝率 = 0.5 + 成本/(2x)；x<=14bp 時 taker 打平勝率>=100%，不可能。"}
OUT["H5_descriptive_cost_vs_range"] = desc
json.dump(OUT, open(os.path.join(ROOT, "results", "h45.json"), "w"), indent=1, ensure_ascii=False)
print("H4 round-trip", RT_COST)
for k, v in H4.items():
    if isinstance(v, dict) and "full" in v:
        print(k, {sg: v[sg]["gross_funding_ann_pct_on_notional"] for sg in segs}, "| 30d net ann mean/median/pos%:", v["hold30d_windows"]["net_ann_pct_on_notional_mean"], v["hold30d_windows"]["median"], v["hold30d_windows"]["pct_windows_net_positive"], "| 90d:", v["hold90d_windows"]["net_ann_pct_on_notional_mean"], v["hold90d_windows"]["pct_windows_net_positive"], "cap1x/3x:", v["hold90d_windows"]["net_ann_pct_on_capital_1x_perp"], v["hold90d_windows"]["net_ann_pct_on_capital_3x_perp"], "neg-days%", v["full"]["pct_days_negative"])
print("H5")
for k, v in H5.items():
    for p in ("train", "validation"):
        x = v[p]; print(k, p, x["n"], "win", x["win_pct"], "gross", x["gross_mean_bp"], "fund", x["funding_mean_bp"], "net", x["net_mean_bp"], "t", x["t_cluster_day"], "maker4", x.get("net_if_cost_4bp_maker_opt"), "x1.5", x.get("net_if_cost_21bp_x1.5"))
    print(k, v["verdict"])
print(json.dumps(desc, ensure_ascii=False, indent=1))
