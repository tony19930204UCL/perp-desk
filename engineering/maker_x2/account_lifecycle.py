"""Single offline account orchestration with explicit fictional configuration."""
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from account_ledger import replay_account, utc_day
from liquidation import liquidation_proxy
from entry_lifecycle import ShortEntry, closed_bar, MINUTE
from exit_lifecycle import ShortExit
from maker_exit_lab import X2Error, number

D = Decimal
Z = D("0")


class OfflineAccount:
    def __init__(self, capital, per_trade_margin, leverage, daily_loss_limit,
                 loss_streak_limit, halt_hours, liq_pct, liq_fee_pct):
        self.capital = number(capital, "capital", True)
        self.margin = number(per_trade_margin, "margin", True)
        self.leverage = number(leverage, "leverage", True)
        self.daily_limit = number(daily_loss_limit, "daily_loss", True)
        if type(loss_streak_limit) is not int or loss_streak_limit < 1:
            raise X2Error("loss streak threshold required")
        self.streak_limit = loss_streak_limit
        self.halt_ms = int(number(halt_hours, "halt hours", True) * 3600000)
        if self.halt_ms != 24 * 3600000:
            raise X2Error("frozen halt duration is 24h")
        self.liq_pct = number(liq_pct, "liq_pct", True)
        self.liq_fee = number(liq_fee_pct, "liq_fee_pct")
        if self.liq_pct != D(".0145") or self.liq_fee != D(".0005"):
            raise X2Error("frozen LIQ assumptions")
        self.events = []
        self.seen_signals = set()
        self.entry = None
        self.exit = None
        self.style = None
        self.symbol = None
        self.allocation = Z
        self.halt_until = None
        self.loss_streak = 0
        self.day = None
        self.day_start = self.capital
        self.day_realized = Z
        self.last_equity = self.capital
        self.last_mark = None
        self.stale = False

    def ledger(self):
        return replay_account(self.capital, self.events)

    def _emit(self, kind, eid, ts, **fields):
        if eid in {e["id"] for e in self.events}:
            raise X2Error("duplicate account event")
        if self.events and ts < self.events[-1]["ts"]:
            raise X2Error("account event moved backwards")
        event = {"type": kind, "id": eid, "ts": ts, "seq": len(self.events)}
        event.update(fields)
        self.events.append(event)

    def _sync(self, source):
        # Import only new executed events, never synthesize fills.
        ids = {e["id"] for e in self.events}
        for e in source:
            if e["id"] in ids:
                continue
            self._emit(e["type"], e["id"], e["ts"], **{
                k: v for k, v in e.items() if k not in ("type", "id", "ts", "seq")})
            ids.add(e["id"])

    def _roll_day(self, ts):
        date = utc_day(ts)
        if self.day != date:
            self.day_start = self.last_equity
            self.day_realized = Z
            self.day = date

    def _halt(self, ts):
        if self.halt_until is None or ts >= self.halt_until:
            self.halt_until = ts + self.halt_ms

    def _risk_limits(self, ts):
        state = self.ledger()
        pnl = self.last_equity - self.day_start
        if (pnl <= -self.daily_limit
                or self.day_realized <= -self.daily_limit
                or self.loss_streak >= self.streak_limit):
            if self.halt_until is None or ts >= self.halt_until:
                self._halt(ts)

    def _valuation(self, mark, ts):
        self._roll_day(ts)
        state = self.ledger()
        unrealized = Z if state["qty"] == Z else state["basis"] - state["qty"] * mark
        self.last_equity = state["cash"] + unrealized
        self.last_mark = mark
        self._risk_limits(ts)

    def submit(self, symbol, signal_id, signal_time, high, close, atr,
               k, ttl, qty, style, signal_vwap=None, known_funding_cost=None):
        if signal_id in self.seen_signals:
            raise X2Error("duplicate signal id")
        if self.entry is not None or self.exit is not None:
            raise X2Error("single slot occupied")
        if self.stale or (self.halt_until is not None
                          and signal_time < self.halt_until):
            raise X2Error("gap/halt blocks entry")
        if style not in ("X1", "X2", "X3"):
            raise X2Error("invalid exit style")
        self._roll_day(signal_time)
        options = (signal_id, signal_time, high, close, atr, k, ttl, qty)
        candidate = (ShortEntry.for_x1(*options, initial_cash=str(self.capital),
                     signal_vwap=signal_vwap, known_funding_cost=known_funding_cost)
                     if style == "X1" else
                     ShortEntry(*options, initial_cash=str(self.capital)))
        self.seen_signals.add(signal_id)
        if candidate.status != "PENDING":
            return candidate.status
        cost = candidate.limit * candidate.qty_target * D(".0002")
        available = self.ledger()["available"]
        if (candidate.qty_target * candidate.limit > self.margin * self.leverage
                or available < self.margin + cost):
            return "INSUFFICIENT_CASH_OR_MARGIN"
        self._emit("reserve", "reserve-"+signal_id, signal_time,
                   amount=str(self.margin), episode=signal_id)
        self.entry = candidate
        self.style = style
        self.symbol = symbol
        self.allocation = self.margin
        return "PENDING"

    def _close_episode(self, ts):
        state = self.ledger()
        if state["qty"] > Z:
            return
        if state["reserved"] > Z:
            self._emit("release", "release-"+str(ts), ts,
                       amount=str(state["reserved"]))
        state = self.ledger()
        if not state["episodes"]:
            return
        net = state["episodes"][-1]["net"]
        self.day_realized += net
        self.loss_streak = self.loss_streak + 1 if net < Z else 0
        self.entry = None
        self.exit = None
        self.style = None
        self.allocation = Z
        self._risk_limits(ts)

    def cancel_entry(self, ts):
        if self.entry is None:
            raise X2Error("no pending entry")
        self._sync(self.entry.events)
        self._emit("release", "cancel-"+str(ts), ts,
                   amount=str(self.ledger()["reserved"]))
        self.entry = None

    def bar(self, bar, entry_qty=None, maker_fill=False, settlements=()):
        op, high, low, close = closed_bar(bar)
        ts = bar["ts"] + MINUTE
        self._roll_day(ts)
        if self.entry is None and self.exit is None:
            self._valuation(close, ts)
            return "EMPTY"
        # LIQ precedence over old SL, Maker, and new intentions.
        state = self.ledger()
        qty = state["qty"]
        if qty > Z:
            original = self.entry.qty_target
            basis = state["basis"] / qty
            liq = liquidation_proxy(basis, high, qty, original,
                                    self.allocation, self.liq_pct, self.liq_fee)
            if liq:
                self._emit("liquidation", "liq-"+str(ts), ts,
                           qty=str(qty), margin_writeoff=str(liq["margin_writeoff"]),
                           fee=str(liq["fee"]), reason="LIQ_PROXY")
                self._close_episode(ts)
                self._valuation(close, ts)
                return "LIQ_PROXY"
        if self.exit is not None:
            result = self.exit.on_bar(bar, settlements=settlements,
                                      maker_fill=maker_fill)
            self._sync(self.exit.core.events)
        else:
            result = self.entry.process(bar, fill_qty=entry_qty,
                                         settlements=settlements)
            self._sync(self.entry.events)
            if self.entry.status == "FILLED":
                self.exit = ShortExit(self.entry, self.style,
                    vwap=self.entry.x1_vwap,
                    known_funding_cost=self.entry.x1_known_funding_cost)
        state = self.ledger()
        if state["qty"] > Z and self.entry is not None:
            original = self.entry.qty_target
            liq = liquidation_proxy(state["basis"]/state["qty"], high,
                      state["qty"], original, self.allocation,
                      self.liq_pct, self.liq_fee)
            if liq:
                self._emit("liquidation", "liq-"+str(ts), ts,
                    qty=str(state["qty"]), margin_writeoff=str(liq["margin_writeoff"]),
                    fee=str(liq["fee"]), reason="LIQ_PROXY")
                result = "LIQ_PROXY"
        if state["qty"] == Z or result in ("LIQ_PROXY", "CLOSED_SL", "CLOSED",
                                         "POST_ONLY_REJECT", "EXPIRED"):
            self._close_episode(ts)
        self._valuation(close, ts)
        return result

    def snapshot(self):
        state = self.ledger()
        mark = self.last_mark
        mtm = None if mark is None else state["basis"] - state["qty"] * mark
        return {"ledger": state, "equity": self.last_equity,
                "unrealized": mtm, "day": self.day,
                "day_start_equity": self.day_start,
                "day_mtm_change": self.last_equity-self.day_start,
                "day_realized": self.day_realized,
                "halt_until": self.halt_until, "loss_streak": self.loss_streak,
                "slot": self.entry is not None or self.exit is not None,
                "stale": self.stale, "last_mark": mark,
                "estimated_close_fee_not_paid": (
                    None if mark is None else state["qty"]*mark*D(".0005"))}
