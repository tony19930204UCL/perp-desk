#!/usr/bin/env python3
"""RESEARCH-R1 共用函式庫：資料載入、W1 信號、出場模擬、成本情境、統計。純 stdlib。
所有時間為 UTC 毫秒。bar 以 open_time 標示；bar i 的收盤時間 = open_time+3600000。
"""
import csv, math, os, random, statistics
from bisect import bisect_right
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
STOP = {"ETHUSDT": 0.0100, "BTCUSDT": 0.0070, "SOLUSDT": 0.0115, "XRPUSDT": 0.0140}
H = 3600000
DAY = 86400000
# 固定切分日 (事前登記，見 PREREG.md)
import datetime as _dt
def ms(y, m, d):
    return int(_dt.datetime(y, m, d, tzinfo=_dt.timezone.utc).timestamp() * 1000)
T0 = ms(2023, 10, 1)
SPLIT = ms(2025, 4, 10)        # 前半=定參(train)，後半=驗證
CONTAM = ms(2026, 8, 12)       # 2026-10-11 的 60 天回放窗口起點之後=已被看過，不算乾淨樣本外
T_END = ms(2026, 10, 19)


def load(sym):
    rows = []
    with open(os.path.join(DATA, f"{sym}_1h.csv")) as f:
        r = csv.reader(f); next(r)
        for p in r:
            rows.append((int(p[0]), float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
    return rows  # t,o,h,l,c,v


def load_funding(sym):
    t, r = [], []
    with open(os.path.join(DATA, f"{sym}_funding.csv")) as f:
        rd = csv.reader(f); next(rd)
        for p in rd:
            t.append(int(p[0])); r.append(float(p[1]))
    return t, r


def roll_stats(bars, n=60):
    """每根 i 的前 n 根(不含 i)收盤均值/樣本標準差(ddof=1)/成交量中位數。"""
    c = [b[4] for b in bars]; v = [b[5] for b in bars]
    mean = [None] * len(bars); std = [None] * len(bars); med = [None] * len(bars)
    for i in range(n, len(bars)):
        w = c[i - n:i]
        m = sum(w) / n
        s = math.sqrt(sum((x - m) ** 2 for x in w) / (n - 1))
        mean[i], std[i] = m, s
        med[i] = statistics.median(v[i - n:i])
    return mean, std, med


def w1_signals(bars, stats, z=0.75, vm=1.0):
    mean, std, med = stats
    out = []
    for i in range(60, len(bars)):
        if bars[i][4] < mean[i] - z * std[i] and bars[i][5] >= vm * med[i] and std[i] > 0:
            out.append(i)
    return out


class Fund:
    def __init__(self, sym):
        self.t, self.r = load_funding(sym)
        self.cum = [0.0]
        for x in self.r:
            self.cum.append(self.cum[-1] + x)

    def sum_between(self, t0, t1):
        """t0 < fundingTime <= t1 的費率總和 (多單付正費率)。fundingTime 可能有毫秒偏移，用 <= 即可。"""
        a = bisect_right(self.t, t0); b = bisect_right(self.t, t1)
        return self.cum[b] - self.cum[a]


# 成本情境 (單邊 bp)。taker: 手續費 5bp + 滑價 2bp = 7bp，往返 14bp (與登記檔一致)。
TAKER_SIDE = 7.0
MAKER_SIDE = 2.0


def simulate(bars, sig, sym, fund, scenario="close", hold=12, stop_pct=None, target_mode="prev_close", ret_target=None, order="stop_first"):
    """回傳 trades list。scenario:
    close      : 信號收盤價成交 (全吃單 14bp 往返)  ← 登記檔回放的假設
    next_open  : 下一根開盤成交 (全吃單)，更保守
    maker_opt  : 同 close 的成交但往返只計 4bp (樂觀上界，假設 100% 掛單成交、無逆選擇)
    maker_limit: 在信號收盤價掛限價買單，下一根最低價 < 掛價才成交；止盈用限價(maker)，止損/時間出場吃單
    一次一單。止損先於止盈 (同根不明時保守)。跳空穿過止損以開盤價出場。
    """
    stop_pct = STOP[sym] if stop_pct is None else stop_pct
    trades = []
    free = -1  # 持倉占用到哪根 bar (含)
    n = len(bars)
    for i in sig:
        if i <= free or i + 1 >= n:
            continue
        target = bars[i - 1][4] if target_mode == "prev_close" else ret_target
        if scenario in ("close", "maker_opt"):
            entry = bars[i][4]; first = i + 1; t_entry = bars[i + 1][0]; ecost_maker = False
        elif scenario == "next_open":
            entry = bars[i + 1][1]; first = i + 1; t_entry = bars[i + 1][0]
            if entry >= target:
                continue
        elif scenario == "maker_limit":
            lim = bars[i][4]
            if not (bars[i + 1][3] < lim):
                continue  # 沒成交
            entry = lim; first = i + 1; t_entry = bars[i + 1][0]
        else:
            raise ValueError(scenario)
        if target <= entry:
            continue
        stop = entry * (1 - stop_pct)
        last = min(i + hold, n - 1) if scenario != "next_open" else min(i + hold, n - 1)
        exit_px = None; reason = None; ex = None
        for j in range(first, last + 1):
            o, h, l, c = bars[j][1], bars[j][2], bars[j][3], bars[j][4]
            if scenario == "maker_limit" and j == first:
                o = entry  # 成交發生在本根內，不使用開盤價判斷跳空
            else:
                if o <= stop:
                    exit_px, reason, ex = o, "gap_stop", j; break
                if o >= target:
                    exit_px, reason, ex = o, "gap_target", j; break
            hit = (h > target) if scenario == "maker_limit" else (h >= target)
            if order == "target_first" and hit:
                exit_px, reason, ex = target, "target", j; break
            if l <= stop:
                exit_px, reason, ex = stop, "stop", j; break
            if hit:
                exit_px, reason, ex = target, "target", j; break
        if exit_px is None:
            ex = last; exit_px = bars[last][4]; reason = "time"
        t_exit = bars[ex][0] + H
        gross = (exit_px / entry - 1) * 1e4
        if scenario == "maker_opt":
            cost = 4.0
        elif scenario == "maker_limit":
            cost = MAKER_SIDE + (MAKER_SIDE if reason == "target" else TAKER_SIDE)
        else:
            cost = 2 * TAKER_SIDE
        fcost = fund.sum_between(t_entry, t_exit) * 1e4
        trades.append({"sym": sym, "i": i, "t_entry": t_entry, "t_exit": t_exit, "gross": gross, "cost": cost,
                       "funding": fcost, "net": gross - cost - fcost, "reason": reason})
        free = ex
    return trades


# ---------- 統計 ----------
def cluster_t(trs, key=lambda t: t["t_entry"] // DAY, val="net"):
    """依進場日分群的穩健 t 值 (跨幣同日視為同一群)。"""
    N = len(trs)
    if N < 5:
        return None, 0
    m = sum(t[val] for t in trs) / N
    cl = defaultdict(lambda: [0.0, 0])
    for t in trs:
        a = cl[key(t)]; a[0] += t[val]; a[1] += 1
    V = sum((s - m * k) ** 2 for s, k in cl.values()) / (N * N)
    return (m / math.sqrt(V) if V > 0 else None), len(cl)


def summarize(trs, years=None, ncoins=4):
    N = len(trs)
    if N == 0:
        return {"n": 0}
    net = [t["net"] for t in trs]; gr = [t["gross"] for t in trs]
    mean = sum(net) / N
    sd = statistics.pstdev(net) if N > 1 else 0
    tn = mean / (sd / math.sqrt(N)) if sd > 0 else None
    tc, ncl = cluster_t(trs)
    res = {"n": N, "win_pct": round(100 * sum(1 for x in net if x > 0) / N, 1),
           "gross_mean_bp": round(sum(gr) / N, 2), "cost_mean_bp": round(sum(t["cost"] for t in trs) / N, 2),
           "funding_mean_bp": round(sum(t["funding"] for t in trs) / N, 2),
           "net_mean_bp": round(mean, 2), "net_median_bp": round(statistics.median(net), 2),
           "t_naive": round(tn, 2) if tn is not None else None, "t_cluster_day": round(tc, 2) if tc is not None else None,
           "n_clusters_days": ncl,
           "avg_win_bp": round(statistics.mean([x for x in net if x > 0]), 1) if any(x > 0 for x in net) else None,
           "avg_loss_bp": round(statistics.mean([x for x in net if x <= 0]), 1) if any(x <= 0 for x in net) else None,
           "reasons": {r: sum(1 for t in trs if t["reason"] == r) for r in sorted({t["reason"] for t in trs})}}
    if years:
        tot = sum(net)
        res["annualized_pct_capital4"] = round(tot / ncoins / 1e4 / years * 100, 2)  # 每幣1單位名目、資本=4單位
        res["maxdd_pct_capital4"] = round(max_dd(trs, ncoins) * 100, 2)
    return res


def max_dd(trs, ncoins=4):
    by = defaultdict(float)
    for t in trs:
        by[t["t_exit"] // DAY] += t["net"]
    eq = 0.0; peak = 0.0; dd = 0.0
    for d in sorted(by):
        eq += by[d] / ncoins / 1e4
        peak = max(peak, eq); dd = max(dd, peak - eq)
    return dd


def years_between(a, b):
    return (b - a) / DAY / 365.25


def split(trs, a, b):
    return [t for t in trs if a <= t["t_entry"] < b]


def avg_pair_corr(allbars):
    rets = []
    for s in SYMS:
        c = [b[4] for b in allbars[s]]
        rets.append([c[i] / c[i - 1] - 1 for i in range(1, len(c))])
    cs = []
    for a in range(len(rets)):
        for b in range(a + 1, len(rets)):
            x, y = rets[a], rets[b]
            mx, my = statistics.fmean(x), statistics.fmean(y)
            cov = sum((p - mx) * (q - my) for p, q in zip(x, y))
            cs.append(cov / math.sqrt(sum((p - mx) ** 2 for p in x) * sum((q - my) ** 2 for q in y)))
    return sum(cs) / len(cs)
