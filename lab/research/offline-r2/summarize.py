#!/usr/bin/env python3
"""RESEARCH-R2: 彙整 35 格登記測試的結果與判定。輸出 results/summary.json"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2lib import ROOT, Z_BONF, N_TESTS
R = lambda n: json.load(open(os.path.join(ROOT, "results", n)))
h1, h23, h45 = R("h1.json"), R("h23.json"), R("h45.json")
rows = []
for fam, cells in (("H1", h1["cells"]), ("H2", h23["H2"]), ("H3", h23["H3"])):
    for k, v in cells.items():
        rows.append({"family": fam, "cell": k, "train_bp_day": v["train"]["mean_bp_day"], "val_bp_day": v["validation"]["mean_bp_day"], "val_ann_pct": v["validation"]["ann_pct"],
                     "val_sharpe": v["validation"]["sharpe"], "val_nw_t": v["validation"]["nw_t"], "val_win_day_pct": v["validation"]["win_day_pct"],
                     "val_payoff": v["validation"]["payoff_avg_win_over_avg_loss"], "val_maxdd_pct": v["validation"]["maxdd_pct"],
                     "val_x1.5cost_bp_day": v["validation_stress_mean_bp_day"]["cost_x1.5"], "recent_seen_bp_day": v["recent_seen"]["mean_bp_day"], "verdict": v["verdict"],
                     "blocks_91d": v["blocks_91d_mean_bp_day_from_EV0"]})
for k, v in h45["H5_btc_15m_cells"].items():
    rows.append({"family": "H5", "cell": k, "train_net_bp_trade": v["train"]["net_mean_bp"], "val_net_bp_trade": v["validation"]["net_mean_bp"], "val_gross_bp_trade": v["validation"]["gross_mean_bp"],
                 "val_t_cluster": v["validation"]["t_cluster_day"], "val_n": v["validation"]["n"], "val_win_pct": v["validation"]["win_pct"], "verdict": v["verdict"]})
rows.append({"family": "H4", "cell": "spot_perp_funding_upper_bound", "verdict": "DESCRIPTIVE_NOT_TESTED"})
n_cells = len(rows)
cnt = {}
for r in rows: cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1
S = {"n_registered_tests": N_TESTS, "n_cells_reported": n_cells, "z_bonferroni": round(Z_BONF, 3), "verdict_counts": cnt, "rows": rows}
assert n_cells == N_TESTS, n_cells
json.dump(S, open(os.path.join(ROOT, "results", "summary.json"), "w"), indent=1, ensure_ascii=False)
print("cells reported", n_cells, "registered", N_TESTS, "z", round(Z_BONF, 3), cnt)
top = sorted([r for r in rows if r["family"] in ("H1", "H2", "H3")], key=lambda r: -(r["val_nw_t"] or -99))[:5]
for r in top: print("TOP-t", r["family"], r["cell"], "val", r["val_bp_day"], "t", r["val_nw_t"], "sharpe", r["val_sharpe"], "x1.5", r["val_x1.5cost_bp_day"], "recent", r["recent_seen_bp_day"], "blocks", r["blocks_91d"])
