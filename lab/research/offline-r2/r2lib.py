#!/usr/bin/env python3
"""RESEARCH-R2 共用函式庫：持倉序列引擎 + 統計。沿用 r1 lib 的切分日、Fund(資金費率符號)、吃單成本 7bp/邊。純 stdlib。

引擎約定 (與 PREREG.md 一致):
  bars 以 open_time 對齊 ts[k]；權重 W[c][k] = 持有 bar k 期間的部位(資本比例,多正空負)，由 bar k-1 收盤前資料決定。
  報酬 ret_k = O[k+1]/O[k]-1 (開盤到下一根開盤，等同 r1 S2 保守成交)。
  成本 = |W[k]-W[k-1]| * cost_side(bp)；資金費率 = W[k]*sum(funding in (ts[k], ts[k+1]]) (多單付正費率)。
"""
import csv, math, os, statistics, sys
from collections import defaultdict
from statistics import NormalDist

ROOT = os.path.dirname(os.path.abspath(__file__))
R1 = os.path.join(os.path.dirname(ROOT), "offline-r1")
sys.path.insert(0, R1)
import lib as r1   # 已驗證的 r1 函式庫 (資料載入、Fund、切分日、cluster_t)
from lib import SYMS, H, DAY, T0, SPLIT, CONTAM, T_END, ms, cluster_t

DATA2 = os.path.join(ROOT, "data")
EV0 = ms(2024, 1, 15)
TAKER_SIDE = r1.TAKER_SIDE          # 7bp
PERIODS = {"train": (EV0, SPLIT), "validation": (SPLIT, CONTAM), "recent_seen": (CONTAM, T_END)}
N_TESTS = 35
Z_BONF = NormalDist().inv_cdf(1 - 0.05 / (2 * N_TESTS))


class FundF:
    """與 r1.Fund 相同語意 (t0 < fundingTime <= t1)，但讀 r2 data。"""
    def __init__(self, sym):
        from bisect import bisect_right
        self._br = bisect_right
        self.t, self.r = [], []
        with open(os.path.join(DATA2, f"{sym}_funding.csv")) as f:
            rd = csv.reader(f); next(rd)
            for p in rd:
                self.t.append(int(p[0])); self.r.append(float(p[1]))
        self.cum = [0.0]
        for x in self.r:
            self.cum.append(self.cum[-1] + x)

    def sum_between(self, t0, t1):
        a = self._br(self.t, t0); b = self._br(self.t, t1)
        return self.cum[b] - self.cum[a]


def load_1d(sym):
    rows = []
    with open(os.path.join(DATA2, f"{sym}_1d.csv")) as f:
        r = csv.reader(f); next(r)
        for p in r:
            rows.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
    return rows


