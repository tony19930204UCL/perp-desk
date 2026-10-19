#!/usr/bin/env python3
"""RESEARCH-R2 H1: 4h/日線 趨勢與突破 (BTC/ETH/SOL/XRP 多空等權, 12 格)。輸出 results/h1.json"""
import json, math, os, statistics, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2lib import *

COINS = SYMS
fund_r1 = {s: r1.Fund(s) for s in COINS}


def build(freq):
    ts = None; O = {}; Cc = {}
    for s in COINS:
        bars = load_1d(s) if freq == "1d" else resample(r1.load(s), 4)
        t = [b[0] for b in bars]
        if ts is None: ts = t
        assert t == ts, (s, freq)
        O[s] = [b[1] for b in bars]; Cc[s] = [b[4] for b in bars]
    F = fund_arrays(ts, fund_r1)
    return ts, O, Cc, F


def sig_donchian(c, N):
    out = [0] * len(c); st = 0
    for k in range(N, len(c)):
        hi = max(c[k - N:k]); lo = min(c[k - N:k])
        if c[k] > hi: st = 1
        elif c[k] < lo: st = -1
        out[k] = st
    return out


def sig_ma(c, f, s):
    pre = [0.0]
    for x in c: pre.append(pre[-1] + x)
    out = [0] * len(c)
    for k in range(s - 1, len(c)):
        mf = (pre[k + 1] - pre[k + 1 - f]) / f; ms_ = (pre[k + 1] - pre[k + 1 - s]) / s
        out[k] = 1 if mf > ms_ else -1
    return out


def sig_tsmom(c, L, volwin, bars_per_year):
    out = [0.0] * len(c)
    for k in range(max(L, volwin + 1), len(c)):
        s = 1 if c[k] / c[k - L] > 1 else -1
        rets = [c[j] / c[j - 1] - 1 for j in range(k - volwin + 1, k + 1)]
        vol = statistics.stdev(rets) * math.sqrt(bars_per_year)
        out[k] = s * min(2.0, 0.30 / vol) if vol > 0 else 0.0
    return out


def weights(ts, sig):
    n = len(ts); W = [0.0] * n
    for k in range(1, n):
        if ts[k] >= EV0:
            W[k] = sig[k - 1] / len(COINS)
    return W


CFG = []
for N in (20, 55): CFG.append(("1d", "donchian", {"N": N}))
for f, s in ((10, 50), (20, 100)): CFG.append(("1d", "macross", {"fast": f, "slow": s}))
for L in (30, 90): CFG.append(("1d", "tsmom", {"L": L, "volwin": 30}))
for N in (60, 180): CFG.append(("4h", "donchian", {"N": N}))
for f, s in ((60, 300), (120, 600)): CFG.append(("4h", "macross", {"fast": f, "slow": s}))
for L in (180, 540): CFG.append(("4h", "tsmom", {"L": L, "volwin": 180}))
assert len(CFG) == 12

data = {fr: build(fr) for fr in ("1d", "4h")}
BPY = {"1d": 365, "4h": 365 * 6}
OUT = {"n_tests_registered_total": N_TESTS, "z_bonferroni": round(Z_BONF, 3), "cells": {}}
va, vb = PERIODS["validation"]
nva = (vb - va) // DAY
for fr, kind, p in CFG:
    ts, O, Cc, F = data[fr]
    W = {}
    for s in COINS:
        if kind == "donchian": sg = sig_donchian(Cc[s], p["N"])
        elif kind == "macross": sg = sig_ma(Cc[s], p["fast"], p["slow"])
        else: sg = sig_tsmom(Cc[s], p["L"], p["volwin"], BPY[fr])
        W[s] = weights(ts, sg)
    res = engine(ts, O, W, F)
    ev = evaluate(res)
    ev["verdict"] = verdict(ev); ev["blocks_91d_mean_bp_day_from_EV0"] = block_means(res)
    # 描述：多空腿、各幣貢獻 (不計格, 不得用來選格)
    Wl = {s: [max(x, 0) for x in W[s]] for s in COINS}; Ws = {s: [min(x, 0) for x in W[s]] for s in COINS}
    ev["leg_validation_mean_bp_day"] = {"long_leg": period_stats(engine(ts, O, Wl, F), va, vb)["mean_bp_day"],
                                         "short_leg": period_stats(engine(ts, O, Ws, F), va, vb)["mean_bp_day"]}
    pc = {}
    for s in COINS:
        pc[s] = round(sum(res["percoin"][s][k] for k, t in enumerate(res["ts"]) if va <= t < vb) / nva, 3)
    ev["percoin_validation_net_bp_day"] = pc
    key = f"{fr}_{kind}_" + "_".join(f"{k}{v}" for k, v in p.items())
    OUT["cells"][key] = ev
# 對照：等權買進持有 (含資金費率與進場成本)
ts, O, Cc, F = data["1d"]
W = {s: [0.0] + [1.0 / len(COINS)] * (len(ts) - 1) for s in COINS}
for s in COINS: W[s] = [x if ts[k] >= EV0 else 0.0 for k, x in enumerate(W[s])]
OUT["control_buy_and_hold_equal_weight"] = evaluate(engine(ts, O, W, F), with_stress=False)
json.dump(OUT, open(os.path.join(ROOT, "results", "h1.json"), "w"), indent=1, ensure_ascii=False)
print("Z_BONF", round(Z_BONF, 3), "validation days", nva)
print(f"{'cell':34s} {'verd':9s} | train bp/d  | val bp/d  ann%  sharpe  nwt  win%  payoff  dd%   | gross cost fund turn/yr | x1.5  maker0  cost0 | long short")
for k, v in OUT["cells"].items():
    tr, va_ = v["train"], v["validation"]; st = v["validation_stress_mean_bp_day"]
    print(f"{k:34s} {v['verdict']:9s} | {tr['mean_bp_day']:7.2f} | {va_['mean_bp_day']:7.2f} {va_['ann_pct']:6.1f} {va_['sharpe']} {va_['nw_t']} {va_['win_day_pct']} {va_['payoff_avg_win_over_avg_loss']} {va_['maxdd_pct']} | {va_['gross_bp_day']} {va_['cost_bp_day']} {va_['fund_bp_day']} {va_['turnover_per_year']} | {st['cost_x1.5']} {st['maker_opt_2bp_side']} {st['cost_0']} | {v['leg_validation_mean_bp_day']['long_leg']} {v['leg_validation_mean_bp_day']['short_leg']}")
c = OUT["control_buy_and_hold_equal_weight"]
print("B&H equal-weight 4 coins: train", c["train"]["mean_bp_day"], c["train"]["ann_pct"], "val", c["validation"]["mean_bp_day"], c["validation"]["ann_pct"], "sharpe", c["validation"]["sharpe"], "recent", c["recent_seen"]["mean_bp_day"])
print("percoin val", {k: v["percoin_validation_net_bp_day"] for k, v in list(OUT["cells"].items())[:2]})
