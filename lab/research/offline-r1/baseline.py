#!/usr/bin/env python3
"""RESEARCH-R1 B: W1 基線(凍結)重測 + 前向驗證(walk-forward) + 安慰劑。輸出 results/baseline.json"""
import json, os, random, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import *

random.seed(20261019)
bars = {s: load(s) for s in SYMS}
stats = {s: roll_stats(bars[s]) for s in SYMS}
fund = {s: Fund(s) for s in SYMS}
OUT = {"script": "baseline.py", "test_counter": []}


def run(z, vm, scen, syms=SYMS):
    trs = []
    for s in syms:
        sig = w1_signals(bars[s], stats[s], z, vm)
        trs += simulate(bars[s], sig, s, fund[s], scen)
    return trs


def Y(a, b):
    return years_between(a, b)

periods = {
    "full": (T0, T_END), "train": (T0, SPLIT), "validation_clean_oos": (SPLIT, CONTAM),
    "second_half_incl_recent": (SPLIT, T_END), "recent_seen": (CONTAM, T_END),
}
# ---- 1. W1 凍結，四種成本情境 x 各期間
W = {}
for scen in ["close", "next_open", "maker_opt", "maker_limit"]:
    trs = run(0.75, 1.0, scen)
    W[scen] = {}
    for p, (a, b) in periods.items():
        W[scen][p] = summarize(split(trs, a, b), Y(a, b))
    W[scen]["per_coin_full"] = {s: summarize(split([t for t in trs if t["sym"] == s], T0, T_END), Y(T0, T_END)) for s in SYMS}
    OUT["test_counter"].append(f"W1_frozen_{scen}")
OUT["w1_frozen"] = W
# 毛優勢(不扣成本)：close 情境下 gross 平均 vs 14bp
# ---- 2. 前向驗證網格(8 組) S1(close)
grid = {}
for z in (0.75, 1.0, 1.5, 2.0):
    for vm in (1.0, 1.5):
        trs = run(z, vm, "close")
        grid[f"z{z}_vm{vm}"] = {p: summarize(split(trs, *periods[p]), Y(*periods[p])) for p in ("train", "validation_clean_oos", "recent_seen")}
        OUT["test_counter"].append(f"grid_z{z}_vm{vm}")
OUT["grid"] = grid
cands = {k: v["train"]["net_mean_bp"] for k, v in grid.items() if v["train"]["n"] >= 100}
sel = max(cands, key=cands.get)
OUT["selected_on_train"] = {"config": sel, "train_net_mean_bp": cands[sel], "validation": grid[sel]["validation_clean_oos"]}
OUT["selected_vs_w1_validation"] = {"selected": grid[sel]["validation_clean_oos"]["net_mean_bp"], "w1": grid["z0.75_vm1.0"]["validation_clean_oos"]["net_mean_bp"]}
# ---- 3. 成本敏感：W1 close 情境用 gross 重算 0/4/8/14 bp 往返
trs_close = run(0.75, 1.0, "close")
sens = {}
for cost in (0, 4, 8, 14, 20):
    r = {}
    for p in ("full", "train", "validation_clean_oos", "recent_seen"):
        tt = split(trs_close, *periods[p])
        r[p] = round(statistics.mean([t["gross"] - cost - t["funding"] for t in tt]), 2) if tt else None
    sens[f"roundtrip_{cost}bp"] = r
OUT["w1_cost_sensitivity_net_mean_bp"] = sens
OUT["avg_pair_corr_hourly_ret"] = round(avg_pair_corr(bars), 3)
rho = OUT["avg_pair_corr_hourly_ret"]
OUT["effective_sample_note"] = {
    "n_trades_all_coins": len(trs_close),
    "rho_based_neff": round(len(trs_close) / (1 + 3 * rho), 0),
    "distinct_entry_days": len({t["t_entry"] // DAY for t in trs_close}),
    "note": "四幣小時報酬平均相關 rho；信號多在市場同步下跌時同時出現，N_eff≈N/(1+3rho)；分群 t 值以進場日為群。",
}
# ---- 4. 安慰劑: 隨機買「收盤<前一根收盤」的 bar，同出場/成本(S1)
real = {p: summarize(split(trs_close, *periods[p]), Y(*periods[p])) for p in ("full", "validation_clean_oos")}
plc = {}
for p in ("full", "validation_clean_oos"):
    a, b = periods[p]
    n_sig = {s: len([i for i in w1_signals(bars[s], stats[s], 0.75, 1.0) if a <= bars[s][i][0] < b]) for s in SYMS}
    means = []
    for rep in range(300):
        tr = []
        for s in SYMS:
            pool = [i for i in range(61, len(bars[s]) - 14) if a <= bars[s][i][0] < b and bars[s][i][4] < bars[s][i - 1][4]]
            pick = sorted(random.sample(pool, min(n_sig[s], len(pool))))
            tr += simulate(bars[s], pick, s, fund[s], "close")
        means.append(statistics.mean([t["net"] for t in tr]) if tr else 0)
    realm = real[p]["net_mean_bp"]
    plc[p] = {"placebo_mean_of_means_bp": round(statistics.mean(means), 2), "placebo_p5": round(sorted(means)[15], 2),
              "placebo_p95": round(sorted(means)[284], 2), "w1_net_mean_bp": realm,
              "pct_placebo_ge_w1": round(100 * sum(1 for m in means if m >= realm) / len(means), 1)}
    OUT["test_counter"].append(f"placebo_{p}")
OUT["placebo"] = plc
OUT["n_tests"] = len(OUT["test_counter"])
os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)
json.dump(OUT, open(os.path.join(ROOT, "results", "baseline.json"), "w"), indent=1, ensure_ascii=False)
# 摘要
def line(tag, r):
    print(f"{tag:34s} n={r['n']:4d} win={r.get('win_pct')} gross={r.get('gross_mean_bp')} cost={r.get('cost_mean_bp')} fund={r.get('funding_mean_bp')} net={r.get('net_mean_bp')} med={r.get('net_median_bp')} tc={r.get('t_cluster_day')} ann%={r.get('annualized_pct_capital4')} dd%={r.get('maxdd_pct_capital4')}")
for scen in W:
    for p in periods:
        if W[scen][p]["n"]:
            line(f"W1 {scen} {p}", W[scen][p])
print("--grid (train / validation)")
for k, v in grid.items():
    print(k, "train n/net", v["train"]["n"], v["train"]["net_mean_bp"], "| val n/net/tc", v["validation_clean_oos"]["n"], v["validation_clean_oos"]["net_mean_bp"], v["validation_clean_oos"]["t_cluster_day"])
print("selected", OUT["selected_on_train"]["config"], OUT["selected_vs_w1_validation"])
print("sens", json.dumps(sens))
print("rho", rho, OUT["effective_sample_note"])
print("placebo", json.dumps(plc))
print("per-coin full close", {s: (W['close']['per_coin_full'][s]['n'], W['close']['per_coin_full'][s]['net_mean_bp']) for s in SYMS})
