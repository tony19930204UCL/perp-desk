"""Causal, closed-minute short entry placement; public fictional inputs only.

Bar ts is UTC millisecond OPEN, duration 60000 ms; closed=True confirms
the completed minute. signal_time is the signal close and j0 open.
"""
from decimal import Decimal, localcontext

from maker_exit_lab import MAKER_FEE, SL_PCT, SLIP, TAKER_FEE, X2Error, number
from accounting import replay

D = Decimal
MINUTE = 60000
ZERO = D("0")


def closed_bar(bar):
    if type(bar) is not dict or not bar.get("closed"):
        raise X2Error("closed minute bar required")
    if type(bar.get("ts")) is not int or bar["ts"] < 0:
        raise X2Error("bar open UTC ms required")
    op = number(bar["open"], "open", True)
    hi = number(bar["high"], "high", True)
    lo = number(bar["low"], "low", True)
    cl = number(bar["close"], "close", True)
    if not (lo <= min(op, cl) <= max(op, cl) <= hi):
        raise X2Error("invalid OHLC")
    return op, hi, lo, cl


def atr14(complete_bars):
    """14 one-minute TRs require 15 complete bars including previous close."""
    if len(complete_bars) < 15:
        raise X2Error("ATR14 needs 15 complete minutes")
    recent = complete_bars[-15:]
    for bar in recent:
        closed_bar(bar)
    if any(b["ts"] + MINUTE != n["ts"]
           for b, n in zip(recent, recent[1:])):
        raise X2Error("ATR14 requires contiguous complete bars")
    with localcontext() as ctx:
        ctx.prec = 50
        total = ZERO
        for previous, bar in zip(recent, recent[1:]):
            _, hi, lo, _ = closed_bar(bar)
            prev_close = number(previous["close"], "prev_close", True)
            total += max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        return total / D("14")


def vwap60(complete_bars):
    """Use actual quote/base volumes, not quote-weighted close."""
    if len(complete_bars) < 60:
        raise X2Error("VWAP60 needs 60 complete minutes")
    recent = complete_bars[-60:]
    quote = ZERO
    base = ZERO
    for i, bar in enumerate(recent):
        closed_bar(bar)
        if i and recent[i - 1]["ts"] + MINUTE != bar["ts"]:
            raise X2Error("VWAP60 needs contiguous bars")
        quote += number(bar["quote_volume"], "quote_volume")
        base += number(bar["base_volume"], "base_volume")
    if quote < ZERO or base <= ZERO:
        raise X2Error("VWAP60 invalid volumes")
    with localcontext() as ctx:
        ctx.prec = 50
        return quote / base


