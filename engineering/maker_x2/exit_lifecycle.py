"""Closed-minute X1/X2/X3 withdrawal lifecycle; no market feeds or strategy.

One-bar methods consume confirmed OHLC, optional finalized settlements, and
synthetic explicit fills. Immutable accounting.replay checks actual events.
"""
from decimal import Decimal, localcontext

from accounting import replay
from entry_lifecycle import MINUTE, closed_bar
from maker_exit_lab import MakerX2, X2Error, MAKER_FEE, TAKER_FEE, SLIP, number

D = Decimal
ZERO = D("0")


class ShortExit:
    def __init__(self, entry, style, vwap=None, known_funding_cost=None, rates=None):
        from scenario_rates import resolve_rates
        self.rates = resolve_rates(rates if rates is not None else entry.rates)
        if style not in ("X1", "X2", "X3"):
            raise X2Error("unknown frozen exit style")
        if not entry.events or replay(entry.cash_start, entry.events)["remaining_qty"] <= ZERO:
            raise X2Error("actual open short required")
        self.style = style
        self.core = MakerX2(str(entry.cash_start), rates=self.rates)
        self.core.events = [dict(e) for e in entry.events]
        self.core.seen = {e["id"] for e in entry.events}
        self.core.settled = {e["settlement_id"] for e in entry.events
                             if e["type"] == "funding"}
        self.core.last_ts = entry.events[-1]["ts"]
        self.core.entry_qty = replay(entry.cash_start, entry.events)["remaining_qty"]
        self.core.qty = self.core.entry_qty
        self.core.entry_basis = replay(entry.cash_start, entry.events)["entry_basis"]
        with localcontext() as ctx:
            ctx.prec = 50
            self.core.fees_paid = sum(
                (number(ev["qty"],"qty",True)*
                 number(ev["price"],"price",True)*
                 (self.rates.maker_fee if ev["liquidity"]=="maker"
                  else self.rates.taker_fee)
                 for ev in entry.events if ev["type"]=="fill"), ZERO)
        self.core.funding_cost = replay(entry.cash_start, entry.events)["funding_cost"]
        self.core.entry_bar = -1
        self.core.arm()
        self.core.orders = {"tp1": ZERO, "tp2": ZERO}
        self.first_fill_time = entry.first_fill_time
        self.last_close = entry.last_close
        self.last_bar = -1
        self.holding_count = 0
        self.previous_close = entry.last_close
        self.adverse = 0
        self.withdraw = False
        self.deadline = self.first_fill_time + 240 * MINUTE
        self.pending = None
        self.next_order = 0
        self.last_mark = entry.last_close
        self.stale = False
        self.truncated = False
        self.history = []
        self.unknown_future_funding = known_funding_cost is None
        if style == "X1":
            self.target = number(vwap, "signal_frozen_vwap", True)
            if self.target >= self.core.E:
                raise X2Error("X1 target not profit-side")
            assumed = (ZERO if known_funding_cost is None
                       else number(known_funding_cost, "known_funding_cost"))
            projected = ((self.core.E - self.target) * self.core.qty
                         - self.core.entry_basis * self.rates.maker_fee
                         - self.core.qty * self.target * self.rates.maker_fee - assumed)
            if projected <= ZERO:
                raise X2Error("X1 projected net nonpositive")
            self.core.targets["tp1"] = self.target
            self.core.orders["tp1"] = self.core.qty
        elif style == "X2":
            # Exact complementary quantities, never two separately rounded
            # halves whose sum can exceed a long-precision entry fill.
            with localcontext() as ctx:
                ctx.prec = max(50, len(self.core.qty.as_tuple().digits) + 2)
                first = self.core.qty / D(2)
                self.core.orders = {"tp1": first,
                                    "tp2": self.core.qty - first}
        self.status = "OPEN"

    def _schedule(self, close, eligible_ts):
        self.next_order += 1
        self.pending = {"id": "withdraw-" + str(self.next_order),
                        "limit": close * (1 - MAKER_FEE),
                        "start": eligible_ts, "expiry": eligible_ts + 2 * MINUTE,
                        "delivered": False}
        self.history.append(("INTENT", eligible_ts))

    def on_bar(self, bar, settlements=(), maker_fill=False):
        # MakerX2's incremental qty subtraction uses the active Decimal
        # context. Keep its exact producer quantities through both TP legs.
        with localcontext() as ctx:
            ctx.prec = max(50, len(self.core.entry_qty.as_tuple().digits) + 2)
            return self._on_bar(bar, settlements=settlements,
                                maker_fill=maker_fill)

    def _on_bar(self, bar, settlements=(), maker_fill=False):
        if self.core.phase != "OPEN":
            raise X2Error("closed episode cannot process more bars")
        op, hi, lo, close = closed_bar(bar)
        ts = bar["ts"]
        if self.last_bar >= ts or ts < self.core.last_ts:
            raise X2Error("out of order minute")
        if self.last_bar >= 0 and ts != self.last_bar + MINUTE:
            self.stale = True
            self.history.append(("GAP", ts))
            return "GAP"
        end = ts + MINUTE
        self.last_bar = ts
        self.last_mark = close
        self.holding_count += 1
        # Deadline cancels profit orders. No forced market TIME exit.
        if end >= self.deadline and not self.withdraw:
            self.withdraw = True
            self.core.orders = {"tp1": ZERO, "tp2": ZERO}
            self.history.append(("DEADLINE_INTENT", end))
        # Risk takes priority over any passive filled limit.
        self.core._promote_be(self.holding_count)
        risk = hi >= self.core.stop
        for settlement in settlements:
            if settlement["ts"] > end:
                raise X2Error("future funding")
            self.core._settle(settlement)
        if risk:
            with localcontext() as ctx:
                ctx.prec = 50
                price = max(op, self.core.stop) * (1 + self.rates.slip)
            self.core._exit("risk-" + str(end), end, self.core.qty, price,
                            "taker", "SL_GAP" if op >= self.core.stop else "SL")
            self.status = "CLOSED_SL"
            self.pending = None
            return self.status
        if self.withdraw and self.pending and ts >= self.pending["expiry"]:
            self.history.append(("EXPIRED", ts))
            self.pending = None
            if self.previous_close is not None:
                self._schedule(self.previous_close, ts + MINUTE)
        # Delivery cross is distinct from an already-resting gap.
        if self.withdraw and self.pending and ts >= self.pending["start"]:
            order = self.pending
            if not order["delivered"]:
                order["delivered"] = True
                if op <= order["limit"]:
                    self.history.append(("POST_ONLY_REJECT", ts))
                    self.pending = None
                elif maker_fill and lo <= order["limit"] * (1 - MAKER_FEE):
                    self.core._exit(order["id"], end, self.core.qty,
                                    order["limit"], "maker", "X3")
                    self.pending = None
            elif maker_fill and lo <= order["limit"] * (1 - MAKER_FEE):
                self.core._exit(order["id"], end, self.core.qty,
                                order["limit"], "maker", "X3")
                self.pending = None
        elif not self.withdraw and self.style in ("X1", "X2") and maker_fill:
            order = ("tp1" if self.core.orders["tp1"] > ZERO else "tp2")
            amount = self.core.orders[order]
            limit = self.core.targets[order]
            if amount > ZERO and lo <= limit * (1 - MAKER_FEE):
                self.core._exit("profit-" + str(end), end, amount,
                                limit, "maker", order)
                if self.core.phase == "OPEN":
                    self.core.orders[order] = ZERO
                if order == "tp1" and self.style == "X2":
                    self.core.first_tp_bar = self.holding_count
        if self.core.phase != "OPEN":
            self.status = "CLOSED"
            self.pending = None
            return self.status
        # X3 adverse closes use successive held complete bars only.
        if self.previous_close is not None:
            if close > self.previous_close:
                self.adverse += 1
            else:
                self.adverse = 0
        self.previous_close = close
        if self.style == "X3" and self.adverse >= 3:
            self.withdraw = True
        if self.withdraw and self.pending is None:
            self._schedule(close, end)
        return "OPEN"

    def tail(self, gap=False):
        self.truncated = True
        self.stale = self.stale or gap
        return self.snapshot()

    def snapshot(self):
        ledger = replay(self.core.initial_cash, self.core.events)
        unrealized = (self.core.qty * (self.core.E - self.last_mark)
                      if self.last_mark is not None else None)
        estimate = (self.core.qty * max(self.last_mark, self.core.stop)
                    * (1 + self.rates.slip) * self.rates.taker_fee
                    if self.last_mark is not None else None)
        return {"status": self.status, "style": self.style,
                "events": list(self.core.events), "ledger": ledger,
                "qty": self.core.qty, "stop": self.core.stop,
                "target": getattr(self, "target", None),
                "unknown_future_funding": self.unknown_future_funding,
                "pending": dict(self.pending) if self.pending else None,
                "pending_slot": self.core.phase == "OPEN",
                "held_bars": self.holding_count, "withdraw": self.withdraw,
                "last_mark": self.last_mark, "unrealized": unrealized,
                "equity": (ledger["cash"] + unrealized
                           if unrealized is not None else None),
                "estimated_close_fee_not_paid": estimate,
                "stale": self.stale, "truncated": self.truncated,
                "history": list(self.history)}
