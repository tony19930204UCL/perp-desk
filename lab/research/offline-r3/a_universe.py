#!/usr/bin/env python3
"""RESEARCH-R3 A 階段一: 只用 1d 的『成交額』建 point-in-time 宇宙、EV0、切分日、需下載資金費率的符號集合。不計算任何報酬。
輸出 results/a_universe.json"""
import json, os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from r3lib import *

bars = load_1d_all()
d_lo = min(min(b) for b in bars.values()); d_hi = max(max(b) for b in bars.values())
uni, nel = build_pit(bars, d_lo + WIN - 1, d_hi)
d_min_ev = ms(2020, 4, 1) // DAY
ev0 = next(k for k in range(d_lo + WIN, d_hi + 1) if k >= d_min_ev and nel[k - 1] >= 30)
a_end_d = ms(2026, 10, 18) // DAY
split_d = ev0 + (a_end_d - ev0) // 2
union = sorted({s for k in range(ev0 - 1, d_hi + 1) for s in uni[k]})
dt = lambda d: datetime.datetime.fromtimestamp(d * 86400, datetime.timezone.utc).strftime("%Y-%m-%d")
last_bar = {s: max(bars[s]) for s in union}
delisted = sorted(s for s in union if last_bar[s] < d_hi - 3)
OUT = {"n_symbols_loaded": len(bars), "first_day": dt(d_lo), "last_day": dt(d_hi), "ev0_trade_day": dt(ev0), "split_a_day": dt(split_d),
       "a_end_exclusive_day": dt(a_end_d), "n_symbols_ever_in_top20": len(union), "n_ever_top20_delisted_or_ended_before_last_day": len(delisted),
       "delisted_in_universe": {s: dt(last_bar[s]) for s in delisted}, "symbols_ever_in_top20": union,
       "n_eligible_by_year_end": {dt(ms(y, 12, 31) // DAY): nel.get(ms(y, 12, 31) // DAY) for y in range(2020, 2027) if ms(y, 12, 31) // DAY in nel},
       "uni": {str(k): uni[k] for k in range(ev0 - 1, d_hi + 1)}}
json.dump(OUT, open(os.path.join(ROOT, "results", "a_universe.json"), "w"), ensure_ascii=False)
print({k: v for k, v in OUT.items() if k not in ("uni", "symbols_ever_in_top20", "delisted_in_universe")})
print("delisted sample", dict(list(OUT["delisted_in_universe"].items())[:12]))