class ShortEntry:
    """One signal, one pending slot, explicit partial fills at exact limit.

    Partial positions retain real ledger events and hard stop while pending;
    no synthetic TIME close. Finalized funding is supplied by the caller
    only after its timestamp is known. Never prefetch the TTL suffix.
    """

    def __init__(self, signal_id, signal_time, high, close, atr, k,
                 ttl, qty, initial_cash="1000", x1_vwap=None,
                 x1_known_funding_cost=None):
        if not isinstance(signal_id, str) or not signal_id:
            raise X2Error("invalid signal id")
        if type(signal_time) is not int or signal_time < 0:
            raise X2Error("signal close timestamp required")
        if k not in (".25", ".50") or type(ttl) is not int or ttl not in (2, 5):
            raise X2Error("frozen k .25/.50 and TTL 2/5 only")
        self.signal_id = signal_id
        self.seen_signals = {signal_id}
        self.signal_time = signal_time
        self.ttl = ttl
        self.qty_target = number(qty, "qty", True)
        self.remaining = self.qty_target
        self.cash_start = number(initial_cash, "cash", True)
        atr_value = number(atr, "ATR14", True)
        high_value = number(high, "signal_high", True)
        close_value = number(close, "signal_close", True)
        with localcontext() as ctx:
            ctx.prec = 50
            self.limit = high_value + max(
                number(k, "k") * atr_value, close_value * D(".0002"))
            impossible = self.limit / close_value - 1 > D(".006")
        self.status = "PLACEMENT_INFEASIBLE" if impossible else "PENDING"
        self.first_fill_time = None
        self.filled = ZERO
        self.basis = ZERO
        self.events = []
        self.ids = set()
        self.settlements = set()
        self.next_ts = signal_time
        self.gap = False
        self.last_close = None
        self.audit = []
        # X1-only pre-entry admission. Unspecified future funding is zero
        # for this decision, but explicitly flagged as unknown.
        if x1_vwap is None and x1_known_funding_cost is not None:
            raise X2Error("X1 known funding requires frozen signal VWAP")
        self.x1_vwap = (None if x1_vwap is None else
                        number(x1_vwap, "signal_frozen_vwap", True))
        self.x1_known_funding_cost = (
            None if x1_known_funding_cost is None else
            number(x1_known_funding_cost, "known_funding_cost"))
        self.unknown_future_funding = (
            self.x1_vwap is not None and x1_known_funding_cost is None)
        self.x1_projected_net = None
        if self.x1_vwap is not None:
            self.x1_projected_net = self._x1_net(self.qty_target)
            if self.x1_vwap >= self.limit or self.x1_projected_net <= ZERO:
                self.status = "X1_ADMISSION_REJECTED"
                self.remaining = ZERO
                self.audit.append(("X1_REJECT", self.x1_projected_net))

    @classmethod
    def for_x1(cls, *args, signal_vwap, known_funding_cost=None, **kwargs):
        """Require a frozen X1 target before creating an entry lifecycle."""
        if signal_vwap is None:
            raise X2Error("X1 signal_vwap target is required before entry")
        return cls(*args, x1_vwap=signal_vwap,
                   x1_known_funding_cost=known_funding_cost, **kwargs)

    def _x1_net(self, proposed_qty):
        """Independent prospective two-Maker-leg X1 net, no future rates."""
        with localcontext() as ctx:
            ctx.prec = 50
            cost = (ZERO if self.x1_known_funding_cost is None
                    else self.x1_known_funding_cost)
            return ((self.limit - self.x1_vwap) * proposed_qty
                    - self.limit * proposed_qty * MAKER_FEE
                    - self.x1_vwap * proposed_qty * MAKER_FEE - cost)

    def submit_again(self, signal_id):
        if signal_id in self.seen_signals:
            raise X2Error("same signal cannot be resubmitted")
        if self.status in ("PENDING", "PARTIAL") or self.filled > ZERO:
            raise X2Error("pending/position occupies single slot")
        raise X2Error("new signals require a new lifecycle instance")

    def _event(self, event_id, ts, kind, **extra):
        if not isinstance(event_id, str) or not event_id or event_id in self.ids:
            raise X2Error("duplicate/invalid event id")
        if self.events and ts < self.events[-1]["ts"]:
            raise X2Error("backwards ledger timestamp")
        event = {"id": event_id, "seq": len(self.events), "ts": ts, "type": kind}
        event.update(extra)
        self.events.append(event)
        self.ids.add(event_id)

    def _risk(self, op, hi, ts):
        if self.filled == ZERO:
            return False
        with localcontext() as ctx:
            ctx.prec = 50
            stop = self.basis / self.filled * (1 + SL_PCT)
            if hi < stop:
                return False
            price = max(op, stop) * (1 + SLIP)
        self._event("entry-risk-" + str(ts), ts, "fill",
                    side="buy", qty=str(self.filled), price=str(price),
                    liquidity="taker")
        self.audit.append(("SL_GAP" if op >= stop else "SL", ts))
        self.status = "CLOSED_SL"
        self.remaining = ZERO
        self.filled = ZERO
        self.basis = ZERO
        return True

    def process(self, bar, fill_qty=None, settlements=()):
        if self.status not in ("PENDING", "PARTIAL"):
            raise X2Error("entry not pending")
        op, hi, lo, cl = closed_bar(bar)
        start = bar["ts"]
        if start < self.signal_time or start < self.next_ts:
            raise X2Error("forming/signal bar or out-of-order minute")
        if start != self.next_ts:
            self.status, self.gap = "GAP", True
            self.audit.append(("GAP", start))
            return self.status
        # Expire BEFORE touching the expired bar; preserve any partial short.
        if start >= self.signal_time + self.ttl * MINUTE:
            self.status = "PARTIAL_OPEN" if self.filled else "EXPIRED"
            return self.status
        end = start + MINUTE
        self.next_ts = end
        self.last_close = cl
        if self._risk(op, hi, end):
            return self.status
        for settlement in settlements:
            if set(settlement) != {"id", "settlement_id", "ts", "mark", "rate"}:
                raise X2Error("funding schema")
            sid = settlement["settlement_id"]
            st = settlement["ts"]
            if type(st) is not int or st > end or st < self.signal_time:
                raise X2Error("unfinalized/out-of-window funding")
            if not isinstance(sid, str) or not sid or sid in self.settlements:
                raise X2Error("duplicate settlement")
            mark = number(settlement["mark"], "mark", True)
            rate = number(settlement["rate"], "rate")
            self._event(settlement["id"], st, "funding",
                        settlement_id=sid, mark=str(mark), rate=str(rate))
            self.settlements.add(sid)
        # Only the activation delivery checks open cross, not resting orders.
        if start == self.signal_time and op >= self.limit:
            self.status = "POST_ONLY_REJECT"
            return self.status
        if fill_qty is not None:
            amount = number(fill_qty, "fill_qty", True)
            if amount > self.remaining:
                raise X2Error("entry overfill")
            if hi < self.limit * (1 + MAKER_FEE):
                raise X2Error("insufficient 2bp entry penetration")
            if self.x1_vwap is not None:
                proposed = self._x1_net(self.filled + amount)
                if proposed <= ZERO:
                    if self.filled:
                        raise X2Error("X1 additional fill would fail net gate")
                    self.status = "X1_ADMISSION_REJECTED"
                    self.remaining = ZERO
                    self.audit.append(("X1_REJECT", proposed))
                    return self.status
                self.x1_projected_net = proposed
            self._event("entry-" + str(end), end, "fill", side="sell",
                        qty=str(amount), price=str(self.limit),
                        liquidity="maker")
            self.filled += amount
            self.basis += amount * self.limit
            self.remaining -= amount
            if self.first_fill_time is None:
                self.first_fill_time = end
            self.status = "FILLED" if self.remaining == ZERO else "PARTIAL"
            # Conservative same-fill bar: immediate hard risk, never TP.
            self._risk(op, hi, end)
        return self.status

    def tail(self):
        if self.status in ("PENDING", "PARTIAL"):
            self.status = "TRUNCATED"
        return self.snapshot()

    def snapshot(self):
        ledger = replay(self.cash_start, self.events)
        return {"status": self.status, "limit": self.limit,
                "remaining_order_qty": self.remaining,
                "open_qty": ledger["remaining_qty"],
                "first_fill_time": self.first_fill_time,
                "pending_slot": self.status in ("PENDING", "PARTIAL")
                or ledger["remaining_qty"] > ZERO,
                "last_close": self.last_close, "gap": self.gap,
                "events": list(self.events), "ledger": ledger,
                "x1_frozen_vwap": self.x1_vwap,
                "x1_projected_net": self.x1_projected_net,
                "unknown_future_funding": self.unknown_future_funding}
