#!/usr/bin/env python3
"""RESEARCH-R2 H2 橫截面動量/反轉 (10 格) + H3 資金費率橫截面傾斜 (8 格), 20 幣日線。輸出 results/h23.json"""
import json, os, sys, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2lib import *

uni = [u["symbol"] for u in json.load(open(os.path.join(DATA2, "universe.json")))["universe"]]
assert len(uni) == 20
ts = None; O = {}; Cc = {}; fund = {}
for s in uni:
    bars = load_1d(s); t = [b[0] for b in bars]
    if ts is None: ts = t
    assert t == ts, s
    O[s] = [b[1] for b in bars]; Cc[s] = [b[4] for b in bars]; fund[s] = FundF(s)
F = fund_arrays(ts, fund)
n = len(ts)
# H3 用的滾動資金費率: 到 bar j 結束 (ts[j]+DAY) 為止 W 日內結算總和
def fsum(s, j, Wd):
    return fund[s].sum_between(ts[j] + DAY - Wd * DAY, ts[j] + DAY)


def build_W(score_fn, R, long_top, k_each=4, half=0.5):
    """score_fn(s,j)->分數; long_top=True 做多分數最高 k_each 檔、空分數最低 k_each 檔。"""
    W = {s: [0.0] * n for s in uni}
    tgt = {s: 0.0 for s in uni}
    for k in range(1, n):
        j = k - 1
        if ts[k] >= EV0:
            d = (ts[j] - T0) // DAY
            if d % R == 0:
                sc = sorted(uni, key=lambda s: (score_fn(s, j), s))   # 升冪
                tgt = {s: 0.0 for s in uni}
                top, bot = sc[-k_each:], sc[:k_each]
                for s in top: tgt[s] = (half / k_each) * (1 if long_top else -1)
                for s in bot: tgt[s] = (half / k_each) * (-1 if long_top else 1)
        else:
            tgt = {s: 0.0 for s in uni}
        for s in uni: W[s][k] = tgt[s]
    return W


OUT = {"n_tests_registered_total": N_TESTS, "z_bonferroni": round(Z_BONF, 3), "universe": uni, "H2": {}, "H3": {}}
va, vb = PERIODS["validation"]; nva = (vb - va) // DAY
def run(W):
    res = engine(ts, O, W, F)
    ev = evaluate(res); ev["verdict"] = verdict(ev); ev["blocks_91d_mean_bp_day_from_EV0"] = block_means(res)
    ev["percoin_validation_net_bp_day"] = {s: round(sum(res["percoin"][s][k] for k, t in enumerate(res["ts"]) if va <= t < vb) / nva, 2) for s in uni}
    return ev

cells = [(1, 1)] + [(L, R) for L in (7, 30) for R in (1, 7)]
for (L, R) in cells:
    for dirn in ("momentum", "reversal"):
        if L == 1 and R != 1: continue
        score = (lambda s, j, L=L: Cc[s][j] / Cc[s][j - L] - 1) if True else None
        W = build_W(lambda s, j, L=L: (Cc[s][j] / Cc[s][j - L] - 1) if j >= L else 0.0, R, long_top=(dirn == "momentum"))
        OUT["H2"][f"{dirn}_L{L}_R{R}"] = run(W)
assert len(OUT["H2"]) == 10, len(OUT["H2"])
for Wd in (3, 7):
    for R in (1, 7):
        for dirn in ("carry", "anti"):
            # carry: 做空費率最高、做多最低 => long_top=False
            W = build_W(lambda s, j, Wd=Wd: fsum(s, j, Wd), R, long_top=(dirn == "anti"))
            OUT["H3"][f"{dirn}_W{Wd}d_R{R}"] = run(W)
assert len(OUT["H3"]) == 8
# 對照: 20 幣等權買進持有
Wb = {s: [(1.0 / 20) if ts[k] >= EV0 else 0.0 for k in range(n)] for s in uni}
OUT["control_buy_and_hold_equal_weight_20"] = evaluate(engine(ts, O, Wb, F), with_stress=False)
json.dump(OUT, open(os.path.join(ROOT, "results", "h23.json"), "w"), indent=1, ensure_ascii=False)
print("Z_BONF", round(Z_BONF, 3))
for fam in ("H2", "H3"):
    print("==", fam)
    for k, v in OUT[fam].items():
        tr, va_ = v["train"], v["validation"]; st = v["validation_stress_mean_bp_day"]
        print(f"{k:22s} {v['verdict']:9s} | tr {tr['mean_bp_day']:7.2f} | va {va_['mean_bp_day']:7.2f} ann% {va_['ann_pct']:6.1f} sh {va_['sharpe']} nwt {va_['nw_t']} win% {va_['win_day_pct']} payoff {va_['payoff_avg_win_over_avg_loss']} dd% {va_['maxdd_pct']} | g {va_['gross_bp_day']} c {va_['cost_bp_day']} f {va_['fund_bp_day']} turn {va_['turnover_per_year']} | x1.5 {st['cost_x1.5']} mk {st['maker_opt_2bp_side']} c0 {st['cost_0']} | recent {v['recent_seen']['mean_bp_day']}")
c = OUT["control_buy_and_hold_equal_weight_20"]
print("B&H 20 coins: train", c["train"]["mean_bp_day"], "val", c["validation"]["mean_bp_day"], c["validation"]["ann_pct"], "sharpe", c["validation"]["sharpe"], "recent", c["recent_seen"]["mean_bp_day"])
