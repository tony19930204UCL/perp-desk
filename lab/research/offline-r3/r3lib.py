#!/usr/bin/env python3
"""RESEARCH-R3 A 組函式庫：point-in-time 變動宇宙 + 橫截面持倉引擎。沿用 r2lib 的統計 (period_stats / nw_t) 與 r1 的成本。純 stdlib。

約定 (與 PREREG.md 一致):
  日索引 d = open_time // DAY。訊號日 j 收盤後決策，次日 k=j+1 開盤成交，持有到 k+1 開盤。
  PIT 宇宙 uni[j] = 只用 <= j 日資料: 過去 30 日(j-29..j) 日 K 全數存在的符號，依 30 日平均 quote_volume 取前 20。
"""
import csv, glob, json, math, os, sys
from collections import defaultdict
from statistics import NormalDist

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "offline-r2"))
import r2lib                      # 已驗證的 r2 引擎與統計 (同時載入 r1 lib)
from r2lib import nw_t, period_stats, DAY, ms, TAKER_SIDE
from bisect import bisect_right

DATA = os.path.join(ROOT, "data")
N_TESTS = 12
Z_BONF = NormalDist().inv_cdf(1 - 0.05 / (2 * N_TESTS))
T_END = ms(2026, 10, 19)
D_2019 = ms(2019, 1, 1) // DAY
TOPN = 20
WIN = 30
MIN_SCORABLE = 8


