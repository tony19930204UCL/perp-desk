#!/usr/bin/env python3
"""RESEARCH-R3 A 組：橫截面動量/反轉 point-in-time 長歷史重測 (6 格)。輸出 results/a.json"""
import json, os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r3lib import *

U = json.load(open(os.path.join(ROOT, "results", "a_universe.json")))
dparse = lambda s: ms(*map(int, s.split("-"))) // DAY
EV0 = dparse(U["ev0_trade_day"]); SPLIT_D = dparse(U["split_a_day"]); A_END_D = dparse(U["a_end_exclusive_day"])
uni = {int(k): v for k, v in U["uni"].items()}
bars = load_1d_all()
d_hi = max(max(b) for b in bars.values())
funds = load_funds(U["symbols_ever_in_top20"])
EV0_MS, SPLIT_MS, A_END_MS = EV0 * DAY, SPLIT_D * DAY, A_END_D * DAY
R2_SEEN_MS = ms(2023, 10, 1)            # r2 看過的資料自 2023-10 起 (評估起點 2024-01-15)
CELLS = [("momentum", 1, 1), ("momentum", 7, 7), ("momentum", 30, 1), ("momentum", 30, 7), ("reversal", 1, 1), ("reversal", 7, 7)]
days_of = lambda res: [t // DAY for t in res["ts"]]


def year_rows(res):
    days = days_of(res); out = {}
    for y in range(2020, 2027):
        a = max(EV0, ms(y, 1, 1) // DAY); b = min(A_END_D, ms(y + 1, 1, 1) // DAY)
        if b <= a: continue
        w = window_stats(res["net"], days, a, b)
        w["complete_year(n>=180)"] = w["n_days"] >= 180
        out[str(y)] = w
    return out


def blocks(res, size=91):
    days = days_of(res); out = []
    for s in range(EV0, A_END_D, size):
        e = min(s + size, A_END_D); w = window_stats(res["net"], days, s, e); out.append(w["mean_bp_day"] if w["n_days"] else None)
    return out


def analyze(name, tgt):
    res, pc, dg = run_engine(tgt, bars, funds, EV0, d_hi)
    days = days_of(res)
    st = {"train": period_stats(res, EV0_MS, SPLIT_MS), "validation": period_stats(res, SPLIT_MS, A_END_MS), "all": period_stats(res, EV0_MS, A_END_MS)}
    st["validation_cost_x1.5_mean"] = period_stats(res, SPLIT_MS, A_END_MS, 1.5)["mean_bp_day"]
    st["validation_cost_x2_mean"] = period_stats(res, SPLIT_MS, A_END_MS, 2.0)["mean_bp_day"]
    st["train_cost_x1.5_mean"] = period_stats(res, EV0_MS, SPLIT_MS, 1.5)["mean_bp_day"]
    nva = A_END_D - SPLIT_D
    contrib = {s: sum(v for d, v in pc[s].items() if SPLIT_D <= d < A_END_D) / nva for s in pc}
    top = sorted(contrib, key=lambda s: -contrib[s])
    st["validation_top5_coin_contrib_bp_day"] = {s: round(contrib[s], 2) for s in top[:5]}
    st["validation_bottom3_coin_contrib_bp_day"] = {s: round(contrib[s], 2) for s in top[-3:]}
    top1 = top[0]
    ex = net_series_ex(res, pc, top1)
    st["validation_ex_top_coin"] = {"coin": top1, "top_coin_share_of_total_pct": round(100 * contrib[top1] / st["validation"]["mean_bp_day"], 1) if st["validation"]["mean_bp_day"] else None,
                                    **window_stats(ex, days, SPLIT_D, A_END_D)}
    ex2 = net_series_ex(res, pc, top[1])
    st["validation_ex_top2nd_coin"] = {"coin": top[1], **window_stats(ex2, days, SPLIT_D, A_END_D)}
    ex12 = [ex[i] - pc[top[1]].get(d, 0.0) for i, d in enumerate(days)]
    st["validation_ex_top2_coins"] = window_stats(ex12, days, SPLIT_D, A_END_D)
    st["by_calendar_year"] = year_rows(res)
    comp = [v for v in st["by_calendar_year"].values() if v["complete_year(n>=180)"]]
    st["years_positive_over_complete"] = f"{sum(1 for v in comp if v['mean_bp_day'] > 0)}/{len(comp)}"
    st["blocks_91d_mean_bp_day_from_EV0"] = blocks(res)
    st["pre_2023-10(r2未看過)"] = window_stats(res["net"], days, EV0, R2_SEEN_MS // DAY)
    st["post_2023-10(r2已看過時段,不同宇宙)"] = window_stats(res["net"], days, R2_SEEN_MS // DAY, A_END_D)
    st["validation_leg_gross_bp_day"] = {"long": round(sum(x for x, d in zip(res["g_long"], days) if SPLIT_D <= d < A_END_D) / nva, 2),
                                         "short": round(sum(x for x, d in zip(res["g_short"], days) if SPLIT_D <= d < A_END_D) / nva, 2)}
    st["engine_diag"] = dg
    va, tr = st["validation"], st["train"]
    ok_a = tr["mean_bp_day"] > 0 and va["mean_bp_day"] > 0
    ok_b = va["nw_t"] is not None and va["nw_t"] > Z_BONF
    ok_c = st["validation_cost_x1.5_mean"] > 0
    ok_d = st["validation_ex_top_coin"]["mean_bp_day"] > 0
    ok_e = len(comp) > 0 and sum(1 for v in comp if v["mean_bp_day"] > 0) / len(comp) >= 0.6
    st["criteria"] = {"a_train_val_both_pos": ok_a, "b_val_nw_t_gt_z": ok_b, "c_x1.5_cost_val_pos": ok_c, "d_ex_top_coin_val_pos": ok_d, "e_years_pos_ge_60pct": ok_e}
    st["verdict"] = "PASS" if all((ok_a, ok_b, ok_c, ok_d, ok_e)) else ("WEAK_LEAD" if ok_a and ok_c and va["nw_t"] is not None and va["nw_t"] > 2.0 else "FAIL")
    return st


OUT = {"n_tests_registered_total": N_TESTS, "z_bonferroni": round(Z_BONF, 4), "ev0": U["ev0_trade_day"], "split_a": U["split_a_day"], "a_end_exclusive": U["a_end_exclusive_day"],
       "n_symbols_loaded": U["n_symbols_loaded"], "n_symbols_ever_in_top20": U["n_symbols_ever_in_top20"], "n_ever_top20_delisted": U["n_ever_top20_delisted_or_ended_before_last_day"],
       "n_funding_files_loaded": len(funds), "cells": {}}
for (dirn, L, R) in CELLS:
    name = f"{dirn}_L{L}_R{R}"
    OUT["cells"][name] = analyze(name, targets(uni, bars, EV0, d_hi, L, R, dirn))
OUT["control_buyhold_pit_equal_weight"] = {k: v for k, v in analyze("bh", targets(uni, bars, EV0, d_hi, 1, 1, "buyhold")).items()
                                           if k in ("train", "validation", "all", "by_calendar_year", "engine_diag")}
json.dump(OUT, open(os.path.join(ROOT, "results", "a.json"), "w"), indent=1, ensure_ascii=False)
print("Z", round(Z_BONF, 4), "EV0", OUT["ev0"], "SPLIT", OUT["split_a"], "end", OUT["a_end_exclusive"], "funding files", len(funds), "/", U["n_symbols_ever_in_top20"])
for k, c in OUT["cells"].items():
    tr, va, al = c["train"], c["validation"], c["all"]
    print(f"{k:16s} {c['verdict']:9s} | tr {tr['mean_bp_day']:7.2f} | va {va['mean_bp_day']:7.2f} ann% {va['ann_pct']} sh {va['sharpe']} t {va['nw_t']} win% {va['win_day_pct']} payoff {va['payoff_avg_win_over_avg_loss']} dd% {va['maxdd_pct']} g {va['gross_bp_day']} c {va['cost_bp_day']} f {va['fund_bp_day']} | x1.5 {c['validation_cost_x1.5_mean']} x2 {c['validation_cost_x2_mean']} | exTop[{c['validation_ex_top_coin']['coin']}] {c['validation_ex_top_coin']['mean_bp_day']} t {c['validation_ex_top_coin']['nw_t']} | allT {al['mean_bp_day']} t {al['nw_t']} | yrs+ {c['years_positive_over_complete']} | crit {c['criteria']}")
bh = OUT["control_buyhold_pit_equal_weight"]
print("BH PIT: train", bh["train"]["mean_bp_day"], "val", bh["validation"]["mean_bp_day"], "all", bh["all"]["mean_bp_day"])
