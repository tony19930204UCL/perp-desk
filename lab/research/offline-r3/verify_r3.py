#!/usr/bin/env python3
"""RESEARCH-R3 引擎自檢 (在看任何策略結果之前跑)。失敗即 assert。
B: 手造 1m 序列的事件/進出場/冷卻/資金費率；無偷看(截斷未來事件相同)；真資料 1m->1h 與 r1 逐根相同；時間連續。
A: 手造 PIT 宇宙(新幣/下架幣/30日均額)；r3 引擎與已驗證 r2lib.engine 差分相同；無偷看(截斷未來宇宙與目標相同)；與 r2 1d 資料逐根相同。
"""
import os, sys, json, random, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import r3b
from r3b import *
import r3lib
from r3lib import *

# ---------------- B ----------------
class FStub:
    def __init__(s, items): s.items = items
    def sum_between(s, a, b): return sum(r for t, r in s.items if a < t <= b)

def toy(n=800, seed=7):
    rnd = random.Random(seed); p = 100.0; rows = []
    for i in range(n):
        o = p; p = p * (1 + rnd.gauss(0, 0.0003)); c = p
        rows.append([i * MIN, o, max(o, c) * 1.0001, min(o, c) * 0.9999, c, 1000.0 + rnd.random() * 100])
    return rows

def cols(rows):
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows]

rows = toy()
# 在 bar 500 注入 -3% 急跌 + 20 倍量 (open 不變, close 下跌); bar 501 open = bar 500 close
def inject(rows, i, drop, vmult):
    r = rows[i]; newc = r[1] * (1 + drop); r[4] = newc; r[3] = min(r[3], newc); r[5] *= vmult
    for j in range(i + 1, len(rows)):               # 後續價格整體平移
        k = rows[j]; f = newc / rows[i + 1][1] if j == i + 1 else None
    scale = newc / rows[i + 1][1]
    for j in range(i + 1, len(rows)):
        for q in (1, 2, 3, 4): rows[j][q] *= scale
inject(rows, 500, -0.03, 20)
t, o, h, l, c, qv = cols(rows)
ev = detect_events(t, o, h, l, c, qv, ev_start=0, lag=200)
assert [e["i"] for e in ev] == [500] and ev[0]["s"] == -1, ev
assert ev[0]["vratio"] > 3 and abs(ev[0]["r5"]) > 4 * ev[0]["sigma"]
# 進出場: fade (做多) 15 分鐘: 進場 = open[501], 出場 = open[516]
tr = trade(t, o, 500, +1, 15, FStub([]))
assert tr["t_entry"] == t[501] and tr["t_exit"] == t[516]
assert abs(tr["gross"] - (o[516] / o[501] - 1) * 1e4) < 1e-9 and tr["cost"] == 14.0 and abs(tr["net"] - (tr["gross"] - 14)) < 1e-9
assert abs(tr["net_2x"] - (tr["gross"] - 28)) < 1e-9
# follow (做空) = 毛報酬相反
tr2 = trade(t, o, 500, -1, 15, FStub([]))
assert abs(tr2["gross"] + tr["gross"]) < 1e-9
# 資金費率: 持有期 (t_entry, t_exit] 內 +0.0001 結算, 多單付 1bp; 空單收 1bp; 邊界 t_entry 不算、t_exit 算
f = FStub([(t[501], 0.5), (t[510], 0.0001), (t[516], 0.0002)])
trf = trade(t, o, 500, +1, 15, f)
assert abs(trf["funding"] - (0.0001 + 0.0002) * 1e4) < 1e-9
assert abs(trade(t, o, 500, -1, 15, f)["funding"] + (0.0003) * 1e4) < 1e-9
# 缺 bar / 超出尾端 -> None
assert trade(t, o, len(t) - 5, +1, 15, FStub([])) is None
tg = t[:]; tg[516] += MIN                           # 出場 bar 時間不連續
assert trade(tg, o, 500, +1, 15, FStub([])) is None
# 冷卻: 第二次大跌在 30 分鐘後 -> 不另計；在 70 分鐘後 -> 計
rows2 = toy(); inject(rows2, 500, -0.03, 20); inject(rows2, 530, -0.03, 20); inject(rows2, 575, 0.03, 20)
ev2 = detect_events(*cols(rows2), ev_start=0, lag=200)
assert [e["i"] for e in ev2] == [500, 575], [e["i"] for e in ev2]
# 量能門檻: 無量放大不觸發
rows3 = toy(); inject(rows3, 500, -0.03, 1)
assert detect_events(*cols(rows3), ev_start=0, lag=200) == []
# 無偷看: 截斷在 i 之後 -> 事件集相同 (只看 <= 截斷點)
rows4 = toy(1500, seed=11)
for k in (400, 700, 1000): inject(rows4, k, -0.025 if k % 2 else 0.025, 15)
T = cols(rows4)
full = detect_events(*T, ev_start=0, lag=200)
for cut in (450, 750, 1100):
    part = detect_events(*[x[:cut + 1] for x in T], ev_start=0, lag=200)
    assert part == [e for e in full if e["i"] <= cut], (cut, part, full)
