"""Causal five-minute path-speed filter with amortized sorted trailing window."""
from bisect import bisect_left, bisect_right, insort
from collections import deque
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
        self.bars = list(bars)
        self.ends, self.speeds = [], []
        self.gaps = [0]
        last = None
        close = []
        times = []
        for bar in self.bars:
            ts = bar["ts"]
            if type(ts) is not int or (last is not None and ts <= last):
                raise ValueError("bar timestamps must strictly increase")
            self.gaps.append(self.gaps[-1] + int(last is not None and ts-last != MINUTE))
            last = ts
            price = Decimal(str(bar["close"]))
            if not price.is_finite() or price <= 0:
                raise ValueError("invalid close")
            close.append(price)
            times.append(ts)
        for i in range(len(close)):
            if i < WIN_BARS:
                self.speeds.append(None)
            else:
                self.speeds.append(float(sum(
                    (abs(close[j] / close[j-1] - 1)
                     for j in range(i-4, i+1)), Decimal(0))))
            self.ends.append(times[i] + MINUTE)
        self._right = self._left = 0
        self._ordered = []
        self._previous = None

    def _direct(self, t):
        self._previous = t
        right = bisect_right(self.ends, t)
        left = bisect_left(self.ends, t - LOOK_DAYS * DAY_MS)
        values = [v for v in self.speeds[left:right] if v is not None]
        return self._result(values, right, left)

    def _result(self, values, right, left):
        # Require the complete seven-day minute interval, with enough
        # prior bars to form all five-return speeds without any gaps.
        complete = (right - left == MIN_HIST
                    and right > 0 and self.ends[right-1] == self._previous
                    and self.ends[left] == self._previous - LOOK_DAYS * DAY_MS + MINUTE
                    and self.gaps[right] == self.gaps[left]
                    and len(values) == MIN_HIST)
        if not complete or self.speeds[right-1] is None:
            return False, "H2_WARMUP"
        return ((True, "H2_SKIP") if self.speeds[right - 1] > pct7(values, Q90)
                else (False, "H2_PASS"))

    def skip(self, decision_ts):
        if type(decision_ts) is not int:
            raise ValueError("decision timestamp must be integer milliseconds")
        if self._previous is not None and decision_ts < self._previous:
            return self._direct(decision_ts)
        self._previous = decision_ts
        right = bisect_right(self.ends, decision_ts)
        left = bisect_left(self.ends, decision_ts - LOOK_DAYS * DAY_MS)
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
        return self._result(self._ordered, right, left)
