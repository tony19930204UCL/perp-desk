#!/usr/bin/env python3
"""RESEARCH-R1 D: H-A 資金費率極端值反轉、H-B 波動率區間/時段/DVOL 過濾、H-C 上一根逆勢、成交順序敏感度。
格子與規則見 PREREG.md（跑前登記）。輸出 results/hyp.json"""
import csv, json, os, statistics, sys, math
from bisect import bisect_left
from statistics import NormalDist
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import *

bars = {s: load(s) for s in SYMS}
stats = {s: roll_stats(bars[s]) for s in SYMS}
fund = {s: Fund(s) for s in SYMS}
idx = {s: {b[0]: i for i, b in enumerate(bars[s])} for s in SYMS}
N_TESTS = 36
Z_ALL = NormalDist().inv_cdf(1 - 0.05 / (2 * N_TESTS))
PER = {"train": (T0, SPLIT), "validation_clean_oos": (SPLIT, CONTAM), "recent_seen": (CONTAM, T_END)}
OUT = {"n_tests_prereg": N_TESTS, "bonferroni_z_all": round(Z_ALL, 2)}


def per_period(trs):
    r = {}
    for p, (a, b) in PER.items():
        r[p] = summarize(split(trs, a, b), years_between(a, b))
    return r


def short(r):
    return {k: r.get(k) for k in ("n", "win_pct", "gross_mean_bp", "funding_mean_bp", "net_mean_bp", "net_median_bp", "t_cluster_day")} if r.get("n") else {"n": 0}


# ---------------- H-A ----------------
def quantile(sorted_vals, q):
    k = (len(sorted_vals) - 1) * q
    lo = int(math.floor(k)); hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def hA(q, hold, side):
    trs = []
    for s in SYMS:
        ft, fr = fund[s].t, fund[s].r
        free_t = -1
        for k in range(270, len(ft)):
            if ft[k] < free_t:
                continue
            prior = sorted(fr[k - 270:k])
            r = fr[k]
            hi = r > quantile(prior, q); lo = r < quantile(prior, 1 - q)
            if side == "high_short" and not hi: continue
            if side == "low_long" and not lo: continue
            T = ft[k] // H * H
            eo = T + H
            if eo not in idx[s]: continue
            i0 = idx[s][eo]; i1 = i0 + hold - 1
            if i1 >= len(bars[s]): continue
            entry = bars[s][i0][1]; exitp = bars[s][i1][4]
            t_exit = bars[s][i1][0] + H
            fs = fund[s].sum_between(eo, t_exit) * 1e4
            if side == "high_short":
                gross = (entry / exitp - 1) * 1e4; fcost = -fs
            else:
                gross = (exitp / entry - 1) * 1e4; fcost = fs
            trs.append({"sym": s, "t_entry": eo, "t_exit": t_exit, "gross": gross, "cost": 14.0, "funding": fcost,
                        "net": gross - 14.0 - fcost, "reason": "time"})
            free_t = t_exit
    return trs

HA = {}
for q in (0.95, 0.99):
    for hold in (8, 24):
        for side in ("high_short", "low_long"):
            key = f"q{q}_h{hold}_{side}"
            trs = hA(q, hold, side)
            HA[key] = {"full": short(summarize(trs, years_between(T0, T_END))), **{p: short(v) for p, v in per_period(trs).items()},
                       "per_coin_full_n_net": {s: (len([t for t in trs if t["sym"] == s]), round(statistics.mean([t["net"] for t in trs if t["sym"] == s]), 1) if any(t["sym"] == s for t in trs) else None) for s in SYMS}}
OUT["H_A_funding_fade"] = HA

# ---------------- H-B ----------------
def load_dvol(cur):
    d = {}
    with open(os.path.join(DATA, f"DVOL_{cur}_1h.csv")) as f:
        r = csv.reader(f); next(r)
        for p in r: d[int(p[0])] = float(p[4])
    return d