print("B toy tests OK; toy events", [e["i"] for e in full])

# 真資料: 連續、1m->1h 與 r1 逐根相同、事件無偷看
for s in SYMS_B if False else ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
    T = load_1m(s)
    t = T[0]
    assert len(t) == 1604160 and all(t[i + 1] - t[i] == MIN for i in range(len(t) - 1)), s
    if s == "BTCUSDT":
        b1 = r1.load(s)                                  # (t,o,h,l,c,v)
        import csv
        r1q = {}
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "offline-r1", "data", f"{s}_1h.csv")) as fh:
            rd = csv.reader(fh); next(rd)
            for p in rd: r1q[int(p[0])] = (float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[6]))
        n_ok = 0
        for k in range(0, len(t) - 60, 60 * 97):                       # 抽樣每 97 小時
            hh = t[k] - t[k] % H
            if hh not in r1q or t[k] != hh: continue
            seg = slice(k, k + 60)
            agg = (T[1][k], max(T[2][seg]), min(T[3][seg]), T[4][k + 59], sum(T[5][seg]))
            ref = r1q[hh]
            assert all(abs(a - b) <= 1e-9 * max(1, abs(b)) + 1e-6 for a, b in zip(agg[:4], ref[:4])), (hh, agg, ref)
            assert abs(agg[4] - ref[4]) <= 1e-6 * ref[4], (hh, agg, ref)
            n_ok += 1
        assert n_ok > 100
        print("BTC 1m->1h 與 r1 1h 抽樣", n_ok, "根 OHLC/成交額 逐根相同")
        full = detect_events(*T)
        cut = t.index(ms(2025, 4, 10))
        part = detect_events(*[x[:cut + 1] for x in T])
        assert part == [e for e in full if e["i"] <= cut], "real-data lookahead"
        print("BTC 真資料事件數", len(full), "截斷未來後前段事件逐筆相同", len(part))
print("B real-data tests OK")
if len(sys.argv) > 1 and sys.argv[1] == "B":
    print("B-only run done"); sys.exit(0)

# ---------------- A ----------------
def mkbars(daystart, vals):             # vals: list of (o,h,l,c,v,qv)
    return {daystart + i: v for i, v in enumerate(vals)}

