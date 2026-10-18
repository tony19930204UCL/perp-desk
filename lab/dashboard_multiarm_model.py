"""Pure, conservative PAPER broker snapshot projection; no I/O, no floats."""
from decimal import Decimal, InvalidOperation, localcontext

ARMS = (
    ('h2', 'paper-v4'),
    ('f1h-btcusdt', 'paper-f1h-btcusdt'),
    ('f1h-solusdt', 'paper-f1h-solusdt'),
    ('f1h-xrpusdt', 'paper-f1h-xrpusdt'),
    ('w1-btcusdt', 'paper-w1-btcusdt'),
    ('w1-ethusdt', 'paper-w1-ethusdt'),
    ('w1-solusdt', 'paper-w1-solusdt'),
    ('w1-xrpusdt', 'paper-w1-xrpusdt'),
    ('t1-btcusdt', 'paper-t1-btcusdt'),
    ('t1-ethusdt', 'paper-t1-ethusdt'),
    ('t1-solusdt', 'paper-t1-solusdt'),
    ('t1-xrpusdt', 'paper-t1-xrpusdt'),
)
ACTIVE = frozenset(('PENDING', 'RESTING'))
TERMINAL = frozenset(('FILLED', 'EXPIRED', 'CANCELED', 'REJECTED'))
LIMIT = 500


def dec(value):
    if not isinstance(value, str):
        return None
    try:
        out = Decimal(value)
        return out if out.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def exact(value):
    return str(value) if isinstance(value, Decimal) and value.is_finite() else None


def integer(value):
    return value if type(value) is int and value >= 0 else None


def unknown(arm, code):
    return dict(id=arm, state='unknown', reason=code, stale=None, age_s=None,
                last_ts=None, account=dict(initial_cash=None, cash=None,
                equity=None, unrealized=None, modelled=False),
                positions=dict(rows=[], total=None, truncated=False, state='unknown'),
                closed=dict(rows=[], total=None, truncated=False, state='unknown'),
                orders=dict(rows=[], total=None, truncated=False, state='unknown'),
                open_orders=dict(rows=[], total=None, truncated=False, state='unknown'),
                fills=dict(rows=[], total=None, truncated=False, state='unknown'),
                reconcile=dict(state='unknown', attributed=None,
                               unattributed=None, delta=None, reason=code))


def table(rows, *, valid=True, key='ts'):
    if not valid:
        return dict(rows=[], total=None, truncated=False, state='unknown')
    ordered = sorted(rows, key=lambda row: row.get(key) if type(row.get(key)) is int else -1,
                     reverse=True)
    return dict(rows=ordered[:LIMIT], total=len(rows), truncated=len(rows) > LIMIT,
                state='known')