def resample(bars, k_hours):
    """1h -> k_hours 小時 (UTC 對齊)，只保留完整的組。"""
    step = k_hours * H
    g = defaultdict(list)
    for b in bars:
        g[b[0] // step].append(b)
    out = []
    for key in sorted(g):
        w = g[key]
        if len(w) != k_hours:
            continue
        out.append((key * step, w[0][1], max(x[2] for x in w), min(x[3] for x in w), w[-1][4], sum(x[5] for x in w)))
    return out


def engine(ts, O, W, F, cost_side=TAKER_SIDE):
    """ts:list[ms]; O:{c:[open]}; W:{c:[weight]}; F:{c:[funding sum per bar]} -> dict of per-bar arrays (長度 n-1)。"""
    coins = list(O)
    n = len(ts)
    gross = [0.0] * (n - 1); cost = [0.0] * (n - 1); fund = [0.0] * (n - 1); turn = [0.0] * (n - 1)
    percoin = {c: [0.0] * (n - 1) for c in coins}
    for c in coins:
        w = W[c]; o = O[c]; f = F[c]; prev = 0.0
        for k in range(n - 1):
            wk = w[k]
            g = wk * (o[k + 1] / o[k] - 1) * 1e4
            tn = abs(wk - prev)
            cs = tn * cost_side
            fc = wk * f[k] * 1e4
            gross[k] += g; cost[k] += cs; fund[k] += fc; turn[k] += tn
            percoin[c][k] = g - cs - fc
            prev = wk
    net = [gross[k] - cost[k] - fund[k] for k in range(n - 1)]
    return {"ts": ts[:-1], "gross": gross, "cost": cost, "fund": fund, "net": net, "turn": turn, "percoin": percoin}


def fund_arrays(ts, funds):
    return {c: [funds[c].sum_between(ts[k], ts[k + 1]) for k in range(len(ts) - 1)] + [0.0] for c in funds}


def to_daily(res, key="net", cost_mult=1.0):
    d = defaultdict(float)
    for k, t in enumerate(res["ts"]):
        v = res["gross"][k] - cost_mult * res["cost"][k] - res["fund"][k] if key == "net" else res[key][k]
        d[t // DAY] += v
    return d


def nw_t(x, lag=10):
    n = len(x)
    if n < 30:
        return None
    m = sum(x) / n
    d = [v - m for v in x]
    g0 = sum(v * v for v in d) / n
    var = g0
    for l in range(1, lag + 1):
        gl = sum(d[i] * d[i - l] for i in range(l, n)) / n
        var += 2 * (1 - l / (lag + 1)) * gl
    if var <= 0:
        return None
    return m / math.sqrt(var / n)


def period_stats(res, a, b, cost_mult=1.0):
    """a<=t<b 的日序列 (bp/日)。含零部位日。"""
    daily = to_daily(res, "net", cost_mult)
    days = list(range(a // DAY, b // DAY))
    x = [daily.get(d, 0.0) for d in days]
    n = len(x)
    if n == 0:
        return {"n_days": 0}
    m = sum(x) / n
    sd = statistics.pstdev(x) if n > 1 else 0.0
    wins = [v for v in x if v > 0]; losses = [v for v in x if v < 0]
    cum = 0.0; peak = 0.0; dd = 0.0
    for v in x:
        cum += v; peak = max(peak, cum); dd = max(dd, peak - cum)
    gs = defaultdict(float); cs = defaultdict(float); fs = defaultdict(float); ts_ = defaultdict(float)
    for k, t in enumerate(res["ts"]):
        if a <= t < b:
            gs[0] += res["gross"][k]; cs[0] += res["cost"][k]; fs[0] += res["fund"][k]; ts_[0] += res["turn"][k]
    t = nw_t(x)
    return {"n_days": n, "mean_bp_day": round(m, 3), "ann_pct": round(m * 365 / 100, 2),
            "sharpe": round(m / sd * math.sqrt(365), 2) if sd > 0 else None,
            "nw_t": round(t, 2) if t is not None else None,
            "win_day_pct": round(100 * len(wins) / n, 1),
            "payoff_avg_win_over_avg_loss": round((sum(wins) / len(wins)) / abs(sum(losses) / len(losses)), 2) if wins and losses else None,
            "maxdd_pct": round(dd / 100, 2),
            "gross_bp_day": round(gs[0] / n, 3), "cost_bp_day": round(cs[0] / n * cost_mult, 3), "fund_bp_day": round(fs[0] / n, 3),
            "turnover_per_year": round(ts_[0] / n * 365, 1)}


def evaluate(res, with_stress=True):
    out = {}
    for p, (a, b) in PERIODS.items():
        out[p] = period_stats(res, a, b)
    if with_stress:
        a, b = PERIODS["validation"]
        out["validation_stress_mean_bp_day"] = {
            "cost_x1.5": period_stats(res, a, b, 1.5)["mean_bp_day"],
            "cost_x1.0": out["validation"]["mean_bp_day"],
            "maker_opt_2bp_side": period_stats(res, a, b, 2.0 / TAKER_SIDE)["mean_bp_day"],
            "cost_0": period_stats(res, a, b, 0.0)["mean_bp_day"]}
    return out


def verdict(ev):
    tr, va = ev["train"], ev["validation"]
    ok_sign = tr.get("mean_bp_day", -1) > 0 and va.get("mean_bp_day", -1) > 0
    t = va.get("nw_t")
    stress = ev["validation_stress_mean_bp_day"]["cost_x1.5"] > 0
    if ok_sign and t is not None and t > Z_BONF and stress:
        return "PASS"
    if ok_sign and t is not None and t > 2.0 and stress:
        return "WEAK_LEAD"
    return "FAIL"


def block_means(res, block_days=91):
    """EV0 起每 91 天一塊的平均淨 bp/日 (描述：看穩定性，不計格、不能用來選格)。"""
    daily = to_daily(res, "net", 1.0)
    d0 = EV0 // DAY; d1 = T_END // DAY
    out = []
    for s in range(d0, d1, block_days):
        e = min(s + block_days, d1)
        x = [daily.get(d, 0.0) for d in range(s, e)]
        out.append(round(sum(x) / len(x), 2))
    return out
