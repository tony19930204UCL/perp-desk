"""Causal closed-minute H2 controls, order invariance and 20-second scale."""
import inspect
import time
import unittest
from path_speed import SpeedFilter, speed5, MIN_HIST
from regime_split import pct7


def bars(n):
    return [{"ts":i*60000,"close":100+(i%19)*.01} for i in range(n)]


class Control(unittest.TestCase):
    def test_speed_formula(self):
        # Kills four-return/off-by-one sums.
        b=[{"ts":i*60000,"close":v}
           for i,v in enumerate((100,101,100,101,100,101))]
        self.assertIsNone(speed5(b,4))
        self.assertAlmostEqual(speed5(b,5),3*.01+2/101)

    def test_signature(self):
        # Kills adjustable H2 thresholds.
        self.assertEqual(list(inspect.signature(SpeedFilter.__init__).parameters),
                         ["self","bars"])
        self.assertEqual(list(inspect.signature(SpeedFilter.skip).parameters),
                         ["self","decision_ts"])

    def test_warmup(self):
        # Kills treating missing/short history as H2_SKIP.
        f=SpeedFilter(bars(100))
        self.assertEqual(f.skip(100*60000),(False,"H2_WARMUP"))

    def test_strict_equal(self):
        # Kills >= instead of > and false exclusion on flat history.
        b=[{"ts":i*60000,"close":100} for i in range(MIN_HIST+10)]
        f=SpeedFilter(b)
        self.assertEqual(f.skip(len(b)*60000),(False,"H2_PASS"))
        burst=b+[{"ts":len(b)*60000,"close":120}]
        self.assertEqual(SpeedFilter(burst).skip(len(burst)*60000),
                         (True,"H2_SKIP"))

    def test_future_and_order(self):
        # Kills formed-bar lookahead and shared state on out-of-order calls.
        b=bars(10200)
        future=b+[{"ts":(10200+i)*60000,"close":200+i} for i in range(50)]
        times=[10020*60000,10100*60000,10200*60000]
        expected=[SpeedFilter(b).skip(t) for t in times]
        self.assertEqual(expected,[SpeedFilter(future).skip(t) for t in times])
        f=SpeedFilter(b)
        self.assertEqual([f.skip(t) for t in times],expected)
        self.assertEqual([f.skip(t) for t in reversed(times)],
                         list(reversed(expected)))
        values=[.01,.03,.05,.09]
        self.assertEqual(pct7(values,.9),.078)

    def test_scale_200k(self):
        # Kills full sort per query: 200k bars, 20k incremental decisions.
        start=time.monotonic()
        f=SpeedFilter(bars(200000))
        base=180000*60000
        for i in range(20000):
            f.skip(base+i*60000)
        self.assertLess(time.monotonic()-start,20)
