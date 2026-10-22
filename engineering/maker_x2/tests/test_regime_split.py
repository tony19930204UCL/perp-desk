"""Synthetic UTC regime boundaries; every control targets a wrong rule."""
import inspect
import unittest
from regime_split import (DAY_MS, MIN_BARS, pct7, classify, regime_of)


def day(d, price, amp="0", count=1000):
    from decimal import Decimal as D
    p, a = D(str(price)), D(str(amp))
    return [{"ts":d*DAY_MS+i*60000,
             "close":str(p + (a if i%2 else 0))}
            for i in range(count)]


class Control(unittest.TestCase):
    def test_percentile(self):
        # Kills wrong percentile type or nearest-rank interpolation.
        self.assertEqual(pct7([1,2,3,4,5],.75),4.0)
        self.assertEqual(pct7([1,3],.75),2.5)
        with self.assertRaises(ValueError):
            pct7([],.75)

    def test_signature(self):
        # Kills hidden tunable strategy thresholds.
        self.assertEqual(list(inspect.signature(classify).parameters),
                         ["bars","start_ms","end_ms"])

    def test_trend_bounds(self):
        # Kills >= at +/-5%, current-day ret and reversed sign.
        for last, expected in [(106,"UP_TREND"),(94,"DOWN_TREND"),
                               (105,"RANGE"),(95,"RANGE")]:
            with self.subTest(last=last):
                bars=day(0,100)+day(7,last)+day(8,100,".001")+day(9,100,"10")
                result=classify(bars,8*DAY_MS,10*DAY_MS)
                self.assertEqual(result[8]["regime"],expected)
                self.assertFalse(result[8]["insufficient_history"])

    def test_high_first(self):
        # Kills trend-before-vol and missing window P75 priority.
        bars=day(0,100)+day(7,108)+day(8,100,"10")+day(9,100,".001")
        result=classify(bars,8*DAY_MS,10*DAY_MS)
        self.assertEqual(result[8]["regime"],"HIGH_VOL")

    def test_missing_unclassified(self):
        # Kills use of missing ret7, <1000 day in P75, output leakage.
        bars=day(7,105)+day(8,100,".001")+day(9,100,"10")
        result=classify(bars,8*DAY_MS,10*DAY_MS)
        self.assertEqual(result[8]["regime"],"RANGE")
        self.assertTrue(result[8]["insufficient_history"])
        self.assertNotIn(7,result)
        self.assertEqual(regime_of(result,6*DAY_MS),"UNCLASSIFIED")
        other=classify(day(8,100,"10",999)+day(9,100,".001"),
                       8*DAY_MS,10*DAY_MS)
        self.assertEqual(other[8]["regime"],"UNCLASSIFIED")
        self.assertIsNone(other[8]["vol"])

    def test_future_history(self):
        # Kills future closes entering the seven-day trend window.
        bars=day(0,100)+day(7,106)+day(8,100,".001")+day(9,100,"10")
        left=classify(bars,8*DAY_MS,10*DAY_MS)
        changed=bars+day(10,10000,"500")
        right=classify(changed,8*DAY_MS,10*DAY_MS)
        self.assertEqual(left[8],right[8])
