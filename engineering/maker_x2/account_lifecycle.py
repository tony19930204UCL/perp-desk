"""Single offline account orchestration with explicit fictional configuration."""
from copy import deepcopy
from decimal import Decimal, localcontext
from account_ledger import replay_account, utc_day
from liquidation import liquidation_proxy
from entry_lifecycle import ShortEntry, closed_bar, MINUTE
from exit_lifecycle import ShortExit
from maker_exit_lab import X2Error, number

D = Decimal
Z = D("0")


class _LedgerView(dict):
    """Materialized scalar keys plus shared O(1) incremental collections."""
    def __init__(self, account, state, count):
        super().__init__(state)
        self._account, self._count = account, count

    def copy(self):
        return {k: (v.copy() if isinstance(v, (dict, set, list)) else v)
                for k, v in self.items()}


class OfflineAccount:
    def __init__(self, capital, per_trade_margin, leverage, daily_loss_limit,
                 loss_streak_limit, halt_hours, liq_pct, liq_fee_pct, rates=None):
        from scenario_rates import resolve_rates
        self.rates = resolve_rates(rates)
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
        self._event_ids = set()
        self._applied_ids = set()
        self._applied_settlements = set()
        self._sell_ids = set()
        self._cursor = 0
        self._applied_events = 0
        self._full_replays = 0
        self._last_ledger_ts = None
        self._ledger_state = {
            "cash": self.capital, "reserved": Z, "qty": Z, "basis": Z,
            "gross": Z, "fees": Z, "funding_cost": Z, "writeoff": Z,
            "episodes": [], "open_episode": None}
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
        self.counted_episodes = 0
        self.day_halt_triggers = set()
        self.streak_halt_latched = False
        self.last_equity = self.capital
        self.last_mark = None
        self.stale = False

    def _apply_event(self, event):
        """One strict Decimal50 transition; replay_account stays independent."""
        from account_ledger import utc_day
        with localcontext() as ctx:
            ctx.prec = 50
            s, seq = self._ledger_state, self._cursor
            if type(event) is not dict:
                raise X2Error("account event must be dict")
            eid, ts = event.get("id"), event.get("ts")
            if not isinstance(eid, str) or not eid or eid in self._applied_ids:
                raise X2Error("duplicate account event id")
            if event.get("seq") != seq or type(event.get("seq")) is not int:
                raise X2Error("account event sequence gap")
            utc_day(ts)
            if self._last_ledger_ts is not None and ts < self._last_ledger_ts:
                raise X2Error("backwards account event")
            kind = event["type"]
            ep = s["open_episode"]
            if kind == "reserve":
                amount = number(event["amount"], "reserve", True)
                if s["reserved"] != Z or s["qty"] != Z or amount > s["cash"]:
                    raise X2Error("double/insufficient reserve")
                s["reserved"] += amount
                ep = {"id": event["episode"], "start_cash": s["cash"],
                      "gross": Z, "fees": Z, "funding_cost": Z,
                      "writeoff": Z, "net": Z}
            elif kind == "release":
                amount = number(event["amount"], "release", True)
                if amount > s["reserved"]:
                    raise X2Error("overrelease margin")
                s["reserved"] -= amount
            elif kind == "fill":
                amount = number(event["qty"], "qty", True)
                price = number(event["price"], "price", True)
                liquidity = event["liquidity"]
                if liquidity not in ("maker", "taker"):
                    raise X2Error("bad fill liquidity")
                fee = amount * price * (self.rates.maker_fee if
                      liquidity == "maker" else self.rates.taker_fee)
                if event["side"] == "sell":
                    if s["reserved"] <= Z or ep is None:
                        raise X2Error("fill without reserved episode")
                    s["qty"] += amount
                    s["basis"] += amount * price
                    realized = Z
                elif event["side"] == "buy":
                    if amount > s["qty"]:
                        raise X2Error("over-close")
                    realized = (s["basis"] / s["qty"] - price) * amount
                    s["basis"] -= s["basis"] / s["qty"] * amount
                    s["qty"] -= amount
                    if s["qty"] == Z:
                        s["basis"] = Z
                else:
                    raise X2Error("bad fill side")
                s["cash"] += realized - fee
                s["gross"] += realized
                s["fees"] += fee
                ep = dict(ep)
                ep["gross"] += realized
                ep["fees"] += fee
            elif kind == "funding":
                sid = event["settlement_id"]
                if (not isinstance(sid, str) or not sid or
                        sid in self._applied_settlements):
                    raise X2Error("duplicate settlement")
                amount = s["qty"] * number(event["mark"], "mark", True) * (
                    number(event["rate"], "rate"))
                s["cash"] += amount
                s["funding_cost"] -= amount
                ep = dict(ep)
                ep["funding_cost"] -= amount
            elif kind == "liquidation":
                amount = number(event["qty"], "liq_qty", True)
                margin = number(event["margin_writeoff"], "writeoff")
                fee = number(event["fee"], "liq_fee")
                if (amount != s["qty"] or margin < Z or
                        margin > s["reserved"] or fee < Z):
                    raise X2Error("invalid LIQ")
                s["qty"] = s["basis"] = Z
                s["reserved"] -= margin
                s["cash"] -= margin + fee
                s["writeoff"] += margin
                s["fees"] += fee
                ep = dict(ep)
                ep["writeoff"] += margin
                ep["fees"] += fee
            else:
                raise X2Error("unknown account event")
            if ep is not None:
                ep["net"] = ep["gross"] - ep["fees"] - ep["funding_cost"] - ep["writeoff"]
            if ep is not None and s["qty"] == Z and s["reserved"] == Z:
                s["episodes"].append(dict(ep))
                ep = None
            s["open_episode"] = ep
            self._applied_ids.add(eid)
            if kind == "funding":
                self._applied_settlements.add(sid)
            self._last_ledger_ts = ts
            self._cursor += 1
            self._applied_events += 1

    def ledger(self):
        while self._cursor < len(self.events):
            self._apply_event(self.events[self._cursor])
        s = dict(self._ledger_state)
        # replay_account computes cash-reserved inside Decimal50.
        # Decimal arithmetic outside that context silently rounds to 28.
        with localcontext() as ctx:
            ctx.prec = 50
            s["available"] = s["cash"] - s["reserved"]
        s["ids"] = self._applied_ids
        s["settlements"] = self._applied_settlements
        return _LedgerView(self, s, self._cursor)

    def verify_ledger(self):
        self._full_replays += 1
        independent = replay_account(self.capital, self.events, rates=self.rates)
        if self.ledger() != independent:
            raise X2Error("independent full account ledger divergence")
        return independent

    def _emit(self, kind, eid, ts, **fields):
        if eid in self._event_ids:
            raise X2Error("duplicate account event")
        if self.events and ts < self.events[-1]["ts"]:
            raise X2Error("account event moved backwards")
        event = {"type": kind, "id": eid, "ts": ts, "seq": len(self.events)}
        event.update(fields)
        self.events.append(event)
        self._event_ids.add(eid)
        if kind == "fill" and fields.get("side") == "sell":
            self._sell_ids.add(eid)

    def _sync(self, source):
        # Scope original source IDs by episode while retaining provenance.
        prefix = self.entry.signal_id + ":" if self.entry else ""
        for event in source:
            eid = prefix + event["id"]
            if eid in self._event_ids:
                continue
            kind = event["type"]
            old_qty = self.ledger()["qty"]
            extra = {k: v for k, v in event.items()
                     if k not in ("type", "id", "ts", "seq")}
            self._emit(kind, eid, event["ts"], source_id=event["id"], **extra)
            if kind == "fill" and event["side"] == "buy" and old_qty > Z:
                state = self.ledger()
                with localcontext() as ctx:
                    ctx.prec = 50
                    # Keep the post-fill remaining margin at Decimal50.
                    # Match liquidation's allocation * qty / original order.
                    # The next LIQ writeoff then equals reserved by construction.
                    keep = self.allocation * state["qty"] / self.entry.qty_target
                    release = state["reserved"] - keep
                if release > Z:
                    self._emit("release", "margin:"+eid, event["ts"],
                               amount=str(release))

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
        pnl = self.last_equity - self.day_start
        if ((pnl <= -self.daily_limit or
             self.day_realized <= -self.daily_limit)
                and self.day not in self.day_halt_triggers):
            self.day_halt_triggers.add(self.day)
            self._halt(ts)
        if self.loss_streak >= self.streak_limit:
            if not self.streak_halt_latched:
                self._halt(ts)
                self.streak_halt_latched = True
        else:
            self.streak_halt_latched = False

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
        candidate = (ShortEntry.for_x1(*options, initial_cash=str(self.capital), rates=self.rates,
                     signal_vwap=signal_vwap, known_funding_cost=known_funding_cost)
                     if style == "X1" else
                     ShortEntry(*options, initial_cash=str(self.capital), rates=self.rates))
        self.seen_signals.add(signal_id)
        if candidate.status != "PENDING":
            return candidate.status
        cost = candidate.limit * candidate.qty_target * self.rates.maker_fee
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
        if len(state["episodes"]) > self.counted_episodes:
            net = state["episodes"][-1]["net"]
            had_fill = any(eid.startswith(self.entry.signal_id + ":")
                           for eid in self._sell_ids)
            if had_fill:
                self.day_realized += net
                self.loss_streak = self.loss_streak + 1 if net < Z else 0
            self.counted_episodes = len(state["episodes"])
        self.entry = None
        self.exit = None
        self.style = None
        self.allocation = Z
        self._risk_limits(ts)

    def cancel_entry(self, ts):
        if self.entry is None or self.exit is not None:
            raise X2Error("no cancellable pending entry")
        self._sync(self.entry.events)
        remaining_qty = self.ledger()["qty"]
        if remaining_qty > Z:
            # Only release unfilled allocation. Existing filled risk remains.
            unused = self.allocation * self.entry.remaining / self.entry.qty_target
            if unused > Z:
                self._emit("release", "cancel-"+str(ts), ts,
                           amount=str(unused))
            self.exit = ShortExit(self.entry, self.style,
                                  vwap=self.entry.x1_vwap,
                                  known_funding_cost=self.entry.x1_known_funding_cost,
                                  rates=self.rates)
            return "PARTIAL_OPEN"
        self._emit("release", "cancel-"+str(ts), ts,
                   amount=str(self.ledger()["reserved"]))
        self.entry = None
        self.style = None
        return "CANCELLED"

    def bar(self, bar, entry_qty=None, maker_fill=False, settlements=()):
        op, high, low, close = closed_bar(bar)
        ts = bar["ts"] + MINUTE
        gap = (
            self.exit is not None and self.exit.last_bar >= 0 and
            bar["ts"] != self.exit.last_bar + MINUTE
        ) or (
            self.exit is None and self.entry is not None and
            bar["ts"] != self.entry.next_ts
        )
        if gap:
            self.stale = True
            state = self.ledger()
            if state["qty"] > Z:
                entry_price = state["basis"] / state["qty"]
                liq = liquidation_proxy(entry_price, high, state["qty"],
                                        self.entry.qty_target, self.allocation,
                                        self.liq_pct, self.liq_fee, rates=self.rates)
                if liq is not None:
                    self._emit("liquidation", "gap-liq-"+str(ts), ts,
                               qty=str(state["qty"]),
                               margin_writeoff=str(liq["margin_writeoff"]),
                               fee=str(liq["fee"]), reason="LIQ_PROXY")
                    self._close_episode(ts)
                    return "LIQ_PROXY"
                stop = entry_price * D("1.006")
                if high >= stop:
                    price = max(op, stop) * (1 + self.rates.slip)
                    self._emit("fill", "gap-risk-"+str(ts), ts,
                               side="buy", qty=str(state["qty"]),
                               price=str(price), liquidity="taker")
                    self._close_episode(ts)
                    return "CLOSED_SL"
            return "GAP"
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
                                    self.allocation, self.liq_pct, self.liq_fee, rates=self.rates)
            if liq:
                self._emit("liquidation", "liq-"+str(ts), ts,
                           qty=str(qty), margin_writeoff=str(liq["margin_writeoff"]),
                           fee=str(liq["fee"]), reason="LIQ_PROXY")
                self._close_episode(ts)
                self._valuation(close, ts)
                return "LIQ_PROXY"
        # Same-entry-bar OHLC unknown ordering: LIQ outranks hard SL.
        # Only admit an actual fictional Maker entry when delivery permits it
        # and the 2bp penetration is observed, then write off the filled slice.
        if (self.exit is None and self.entry is not None and entry_qty is not None
                and self.entry.status in ("PENDING", "PARTIAL")):
            ep = self.entry
            amount = number(entry_qty, "entry_qty", True)
            price = ep.limit
            delivery_ok = not (bar["ts"] == ep.signal_time and op >= price)
            threshold = price * (1 + self.liq_pct)
            if (delivery_ok and amount <= ep.remaining
                    and high >= price * D("1.0002") and high >= threshold):
                if ep.x1_vwap is not None and ep._x1_net(ep.filled + amount) <= Z:
                    raise X2Error("X1 entry economic gate")
                self._emit("fill", ep.signal_id+":liq-entry:"+str(ts), ts,
                           side="sell", qty=str(amount), price=str(price),
                           liquidity="maker")
                claim = liquidation_proxy(price, high, amount,
                                           ep.qty_target, self.allocation,
                                           self.liq_pct, self.liq_fee, rates=self.rates)
                self._emit("liquidation", ep.signal_id+":liq:"+str(ts), ts,
                           qty=str(amount),
                           margin_writeoff=str(claim["margin_writeoff"]),
                           fee=str(claim["fee"]), reason="LIQ_PROXY")
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
                    known_funding_cost=self.entry.x1_known_funding_cost,
                    rates=self.rates)
        state = self.ledger()
        if state["qty"] > Z and self.entry is not None:
            original = self.entry.qty_target
            liq = liquidation_proxy(state["basis"]/state["qty"], high,
                      state["qty"], original, self.allocation,
                      self.liq_pct, self.liq_fee, rates=self.rates)
            if liq:
                self._emit("liquidation", "liq-"+str(ts), ts,
                    qty=str(state["qty"]), margin_writeoff=str(liq["margin_writeoff"]),
                    fee=str(liq["fee"]), reason="LIQ_PROXY")
                result = "LIQ_PROXY"
        # A zero-qty pending order still owns its reserved slot until the
        # expiry bar (or an explicit terminal cancellation/rejection).
        if result in ("LIQ_PROXY", "CLOSED_SL", "CLOSED",
                      "POST_ONLY_REJECT", "EXPIRED") or (
                          self.ledger()["qty"] == Z
                          and self.entry.status not in ("PENDING", "PARTIAL")):
            self._close_episode(ts)
        self._valuation(close, ts)
        return result

    def tail(self, gap=False):
        """Retain paid costs and open slot; no terminal synthetic trade."""
        self.stale = self.stale or gap
        return self.snapshot()

    def checkpoint(self):
        """Detached pure-Python state including identities and open lifecycle."""
        self.verify_ledger()
        return deepcopy(self)

    @classmethod
    def recover(cls, checkpoint):
        """Recreate from a trusted in-memory checkpoint (no file IO)."""
        if type(checkpoint) is not cls:
            raise X2Error("trusted OfflineAccount checkpoint required")
        restored = deepcopy(checkpoint)
        restored.verify_ledger()  # strict raw replay versus incrementally cached state
        return restored

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
                    None if mark is None else state["qty"]*mark*self.rates.taker_fee)}