def load_1d_all(pattern=None):
    """回傳 {sym: {day: (o,h,l,c,v,qv)}}"""
    out = {}
    for p in sorted(glob.glob(os.path.join(DATA, "1d", "*.csv"))):
        sym = os.path.basename(p)[:-4]
        d = {}
        with open(p) as f:
            r = csv.reader(f); next(r)
            for x in r:
                if not x or not x[0].strip().isdigit():
                    continue
                d[int(x[0]) // DAY] = (float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[6]))
        if d:
            out[sym] = d
    return out


def build_pit(bars, d_lo, d_hi):
    """逐日 PIT 宇宙。回傳 uni[j] (前 TOPN 符號 list，量大到小) 與 n_elig[j]。只用 <= j 日資料。"""
    syms = sorted(bars)
    uni, n_elig = {}, {}
    # 每個符號的前綴和 (縱使缺日也用逐日查找，因為 30 日需全存在)
    for j in range(d_lo, d_hi + 1):
        cand = []
        for s in syms:
            b = bars[s]
            if j not in b or (j - WIN + 1) not in b:
                continue
            tot, ok = 0.0, True
            for q in range(j - WIN + 1, j + 1):
                x = b.get(q)
                if x is None:
                    ok = False; break
                tot += x[5]
            if ok:
                cand.append((tot / WIN, s))
        cand.sort(key=lambda z: (-z[0], z[1]))
        uni[j] = [s for _, s in cand[:TOPN]]
        n_elig[j] = len(cand)
    return uni, n_elig


class Fund:
    def __init__(self, path):
        self.t, self.r = [], []
        with open(path) as f:
            rd = csv.reader(f); next(rd)
            for p in rd:
                self.t.append(int(p[0])); self.r.append(float(p[1]))
        self.cum = [0.0]
        for x in self.r:
            self.cum.append(self.cum[-1] + x)

    def sum_between(self, t0, t1):
        return self.cum[bisect_right(self.t, t1)] - self.cum[bisect_right(self.t, t0)]


def load_funds(syms):
    out = {}
    for s in syms:
        p = os.path.join(DATA, "funding", f"{s}.csv")
        if os.path.exists(p):
            out[s] = Fund(p)
    return out


def targets(uni, bars, d_ev0, d_hi, L, R, mode, k_each=4, half=0.5):
    """回傳 tgt[k] = {sym: weight}，k = 交易日 (>= d_ev0)。mode: 'momentum'|'reversal'|'buyhold'。"""
    tgt = {}
    cur = {}
    for k in range(d_ev0, d_hi + 1):
        j = k - 1
        if mode == "buyhold":
            u = uni.get(j, [])
            cur = {s: 1.0 / len(u) for s in u} if u else {}
        elif (j - D_2019) % R == 0:
            sc = []
            for s in uni.get(j, []):
                b = bars[s]
                if j in b and (j - L) in b:
                    sc.append((b[j][3] / b[j - L][3] - 1.0, s))
            if len(sc) < MIN_SCORABLE:
                cur = {}
            else:
                sc.sort()
                bot = [s for _, s in sc[:k_each]]; top = [s for _, s in sc[-k_each:]]
                sgn = 1.0 if mode == "momentum" else -1.0
                cur = {}
                for s in top: cur[s] = sgn * half / k_each
                for s in bot: cur[s] = -sgn * half / k_each
        tgt[k] = dict(cur)
    return tgt


def run_engine(tgt, bars, funds, d_ev0, d_hi, cost_side=TAKER_SIDE):
    """逐日持倉引擎。回傳 r2lib 格式 res (ts=日起點 ms) + percoin + 診斷。"""
    days = list(range(d_ev0, d_hi))           # k 到 d_hi-1 (k+1 需存在 = d_hi 的開盤；d_hi 為最後一根)
    n = len(days)
    gross = [0.0] * n; cost = [0.0] * n; fund = [0.0] * n; turn = [0.0] * n
    g_long = [0.0] * n; g_short = [0.0] * n
    pc = defaultdict(lambda: defaultdict(float))     # sym -> day -> net bp
    diag = {"missing_bar_coin_days": 0, "last_day_exit_close": 0, "missing_funding_coin_days": 0, "coin_days_held": 0,
            "max_short_leg_single_coin_day_up_pct": 0.0, "max_short_leg_single_coin_day_sym": None, "ncoins_held_per_day": []}
    prev = {}
    for i, k in enumerate(days):
        w_t = tgt.get(k, {})
        w_eff = {}
        for s, w in w_t.items():
            if w == 0.0:
                continue
            b = bars[s]
            if k not in b:
                diag["missing_bar_coin_days"] += 1
                continue                          # 無法成交: 該日權重 0
            w_eff[s] = w
        t0 = k * DAY; t1 = (k + 1) * DAY
        syms = set(w_eff) | set(prev)
        held = 0
        for s in syms:
            w = w_eff.get(s, 0.0); wp = prev.get(s, 0.0)
            tn = abs(w - wp)
            c = tn * cost_side
            g = 0.0; f = 0.0
            if w != 0.0:
                held += 1
                b = bars[s]
                o = b[k][0]
                if (k + 1) in b:
                    r = b[k + 1][0] / o - 1.0
                else:
                    r = b[k][3] / o - 1.0
                    diag["last_day_exit_close"] += 1
                g = w * r * 1e4
                if s in funds:
                    f = w * funds[s].sum_between(t0, t1) * 1e4
                else:
                    diag["missing_funding_coin_days"] += 1
                if w < 0:
                    g_short[i] += g
                    if r * 100 > diag["max_short_leg_single_coin_day_up_pct"]:
                        diag["max_short_leg_single_coin_day_up_pct"] = round(r * 100, 2); diag["max_short_leg_single_coin_day_sym"] = f"{s}@{k}"
                else:
                    g_long[i] += g
            gross[i] += g; cost[i] += c; fund[i] += f; turn[i] += tn
            if g or c or f:
                pc[s][k] += g - c - f
        diag["coin_days_held"] += held
        diag["ncoins_held_per_day"].append(held)
        prev = w_eff
    net = [gross[i] - cost[i] - fund[i] for i in range(n)]
    res = {"ts": [k * DAY for k in days], "gross": gross, "cost": cost, "fund": fund, "net": net, "turn": turn,
           "g_long": g_long, "g_short": g_short}
    diag["ncoins_held_mean"] = round(sum(diag["ncoins_held_per_day"]) / max(1, n), 2)
    del diag["ncoins_held_per_day"]
    return res, pc, diag


def mean_between(x, days, a_d, b_d):
    v = [x[i] for i, d in enumerate(days) if a_d <= d < b_d]
    return sum(v) / len(v) if v else None


def net_series_ex(res, pc, sym):
    """去掉單一幣的日淨序列 (該幣的毛/成本/資金費率損益全數移除)。"""
    days = [t // DAY for t in res["ts"]]
    return [res["net"][i] - pc[sym].get(d, 0.0) for i, d in enumerate(days)]


def window_stats(x, days, a_d, b_d):
    v = [x[i] for i, d in enumerate(days) if a_d <= d < b_d]
    n = len(v)
    if n == 0:
        return {"n_days": 0}
    m = sum(v) / n
    t = nw_t(v)
    return {"n_days": n, "mean_bp_day": round(m, 3), "nw_t": round(t, 2) if t is not None else None}