def episodes(fills, ledger):
    """Flat-to-flat per symbol; uncertain crossing/identity emits UNKNOWN row."""
    by_symbol = {}
    fill_ep = {}
    order_ep = {}
    result = []
    for fill in fills:
        sym = fill.get('symbol')
        if not isinstance(sym, str) or not sym:
            result.append(dict(symbol='未知', state='unknown', reason='FILL_SYMBOL'))
            continue
        ctx = by_symbol.setdefault(sym, dict(q=Decimal(0), active=None))
        qty = dec(fill.get('qty'))
        side = fill.get('side')
        price = dec(fill.get('price'))
        ts = integer(fill.get('ts'))
        signed = qty * (1 if side == 'BUY' else -1) if qty is not None and qty > 0 and side in ('BUY', 'SELL') else None
        if ctx['active'] is None:
            ctx['active'] = dict(symbol=sym, start=ts, end=None, side='LONG' if signed is not None and signed > 0 else 'SHORT',
                                 entry_qty=Decimal(0), entry_cost=Decimal(0),
                                 exit_qty=Decimal(0), exit_cost=Decimal(0), ids=set(),
                                 orders=set(), invalid=False, reason=None)
        ep = ctx['active']
        fid = fill.get('fill_id')
        oid = fill.get('order_id')
        if not isinstance(fid, str) or not fid or fid in fill_ep or not isinstance(oid, str):
            ep['invalid'] = True
            ep['reason'] = 'FILL_ID'
        else:
            fill_ep[fid] = ep
            ep['ids'].add(fid)
            ep['orders'].add(oid)
        if signed is None or price is None or price <= 0 or ts is None:
            ep['invalid'] = True
            ep['reason'] = 'FILL_FIELDS'
            ctx['q'] = Decimal(0)
        else:
            old = ctx['q']
            new = old + signed
            if old and old * signed < 0:
                ep['exit_qty'] += abs(signed)
                ep['exit_cost'] += abs(signed) * price
                order_ep.setdefault(oid, []).append(ep)
            else:
                ep['entry_qty'] += abs(signed)
                ep['entry_cost'] += abs(signed) * price
            if old and old * new < 0:
                ep['invalid'] = True
                ep['reason'] = 'ZERO_CROSS'
            if old and old * signed > 0 and ep['exit_qty']:
                ep['invalid'] = True
                ep['reason'] = 'REOPEN_BEFORE_FLAT'
            ctx['q'] = new
        if ctx['q'] == 0 or ep['invalid']:
            ep['end'] = ts
            result.append(ep)
            ctx['active'] = None
            ctx['q'] = Decimal(0)
    # Any unmatched/open episode remains an open position, not a completed round trip.
    matched = set()
    rows = []
    attributed = Decimal(0)
    for ep in result:
        realized = Decimal(0)
        fee = Decimal(0)
        funding = Decimal(0)
        exit_reason = None
        for index, item in enumerate(ledger):
            val = dec(item.get('amount'))
            typ = item.get('type')
            if val is None:
                ep['invalid'] = True
                ep['reason'] = 'LEDGER_AMOUNT'
                continue
            applies = False
            if typ == 'fee' and item.get('fill_id') in ep['ids']:
                fee += val
                applies = True
            elif typ == 'realized' and ep in order_ep.get(item.get('order_id'), []):
                realized += val
                applies = True
                suffix = str(item.get('order_id', '')).rsplit(':', 1)[-1]
                if suffix in ('take_profit', 'stop'):
                    exit_reason = suffix
            elif typ == 'funding' and item.get('symbol') == ep['symbol'] and (
                    integer(item.get('ts')) is not None and ep['start'] is not None
                    and ep['end'] is not None and ep['start'] <= item['ts'] <= ep['end']
                    and dec(item.get('qty')) not in (None, Decimal(0))):
                funding += val
                applies = True
            if applies:
                if index in matched:
                    ep['invalid'] = True
                    ep['reason'] = 'LEDGER_DUPLICATE'
                matched.add(index)
        if ep['entry_qty'] == 0 or ep['exit_qty'] == 0:
            ep['invalid'] = True
            ep['reason'] = ep['reason'] or 'EPISODE_FIELDS'
        valid = not ep['invalid']
        entry = exact(ep['entry_cost'] / ep['entry_qty']) if ep['entry_qty'] else None
        exit_px = exact(ep['exit_cost'] / ep['exit_qty']) if ep['exit_qty'] else None
        net = realized + fee + funding
        rows.append(dict(symbol=ep['symbol'], state='known' if valid else 'unknown',
                         reason=None if valid else ep['reason'], side=ep['side'],
                         opened_ts=ep['start'], closed_ts=ep['end'], ts=ep['end'],
                         quantity=exact(ep['entry_qty']) if valid else None,
                         entry=entry if valid else None, exit=exit_px if valid else None,
                         realized=exact(realized) if valid else None,
                         fees=exact(-fee) if valid else None, funding=exact(funding) if valid else None,
                         net=exact(net) if valid else None,
                         hold_ms=ep['end'] - ep['start'] if valid and ep['end'] is not None and ep['start'] is not None and ep['end'] >= ep['start'] else None,
                         exit_reason=exit_reason))
        if valid:
            attributed += net
    return rows, matched, attributed


