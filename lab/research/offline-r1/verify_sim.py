#!/usr/bin/env python3
"""RESEARCH-R1: 模擬器自檢 (手造 bar 單元測試 + 真資料不變量)。失敗即 assert。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import *

class F:  # 假資金費率
    def __init__(s, items): s.items = items
    def sum_between(s, a, b): return sum(r for t, r in s.items if a < t <= b)

def mk(rows):  # (o,h,l,c,v) -> bars
    return [(i * H, o, h, l, c, v) for i, (o, h, l, c, v) in enumerate(rows)]

# 1) 目標先到: entry=close_i=99, target=close_{i-1}=100, 下一根 high 100.5 -> target, gross=(100/99-1)
b = mk([(100, 100, 100, 100, 1), (100, 100, 98.9, 99, 1), (99, 100.5, 98.9, 99.5, 1)])
t = simulate(b, [1], "BTCUSDT", F([]), "close")[0]
assert t["reason"] == "target" and abs(t["gross"] - (100 / 99 - 1) * 1e4) < 1e-9, t
# 2) 同根止損與目標都觸及 -> 止損優先; target_first 則為目標
b = mk([(100, 100, 100, 100, 1), (100, 100, 98.9, 99, 1), (99, 100.5, 98.0, 99.5, 1)])
t = simulate(b, [1], "BTCUSDT", F([]), "close")[0]
assert t["reason"] == "stop" and abs(t["gross"] - (-0.007) * 1e4) < 1e-6, t
t = simulate(b, [1], "BTCUSDT", F([]), "close", order="target_first")[0]
assert t["reason"] == "target"
# 3) 跳空穿止損以開盤價
b = mk([(100, 100, 100, 100, 1), (100, 100, 98.9, 99, 1), (97, 98, 96, 97.5, 1)])
t = simulate(b, [1], "BTCUSDT", F([]), "close")[0]
assert t["reason"] == "gap_stop" and abs(t["gross"] - (97 / 99 - 1) * 1e4) < 1e-6
# 4) 12 根時間出場 + 資金費率(多單付正費率): 結算在持有期內 0.0001 -> funding=+1bp
rows = [(100, 100, 100, 100, 1), (100, 100, 98.9, 99, 1)] + [(99, 99.5, 98.8, 99.1, 1)] * 14
b = mk(rows)
t = simulate(b, [1], "BTCUSDT", F([(5 * H, 0.0001)]), "close")[0]
assert t["reason"] == "time" and t["t_exit"] == (1 + 12) * H + H and abs(t["funding"] - 1.0) < 1e-9, t
# 5) 持倉期間重疊信號被略過
t2 = simulate(b, [1, 2, 3], "BTCUSDT", F([]), "close")
assert len(t2) == 1
# 6) maker_limit: 下一根最低價沒低於掛價 -> 不成交
b = mk([(100, 100, 100, 100, 1), (100, 100, 99.0, 99.0, 1), (99.2, 100.5, 99.0, 99.5, 1)])
assert simulate(b, [1], "BTCUSDT", F([]), "maker_limit") == []
# 7) 真資料不變量
for s in SYMS:
    bars = load(s); st = roll_stats(bars); fu = Fund(s)
    trs = simulate(bars, w1_signals(bars, st), s, fu, "close")
    for t in trs:
        i = t["i"]
        if t["reason"] == "stop": assert abs(t["gross"] + STOP[s] * 1e4) < 1e-6
        if t["reason"] == "target": assert abs(t["gross"] - (bars[i - 1][4] / bars[i][4] - 1) * 1e4) < 1e-6
        if t["reason"] == "time": assert t["t_exit"] == bars[i + 12][0] + H
        assert t["t_exit"] > t["t_entry"]
    # 一次一單：進出場不重疊
    trs.sort(key=lambda x: x["t_entry"])
    assert all(trs[k]["t_entry"] >= trs[k - 1]["t_exit"] - H for k in range(1, len(trs)))
    print(s, "trades", len(trs), "invariants OK")
print("ALL OK")
