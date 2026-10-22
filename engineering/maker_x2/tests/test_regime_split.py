"""Causal UTC regime tests: incorrect lookahead and warmup must fail."""
import inspect
import unittest
from regime_split import DAY_MS, pct7, classify, regime_of


def day(d, price, amp="1", n=1000):
    from decimal import Decimal as D
    p, a = D(str(price)), D(str(amp))
    return [{"ts":d*DAY_MS+i*60000,
             "close":str(p+(a if i%2 else 0))} for i in range(n)]


def history(last=106, prior="1", tail="0"):
    bars=[]
    for d in range(31):
        price=100 if d != 29 else last
        amp=tail if d==29 else prior
        bars.extend(day(d,price,amp))
    return bars


class Control(unittest.TestCase):
    def test_percentile(self):
        # Detects nearest-rank in place of Type-7.
        self.assertEqual(pct7([1,2,3,4,5],.75),4.)
        self.assertEqual(pct7([1,3],.75),2.5)
        with self.assertRaises(ValueError):
            pct7([],.75)

    def test_signature(self):
        # Detects hidden thresholds in API.
        self.assertEqual(list(inspect.signature(classify).parameters),
                         ["bars","start_ms","end_ms"])

    def test_trend_bounds(self):
        # Detects inclusive trend thresholds or current-day returns.
        for last, label in [(106,"UP_TREND"),(94,"DOWN_TREND"),
                            (105,"RANGE"),(95,"RANGE")]:
            with self.subTest(last=last):
                bars=history(last)+day(31,100,"1")
                result=classify(bars,30*DAY_MS,32*DAY_MS)
                # Use actual d-8=22 and d-1=29 for day 30.
                self.assertEqual(result[30]["regime"],label)

    def test_high_first(self):
        # Detects trend priority above previous-day volatility.
        bars=history(108,".001","20")
        result=classify(bars,30*DAY_MS,31*DAY_MS)
        self.assertEqual(result[30]["regime"],"HIGH_VOL")

    def test_unknown_window(self):
        # Detects RANGE substitution for missing 30-day history.
        bars=day(28,100)+day(29,100)+day(30,100)
        result=classify(bars,30*DAY_MS,31*DAY_MS)
        self.assertEqual(result[30]["regime"],"UNKNOWN")
        self.assertTrue(result[30]["insufficient_history"])
        self.assertEqual(regime_of(result,27*DAY_MS),"UNCLASSIFIED")
        short=history()+day(31,100)
        # Day29 lacks >=1000 bars: no P75 sample or known state.
        short=[b for b in short if not (b["ts"]//DAY_MS==29
                                      and b["ts"]%DAY_MS>=999*60000)]
        self.assertEqual(classify(short,30*DAY_MS,31*DAY_MS)[30]["regime"],
                         "UNKNOWN")

    def test_future_invariance(self):
        # Detects using day d or later in regime decision.
        bars=history(106)+day(31,100,"2")
        old=classify(bars,30*DAY_MS,32*DAY_MS)[30]
        changed=bars+day(32,10000,"500")
        self.assertEqual(old,classify(changed,30*DAY_MS,32*DAY_MS)[30])
        changed=[dict(v) for v in bars]
        for v in changed:
            if v["ts"]//DAY_MS==30:
                v["close"]="9999"
        self.assertEqual(old,classify(changed,30*DAY_MS,32*DAY_MS)[30])