DV = {"BTCUSDT": load_dvol("BTC"), "ETHUSDT": load_dvol("ETH")}
rv = {}
for s in SYMS:
    c = [b[4] for b in bars[s]]
    ret = [0.0] + [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    arr = [None] * len(c)
    for i in range(24, len(c)):
        arr[i] = statistics.pstdev(ret[i - 23:i + 1])
    rv[s] = arr


def pct_rank(series_fn, i, lookback=720):
    cur = series_fn(i)
    if cur is None: return None
    prior = [series_fn(j) for j in range(i - lookback, i)]
    prior = [x for x in prior if x is not None]
    if len(prior) < lookback * 0.9: return None
    return sum(1 for x in prior if x < cur) / len(prior)

base_trs = []
for s in SYMS:
    sig = w1_signals(bars[s], stats[s], 0.75, 1.0)
    for t in simulate(bars[s], sig, s, fund[s], "close"):
        t["feat"] = {}
        i = t["i"]; bt = bars[s][i][0]
        t["feat"]["rv_pct"] = pct_rank(lambda j: rv[s][j], i)
        if s in DV:
            f = lambda j: DV[s].get(bars[s][j][0])
            t["feat"]["dvol_pct"] = pct_rank(f, i)
            dv = f(i)
            ra = rv[s][i] * math.sqrt(8760) * 100 if rv[s][i] is not None else None
            t["feat"]["iv_gt_rv"] = (dv > ra) if (dv is not None and ra is not None) else None
        t["feat"]["hour"] = (bt // H) % 24
        base_trs.append(t)

groups = {
    "DVOL_high(BTC,ETH)": lambda t: t["feat"].get("dvol_pct") is not None and t["feat"]["dvol_pct"] >= 2 / 3,
    "DVOL_low(BTC,ETH)": lambda t: t["feat"].get("dvol_pct") is not None and t["feat"]["dvol_pct"] <= 1 / 3,
    "RV_high(4coins)": lambda t: t["feat"]["rv_pct"] is not None and t["feat"]["rv_pct"] >= 2 / 3,
    "RV_low(4coins)": lambda t: t["feat"]["rv_pct"] is not None and t["feat"]["rv_pct"] <= 1 / 3,
    "IV>RV(BTC,ETH)": lambda t: t["feat"].get("iv_gt_rv") is True,
    "IV<RV(BTC,ETH)": lambda t: t["feat"].get("iv_gt_rv") is False,
    "session_Asia_00-08": lambda t: 0 <= t["feat"]["hour"] < 8,
    "session_EU_08-16": lambda t: 8 <= t["feat"]["hour"] < 16,
    "session_US_16-24": lambda t: 16 <= t["feat"]["hour"] < 24,
}
HB = {}
for k, fn in groups.items():
    sub = [t for t in base_trs if fn(t)]
    HB[k] = {"full": short(summarize(sub, years_between(T0, T_END))), **{p: short(v) for p, v in per_period(sub).items()}}
# 對照：BTC+ETH 全部 W1 交易 (DVOL/IV 組的比較基準)
be = [t for t in base_trs if t["sym"] in DV]
HB["_ref_all_BTC_ETH"] = {"full": short(summarize(be, years_between(T0, T_END))), **{p: short(v) for p, v in per_period(be).items()}}
OUT["H_B_regime_filters"] = HB
OUT["DVOL_coverage"] = {"BTC_rows": len(DV["BTCUSDT"]), "ETH_rows": len(DV["ETHUSDT"])}

# ---------------- H-C ----------------
HC = {}
for cond in ("all", "top_decile_abs_ret"):
    for side in ("down_long", "up_short"):
        trs = []
        for s in SYMS:
            b = bars[s]
            absr = [None] + [abs(b[i][4] / b[i - 1][4] - 1) for i in range(1, len(b))]
            for i in range(721, len(b) - 1):
                r = b[i][4] / b[i - 1][4] - 1
                if side == "down_long" and not r < 0: continue
                if side == "up_short" and not r > 0: continue
                if cond != "all":
                    prior = absr[i - 720:i]
                    if not (sum(1 for x in prior if x < absr[i]) / 720 >= 0.9): continue
                entry = b[i][4]; ex = b[i + 1][4]
                gross = (ex / entry - 1) * 1e4 * (1 if side == "down_long" else -1)
                fs = fund[s].sum_between(b[i + 1][0], b[i + 1][0] + H) * 1e4
                fcost = fs if side == "down_long" else -fs
                trs.append({"sym": s, "t_entry": b[i + 1][0], "t_exit": b[i + 1][0] + H, "gross": gross, "cost": 14.0, "funding": fcost,
                            "net": gross - 14 - fcost, "reason": "time"})
        r = {"full": short(summarize(trs)), **{p: short(v) for p, v in per_period(trs).items()}}
        r["net_if_cost_4bp_optimistic_bound_full"] = round(statistics.mean([t["gross"] - 4 - t["funding"] for t in trs]), 2)
        HC[f"{cond}_{side}"] = r
OUT["H_C_prev_candle_contrarian_1h"] = HC

# ---------------- 成交順序敏感度(基線 W1, S1) ----------------
sens = {}
for order in ("stop_first", "target_first"):
    trs = []
    for s in SYMS:
        trs += simulate(bars[s], w1_signals(bars[s], stats[s], 0.75, 1.0), s, fund[s], "close", order=order)
    sens[order] = {"full": short(summarize(trs)), **{p: short(v) for p, v in per_period(trs).items()}}
OUT["order_sensitivity_W1_S1"] = sens
json.dump(OUT, open(os.path.join(ROOT, "results", "hyp.json"), "w"), indent=1, ensure_ascii=False)

print("Bonferroni z (36 tests)", OUT["bonferroni_z_all"])
for fam in ("H_A_funding_fade", "H_B_regime_filters", "H_C_prev_candle_contrarian_1h"):
    print("==", fam)
    for k, v in OUT[fam].items():
        row = " | ".join(f"{p[:5]} n={v[p].get('n')} g={v[p].get('gross_mean_bp')} net={v[p].get('net_mean_bp')} t={v[p].get('t_cluster_day')}" for p in ("train", "validation_clean_oos", "recent_seen") if p in v)
        print(f"{k:28s} full n={v['full'].get('n')} g={v['full'].get('gross_mean_bp')} net={v['full'].get('net_mean_bp')} t={v['full'].get('t_cluster_day')} || {row}" + (f" || opt4bp={v.get('net_if_cost_4bp_optimistic_bound_full')}" if 'net_if_cost_4bp_optimistic_bound_full' in v else ""))
print("order", json.dumps({k: (v['full']['n'], v['full']['gross_mean_bp'], v['full']['net_mean_bp']) for k, v in sens.items()}))
