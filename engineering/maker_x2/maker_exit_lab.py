"""Small, offline, short-only X2 exit state; no strategy or live adapter.

Bar semantics: confirmed settlement events are supplied only when the bar
finalizes, in caller-provided timestamp order, before any end-of-bar passive
exit. Intra-bar risk takes precedence over Maker exits. The risk fill is an
OHLC conservative proxy, not tick ordering or guaranteed stop protection.
"""
from decimal import Decimal, InvalidOperation, localcontext

D = Decimal
MAKER_FEE = D("0.0002")
TAKER_FEE = D("0.0005")
SLIP = D("0.0002")
SL_PCT = D("0.006")
MAX_SL = D("0.0085")
TP1_PCT = D("0.0045")
TP2_PCT = D("0.0090")
ZERO = D("0")


class X2Error(ValueError):
    """Malformed transition or an unavailable passive/risk execution."""


def number(value, name, positive=False):
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise X2Error(name + ": non-decimal input")
    try:
        result = D(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise X2Error(name + ": invalid decimal") from exc
    if not result.is_finite() or (positive and result <= ZERO):
        raise X2Error(name + ": nonfinite/nonpositive value")
    return result


def integer(value, name):
    if type(value) is not int or value < 0:
        raise X2Error(name + ": nonnegative integer required")
    return value


class MakerX2:
    """Entry partial fills -> armed TP halves -> risk/TP exits or open.

    All generated ledger events have the exact accounting.replay schema.
    Inputs are fictional fills, finalized settlements and OHLC bars; nothing
    reads an exchange or makes a predictive fill from an unconfirmed order.
    """

    def __init__(self, initial_cash="1000", sl_pct="0.006"):
        self.initial_cash = number(initial_cash, "initial_cash")
        if self.initial_cash < ZERO:
            raise X2Error("negative initial cash")
        candidate = number(sl_pct, "sl_pct", True)
        if candidate >= MAX_SL or candidate != SL_PCT:
            raise X2Error("frozen SL must be 0.006 and strictly below 0.0085")
        self.phase = "ENTRY"
        self.events = []
        self.audit = []
        self.seen = set()
        self.settled = set()
        self.last_ts = None
        self.last_bar = -1
        self.entry_bar = -1
        self.entry_qty = ZERO
        self.entry_basis = ZERO
        self.qty = ZERO
        self.E = None
        self.original_stop = None
        self.stop = None
        self.orders = {}
        self.targets = {}
        self.first_tp_bar = None
        self.realized_gross = ZERO
        self.fees_paid = ZERO
        self.funding_cost = ZERO

    def _claim(self, event_id, ts):
        if not isinstance(event_id, str) or not event_id or event_id in self.seen:
            raise X2Error("duplicate or invalid event id")
        integer(ts, "timestamp")
        if self.last_ts is not None and ts < self.last_ts:
            raise X2Error("backwards event time")
        self.seen.add(event_id)
        self.last_ts = ts

    def _emit(self, event_id, ts, kind, **fields):
        event = {"id": event_id, "seq": len(self.events),
                 "ts": ts, "type": kind}
        event.update(fields)
        self.events.append(event)

    def _fee(self, qty, price, liquidity):
        return qty * price * (MAKER_FEE if liquidity == "maker" else TAKER_FEE)

    def entry_fill(self, event_id, ts, bar_index, qty, price):
        if self.phase != "ENTRY":
            raise X2Error("entry after arming")
        integer(bar_index, "bar_index")
        if bar_index < self.entry_bar:
            raise X2Error("entry bar moved backwards")
        amount = number(qty, "qty", True)
        px = number(price, "price", True)
        self._claim(event_id, ts)
        self.entry_bar = bar_index
        self.entry_qty += amount
        self.qty += amount
        self.entry_basis += amount * px
        self.fees_paid += self._fee(amount, px, "maker")
        self._emit(event_id, ts, "fill", side="sell", qty=str(amount),
                   price=str(px), liquidity="maker")

    def arm(self):
        if self.phase != "ENTRY" or self.qty <= ZERO:
            raise X2Error("cannot arm without entry fills")
        with localcontext() as ctx:
            ctx.prec = 50
            self.E = self.entry_basis / self.entry_qty
            self.original_stop = self.E * (1 + SL_PCT)
            self.stop = self.original_stop
            self.targets = {
                "tp1": self.E * (1 - TP1_PCT),
                "tp2": self.E * (1 - TP2_PCT),
            }
        self.orders = {"tp1": self.entry_qty / 2,
                       "tp2": self.entry_qty / 2}
        self.phase = "OPEN"

    def _exit(self, event_id, ts, qty, price, liquidity, reason):
        if self.phase != "OPEN" or qty <= ZERO or qty > self.qty:
            raise X2Error("exit exceeds remaining position")
        self._claim(event_id, ts)
        self.realized_gross += (self.E - price) * qty
        self.fees_paid += self._fee(qty, price, liquidity)
        self.qty -= qty
        self._emit(event_id, ts, "fill", side="buy", qty=str(qty),
                   price=str(price), liquidity=liquidity)
        self.audit.append(("exit", reason, event_id))
        if self.qty == ZERO:
            self.phase = "CLOSED"
            self.orders = {"tp1": ZERO, "tp2": ZERO}

    def cancel(self, order, event_id, ts):
        if self.phase != "OPEN" or order not in self.orders:
            raise X2Error("cancel after close/unknown order")
        if self.orders[order] <= ZERO:
            raise X2Error("order already exhausted or cancelled")
        self._claim(event_id, ts)
        self.orders[order] = ZERO
        self.audit.append(("cancel", order, event_id))

    def _funding_qty(self):
        return -self.qty

    def _settle(self, settlement):
        if set(settlement) != {"id", "settlement_id", "ts", "mark", "rate"}:
            raise X2Error("funding event fields")
        sid = settlement["settlement_id"]
        if not isinstance(sid, str) or not sid or sid in self.settled:
            raise X2Error("duplicate/invalid settlement")
        mark = number(settlement["mark"], "mark", True)
        rate = number(settlement["rate"], "rate")
        self._claim(settlement["id"], settlement["ts"])
        cost = self._funding_qty() * mark * rate
        self.funding_cost += cost
        self.settled.add(sid)
        self._emit(settlement["id"], settlement["ts"], "funding",
                   settlement_id=sid, mark=str(mark), rate=str(rate))

    def _be_eligible(self, bar_index):
        return self.first_tp_bar is not None and bar_index > self.first_tp_bar

    def _promote_be(self, bar_index):
        if not self._be_eligible(bar_index) or self.qty <= ZERO:
            return
        with localcontext() as ctx:
            ctx.prec = 50
            R = self.realized_gross - self.fees_paid - self.funding_cost
            reference = (R + self.qty * self.E) / (
                self.qty * (1 + SLIP) * (1 + TAKER_FEE))
        if reference <= ZERO:
            self.audit.append(("be_unavailable", bar_index, str(reference)))
            return
        self.stop = min(self.original_stop, self.stop, reference)

    def on_bar(self, bar_index, ts, open_price, high, low, close,
               settlements=(), fills=()):
        """Resolve old risk, then confirmed funding and actual passive fills.

        Open-gap SL uses max(open, existing stop) plus adverse taker slip.
        The first TP's bar cannot activate an economic BE retroactively.
        """
        if self.phase != "OPEN":
            raise X2Error("bar requires open position")
        integer(bar_index, "bar_index")
        integer(ts, "timestamp")
        if bar_index <= self.last_bar or bar_index < self.entry_bar:
            raise X2Error("bar order")
        if self.last_ts is not None and ts < self.last_ts:
            raise X2Error("bar before known events")
        op = number(open_price, "open", True)
        hi = number(high, "high", True)
        lo = number(low, "low", True)
        cl = number(close, "close", True)
        if not (lo <= min(op, cl) <= max(op, cl) <= hi):
            raise X2Error("inconsistent OHLC")
        self.last_bar = bar_index
        self._promote_be(bar_index)  # only previously filled TP1/funding
        risk_hit = hi >= self.stop
        if risk_hit and fills:
            self.audit.append(("risk_over_maker", bar_index, len(fills)))
        # The bar-end funding boundary precedes bar-end exits; timestamps
        # and ledger seq are preserved. No future settlement can be supplied.
        for settlement in settlements:
            if type(settlement) is not dict:
                raise X2Error("funding record not dict")
            integer(settlement.get("ts"), "funding timestamp")
            if settlement["ts"] > ts:
                raise X2Error("future funding")
            self._settle(settlement)
        if risk_hit:
            # Stops are modelled at this bar's end, without claiming tick time.
            px = max(op, self.stop) * (1 + SLIP)
            self._exit("risk-{}".format(bar_index), ts, self.qty, px,
                       "taker", "SL_GAP" if op >= self.stop else "SL")
            return "SL_GAP" if op >= self.stop else "SL"
        if fills and bar_index <= self.entry_bar:
            raise X2Error("Maker exit on entry fill bar")
        for candidate in fills:
            if type(candidate) is not dict or set(candidate) != {
                "id", "order", "qty", "price"
            }:
                raise X2Error("passive fill fields")
            order = candidate["order"]
            if order not in self.orders or self.orders[order] <= ZERO:
                raise X2Error("stale/cancelled Maker order")
            if order == "tp2" and self.orders["tp1"] > ZERO:
                raise X2Error("TP2 before completed TP1")
            amount = number(candidate["qty"], "qty", True)
            price = number(candidate["price"], "price", True)
            limit = self.targets[order]
            if amount > self.orders[order] or amount > self.qty:
                raise X2Error("Maker overfill")
            # Synthetic OHLC proxy: 2bp of downward penetration is required.
            # Executions occur at the posted limit, never at an arbitrary
            # favorable candidate price (even if the candle trades lower).
            if price != limit or lo > limit * (1 - MAKER_FEE):
                raise X2Error("Maker requires exact limit and 2bp penetration")
            self.orders[order] -= amount
            self._exit(candidate["id"], ts, amount, price, "maker", order)
            if order == "tp1" and self.first_tp_bar is None:
                self.first_tp_bar = bar_index
        return self.phase

    def snapshot(self):
        return {
            "phase": self.phase, "qty": self.qty,
            "entry_qty": self.entry_qty, "entry_basis": self.entry_basis,
            "E": self.E, "stop": self.stop, "original_stop": self.original_stop,
            "targets": dict(self.targets), "orders": dict(self.orders),
            "realized_gross": self.realized_gross, "fees_paid": self.fees_paid,
            "funding_cost": self.funding_cost, "events": list(self.events),
            "audit": list(self.audit), "first_tp_bar": self.first_tp_bar,
            "cash": self.initial_cash + self.realized_gross
                    - self.fees_paid - self.funding_cost,
        }
