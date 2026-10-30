"""Causal five-minute path-speed filter with amortized sorted trailing window."""
from bisect import bisect_left, bisect_right, insort
from collections import deque
from array import array
from decimal import Decimal
from regime_split import pct7

WIN_BARS = 5
LOOK_DAYS = 7
Q90 = 0.90
MIN_HIST = 10080
MINUTE = 60000
DAY_MS = 86400000


def speed5(bars, i):
    if i < WIN_BARS:
        return None
    if i >= len(bars):
        raise IndexError(i)
    total = Decimal(0)
    for j in range(i - WIN_BARS + 1, i + 1):
        now = Decimal(str(bars[j]["close"]))
        prev = Decimal(str(bars[j - 1]["close"]))
        if prev <= 0 or now <= 0:
            raise ValueError("nonpositive close")
        total += abs(now / prev - 1)
    return float(total)


class SpeedFilter:
    def __init__(self, bars):
        # Do not materialize multi-year disk-backed bars or Decimal prices.
        # Keep the original five Decimal divisions and float conversion.
        self.ends = array("q")
        self.speeds = []
        self.gaps = array("I", [0])
        last = None
        close = deque(maxlen=WIN_BARS + 1)
        for i, bar in enumerate(bars):
            ts = bar["ts"]
            if type(ts) is not int or (last is not None and ts <= last):
                raise ValueError("bar timestamps must strictly increase")
            self.gaps.append(self.gaps[-1] + int(
                last is not None and ts-last != MINUTE))
            last = ts
            price = Decimal(str(bar["close"]))
            if not price.is_finite() or price <= 0:
                raise ValueError("invalid close")
            close.append(price)
            if i < WIN_BARS:
                self.speeds.append(None)
            else:
                self.speeds.append(float(sum(
                    (abs(close[j] / close[j-1] - 1)
                     for j in range(1, WIN_BARS+1)), Decimal(0))))
            self.ends.append(ts + MINUTE)
        self._right = self._left = 0
        self._ordered = []
        self._previous = None

    def _direct(self, t):
        # Current close (end == t) is observable, but never in history.
        right = bisect_left(self.ends, t)
        left = bisect_left(self.ends, t - LOOK_DAYS * DAY_MS)
        values = sorted(v for v in self.speeds[left:right] if v is not None)
        cur = bisect_right(self.ends, t) - 1
        return self._result(values, right, left, cur, t)

    def _result(self, values, right, left, cur, t):
        # History is [t-7d, t), excluding a current speed ending at t.
        # Each speed needs five contiguous predecessor minutes.
        complete = (left >= WIN_BARS and right-left == MIN_HIST
                    and self.ends[left] == t - LOOK_DAYS * DAY_MS
                    and self.ends[right-1] == t - MINUTE
                    and self.gaps[right] == self.gaps[left-WIN_BARS+1]
                    and len(values) == MIN_HIST
                    and cur >= 0 and self.speeds[cur] is not None)
        if not complete:
            return False, "H2_WARMUP"
        # values is maintained sorted; never sort all 10080 on each query.
        h = (len(values)-1) * Q90
        lo = int(h)
        hi = min(lo+1, len(values)-1)
        threshold = values[lo] + (values[hi]-values[lo])*(h-lo)
        return ((True, "H2_SKIP") if self.speeds[cur] > threshold
                else (False, "H2_PASS"))

    def skip(self, decision_ts):
        if type(decision_ts) is not int:
            raise ValueError("decision timestamp must be integer milliseconds")
        if self._previous is not None and decision_ts < self._previous:
            return self._direct(decision_ts)
        right = bisect_left(self.ends, decision_ts)
        left = bisect_left(self.ends, decision_ts - LOOK_DAYS * DAY_MS)
        cur = bisect_right(self.ends, decision_ts) - 1
        if self._previous is None:
            self._ordered = sorted(v for v in self.speeds[left:right]
                                   if v is not None)
            self._left, self._right = left, right
        self._previous = decision_ts
        while self._right < right:
            value = self.speeds[self._right]
            if value is not None:
                insort(self._ordered, value)
            self._right += 1
        while self._left < left:
            value = self.speeds[self._left]
            if value is not None:
                at = bisect_left(self._ordered, value)
                self._ordered.pop(at)
            self._left += 1
        return self._result(self._ordered, right, left, cur, decision_ts)
