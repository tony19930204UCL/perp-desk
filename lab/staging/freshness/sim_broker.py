"""Offline deterministic simulator. No network, private API or live execution interface."""
from dataclasses import dataclass, asdict
from decimal import Decimal, localcontext, InvalidOperation, Context, ROUND_HALF_EVEN
from functools import wraps
from copy import deepcopy
import json
import sqlite3
import os
import fcntl
from pathlib import Path

def _exact_context(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
            return method(*args, **kwargs)
    return wrapped


@dataclass(frozen=True)
class InstrumentSettings:
    symbol: str
    maker_fee: Decimal
    taker_fee: Decimal
    quantity_step: Decimal
    tick: Decimal
    min_notional: Decimal
    max_quantity: Decimal
    min_quantity: Decimal | None = None
    category: str | None = None

@dataclass(frozen=True)
class ExecutionModel:
    latency_ms: int
    exit_slippage_ticks: int
    depth_extra_ticks: int | None
    entry_slippage_ticks: int | None = None
    exit_fill_slippage_ticks: int | None = None

@dataclass(frozen=True)
class RiskContract:
    approved: bool = False
    version: str | None = None
    max_loss_per_trade_usdt: Decimal | None = None
    max_daily_loss_usdt: Decimal | None = None
    max_effective_exposure_x: Decimal | None = None
    max_positions: int | None = None
    total_loss_limit_usdt: Decimal | None = None
    require_stop: bool | None = None

@dataclass(frozen=True)
class Intent:
    intent_id: str
    symbol: str
    side: str
    qty: Decimal
    stop: Decimal | None
    decision_ts: int
    quantity_step: Decimal
    tick: Decimal
    min_notional: Decimal
    max_quantity: Decimal
    kind: str
    reduce_only: bool
    limit_price: Decimal | None = None
    queue_ahead_qty: Decimal | None = None
    expires_ts: int | None = None

class SimBroker:
    def __init__(self, path, *, initial_cash, instruments, execution, risk, version_id, forward_start):
        if not isinstance(initial_cash, Decimal) or not initial_cash.is_finite() or initial_cash <= 0:
            raise ValueError('explicit finite positive initial cash required')
        if not isinstance(version_id, str) or not version_id.strip() or type(forward_start) is not int or forward_start < 0:
            raise ValueError('explicit version and forward_start required')
        if not isinstance(execution, ExecutionModel) or type(execution.latency_ms) is not int or execution.latency_ms < 0 or type(execution.exit_slippage_ticks) is not int or execution.exit_slippage_ticks < 0 or (execution.depth_extra_ticks is not None and (type(execution.depth_extra_ticks) is not int or execution.depth_extra_ticks <= 0)):
            raise ValueError('invalid explicit execution model')
        for ticks in (execution.entry_slippage_ticks, execution.exit_fill_slippage_ticks):
            if ticks is not None and (type(ticks) is not int or ticks < 0):
                raise ValueError('invalid optional adverse fill ticks')
        instruments = list(instruments)
        if not instruments or len({s.symbol for s in instruments}) != len(instruments):
            raise ValueError('unique external instruments required')
        for s in instruments:
            for name in ('maker_fee','taker_fee','quantity_step','tick','min_notional','max_quantity'):
                value = getattr(s, name)
                if not isinstance(value, Decimal) or not value.is_finite() or (value < 0 if name.endswith('_fee') else value <= 0):
                    raise ValueError('invalid explicit instrument setting: '+name)
            if s.min_quantity is not None and (not isinstance(s.min_quantity, Decimal) or not s.min_quantity.is_finite() or s.min_quantity <= 0 or s.min_quantity % s.quantity_step or s.min_quantity > s.max_quantity):
                raise ValueError('invalid external min_quantity')
            if s.category is not None and (not isinstance(s.category,str) or not s.category.strip()):
                raise ValueError('invalid external category')
        self.cash = initial_cash
        self.initial_cash = str(initial_cash)
        self.day_baselines = {}
        self.last_ts = forward_start
        self.instruments = {s.symbol: s for s in instruments}
        self.execution = execution
        self.risk = risk
        self.version_id = version_id
        self.forward_start = forward_start
        self.audit = []
        self.orders = {}
        self.positions = {}
        self.fills = []
        self.ledger = []
        self.marks = {}
        self.funding_events = {}
        self.funding_status = {}
        self.seen_events = {}
        self.path = Path(path).resolve()
        if self.path.name in ('observations.sqlite', 'observations.sqlite3', 'status.json') or 'shared' in self.path.parts:
            raise ValueError('isolated simulator state path required')
        self._fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            self._fd = None
            raise RuntimeError('simulator state already locked') from None
        self._db = None
        self._in_event = False
        self._meta = dict(version_id=version_id, forward_start=forward_start, instruments=[asdict(s) for s in instruments], execution=asdict(execution))
        self._meta = json.loads(json.dumps(self._meta, default=str, sort_keys=True))
        try:
            self._db = sqlite3.connect(self.path)
            names = {x[0] for x in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names - {'sim_broker_state'}:
                raise ValueError('refuse shared or foreign database')
            self._db.execute('CREATE TABLE IF NOT EXISTS sim_broker_state (singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL)')
            row = self._db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()
            if row:
                state = json.loads(row[0])
                if state.pop('meta') != self._meta:
                    raise ValueError('version/model/instrument metadata frozen; use a new namespace')
                self.cash = Decimal(state.pop('cash'))
                for key, value in state.items():
                    setattr(self, key, value)
            else:
                self._save()
        except Exception:
            self.close()
            raise

    def _check_metadata(self):
        current = dict(version_id=self.version_id, forward_start=self.forward_start, instruments=[asdict(s) for s in self.instruments.values()], execution=asdict(self.execution))
        if json.loads(json.dumps(current, default=str, sort_keys=True)) != self._meta:
            raise ValueError('model/instrument/version metadata frozen; create a new namespace')

    def _save(self):
        state = {k: getattr(self, k) for k in ('orders', 'positions', 'fills', 'ledger', 'audit', 'marks', 'funding_events', 'seen_events', 'funding_status', 'initial_cash', 'day_baselines', 'last_ts')}
        state.update(cash=str(self.cash), meta=self._meta)
        payload = json.dumps(state, default=str, sort_keys=True)
        with self._db:
            self._db.execute('INSERT OR REPLACE INTO sim_broker_state VALUES (1, ?)', (payload,))

    def _risk_approved(self):
        r = self.risk
        limits = ('max_loss_per_trade_usdt', 'max_daily_loss_usdt', 'max_effective_exposure_x', 'total_loss_limit_usdt')
        return (isinstance(r, RiskContract) and r.approved is True and isinstance(r.version, str) and bool(r.version.strip())
                    and type(r.max_positions) is int and r.max_positions > 0 and r.require_stop is True
                    and all(isinstance(getattr(r, k), Decimal) and getattr(r, k).is_finite() and getattr(r, k) > 0 for k in limits))

    @_exact_context
    def submit(self, intent):
        if self._db is None:
            raise RuntimeError('broker is closed')
        self._check_metadata()
        if 'order:'+intent.intent_id in self.orders:
            existing = self.orders['order:'+intent.intent_id]
            if json.dumps(existing['intent'], default=str, sort_keys=True) != json.dumps(asdict(intent), default=str, sort_keys=True):
                raise ValueError('conflicting intent ID')
            return deepcopy(existing)
        if type(intent.decision_ts) is int and self.forward_start <= intent.decision_ts < self.last_ts:
            raise ValueError('decision timestamp older than processed events')
        approved = self._risk_approved()
        reason = None if approved or intent.reduce_only else 'risk_missing_or_unapproved'
        s = self.instruments.get(intent.symbol)
        finite = lambda x: isinstance(x, Decimal) and x.is_finite() and x > 0
        if (s is None or intent.side not in ('BUY', 'SELL') or intent.kind not in ('TAKER', 'MAKER')
                or type(intent.decision_ts) is not int or intent.decision_ts < self.forward_start
                or not finite(intent.qty) or not all(finite(x) for x in (intent.quantity_step, intent.tick, intent.min_notional, intent.max_quantity))):
            reason = 'invalid_intent'
        elif (intent.quantity_step != s.quantity_step or intent.tick != s.tick or intent.min_notional != s.min_notional
              or intent.max_quantity != s.max_quantity or intent.qty % s.quantity_step != 0 or intent.qty > s.max_quantity or (s.min_quantity is not None and intent.qty < s.min_quantity and not intent.reduce_only)):
            reason = 'instrument_filters'
        elif not intent.reduce_only and (not finite(intent.stop) or intent.stop % s.tick != 0):
            reason = 'stop_required_or_invalid'
        if reason is None and not intent.reduce_only and not self._funding_ready(intent.symbol, intent.decision_ts):
            reason = 'funding_feed_incomplete'
        if intent.reduce_only and reason is None:
            p = self.positions.get(intent.symbol)
            if not p or (Decimal(p['qty']) > 0) == (intent.side == 'BUY') or intent.qty > abs(Decimal(p['qty'])):
                reason = 'no_reducible_position'
        if not intent.reduce_only and reason is None:
            p = self.positions.get(intent.symbol)
            if p and (Decimal(p['qty']) > 0) != (intent.side == 'BUY'):
                reason = 'opposite_open_requires_reduce_only'
        if intent.kind == 'MAKER' and reason is None:
            if not finite(intent.limit_price) or intent.limit_price % s.tick != 0:
                reason = 'invalid_limit'
            elif not isinstance(intent.queue_ahead_qty, Decimal) or not intent.queue_ahead_qty.is_finite() or intent.queue_ahead_qty < 0:
                reason = 'unknown_queue'
        order = dict(order_id='order:' + intent.intent_id, intent_id=intent.intent_id,
                     status='PENDING' if reason is None else 'REJECTED',
                     reason=reason,
                     intent=asdict(intent), remaining=str(intent.qty),
                     arrival_ts=intent.decision_ts + self.execution.latency_ms, sequence=len(self.orders)+1)
        self.orders[order['order_id']] = order
        self.audit.append(deepcopy(order))
        if not self._in_event:
            self._save()
        return deepcopy(order)

    def cancel(self, order_id, *, ts):
        order = self.orders[order_id]
        if order['status'] in ('PENDING', 'RESTING'):
            order.update(status='CANCELED', canceled_ts=ts)
            self.audit.append(deepcopy(order))
            self.last_ts = max(self.last_ts, ts)
            self._save()
        return deepcopy(order)

    @staticmethod
    def _decimal(value, *, positive=False):
        if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
            raise ValueError('use exact Decimal/string/integer values')
        try:
            number = Decimal(value)
        except (InvalidOperation, ValueError):
            raise ValueError('invalid decimal') from None
        if not number.is_finite() or (positive and number <= 0):
            raise ValueError('nonfinite or nonpositive decimal')
        return number

    def _validate_event(self, event):
        if not isinstance(event, dict) or not isinstance(event.get('event_id'), str) or not event['event_id']:
            raise ValueError('event_id required')
        if event.get('symbol') not in self.instruments or type(event.get('ts')) is not int or event['ts'] < self.forward_start:
            raise ValueError('unknown symbol or timestamp before forward start')
        if 'source_ts' in event and (type(event['source_ts']) is not int or not self.forward_start <= event['source_ts'] <= event['ts']):
            raise ValueError('invalid source timestamp')
        kind = event.get('type')
        if kind == 'book':
            for side in ('bids', 'asks'):
                if not isinstance(event.get(side), list):
                    raise ValueError('book levels required')
                prices = []
                for level in event[side]:
                    if not isinstance(level, (list, tuple)) or len(level) != 2:
                        raise ValueError('price/quantity level required')
                    price = self._decimal(level[0], positive=True)
                    self._decimal(level[1], positive=True)
                    if price % self.instruments[event['symbol']].tick:
                        raise ValueError('off-tick quote')
                    prices.append(price)
                if len(set(prices)) != len(prices) or prices != sorted(prices, reverse=side=='bids'):
                    raise ValueError('levels must be unique and best-first')
            if event['asks'] and event['bids'] and Decimal(event['bids'][0][0]) >= Decimal(event['asks'][0][0]):
                raise ValueError('crossed book')
        elif kind in ('mark', 'aggTrade'):
            self._decimal(event.get('price'), positive=True)
            if kind == 'aggTrade':
                self._decimal(event.get('qty'), positive=True)
                if event.get('aggressor') not in ('BUY', 'SELL'):
                    raise ValueError('explicit aggressor required')
        elif kind == 'funding':
            if event.get('finalized') is not True or type(event.get('settlement_ts')) is not int or not self.forward_start <= event['settlement_ts'] <= event['ts']:
                raise ValueError('exact finalized settlement required')
            self._decimal(event.get('rate'))
            self._decimal(event.get('mark'), positive=True)
        elif kind == 'funding_status':
            if type(event.get('complete')) is not bool or type(event.get('valid_until_ts')) is not int:
                raise ValueError('external completeness/deadline required')
        else:
            raise ValueError('unsupported public event type')

    @_exact_context
    def on_event(self, event):
        if self._db is None:
            raise RuntimeError('broker is closed')
        self._check_metadata()
        if isinstance(event, dict) and 'kind' in event:
            event = deepcopy(event)
            kind = event.pop('kind')
            source_ts = event.pop('ts_ms')
            observed = event.pop('observed_ms', source_ts)
            event.update(type=kind, ts=observed, event_id=event.pop('id'))
            if kind == 'funding':
                event.update(settlement_ts=source_ts, mark=event.pop('mark_price'))
            else:
                event['source_ts'] = source_ts
        self._validate_event(event)
        event = json.loads(json.dumps(event, default=str, sort_keys=True))
        if event['event_id'] in self.seen_events:
            if event != self.seen_events[event['event_id']]:
                raise ValueError('conflicting event ID')
            return
        if event['type'] != 'funding' and event['ts'] < self.last_ts:
            raise ValueError('out-of-order event; supply chronological observation timestamps')
        names = ('cash','orders','positions','fills','ledger','audit','marks','funding_events','seen_events','funding_status','day_baselines','last_ts')
        before = {name: deepcopy(getattr(self, name)) for name in names}
        self._in_event = True
        self._event_ts = max(self.last_ts, event['ts'])
        try:
            day = str(self._event_ts // 86400000)
            if day not in self.day_baselines:
                self.day_baselines[day] = str(self.equity) if self.equity is not None else None
            self._on_event(event)
            self.seen_events[event['event_id']] = dict(event)
            self.last_ts = max(self.last_ts, event['ts'])
            self._save()
        except Exception:
            for name, value in before.items():
                setattr(self, name, value)
            raise
        finally:
            self._in_event = False

    @property
    @_exact_context
    def equity(self):
        if any(symbol not in self.marks for symbol in self.positions):
            return None
        with localcontext() as ctx:
            ctx.prec = 40
            return self.cash + sum(Decimal(p['qty']) * (Decimal(self.marks[symbol]) - Decimal(p['entry'])) for symbol, p in self.positions.items())

    def _funding_ready(self, symbol, ts):
        status = self.funding_status.get(symbol)
        return bool(status and status['complete'] is True and ts < status['valid_until_ts'])

    def _on_event(self, event):
        if event['type'] == 'funding_status':
            if type(event.get('complete')) is not bool or type(event.get('valid_until_ts')) is not int:
                raise ValueError('explicit funding completeness/deadline required')
            self.funding_status[event['symbol']] = dict(event)
            return
        if event['type'] == 'funding':
            if event.get('finalized') is not True or event['settlement_ts'] > event['ts']:
                raise ValueError('only exact finalized settlement events accepted')
            rate_type = event.get('rate_type', 'Regular')
            if rate_type not in ('Regular','Special'):
                raise ValueError('unsupported exact funding rate_type')
            key = event['symbol'] + ':' + str(event['settlement_ts']) + ':' + rate_type
            if key in self.funding_events:
                prior = self.funding_events[key]
                if Decimal(prior['rate']) != Decimal(event['rate']) or Decimal(prior['mark']) != Decimal(event['mark']):
                    raise ValueError('conflicting finalized settlement; past outcomes immutable')
                return
            rate, mark = Decimal(event['rate']), Decimal(event['mark'])
            if not rate.is_finite() or not mark.is_finite() or mark <= 0:
                raise ValueError('invalid finalized funding values')
            with localcontext() as ctx:
                ctx.prec = 40
                qty = sum((Decimal(f['qty']) if f['side']=='BUY' else -Decimal(f['qty'])) for f in self.fills if f['symbol']==event['symbol'] and f['ts'] < event['settlement_ts'])
                amount = -qty * mark * rate
                self.cash += amount
                self.ledger.append(dict(ledger_id='ledger:'+str(len(self.ledger)+1), type='funding', amount=str(amount), ts=event['settlement_ts'], event_id=event['event_id'], symbol=event['symbol'], qty=str(qty), rate=str(rate), mark=str(mark), rate_type=rate_type))
                self.funding_events[key] = dict(event)
            return
        if event['type'] == 'mark':
            self.marks[event['symbol']] = event['price']
            p = self.positions.get(event['symbol'])
            if p:
                qty = Decimal(p['qty'])
                triggered = Decimal(event['price']) <= Decimal(p['stop']) if qty > 0 else Decimal(event['price']) >= Decimal(p['stop'])
                prior_stop = self.orders.get(p.get('stop_order'), {})
                if triggered and prior_stop.get('status') not in ('PENDING','RESTING'):
                    for pending in self.orders.values():
                        if pending['intent']['symbol'] == event['symbol'] and not pending['intent']['reduce_only'] and pending['status'] in ('PENDING','RESTING'):
                            pending.update(status='CANCELED', reason='stop_superseded', canceled_ts=event['ts'])
                            self.audit.append(deepcopy(pending))
                    s = self.instruments[event['symbol']]
                    i = Intent('stop:'+event['event_id'], event['symbol'], 'SELL' if qty > 0 else 'BUY', abs(qty), None, event['ts'], s.quantity_step, s.tick, s.min_notional, s.max_quantity, 'TAKER', True)
                    p['stop_order'] = self.submit(i)['order_id']
            return
        for order in sorted(self.orders.values(), key=lambda o: o['sequence']):
            expiry = order['intent']['expires_ts']
            if expiry is not None and event['ts'] >= expiry and order['status'] in ('PENDING', 'RESTING'):
                order.update(status='EXPIRED', reason='expiry')
                self.audit.append(deepcopy(order))
        if event['type'] == 'aggTrade':
            with localcontext() as ctx:
                ctx.prec = 40
                volume = Decimal(event['qty'])
                for order in sorted(self.orders.values(), key=lambda o: o['sequence']):
                    i = order['intent']
                    if order['status'] != 'RESTING' or i['symbol'] != event['symbol'] or event['ts'] <= order['resting_ts']:
                        continue
                    limit = Decimal(i['limit_price'])
                    through = (i['side'] == 'BUY' and event['aggressor'] == 'SELL' and Decimal(event['price']) < limit) or (i['side'] == 'SELL' and event['aggressor'] == 'BUY' and Decimal(event['price']) > limit)
                    if not through:
                        continue
                    ahead = min(Decimal(order['queue_remaining']), volume)
                    order['queue_remaining'] = str(Decimal(order['queue_remaining']) - ahead)
                    volume -= ahead
                    qty = min(volume, Decimal(order['remaining']))
                    if qty > 0:
                        reason = self._risk_reason(order, limit, Decimal(order['remaining']))
                        if reason:
                            order.update(status='REJECTED', reason=reason)
                            self.audit.append(deepcopy(order))
                            continue
                        self._fill(order, limit, qty, event['ts'], 'MAKER')
                        volume -= qty
            return
        if event['type'] != 'book':
            return
        with localcontext() as ctx:
            ctx.prec = 40
            shared_levels = {side: [[Decimal(p), Decimal(q)] for p, q in event[side]] for side in ('asks', 'bids')}
            for order in sorted(self.orders.values(), key=lambda o: o['sequence']):
                i = order['intent']
                if order['status'] != 'PENDING' or i['symbol'] != event['symbol'] or event.get('source_ts', event['ts']) <= order['arrival_ts']:
                    continue
                if i['kind'] == 'MAKER':
                    limit = Decimal(i['limit_price'])
                    opposite = event['asks'] if i['side'] == 'BUY' else event['bids']
                    if not opposite:
                        continue
                    cross = limit >= Decimal(opposite[0][0]) if i['side'] == 'BUY' else limit <= Decimal(opposite[0][0])
                    reason = 'post_only_cross' if cross else self._risk_reason(order, limit, Decimal(order['remaining']))
                    if reason:
                        order.update(status='REJECTED', reason=reason)
                    else:
                        order.update(status='RESTING', resting_ts=event['ts'], queue_remaining=str(i['queue_ahead_qty']))
                    continue
                levels = shared_levels['asks'] if i['side'] == 'BUY' else shared_levels['bids']
                remaining = Decimal(order['remaining'])
                if not levels:
                    continue
                levels = list(levels)
                total = sum(q for p, q in levels)
                modeled = False
                if total < remaining:
                    if self.execution.depth_extra_ticks is None:
                        if not i['reduce_only']:
                            order.update(status='REJECTED', reason='insufficient_depth')
                            self.audit.append(deepcopy(order))
                            continue
                    else:
                        shift = self.instruments[i['symbol']].tick * self.execution.depth_extra_ticks
                        price = levels[-1][0] + (shift if i['side'] == 'BUY' else -shift)
                        if price <= 0:
                            order.update(status='REJECTED', reason='invalid_modeled_depth_price')
                            continue
                        levels.append([price, remaining - total])
                        modeled = True
                worst = max(p for p, q in levels) if i['side'] == 'BUY' else min(p for p, q in levels)
                reason = self._risk_reason(order, worst, remaining)
                if reason:
                    order.update(status='REJECTED', reason=reason)
                    self.audit.append(deepcopy(order))
                    continue
                for index, (price, available) in enumerate(levels):
                    qty = min(Decimal(order['remaining']), Decimal(available))
                    if qty > 0:
                        self._fill(order, Decimal(price), qty, event['ts'], 'TAKER', modeled and index == len(levels)-1)
                        levels[index][1] -= qty
                    if Decimal(order['remaining']) == 0:
                        break

    def _adverse_price(self, order, price):
        i = order['intent']
        ticks = self.execution.exit_fill_slippage_ticks if i['reduce_only'] else self.execution.entry_slippage_ticks
        ticks = ticks if ticks is not None else 0
        if ticks == 0:
            return price, ticks
        shift = self.instruments[i['symbol']].tick * ticks
        return price + (shift if i['side']=='BUY' else -shift), ticks

    def _risk_reason(self, order, price, qty):
        i = order['intent']
        if i['reduce_only']:
            p = self.positions.get(i['symbol'])
            if not p or (Decimal(p['qty']) > 0) == (i['side']=='BUY') or qty > abs(Decimal(p['qty'])):
                return 'no_reducible_position'
            return None
        if i['kind']=='TAKER':
            price, _ = self._adverse_price(order,price)
        r = self.risk
        if not self._risk_approved():
            return 'risk_missing_or_unapproved'
        p = self.positions.get(i['symbol'])
        if p and (Decimal(p['qty']) > 0) != (i['side']=='BUY'):
            return 'opposite_open_requires_reduce_only'
        if p is None and len(self.positions) >= r.max_positions:
            return 'risk_positions'
        if not self._funding_ready(i['symbol'], getattr(self, '_event_ts', i['decision_ts'])):
            return 'funding_feed_incomplete'
        s = self.instruments[i['symbol']]
        if price * Decimal(i['qty']) < s.min_notional:
            return 'min_notional'
        stop = Decimal(i['stop'])
        if (i['side'] == 'BUY' and stop >= price) or (i['side'] == 'SELL' and stop <= price):
            return 'wrong_stop_side'
        slip = s.tick * max(self.execution.exit_slippage_ticks, self.execution.exit_fill_slippage_ticks or 0)
        exit_price = stop - slip if i['side'] == 'BUY' else stop + slip
        if exit_price <= 0:
            return 'invalid_exit_price'
        potential_loss = qty * (abs(price - stop) + slip + exit_price * s.taker_fee + price * s.taker_fee)
        existing_loss = Decimal(0)
        if p:
            if Decimal(p['stop']) != stop:
                return 'stop_change_not_supported'
            existing_loss = abs(Decimal(p['qty'])) * (abs(Decimal(p['entry']) - stop) + slip + exit_price * s.taker_fee)
        if potential_loss + existing_loss > self.risk.max_loss_per_trade_usdt:
            return 'risk_per_trade'
        equity = self.equity
        if equity is None:
            return 'risk_mark_missing'
        day = str(getattr(self, '_event_ts', i['decision_ts']) // 86400000)
        baseline = self.day_baselines.get(day)
        if baseline is None:
            return 'risk_day_baseline_missing'
        reserved = Decimal(0)
        for symbol, position in self.positions.items():
            ps = self.instruments[symbol]
            pq = Decimal(position['qty'])
            pstop = Decimal(position['stop'])
            pslip = ps.tick * max(self.execution.exit_slippage_ticks, self.execution.exit_fill_slippage_ticks or 0)
            pexit = pstop - pslip if pq > 0 else pstop + pslip
            distance = (Decimal(self.marks[symbol]) - pstop) * (1 if pq > 0 else -1)
            reserved += abs(pq) * (max(Decimal(0), distance) + pslip + max(Decimal(0), pexit) * ps.taker_fee)
        if max(Decimal(0), Decimal(baseline) - equity) + reserved + potential_loss > self.risk.max_daily_loss_usdt:
            return 'risk_daily_loss'
        if max(Decimal(0), Decimal(self.initial_cash) - equity) + reserved + potential_loss > self.risk.total_loss_limit_usdt:
            return 'risk_total_loss'
        mark = Decimal(self.marks[i['symbol']]) if i['symbol'] in self.marks else price
        immediate_loss = max(Decimal(0), qty * (price - mark) * (1 if i['side']=='BUY' else -1))
        equity_after = equity - price * qty * s.taker_fee - immediate_loss
        new_gross = max(price, mark) * qty
        current = sum(abs(Decimal(p['qty'])) * Decimal(self.marks[symbol]) for symbol, p in self.positions.items())
        if equity <= 0 or equity_after <= 0 or current / equity > self.risk.max_effective_exposure_x or (current + new_gross) / equity_after > self.risk.max_effective_exposure_x:
            return 'risk_exposure'
        return None

    def _fill(self, order, price, qty, ts, liquidity, modeled_depth=False):
        i = order['intent']
        observed_price = price
        fill_slippage_ticks = 0
        if liquidity == 'TAKER':
            price, fill_slippage_ticks = self._adverse_price(order,price)
        if price <= 0:
            raise ValueError('nonpositive adverse execution price; no fill')
        fee_rate = self.instruments[i['symbol']].maker_fee if liquidity == 'MAKER' else self.instruments[i['symbol']].taker_fee
        fee = price * qty * fee_rate
        self.cash -= fee
        signed = qty if i['side'] == 'BUY' else -qty
        p = self.positions.get(i['symbol'])
        old_qty = Decimal(p['qty']) if p else Decimal(0)
        old_entry = Decimal(p['entry']) if p else Decimal(0)
        new_qty = old_qty + signed
        if old_qty and old_qty * signed < 0:
            realized = qty * (price - old_entry) * (1 if old_qty > 0 else -1)
            self.cash += realized
            self.ledger.append(dict(ledger_id='ledger:'+str(len(self.ledger)+1), type='realized', amount=str(realized), ts=ts, order_id=order['order_id']))
            if new_qty == 0:
                del self.positions[i['symbol']]
            else:
                p['qty'] = str(new_qty)
        else:
            entry = (old_entry * abs(old_qty) + price * qty) / abs(new_qty)
            self.positions[i['symbol']] = dict(qty=str(new_qty), entry=str(entry), stop=str(i['stop']), opened_ts=p['opened_ts'] if p else ts)
        order['remaining'] = str(Decimal(order['remaining']) - qty)
        if Decimal(order['remaining']) == 0:
            order['status'] = 'FILLED'
        fill = dict(fill_id='fill:'+str(len(self.fills)+1), order_id=order['order_id'], intent_id=i['intent_id'], symbol=i['symbol'], side=i['side'], qty=str(qty), price=str(price), fee=str(fee), ts=ts, liquidity=liquidity, version_id=self.version_id, forward_start=self.forward_start, modeled_depth=modeled_depth, category=self.instruments[i['symbol']].category, sim_only=True, observed_price=str(observed_price), fill_slippage_ticks=fill_slippage_ticks)
        self.fills.append(fill)
        self.ledger.append(dict(ledger_id='ledger:'+str(len(self.ledger)+1), type='fee', amount=str(-fee), ts=ts, fill_id=fill['fill_id']))

    def close(self):
        if getattr(self, '_db', None) is not None:
            self._db.close()
            self._db = None
        if getattr(self, '_fd', None) is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def self_check():
    """Run artificial unit fixtures only; never create production risk approval."""
    import io
    import unittest
    loader = unittest.TestLoader()
    discovered = loader.discover(str(Path(__file__).parent / 'tests'), pattern='test_sim_broker.py')
    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            elif not item.id().endswith('test_artificial_selfcheck_callable_and_sim_only_interface'):
                yield item
    suite = unittest.TestSuite(flatten(discovered))
    output = io.StringIO()
    result = unittest.TextTestRunner(stream=output, verbosity=1).run(suite)
    return dict(label='ARTIFICIAL ENGINEERING SELF-CHECK — NOT MARKET PERFORMANCE', ok=result.wasSuccessful() and result.testsRun > 0, tests_run=result.testsRun, stdout=output.getvalue())


if __name__ == '__main__':
    result = self_check()
    print(result['label'])
    print(result['stdout'], end='')
    raise SystemExit(0 if result['ok'] else 1)
