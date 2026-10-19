#!/usr/bin/env python3
"""RESEARCH-R3 B 組函式庫：1 分鐘稀有大事件偵測與模擬。純 stdlib。
事件 (PREREG): |r5| > 4*sigma5 且 V5 >= 3*Vbase，只用事件時點以前的資料；冷卻 61 分鐘；下一根 1m 開盤進場，h 分鐘後開盤出場。
"""
import csv, os, sys, math, random, statistics
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "offline-r1"))
import lib as r1
from lib import DAY, H, ms, cluster_t, SPLIT

MIN = 60000
LAG = 43200            # 30 天 * 1440 分
K_SIGMA = 4.0
K_VOL = 3.0
COOL = 61
HORIZONS = (15, 30, 60)
TAKER = 7.0
EV_START = ms(2023, 11, 1)
T_END = ms(2026, 10, 19)


def load_1m(sym):
    t, o, h, l, c, qv = [], [], [], [], [], []
    with open(os.path.join(ROOT, "data", "1m", f"{sym}.csv")) as f:
        rd = csv.reader(f); next(rd)
        for p in rd:
            t.append(int(p[0])); o.append(float(p[1])); h.append(float(p[2])); l.append(float(p[3])); c.append(float(p[4])); qv.append(float(p[6]))
    return t, o, h, l, c, qv


def detect_events(t, o, h, l, c, qv, ev_start=EV_START, k_sigma=K_SIGMA, k_vol=K_VOL, lag=LAG, cool=COOL):
    """回傳事件 list: dict(i, s, r5, sigma, vratio)。i = 決策 bar (用 bar i 收盤資訊)。要求 t 連續 (呼叫端檢查)。"""
    n = len(t)
    r5 = [None] * n
    for i in range(5, n):
        r5[i] = c[i] / c[i - 5] - 1.0
    S1 = [0.0] * (n + 1); S2 = [0.0] * (n + 1); SV = [0.0] * (n + 1)
    for i in range(n):
        x = r5[i] if r5[i] is not None else 0.0
        S1[i + 1] = S1[i] + x; S2[i + 1] = S2[i] + x * x; SV[i + 1] = SV[i] + qv[i]
    ev = []
    last_i = -10 ** 9
    for i in range(5 + lag + 5, n):
        if t[i] < ev_start:
            continue
        if i - last_i < cool:
            continue
        a, b = i - 5 - lag + 1, i - 5          # 樣本 index a..b (含)，結束於 i-5，與當前窗口 (i-4..i) 不重疊
        m = (S1[b + 1] - S1[a]) / lag
        var = (S2[b + 1] - S2[a] - lag * m * m) / (lag - 1)
        if var <= 0:
            continue
        sig = math.sqrt(var)
        x = r5[i]
        if abs(x) <= k_sigma * sig:
            continue
        v5 = SV[i + 1] - SV[i - 4]
        vbase = (SV[b + 1] - SV[a]) / lag * 5.0
        if vbase <= 0 or v5 < k_vol * vbase:
            continue
        ev.append({"i": i, "s": 1 if x > 0 else -1, "r5": x, "sigma": sig, "vratio": v5 / vbase})
        last_i = i
    return ev


def trade(t, o, i, direction, hmin, fund, cost_side=TAKER):
    """於 bar i 之後的下一根 (i+1) 開盤進場，i+1+hmin 開盤出場。direction=+1 多、-1 空。回傳 dict 或 None(缺 bar)。"""
    e, x = i + 1, i + 1 + hmin
    if x >= len(t) or t[x] - t[e] != hmin * MIN:
        return None
    gross = direction * (o[x] / o[e] - 1.0) * 1e4
    f = direction * fund.sum_between(t[e], t[x]) * 1e4          # 多單付正費率
    return {"t_entry": t[e], "t_exit": t[x], "gross": gross, "cost": 2 * cost_side, "funding": f,
            "net": gross - 2 * cost_side - f, "net_2x": gross - 4 * cost_side - f}
