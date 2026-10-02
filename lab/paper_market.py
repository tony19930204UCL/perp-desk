"""Public-market-only PAPER adapter. Never creates exchange orders or reads secrets."""
from perp_collector import PublicClient
from datetime import datetime
from decimal import Decimal


def decimal_string(value, positive=False):
    if not isinstance(value, str):
        raise ValueError('source amounts must be exact decimal strings')
    number = Decimal(value)
    if not number.is_finite() or (positive and number <= 0):
        raise ValueError('invalid source decimal')
    return value


def receipt_ms(receipt):
    stamp = datetime.fromisoformat(receipt['received_at'])
    if stamp.utcoffset() is None:
        raise ValueError('receipt timestamp must have timezone')
    return int(stamp.timestamp() * 1000)


def finalized_funding(receipt, symbol, forward_start_ms):
    if receipt.get('endpoint') != '/fapi/v1/fundingRate':
        raise ValueError('predicted premiumIndex rate is not finalized funding')
    rows = receipt['payload']
    if not isinstance(rows, list):
        raise ValueError('funding response must be an array')
    received_ms = receipt_ms(receipt)
    result = []
    previous = -1
    for row in rows:
        ts = row['fundingTime']
        if (row['symbol'] != symbol or type(ts) is not int or ts < 0
                or ts > received_ms or ts <= previous):
            raise ValueError('invalid, duplicate or unsorted funding event')
        rate = decimal_string(row['fundingRate'])
        price = decimal_string(row['markPrice'], positive=True)
        previous = ts
        if ts >= forward_start_ms:
            result.append({'kind': 'funding', 'symbol': symbol, 'ts_ms': ts,
                           'rate': rate, 'mark_price': price,
                           'id': f'{symbol}:funding:{ts}',
                           'source': '/fapi/v1/fundingRate'})
    return result


def fresh_source(receipt, symbol, max_source_age_ms):
    if receipt.get('params', {}).get('symbol') != symbol:
        raise ValueError('source symbol mismatch')
    ts = receipt.get('source_timestamp_ms')
    observed = receipt_ms(receipt)
    if type(ts) is not int or not -5000 <= observed - ts <= max_source_age_ms:
        raise ValueError('stale, future or missing market source timestamp')
    return ts, observed


def book_event(receipt, symbol, max_source_age_ms):
    if receipt.get('endpoint') != '/fapi/v1/depth':
        raise ValueError('depth event requires an observed depth response')
    ts, observed = fresh_source(receipt, symbol, max_source_age_ms)
    payload = receipt['payload']
    sides = {}
    for name in ('bids', 'asks'):
        levels = payload[name]
        if not isinstance(levels, list) or not levels:
            raise ValueError('empty observed book side')
        result = []
        previous = None
        for level in levels:
            if not isinstance(level, list) or len(level) != 2:
                raise ValueError('invalid observed depth level')
            price = decimal_string(level[0], positive=True)
            quantity = decimal_string(level[1], positive=True)
            value = Decimal(price)
            if previous is not None and not (value < previous if name == 'bids' else value > previous):
                raise ValueError('unsorted or duplicate observed depth prices')
            previous = value
            result.append([price, quantity])
        sides[name] = result
    if Decimal(sides['asks'][0][0]) <= Decimal(sides['bids'][0][0]):
        raise ValueError('crossed or locked observed book')
    return {'kind': 'book', 'symbol': symbol, 'ts_ms': ts, 'observed_ms': observed,
            'bids': sides['bids'], 'asks': sides['asks'], 'source': 'REST depth snapshot'}


def mark_event(receipt, symbol, max_source_age_ms):
    if receipt.get('endpoint') != '/fapi/v1/premiumIndex':
        raise ValueError('mark event requires premiumIndex, not a candle close')
    ts, observed = fresh_source(receipt, symbol, max_source_age_ms)
    if receipt['payload'].get('symbol') != symbol:
        raise ValueError('mark source symbol mismatch')
    price = decimal_string(receipt['payload']['markPrice'], positive=True)
    return {'kind': 'mark', 'symbol': symbol, 'ts_ms': ts, 'observed_ms': observed,
            'mark_price': price, 'source': '/fapi/v1/premiumIndex'}


def instrument(receipt, symbol, category, fees):
    rows = [s for s in receipt['payload']['symbols'] if s['symbol'] == symbol]
    if len(rows) != 1:
        raise ValueError('ambiguous or absent symbol')
    row = rows[0]
    expected_type = {'crypto': 'PERPETUAL', 'TradFi': 'TRADIFI_PERPETUAL'}.get(category)
    if (expected_type is None or row['contractType'] != expected_type
            or row['status'] != 'TRADING' or row['quoteAsset'] != 'USDT'
            or row['marginAsset'] != 'USDT'):
        raise ValueError('unsupported or inactive USDT margined perpetual')
    filters = {f['filterType']: f for f in row['filters']}
    tick = decimal_string(filters['PRICE_FILTER']['tickSize'], positive=True)
    lot = filters['LOT_SIZE']
    market_lot = filters['MARKET_LOT_SIZE']
    step = decimal_string(lot['stepSize'], positive=True)
    market_step = decimal_string(market_lot['stepSize'])
    if Decimal(market_step) < 0:
        raise ValueError('negative market step')
    lower = max(Decimal(decimal_string(lot['minQty'], positive=True)),
                Decimal(decimal_string(market_lot['minQty'])))
    upper = min(Decimal(decimal_string(lot['maxQty'], positive=True)),
                Decimal(decimal_string(market_lot['maxQty'], positive=True)))
    if lower <= 0 or upper < lower:
        raise ValueError('inconsistent quantity bounds')
    notional = decimal_string(filters['MIN_NOTIONAL']['notional'], positive=True)
    maker = decimal_string(fees['maker'])
    taker = decimal_string(fees['taker'])
    if min(Decimal(maker), Decimal(taker)) < 0:
        raise ValueError('negative fee assumption')
    return {'symbol': symbol, 'category': category, 'contract_type': row['contractType'],
            'status': row['status'], 'tick_size': tick, 'qty_step': step,
            'market_qty_step': market_step, 'min_qty': str(lower), 'max_qty': str(upper),
            'min_notional': notional, 'maker_fee': maker, 'taker_fee': taker,
            'fee_source': 'user-supplied assumption, not account/VIP verified'}


def probe_inputs(client, symbol, category, fees, max_source_age_ms):
    """Exercise real GET/validation pipeline without creating paper orders or balances."""
    reference = client.get('/fapi/v1/exchangeInfo')
    spec = instrument(reference, symbol, category, fees)
    depth = client.get('/fapi/v1/depth', {'symbol': symbol, 'limit': 5})
    mark = client.get('/fapi/v1/premiumIndex', {'symbol': symbol})
    funding = client.get('/fapi/v1/fundingRate', {'symbol': symbol, 'limit': 2})
    return {'mode': 'public_input_probe_no_trades', 'symbol': symbol,
            'instrument': spec, 'book': book_event(depth, symbol, max_source_age_ms),
            'mark': mark_event(mark, symbol, max_source_age_ms),
            'finalized_funding': finalized_funding(funding, symbol, 0),
            'receipts': [reference, depth, mark, funding]}


class PublicMarketClient(PublicClient):
    ALLOWED = {**PublicClient.ALLOWED,
               '/fapi/v1/fundingRate': {'symbol', 'startTime', 'endTime', 'limit'},
               '/fapi/v1/time': set()}
