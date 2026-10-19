#!/usr/bin/env python3
"""RESEARCH-R2: 持倉引擎自檢 (手造數字單元測試 + 真資料不變量)。失敗即 assert。需先有 data/ (download2.py)。"""
import os, sys, math, random, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r2lib import *

class F:
    def __init__(s, items): s.items = items
    def sum_between(s, a, b): return sum(r for t, r in s.items if a < t <= b)

# 1) 單幣常數多單：ret=+1% ,+1% ; 進場成本 7bp 一次；資金費率多單付 1bp
ts = [0, DAY, 2 * DAY, 3 * DAY]
O = {"X": [100.0, 101.0, 102.01, 103.0301]}
W = {"X": [1.0, 1.0, 1.0, 0.0]}
Fm = fund_arrays(ts, {"X": F([(DAY + 5, 0.0001)])})
r = engine(ts, O, W, Fm)
assert abs(r["gross"][0] - 100.0) < 1e-6 and abs(r["gross"][1] - 100.0) < 1e-6 and abs(r["gross"][2] - 100.0) < 1e-6
assert abs(r["cost"][0] - 7.0) < 1e-9 and r["cost"][1] == 0.0
assert abs(r["fund"][1] - 1.0) < 1e-9 and r["fund"][0] == 0.0     # 資金費率落在第 2 根 (DAY, 2DAY]
assert abs(r["net"][0] - 93.0) < 1e-6 and abs(r["net"][1] - 99.0) < 1e-6
# 2) 空單：價格漲 1% 虧 100bp；正費率空單收 1bp
W = {"X": [-1.0, -1.0, -1.0, 0.0]}
r = engine(ts, O, W, Fm)
assert abs(r["gross"][0] + 100.0) < 1e-6 and abs(r["fund"][1] + 1.0) < 1e-9 and abs(r["net"][1] - (-100 + 1)) < 1e-6
# 3) 反向換倉：+1 -> -1 換手 2 => 成本 14bp
W = {"X": [1.0, -1.0, -1.0, 0.0]}
r = engine(ts, O, W, Fm)
assert abs(r["cost"][1] - 14.0) < 1e-9
# 4) 成本倍率與日聚合
d = to_daily(r, "net", 2.0)
assert abs(d[0] - (100 - 14)) < 1e-6
# 5) NW t：常數序列加小雜訊 -> t 很大；mean=0 序列 |t| 小；與 iid 手算 t 一致 (lag=0 近似)
random.seed(1)
x = [random.gauss(0.5, 1.0) for _ in range(2000)]
t_nw = nw_t(x, 10); t_iid = (sum(x) / len(x)) / (statistics.pstdev(x) / math.sqrt(len(x)))
assert abs(t_nw - t_iid) / t_iid < 0.15, (t_nw, t_iid)
y = [random.gauss(0, 1.0) for _ in range(2000)]
assert abs(nw_t(y, 10)) < 3.5
# 6) Bonferroni
assert abs(Z_BONF - NormalDist().inv_cdf(1 - 0.05 / 70)) < 1e-12 and 3.18 < Z_BONF < 3.21
print("synthetic OK; Z_BONF", round(Z_BONF, 3))

# 7) 真資料不變量
man = json.load(open(os.path.join(DATA2, "MANIFEST.json")))
for s, m in man["symbols"].items():
    assert m["gaps_n"] == 0 and m["bad_ohlc"] == 0 and m["bars_1d"] >= 1100, (s, m)
    assert m["first"] == T0, s
print("20 coins 1d: no gaps, no bad OHLC, first bar =", T0)
assert man["BTCUSDT_15m"]["gaps_n"] == 0, man["BTCUSDT_15m"]
# 8) 1d klines 與 r1 的 1h 重樣本一致 (四幣)
for s in SYMS:
    d1 = {b[0]: b for b in load_1d(s)}
    h1 = r1.load(s)
    rs = resample(h1, 24)
    bad = 0
    for b in rs:
        x = d1.get(b[0])
        assert x is not None
        if abs(x[1] - b[1]) > 1e-9 or abs(x[4] - b[4]) > 1e-9 or abs(x[2] - b[2]) > 1e-9 or abs(x[3] - b[3]) > 1e-9: bad += 1
    assert bad == 0, (s, bad)
    r4 = resample(h1, 4)
    assert all(r4[i + 1][0] - r4[i][0] == 4 * H for i in range(len(r4) - 1))
    print(s, "1d == resampled-1h(24h) OK; 4h bars", len(r4))
# 9) r2 與 r1 資金費率一致 (四幣)
for s in SYMS:
    a, b = FundF(s), r1.Fund(s)
    assert a.t == b.t and all(abs(x - y) < 1e-15 for x, y in zip(a.r, b.r)), s
print("funding r2 == r1 for 4 majors")
# 10) 買進持有引擎報酬 = 直接相乘 (BTC, 開盤到開盤加總簡單報酬)
bars = load_1d("BTCUSDT"); ts = [b[0] for b in bars]; O = {"B": [b[1] for b in bars]}
fa = fund_arrays(ts, {"B": FundF("BTCUSDT")})
res = engine(ts, O, {"B": [1.0] * len(ts)}, fa)
direct = sum((O["B"][k + 1] / O["B"][k] - 1) * 1e4 for k in range(len(ts) - 1))
assert abs(sum(res["gross"]) - direct) < 1e-6 and abs(res["cost"][0] - 7.0) < 1e-9 and sum(res["cost"]) == 7.0
print("buy&hold engine == direct, total funding bp", round(sum(res["fund"]), 1), "ALL OK")
