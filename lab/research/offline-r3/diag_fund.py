import sys, json, os
sys.path.insert(0, '.')
from r3lib import *
U = json.load(open('results/a_universe.json'))
dparse = lambda s: ms(*map(int, s.split('-'))) // DAY
EV0 = dparse(U["ev0_trade_day"]); SPLIT_D = dparse(U["split_a_day"]); A_END_D = dparse(U["a_end_exclusive_day"])
uni = {int(k): v for k, v in U["uni"].items()}
bars = load_1d_all(); d_hi = max(max(b) for b in bars.values())
funds = load_funds(U["symbols_ever_in_top20"])
# 最極端資金費率
rows = []
for s, f in funds.items():
    for t, r in zip(f.t, f.r): rows.append((abs(r), r, s, t))
rows.sort(reverse=True)
import datetime
for a, r, s, t in rows[:8]: print('extreme', s, r, datetime.datetime.fromtimestamp(t/1000, datetime.timezone.utc).date())
print('n rates', len(rows), 'frac |r|>0.01:', sum(1 for x in rows if x[0] > 0.01)/len(rows))
tgt = targets(uni, bars, EV0, d_hi, 30, 1, 'momentum')
# 逐 coin-day 資金費率貢獻 (驗證期)
contrib = {}
for k in range(SPLIT_D, A_END_D):
    for s, w in tgt[k].items():
        if k in bars[s]:
            f = w * funds[s].sum_between(k*DAY, (k+1)*DAY) * 1e4 if s in funds else 0
            contrib[(s, k)] = f
tot = sum(contrib.values()) / (A_END_D - SPLIT_D)
print('validation mean funding cost bp/day', round(tot, 2))
top = sorted(contrib.items(), key=lambda x: x[1])[:8]
for (s, k), f in top: print('most income', s, datetime.datetime.fromtimestamp(k*86400, datetime.timezone.utc).date(), round(f, 1), 'w', tgt[k][s])
bys = {}
for (s, k), f in contrib.items(): bys[s] = bys.get(s, 0) + f
print('by coin (bp/day over validation)', sorted(((round(v/(A_END_D-SPLIT_D),2), s) for s, v in bys.items()))[:6])
# long vs short legs
lg = sum(f for (s,k),f in contrib.items() if tgt[k][s] > 0)/(A_END_D-SPLIT_D); sh = sum(f for (s,k),f in contrib.items() if tgt[k][s] < 0)/(A_END_D-SPLIT_D)
print('fund cost long leg', round(lg,2), 'short leg', round(sh,2))
# 缺資金費率符號的 coin-day
print('missing funding file coin-days', sum(1 for (s,k) in contrib if s not in funds))