def project(arm, payload, *, now_ms, stale_after_s=90):
    if type(now_ms) is not int or type(stale_after_s) is not int or stale_after_s < 0:
        raise ValueError('clock or freshness threshold invalid')
    if not isinstance(payload, dict):
        return unknown(arm, 'BAD_JSON')
    required = ('initial_cash', 'cash', 'last_ts', 'positions', 'marks', 'orders', 'fills', 'ledger')
    if any(field not in payload for field in required):
        return unknown(arm, 'MISSING_FIELD')
    p = unknown(arm, 'INVALID_FIELD')
    p['state'] = 'known'
    p['reason'] = None
    ts = integer(payload['last_ts'])
    p['last_ts'] = ts
    if ts is None or now_ms < ts:
        p['stale'], p['age_s'] = None, None
        p['reason'] = 'BAD_LAST_TS'
    else:
        p['age_s'] = (now_ms - ts) // 1000
        p['stale'] = now_ms - ts > stale_after_s * 1000
    cash, initial = dec(payload['cash']), dec(payload['initial_cash'])
    p['account']['cash'] = exact(cash)
    p['account']['initial_cash'] = exact(initial)
    positions, marks = payload['positions'], payload['marks']
    unreal = Decimal(0)
    position_valid = isinstance(positions, dict) and isinstance(marks, dict)
    pos_rows = []
    if position_valid:
        for sym, item in positions.items():
            if not isinstance(sym, str) or not isinstance(item, dict):
                position_valid = False
                break
            qty = dec(item.get('qty'))
            entry = dec(item.get('entry'))
            mark = dec(marks.get(sym))
            stop = dec(item.get('stop'))
            valid = qty is not None and qty != 0 and entry is not None and mark is not None
            delta = qty * (mark - entry) if valid else None
            if valid:
                unreal += delta
            else:
                position_valid = False
            pos_rows.append(dict(symbol=sym, side='LONG' if qty is not None and qty > 0 else 'SHORT' if qty is not None and qty < 0 else None,
                                 qty=exact(qty), entry=exact(entry), mark=exact(mark),
                                 stop=exact(stop), unrealized=exact(delta),
                                 opened_ts=integer(item.get('opened_ts')), ts=integer(item.get('opened_ts')),
                                 state='known' if valid else 'unknown',
                                 reason=None if valid else 'POSITION_FIELDS'))
    p['positions'] = table(pos_rows, valid=isinstance(positions, dict), key='ts')
    p['account']['unrealized'] = exact(unreal) if position_valid else None
    p['account']['equity'] = exact(cash + unreal) if cash is not None and position_valid else None
    p['account']['modelled'] = bool(cash is not None and position_valid)
    orders = payload['orders']
    if isinstance(orders, dict):
        order_rows = []
        for oid, item in orders.items():
            if not isinstance(item, dict):
                order_rows.append(dict(id=str(oid), status='未知', state='unknown', reason='ORDER_FIELDS', ts=None))
                continue
            intent = item.get('intent')
            intent = intent if isinstance(intent, dict) else {}
            status = item.get('status')
            status = status if isinstance(status, str) else '未知'
            order_rows.append(dict(id=str(oid), status=status, state='known' if status in ACTIVE | TERMINAL else 'unknown',
                                   symbol=intent.get('symbol') if isinstance(intent.get('symbol'), str) else None,
                                   side=intent.get('side') if isinstance(intent.get('side'), str) else None,
                                   qty=intent.get('qty') if dec(intent.get('qty')) is not None else None,
                                   remaining=item.get('remaining') if dec(item.get('remaining')) is not None else None,
                                   price=intent.get('limit_price') if dec(intent.get('limit_price')) is not None else None,
                                   reason=item.get('reason') if isinstance(item.get('reason'), str) else None,
                                   ts=integer(item.get('arrival_ts'))))
        p['orders'] = table(order_rows)
        p['open_orders'] = table([x for x in order_rows if x['status'] in ACTIVE])
    rawfills = payload['fills']
    if isinstance(rawfills, list):
        fill_rows = []
        valid_fills = True
        for item in rawfills:
            if not isinstance(item, dict):
                valid_fills = False
                break
            fill_rows.append(dict(id=item.get('fill_id') if isinstance(item.get('fill_id'), str) else None,
                                  order_id=item.get('order_id') if isinstance(item.get('order_id'), str) else None,
                                  symbol=item.get('symbol') if isinstance(item.get('symbol'), str) else None,
                                  side=item.get('side') if item.get('side') in ('BUY', 'SELL') else None,
                                  qty=item.get('qty') if dec(item.get('qty')) is not None else None,
                                  price=item.get('price') if dec(item.get('price')) is not None else None,
                                  fee=item.get('fee') if dec(item.get('fee')) is not None else None,
                                  liquidity=item.get('liquidity') if isinstance(item.get('liquidity'), str) else None,
                                  ts=integer(item.get('ts'))))
        p['fills'] = table(fill_rows, valid=valid_fills)
    ledger = payload['ledger']
    if isinstance(rawfills, list) and isinstance(ledger, list) and all(isinstance(x, dict) for x in rawfills + ledger):
        closed, matched, attributed = episodes(rawfills, ledger)
        p['closed'] = table(closed)
        values = [dec(entry.get('amount')) for entry in ledger]
        if cash is None or initial is None or any(v is None for v in values):
            p['reconcile']['reason'] = 'LEDGER_AMOUNT'
        else:
            remaining = sum((v for i, v in enumerate(values) if i not in matched), Decimal(0))
            net = sum(values, Decimal(0))
            delta = cash - initial
            p['reconcile'] = dict(state='consistent' if delta == net else 'inconsistent',
                                  attributed=exact(attributed), unattributed=exact(remaining),
                                  delta=exact(delta), reason=None)
    return p


def project_all(values, *, now_ms, stale_after_s=90):
    return dict(kind='PAPER', read_only=True, tick_seconds=15, now_ms=now_ms,
                arms=[project(arm, values.get(arm), now_ms=now_ms, stale_after_s=stale_after_s)
                      if values.get(arm) is not None else unknown(arm, 'SOURCE_UNAVAILABLE')
                      for arm, _ in ARMS])
