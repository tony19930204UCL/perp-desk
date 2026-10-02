"""Synthetic UNIT fixtures only, never market or performance data."""
import importlib.util
import io
import json
from pathlib import Path
import unittest

LAB = Path(__file__).resolve().parents[1]


def module():
    path = LAB / 'paper_market.py'
    if not path.exists():
        raise AssertionError('paper funding history adapter is not implemented')
    spec = importlib.util.spec_from_file_location('paper_market_under_test', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class MarketTests(unittest.TestCase):
    def test_finalized_funding_is_available_only_through_public_get(self):
        adapter = module()
        seen = []
        payload = [{'symbol': 'ETHUSDT', 'fundingTime': 1790841600005,
                    'fundingRate': '0.00007243', 'markPrice': '2676.00210078'}]
        def opener(request, timeout):
            seen.append((request.full_url, request.get_method(), dict(request.headers)))
            return io.BytesIO(json.dumps(payload).encode())
        client = adapter.PublicMarketClient(opener=opener, sleep=lambda _: None,
                                            clock=lambda: '2026-10-01T15:00:00Z')
        receipt = client.get('/fapi/v1/fundingRate', {'symbol': 'ETHUSDT', 'limit': 2})
        self.assertEqual(receipt['payload'], payload)
        self.assertEqual(seen[0][1], 'GET')
        self.assertFalse(any('api' in k.lower() for k in seen[0][2]))
        for path in ('/fapi/v1/order', '/fapi/v2/account', '/fapi/v1/userTrades'):
            with self.assertRaises(ValueError):
                client.get(path, {'symbol': 'ETHUSDT'})
        self.assertEqual(len(seen), 1)
    def test_finalized_funding_uses_exact_settlement_price_and_time(self):
        adapter = module()
        receipt = {'endpoint': '/fapi/v1/fundingRate', 'params': {'symbol': 'ETHUSDT'},
                   'received_at': '2026-10-01T15:00:00Z',
                   'payload': [{'symbol': 'ETHUSDT', 'fundingTime': 1790841600005,
                                'fundingRate': '0.00007243', 'markPrice': '2676.00210078'}]}
        self.assertTrue(callable(getattr(adapter, 'finalized_funding', None)),
                        'funding must be normalized from finalized public settlement records')
        events = adapter.finalized_funding(receipt, 'ETHUSDT', 1790800000000)
        self.assertEqual(events, [{'kind': 'funding', 'symbol': 'ETHUSDT',
                          'ts_ms': 1790841600005, 'rate': '0.00007243',
                          'mark_price': '2676.00210078',
                          'id': 'ETHUSDT:funding:1790841600005',
                          'source': '/fapi/v1/fundingRate'}])
        for field, value in [('fundingRate', 'NaN'), ('markPrice', '0'),
                             ('symbol', 'BTCUSDT'), ('fundingTime', 9999999999999)]:
            bad = json.loads(json.dumps(receipt))
            bad['payload'][0][field] = value
            with self.assertRaises(ValueError, msg=f'invalid finalized funding {field}'):
                adapter.finalized_funding(bad, 'ETHUSDT', 1790800000000)
        self.assertEqual(adapter.finalized_funding(receipt, 'ETHUSDT', 1790900000000), [])
    def test_book_depth_rejects_stale_crossed_and_nonfinite_sources(self):
        from datetime import datetime
        adapter = module()
        self.assertTrue(callable(getattr(adapter, 'book_event', None)),
                        'only valid fresh observed depth may feed paper execution')
        now = int(datetime.fromisoformat('2026-10-01T08:00:00Z').timestamp() * 1000)
        receipt = {'endpoint': '/fapi/v1/depth', 'params': {'symbol': 'ETHUSDT'},
                   'received_at': '2026-10-01T08:00:00Z',
                   'source_timestamp_ms': now,
                   'payload': {'E': now, 'T': now, 'lastUpdateId': 4,
                               'bids': [['99', '2'], ['98', '3']],
                               'asks': [['100', '1'], ['101', '4']]}}
        event = adapter.book_event(receipt, 'ETHUSDT', 15000)
        self.assertEqual(event['kind'], 'book')
        self.assertEqual(event['ts_ms'], now)
        self.assertEqual(event['bids'], [['99', '2'], ['98', '3']])
        self.assertEqual(event['asks'], [['100', '1'], ['101', '4']])
        self.assertEqual(event['source'], 'REST depth snapshot')
        variants = []
        stale = json.loads(json.dumps(receipt)); stale['source_timestamp_ms'] = now - 15001; variants.append(stale)
        crossed = json.loads(json.dumps(receipt)); crossed['payload']['asks'][0][0] = '98'; variants.append(crossed)
        invalid = json.loads(json.dumps(receipt)); invalid['payload']['bids'][0][1] = 'NaN'; variants.append(invalid)
        unordered = json.loads(json.dumps(receipt)); unordered['payload']['asks'].reverse(); variants.append(unordered)
        wrong = json.loads(json.dumps(receipt)); wrong['params']['symbol'] = 'XAUUSDT'; variants.append(wrong)
        for bad in variants:
            with self.assertRaises(ValueError):
                adapter.book_event(bad, 'ETHUSDT', 15000)
    def test_instrument_uses_exchange_filters_not_display_precision(self):
        adapter = module()
        self.assertTrue(callable(getattr(adapter, 'instrument', None)),
                        'paper quantities must use exchange tick and step filters')
        row = {'symbol': 'ETHUSDT', 'status': 'TRADING', 'contractType': 'PERPETUAL',
               'quoteAsset': 'USDT', 'marginAsset': 'USDT', 'pricePrecision': 1,
               'quantityPrecision': 1, 'filters': [
                   {'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},
                   {'filterType': 'LOT_SIZE', 'minQty': '0.001', 'maxQty': '100', 'stepSize': '0.001'},
                   {'filterType': 'MARKET_LOT_SIZE', 'minQty': '0.005', 'maxQty': '50', 'stepSize': '0.001'},
                   {'filterType': 'MIN_NOTIONAL', 'notional': '5'}]}
        receipt = {'payload': {'symbols': [row]}}
        fees = {'maker': '0.0002', 'taker': '0.0005'}
        result = adapter.instrument(receipt, 'ETHUSDT', 'crypto', fees)
        self.assertEqual(result['tick_size'], '0.01')
        self.assertEqual(result['qty_step'], '0.001')
        self.assertEqual(result['min_qty'], '0.005')
        self.assertEqual(result['max_qty'], '50')
        self.assertEqual(result['min_notional'], '5')
        self.assertEqual(result['taker_fee'], fees['taker'])
        for key, bad in [('status', 'PENDING_TRADING'), ('quoteAsset', 'USD'),
                         ('marginAsset', 'BTC'), ('contractType', 'CURRENT_QUARTER')]:
            invalid = json.loads(json.dumps(receipt)); invalid['payload']['symbols'][0][key] = bad
            with self.assertRaises(ValueError):
                adapter.instrument(invalid, 'ETHUSDT', 'crypto', fees)
        invalid = json.loads(json.dumps(receipt)); invalid['payload']['symbols'][0]['filters'][0]['tickSize'] = '0'
        with self.assertRaises(ValueError):
            adapter.instrument(invalid, 'ETHUSDT', 'crypto', fees)
    def test_mark_event_is_not_a_funding_settlement(self):
        from datetime import datetime
        adapter = module()
        self.assertTrue(callable(getattr(adapter, 'mark_event', None)),
                        'stop triggers require validated actual mark observations')
        now = int(datetime.fromisoformat('2026-10-01T08:00:00Z').timestamp() * 1000)
        receipt = {'endpoint': '/fapi/v1/premiumIndex', 'params': {'symbol': 'ETHUSDT'},
                   'received_at': '2026-10-01T08:00:00Z', 'source_timestamp_ms': now,
                   'payload': {'symbol': 'ETHUSDT', 'markPrice': '99.5',
                               'lastFundingRate': '0.0001', 'nextFundingTime': now + 28800000}}
        result = adapter.mark_event(receipt, 'ETHUSDT', 15000)
        self.assertEqual(result, {'kind': 'mark', 'symbol': 'ETHUSDT', 'ts_ms': now,
                                 'observed_ms': now, 'mark_price': '99.5',
                                 'source': '/fapi/v1/premiumIndex'})
        self.assertNotIn('rate', result)
        with self.assertRaises(ValueError):
            adapter.finalized_funding(receipt, 'ETHUSDT', now)
        receipt['payload']['markPrice'] = 'NaN'
        with self.assertRaises(ValueError):
            adapter.mark_event(receipt, 'ETHUSDT', 15000)
    def test_probe_exercises_normalizers_without_paper_trades(self):
        from datetime import datetime
        adapter = module()
        self.assertTrue(callable(getattr(adapter, 'probe_inputs', None)),
                        'actual public input pipeline must be executable before activation')
        stamp = '2026-10-01T08:00:00Z'
        ts = int(datetime.fromisoformat(stamp).timestamp() * 1000)
        row = {'symbol': 'ETHUSDT', 'status': 'TRADING', 'contractType': 'PERPETUAL',
               'quoteAsset': 'USDT', 'marginAsset': 'USDT', 'filters': [
                   {'filterType': 'PRICE_FILTER', 'tickSize': '0.01'},
                   {'filterType': 'LOT_SIZE', 'minQty': '0.001', 'maxQty': '100', 'stepSize': '0.001'},
                   {'filterType': 'MARKET_LOT_SIZE', 'minQty': '0.001', 'maxQty': '100', 'stepSize': '0.001'},
                   {'filterType': 'MIN_NOTIONAL', 'notional': '5'}]}
        class Client:
            def __init__(self): self.endpoints = []
            def get(self, endpoint, params=None):
                self.endpoints.append(endpoint)
                data = {'/fapi/v1/exchangeInfo': {'symbols': [row]},
                        '/fapi/v1/depth': {'E': ts, 'bids': [['99','2']], 'asks': [['100','2']]},
                        '/fapi/v1/premiumIndex': {'symbol': 'ETHUSDT', 'markPrice': '99.5'},
                        '/fapi/v1/fundingRate': []}[endpoint]
                return {'endpoint': endpoint, 'params': params or {}, 'payload': data,
                        'received_at': stamp, 'source_timestamp_ms': ts}
        client = Client()
        result = adapter.probe_inputs(client, 'ETHUSDT', 'crypto',
                                     {'maker':'0.0002', 'taker':'0.0005'}, 15000)
        self.assertEqual(result['mode'], 'public_input_probe_no_trades')
        self.assertEqual(result['book']['asks'][0][0], '100')
        self.assertEqual(result['mark']['mark_price'], '99.5')
        self.assertEqual(result['finalized_funding'], [])
        self.assertEqual(len(result['receipts']), 4)
        self.assertNotIn('equity_usdt', result)
        self.assertNotIn('fills_count', result)
        self.assertEqual(set(client.endpoints), {'/fapi/v1/exchangeInfo', '/fapi/v1/depth',
                         '/fapi/v1/premiumIndex', '/fapi/v1/fundingRate'})


if __name__ == '__main__':
    unittest.main()