# 手造 PIT: X 上市滿 30 日(日均額 100)、Y 只 29 日歷史(日均額 1e6 但不合格)、Z 下架(最後一日 d=40) 仍在其下架前 PIT 內
def flat(n, qv, px=10.0): return [(px, px, px, px, 1.0, qv)] * n
bars = {"XUSDT": mkbars(0, flat(80, 100.0)), "YUSDT": mkbars(51, flat(29, 1e6)), "ZUSDT": mkbars(0, flat(41, 500.0)), "WUSDT": mkbars(0, flat(80, 50.0))}
uni, nel = build_pit(bars, 29, 79)
assert uni[29] == ["ZUSDT", "XUSDT", "WUSDT"], uni[29]               # 日 29: 三檔皆有 30 日
assert uni[40] == ["ZUSDT", "XUSDT", "WUSDT"]                          # 下架當日仍在名單
assert uni[41] == ["XUSDT", "WUSDT"] and nel[41] == 2                  # Z 缺 d=41 K 線 -> 不在名單
assert "YUSDT" not in uni[79] and "YUSDT" not in uni[75]               # Y 日 79 只有 29 根 (51..79) -> 日 79 不合格
assert uni[79][0] == "XUSDT"
bars["YUSDT"].update(mkbars(80, flat(1, 1e6)))                         # Y 補到 30 根 -> 日 80 合格
u80, _ = build_pit(bars, 80, 80); assert u80[80][0] == "YUSDT"
# 30 日均額是平均不是最後一日: X 最後一日爆量不改變日排序以前的資料 (日 j 只用 <=j)
b2 = {k: dict(v) for k, v in bars.items()}; b2["XUSDT"][79] = (10, 10, 10, 10, 1, 1e9)
u_a, _ = build_pit(bars, 60, 78); u_b, _ = build_pit(b2, 60, 78); assert u_a == u_b                    # 未來日爆量不影響過去
print("A PIT toy tests OK")

# 成本/換倉/缺 bar/下架日 toy
tb = {"AUSDT": mkbars(0, [(10, 10, 10, 10, 1, 1)] * 5), "BUSDT": mkbars(0, [(10, 11, 9, 10, 1, 1), (20, 20, 20, 20, 1, 1), (20, 20, 20, 20, 1, 1)])}
tgt = {1: {"AUSDT": 0.5, "BUSDT": -0.5}, 2: {"AUSDT": -0.5, "BUSDT": 0.5}, 3: {"AUSDT": 0.5}}
res, pc, dg = run_engine(tgt, tb, {}, 1, 4)
# 日 1: B 的日 1 開盤 20 -> 日 2 開盤 20: 報酬 0；A 持平 -> gross 0；建倉成本 (0.5+0.5)*7=7
assert abs(res["cost"][0] - 7.0) < 1e-9 and abs(res["gross"][0]) < 1e-9
# 日 2: 翻倉 A:+0.5->-0.5, B:-0.5->+0.5 -> 換手 2.0 -> 成本 14; B 無日 3 K 線? B 有日 0,1,2 -> 日 2 存在、日 3 不存在 -> 以 close_2 出場 (20/20-1=0)
assert abs(res["cost"][1] - 14.0) < 1e-9, res["cost"]
assert dg["last_day_exit_close"] >= 1
# 日 3: B 沒有 K 線 (目標已無 B)；舊倉 B(+0.5) 被視為無法持有 -> 平倉成本 0.5*7 ; A 由 -0.5 -> +0.5 換手 1.0 -> 7 ; 合計 10.5
assert abs(res["cost"][2] - 10.5) < 1e-9, res["cost"]
# 缺 bar: 目標含 B 但日 k 沒有 B 的 K 線 -> 不持有、記缺口
tgt2 = {3: {"AUSDT": 0.5, "BUSDT": -0.5}}
_, _, dg2 = run_engine(tgt2, tb, {}, 3, 4)
assert dg2["missing_bar_coin_days"] == 1
# 資金費率: 多單付正費率 +1bp/日 -> fund = +0.5*1bp
res3, _, _ = run_engine({1: {"AUSDT": 0.5}}, tb, {"AUSDT": FStub([(1.5 * DAY, 0.0001)])}, 1, 3) if False else (None, None, None)
class FD:                                # FD.sum_between 吃 ms
    def sum_between(s, a, b): return 0.0001 if a < int(1.5 * DAY) <= b else 0.0
