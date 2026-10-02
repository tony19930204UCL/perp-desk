"""Isolated ARTIFICIAL fixtures, never performance or production state."""
import unittest
import tempfile
import json
import importlib
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

LAB = Path(__file__).resolve().parents[1]
BASE = 1790812800000
SCRATCH = tempfile.gettempdir()

def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()

class FixtureClient:
    label = 'ARTIFICIAL FIXTURE NOT PERFORMANCE'
    def __init__(self):
        self.now = BASE + 10000
        self.price = '100'
        self.bid = '99.99'
        self.ask = '100.01'
        self.bars = []
        self.stale = False
        self.missing = None
        self.calls = []
        self.funding_jitter = 0
        self.funding_interval = 8
        self.rate_type = 'Regular'
        self.funding_truncate = False
        self.next_funding_override = None
        self.kline_delay_ms = 0
    def get(self, endpoint, params=None):
        params = params or {}
        self.calls.append((endpoint, params))
        if endpoint == self.missing:
            raise OSError('ARTIFICIAL missing public source')
        symbol = params.get('symbol', 'ETHUSDT')
        source = self.now - (20000 if self.stale else 0)
        if endpoint.endswith('exchangeInfo'):
            filters = [dict(filterType='PRICE_FILTER', tickSize='0.01'),
                       dict(filterType='LOT_SIZE', stepSize='0.001', minQty='0.001', maxQty='1000'),
                       dict(filterType='MARKET_LOT_SIZE', stepSize='0.001', minQty='0.001', maxQty='1000'),
                       dict(filterType='MIN_NOTIONAL', notional='20')]
            payload = dict(symbols=[dict(symbol=s, contractType=t, status='TRADING', quoteAsset='USDT', marginAsset='USDT', filters=filters)
                for s,t in [('ETHUSDT','PERPETUAL'),('XAUUSDT','TRADIFI_PERPETUAL')]])
        elif endpoint.endswith('fundingInfo'):
            payload = [dict(symbol='ETHUSDT',fundingIntervalHours=self.funding_interval,adjustedFundingRateCap='0.003',adjustedFundingRateFloor='-0.003')]
        elif endpoint.endswith('premiumIndex'):
            payload = dict(symbol=symbol, markPrice=self.price, lastFundingRate='0.0001', time=source,
                           nextFundingTime=self.next_funding_override if self.next_funding_override is not None else (self.now//28800000+1)*28800000)
        elif endpoint.endswith('bookTicker'):
            payload = dict(symbol=symbol, bidPrice=self.bid, askPrice=self.ask, time=source)
        elif endpoint.endswith('depth'):
            payload = dict(E=source, bids=[[self.bid,'100']], asks=[[self.ask,'100']])
        elif endpoint.endswith('fundingRate'):
            times = [self.now//28800000*28800000 - 28800000, self.now//28800000*28800000]
            if 'startTime' in params:
                times = [t for t in range((BASE//28800000)*28800000, self.now+1, 28800000) if t+self.funding_jitter >= params['startTime']]
            payload = [dict(symbol=symbol, fundingTime=t+self.funding_jitter, fundingRate='0.0001', markPrice='100',rateType=self.rate_type) for t in times]
            if self.funding_truncate:
                payload=payload[-1:]
        elif endpoint.endswith('klines'):
            self.now+=self.kline_delay_ms
            payload = [b for b in self.bars if b[0] >= params['startTime']][:params['limit']]
        else:
            raise AssertionError(endpoint)
        return dict(endpoint=endpoint, params=params, received_at=iso(self.now), source_timestamp_ms=source, payload=payload)
    def closed_bar(self, i, price='100', volume='10'):
        t = BASE + i*300000
        self.bars.append([t,price,price,price,price,volume,t+299999,'0',0,'0','0','0'])
        self.now = t+300000+1000

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH, prefix='runtime-FIXTURE-')
        self.root = Path(self.tmp.name)
        self.client = FixtureClient()
        self.config = LAB/'paper_config.json'
        self.runtime = None
    def tearDown(self):
        if self.runtime:
            self.runtime.close()
        self.tmp.cleanup()
    def open(self):
        self.assertIsNotNone(importlib.util.find_spec('paper_runtime'), 'durable runtime integration missing')
        from paper_runtime import PaperRuntime
        self.runtime = PaperRuntime(self.root, self.config, client=self.client, clock_ms=lambda:self.client.now, fixture=True)
        return self.runtime
    def warm_signal(self, r):
        # Continuous artificial forward candles through the real polling path.
        for i in range(1,63):
            end=BASE+(i+1)*300000+1000
            while self.client.now+50000 < end:
                self.client.now+=50000
                r.poll()
            self.client.closed_bar(i, '95' if i==62 else '100', '21' if i==62 else '10')
            if i==62:
                self.client.price='95'
                self.client.bid='94.99'
                self.client.ask='95.01'
            s=r.poll()
        return s
    def test_signal_delayed_entry_and_restart_exact_accounting(self):
        r=self.open()
        r.poll()
        s=self.warm_signal(r)
        self.assertEqual(s['signals_count'],1)
        self.assertEqual(s['fills_count'],0, 'decision-time book is not an execution')
        self.assertEqual(len(r.broker.orders),1)
        # Emulate a crash after broker.submit commits but before runtime routing commits.
        r.state['handled_signals']={}
        r.save()
        # Clone only labelled fixture stores to test an offline restart.
        import shutil
        from paper_runtime import PaperRuntime
        decision_time=self.client.now
        r.close()
        branch=self.root/'offline-fixture'
        branch.mkdir()
        for p in self.root.glob('*.sqlite3'):
            shutil.copy2(p,branch/p.name)
        offline=PaperRuntime(branch,self.config,client=self.client,clock_ms=lambda:self.client.now,fixture=True)
        try:
            self.client.now+=61000
            offline_snapshot=offline.poll()
            self.assertEqual(offline_snapshot['fills_count'],0,'restart gap must cancel pending entry before receiving a book')
            self.assertTrue(all(o['status']=='CANCELED' for o in offline.broker.orders.values()))
        finally:
            offline.close()
        self.client.now=decision_time
        r=self.open()
        self.client.now+=1000
        self.assertEqual(r.poll()['fills_count'],0)
        r.state['handled_signals']={}
        r.save()
        r.close()
        r=self.open()
        self.client.now+=2000
        s=r.poll()
        self.assertIsNone(s['latest_error'],'recover target before a post-crash book fills the persisted order')
        self.assertEqual(s['fills_count'],1)
        self.assertEqual(s['positions'][0]['side'],'BUY')
        self.assertEqual(s['fills'][0]['observed_price'],'95.01')
        self.assertEqual(s['fills'][0]['price'],'95.03')
        self.assertFalse(s['fills'][0]['modeled_depth'])
        self.assertEqual(Decimal(s['cash_usdt']),Decimal('100')-Decimal(s['fees_usdt']))
        self.assertEqual(Decimal(s['equity_usdt']),Decimal(s['cash_usdt'])+Decimal(s['unrealized_pnl_usdt']))
        count=s['fills_count']
        self.assertEqual(r.poll()['fills_count'],count, 'repeated receipt cannot duplicate a fill')
        self.assertEqual(r.poll()['signals_count'],1)
    def test_exit_triggers_use_later_real_book_stop_takeprofit_maxhold(self):
        import shutil
        r=self.open()
        r.poll()
        self.warm_signal(r)
        self.client.now+=3000
        s=r.poll()
        self.assertEqual(s['fills_count'],1)
        entry_time=self.client.now
        r.close()
        seed=self.root/'seed'
        seed.mkdir()
        for p in self.root.glob('*.sqlite3'):
            shutil.copy2(p,seed/p.name)
        from paper_runtime import PaperRuntime
        for reason,price,elapsed in [('stop','94',1000),('take_profit','100',1000),('max_hold','95',1800001),('data_gap','95',61000)]:
            with self.subTest(reason=reason):
                branch=self.root/reason
                shutil.copytree(seed,branch)
                self.client.now=entry_time
                self.client.price='95'
                self.client.bid='94.99'
                self.client.ask='95.01'
                r=PaperRuntime(branch,self.config,client=self.client,clock_ms=lambda:self.client.now,fixture=True)
                self.runtime=r
                # Keep a genuine observation heartbeat until duration exit.
                while reason!='data_gap' and self.client.now+50000<entry_time+elapsed:
                    self.client.now+=50000
                    r.poll()
                self.client.now=entry_time+elapsed
                self.client.price=price
                self.client.bid=str(Decimal(price)-Decimal('0.01'))
                self.client.ask=str(Decimal(price)+Decimal('0.01'))
                s=r.poll()
                self.assertEqual(s['fills_count'],1, 'trigger mark cannot fill at stop/target')
                pending=[o for o in r.broker.orders.values() if o['intent']['reduce_only'] and o['status']=='PENDING']
                self.assertEqual(len(pending),1)
                self.client.now+=3000
                self.client.bid='93.99' if reason=='stop' else '99.99' if reason=='take_profit' else '94.99'
                self.client.ask=str(Decimal(self.client.bid)+Decimal('0.02'))
                s=r.poll()
                self.assertEqual(s['positions'],[])
                self.assertEqual(s['fills_count'],2)
                self.assertEqual(s['fills'][-1]['observed_price'],self.client.bid)
                self.assertEqual(Decimal(s['fills'][-1]['price']),Decimal(self.client.bid)-Decimal('0.02'))
                self.assertEqual(Decimal(s['cash_usdt']),Decimal('100')+Decimal(s['gross_realized_pnl_usdt'])-Decimal(s['fees_usdt'])+Decimal(s['funding_pnl_usdt']))
                r.close()
    def test_gap_and_stale_inputs_fail_closed_cancel_pending_reset_warmup(self):
        r=self.open()
        r.poll()
        while self.client.now+50000<BASE+601000:
            self.client.now+=50000
            r.poll()
        self.client.closed_bar(1)
        self.assertEqual(r.poll()['engine']['warmup_received'],1)
        self.client.stale=True
        self.client.now+=1000
        s=r.poll()
        self.assertEqual(s['runtime_state'],'blocked')
        self.assertFalse(s['paper_trading_enabled'])
        self.assertIn('stale',s['latest_error'])
        self.client.stale=False
        self.client.now+=61000
        self.client.closed_bar(2)
        s=r.poll()
        self.assertEqual(s['engine']['warmup_received'],0, 'offline bars cannot repair warmup using backfill')
        self.assertGreaterEqual(s['feed']['gaps_count'],1)
        self.assertEqual(s['fills_count'],0)
        self.client.missing='/fapi/v1/fundingRate'
        self.client.now+=31000
        s=r.poll()
        self.assertEqual(s['runtime_state'],'blocked')
        self.assertFalse(s['feed']['connected'])
        self.assertIn('missing',s['latest_error'])
    def test_once_cli_publishes_separate_valid_snapshot(self):
        import io
        from contextlib import redirect_stdout
        self.assertIsNotNone(importlib.util.find_spec('paper_runtime'))
        import paper_runtime
        self.assertTrue(callable(getattr(paper_runtime,'main',None)), 'public --once runtime CLI missing')
        out=io.StringIO()
        with redirect_stdout(out):
            code=paper_runtime.main(['--once','--state-dir',str(self.root),'--config',str(self.config),'--status',str(self.root/'shared'/'paper_status.json')],client=self.client,clock_ms=lambda:self.client.now,fixture=True)
        self.assertEqual(code,0)
        snapshot=json.loads((self.root/'shared'/'paper_status.json').read_text())
        self.assertEqual(snapshot['runtime_state'],'warming_up')
        self.assertEqual(json.loads(out.getvalue())['fills_count'],0)
        self.assertFalse((self.root/'shared'/'status.json').exists())
        self.assertEqual(len(snapshot['equity_history']),1, 'persist an actual snapshot point, never a synthetic plot')
        self.assertEqual(snapshot['equity_history'][0]['equity_usdt'],'100')
        self.assertIn('paper_runtime.py',snapshot['engine']['source_sha256'])
    def test_actual_funding_metadata_and_millisecond_settlement_preserved(self):
        self.client.funding_jitter=5
        r=self.open()
        s=r.poll()
        self.assertIsNone(s['latest_error'], '8h cap adjustment is not a changed interval')
        self.assertTrue(r.broker.funding_status['ETHUSDT']['complete'])
        # Cross a real schedule boundary in this isolated fixture, not performance.
        self.client.now=BASE+28800000+10000
        s=r.poll()
        self.assertIsNone(s['latest_error'])
        events=list(r.broker.funding_events.values())
        self.assertTrue(events)
        self.assertEqual(events[-1]['settlement_ts'],BASE+28800000+5)
        self.assertEqual(events[-1]['rate_type'],'Regular')
        self.client.funding_interval=4
        self.client.now+=31000
        self.assertIn('interval',r.poll()['latest_error'])
    def test_daily_total_halt_after_actual_fixture_gap_loss(self):
        r=self.open()
        r.poll()
        self.warm_signal(r)
        self.client.now+=3000
        self.assertEqual(r.poll()['fills_count'],1)
        self.client.now+=1000
        self.client.price='85'
        self.client.bid='84.99'
        self.client.ask='85.01'
        s=r.poll()
        self.assertEqual(s['runtime_state'],'blocked')
        self.assertFalse(s['paper_trading_enabled'])
        self.assertIn('risk_total_loss',s['blockers'])
        self.client.now+=3000
        s=r.poll()
        self.assertEqual(s['positions'],[],'risk halt must not block reductions')
        self.assertIn('risk_daily_loss',s['blockers'])
        self.assertIn('risk_total_loss',s['blockers'])
        self.assertLess(Decimal(s['cash_usdt']),Decimal('90'))
        r.close()
        r=self.open()
        self.assertIn('risk_total_loss',r.poll()['blockers'])
    def test_funding_missing_intermediate_slot_is_not_complete(self):
        r=self.open()
        self.assertIsNone(r.poll()['latest_error'])
        self.client.now=BASE+2*28800000+10000
        self.client.funding_truncate=True
        s=r.poll()
        self.assertEqual(s['runtime_state'],'blocked')
        self.assertIn('coverage',s['latest_error'])
        self.assertEqual(s['fills_count'],0)
    def test_broker_arrival_rejections_are_visible_in_snapshot(self):
        r=self.open()
        r.poll()
        from sim_broker import Intent
        spec=r.broker.instruments['ETHUSDT']
        order=r.broker.submit(Intent('ARTIFICIAL-REJECTED','ETHUSDT','BUY',Decimal('0.0001'),Decimal('99'),self.client.now,spec.quantity_step,spec.tick,spec.min_notional,spec.max_quantity,'TAKER',False))
        self.assertEqual(order['status'],'REJECTED')
        s=r.snapshot()
        self.assertTrue(any(x.get('order_id')==order['order_id'] for x in s['rejections']))
        self.assertEqual(s['orders'][0]['reason'],'instrument_filters')
    def test_new_namespace_rejects_unimplemented_or_loosened_strategy(self):
        from paper_runtime import PaperRuntime
        config=json.loads(self.config.read_text())
        config['strategy']['downside_sigma']='1'
        path=self.root/'unfrozen-config.json'
        path.write_text(json.dumps(config))
        with self.assertRaises(ValueError):
            other=PaperRuntime(self.root/'new-namespace',path,client=self.client,clock_ms=lambda:self.client.now,fixture=True)
            other.close()
    def test_changed_funding_deadline_blocks_unverified_schedule(self):
        self.client.next_funding_override=BASE+3600000
        r=self.open()
        s=r.poll()
        self.assertEqual(s['runtime_state'],'blocked')
        self.assertIn('funding deadline',s['latest_error'])
    def test_quote_age_rechecked_after_slow_kline_request(self):
        self.client.kline_delay_ms=16000
        r=self.open()
        s=r.poll()
        self.assertEqual(s['runtime_state'],'blocked')
        self.assertIn('decision source age',s['latest_error'])
        self.assertEqual(s['fills_count'],0)
    def test_corrupt_persisted_cash_cannot_become_fictitious_pnl(self):
        r=self.open()
        r.poll()
        r.broker.cash-=Decimal('1')
        r.broker._save()
        with self.assertRaisesRegex(ValueError,'ledger cash'):
            r.snapshot()
    def test_public_pipeline_warmup_restart_hash_lock(self):
        r = self.open()
        s = r.poll()
        from dashboard import validate_snapshot
        validate_snapshot(s)
        self.assertEqual(s['mode'], 'paper')
        self.assertEqual(s['runtime_state'], 'warming_up')
        self.assertEqual(s['fills_count'], 0)
        self.assertEqual(s['cash_usdt'], '100')
        self.assertFalse(s['live_trading_enabled'])
        self.assertEqual(s['engine']['warmup_received'], 0)
        from paper_runtime import PaperRuntime
        with self.assertRaises(RuntimeError):
            PaperRuntime(self.root, self.config, client=self.client, clock_ms=lambda:self.client.now, fixture=True)
        start = s['engine']['forward_start_at']
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000
            r.poll()
        self.client.closed_bar(0)
        self.assertEqual(r.poll()['engine']['warmup_received'], 0, 'bar opened before forward start cannot count')
        while self.client.now+50000<BASE+601000:
            self.client.now+=50000
            r.poll()
        self.client.closed_bar(1)
        self.assertEqual(r.poll()['engine']['warmup_received'], 1)
        r.close()
        r = self.open()
        s = r.poll()
        self.assertEqual(s['engine']['forward_start_at'], start)
        self.assertEqual(s['engine']['warmup_received'], 1)
        self.assertEqual(s['fills_count'], 0)
        modified = json.loads(self.config.read_text())
        modified['max_daily_loss_usdt']='4'
        p=self.root/'changed.json'
        p.write_text(json.dumps(modified))
        r.close()
        with self.assertRaises(ValueError):
            PaperRuntime(self.root, p, client=self.client, clock_ms=lambda:self.client.now, fixture=True)
