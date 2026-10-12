"""Pure, restartable daily close Donchian detector."""
from decimal import Decimal, InvalidOperation

DAY = 86400000
FIELDS = ("open", "high", "low", "close", "volume")


class TrendDetector:
    def __init__(self, symbol, entry_lookback=20, exit_lookback=10):
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("symbol required")
        if type(entry_lookback) is not int or type(exit_lookback) is not int or min(entry_lookback, exit_lookback) < 1:
            raise ValueError("lookbacks must be positive integers")
        self.symbol = symbol
        self.entry_lookback = entry_lookback
        self.exit_lookback = exit_lookback
        self.resync()

    def resync(self):
        """Explicitly discard history after a gap; caller must replay contiguous bars."""
        self.closes = []
        self.position = 0
        self.last = None
        self.last_bar = None
        self.last_decision = None
        self.gapped = False

    def _decision(self, diagnostic):
        return {"diagnostic": diagnostic, "target_position": self.position}

    def process(self, bar):
        if not isinstance(bar, dict) or bar.get("symbol") != self.symbol:
            return self._decision("reject_symbol")
        try:
            start, end = bar["open_time_ms"], bar["close_time_ms"]
            if type(start) is not int or type(end) is not int:
                return self._decision("reject_time")
            if end - start != DAY - 1 or start % DAY != 0 or end % DAY != DAY - 1:
                return self._decision("reject_time")
            values = tuple(Decimal(str(bar[key])) for key in FIELDS)
            if not all(x.is_finite() for x in values):
                return self._decision("reject_invalid_bar")
            op, high, low, close, volume = values
            if low > min(op, close) or high < max(op, close) or volume < 0:
                return self._decision("reject_invalid_bar")
        except (KeyError, TypeError, ValueError, InvalidOperation):
            return self._decision("reject_invalid_bar")
        canonical = (start, end) + tuple(str(x) for x in values)
        if self.last is not None:
            if end == self.last:
                if canonical == self.last_bar:
                    return dict(self.last_decision)
                return self._decision("reject_duplicate_conflict")
            if end < self.last:
                return self._decision("reject_out_of_order")
            if end != self.last + DAY:
                self.gapped = True
                return self._decision("reject_gap")
        if self.gapped:
            return self._decision("reject_gap")
        if len(self.closes) < self.entry_lookback:
            diagnostic = "warmup"
        elif self.position == 0 and close > max(self.closes[-self.entry_lookback:]):
            self.position = 1
            diagnostic = "entry"
        elif self.position == 1 and close < min(self.closes[-self.exit_lookback:]):
            self.position = 0
            diagnostic = "exit"
        else:
            diagnostic = "hold" if self.position else "no_signal"
        self.closes.append(close)
        self.closes = self.closes[-max(self.entry_lookback, self.exit_lookback):]
        self.last = end
        self.last_bar = canonical
        self.last_decision = self._decision(diagnostic)
        return dict(self.last_decision)

    def export_state(self):
        return {"symbol": self.symbol, "entry_lookback": self.entry_lookback,
                "exit_lookback": self.exit_lookback, "closes": [str(x) for x in self.closes],
                "position": self.position, "last": self.last,
                "last_bar": list(self.last_bar) if self.last_bar is not None else None,
                "last_decision": dict(self.last_decision) if self.last_decision else None,
                "gapped": self.gapped}

    def restore_state(self, state):
        if (state["symbol"], state["entry_lookback"], state["exit_lookback"]) != (
                self.symbol, self.entry_lookback, self.exit_lookback):
            raise ValueError("state configuration mismatch")
        closes = [Decimal(x) for x in state["closes"]]
        if (len(closes) > max(self.entry_lookback, self.exit_lookback)
                or not all(x.is_finite() for x in closes)
                or type(state["position"]) is not int or state["position"] not in (0, 1)):
            raise ValueError("invalid state")
        self.closes = closes
        self.position = state["position"]
        self.last = state["last"]
        self.last_bar = tuple(state["last_bar"]) if state["last_bar"] is not None else None
        self.last_decision = dict(state["last_decision"]) if state["last_decision"] else None
        self.gapped = bool(state["gapped"])