res3, _, _ = run_engine({1: {"AUSDT": 0.5}}, tb, {"AUSDT": FD()}, 1, 3)
assert abs(res3["fund"][0] - 0.5 * 1.0) < 1e-9, res3["fund"]
print("A engine toy tests OK")

# 真資料: 與 r2lib.engine 差分 + 與 r2 1d 資料逐根相同
bars_all = load_1d_all()
R2D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "offline-r2", "data")
n_cmp = 0
for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"]:
    r2 = r2lib.load_1d(s)
    for (tt, oo, hh, ll, cc, vv) in r2:
        x = bars_all[s][tt // DAY]
        assert (x[0], x[1], x[2], x[3], x[4]) == (oo, hh, ll, cc, vv), (s, tt)
        n_cmp += 1
print("與 r2 1d 資料逐根相同", n_cmp)
syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LINKUSDT"]
d_lo = max(min(bars_all[s]) for s in syms); d_hi = min(max(bars_all[s]) for s in syms)
d_ev0 = ms(2024, 1, 15) // DAY
rnd = random.Random(3)
tg3 = {}
cur = {}
for k in range(d_ev0, d_hi + 1):
    if (k - d_ev0) % 3 == 0:
        ws = [rnd.choice([-0.25, 0.0, 0.1, 0.25]) for _ in syms]; cur = dict(zip(syms, ws))
    tg3[k] = dict(cur)
funds_r2 = {s: r2lib.FundF(s) for s in syms}
res_r3, pc3, _ = run_engine(tg3, bars_all, funds_r2, d_ev0, d_hi)
ts = [(k) * DAY for k in range(d_ev0, d_hi + 1)]
O = {s: [bars_all[s][k][0] for k in range(d_ev0, d_hi + 1)] for s in syms}
W = {s: [tg3[k].get(s, 0.0) for k in range(d_ev0, d_hi + 1)] for s in syms}
F = r2lib.fund_arrays(ts, funds_r2)
res_r2 = r2lib.engine(ts, O, W, F)
assert len(res_r2["net"]) == len(res_r3["net"])
for key in ("gross", "cost", "fund", "net", "turn"):
    assert max(abs(a - b) for a, b in zip(res_r2[key], res_r3[key])) < 1e-9, key
# 每幣損益加總 = 總 net
tot = [sum(pc3[s].get(d // DAY, 0.0) for s in syms) for d in res_r3["ts"]]
assert max(abs(a - b) for a, b in zip(tot, res_r3["net"])) < 1e-9
print("A 引擎與 r2lib.engine 在真資料(6 幣, 隨機權重)逐日 gross/cost/fund/net/turn 差分 < 1e-9，每幣損益加總一致")

# 無偷看: 截斷 j 之後的所有資料 -> uni[j]、目標不變 (抽樣 40 日, 以全 900 符號資料)
d0 = ms(2021, 6, 1) // DAY; d1 = ms(2026, 9, 1) // DAY
sample = sorted(random.Random(5).sample(range(d0, d1), 25))
uni_full, _ = build_pit(bars_all, sample[0], sample[-1])
for j in sample:
    trunc = {s: {d: v for d, v in b.items() if d <= j} for s, b in bars_all.items()}
    u_t, _ = build_pit(trunc, j, j)
    assert u_t[j] == uni_full[j], j
    tf = targets(uni_full, bars_all, j + 1, j + 1, 7, 1, "momentum")
    tt_ = targets(u_t, trunc, j + 1, j + 1, 7, 1, "momentum")
    assert tf == tt_, j
print("A PIT 宇宙與目標權重在截斷未來資料後逐日相同 (無偷看)", len(sample))
print("Z_BONF", round(Z_BONF, 4), "N_TESTS", N_TESTS)
assert abs(Z_BONF - 2.8653) < 1e-3
print("ALL OK")
