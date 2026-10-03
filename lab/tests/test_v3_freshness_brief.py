"""ARTIFICIAL isolated regressions, not forward performance."""
import unittest, tempfile, json, io
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
import test_paper_v2 as v2
from test_paper_runtime import LAB, BASE
from paper_runtime_v3 import PaperRuntime

class BatchRefreshTests(unittest.TestCase):
    setUp=v2.V2Tests.setUp
    tearDown=v2.V2Tests.tearDown

    def open_v3(self):
        self.client.bars=[[BASE+i*60000,'100','100','100','100','10',BASE+(i+1)*60000-1] for i in range(-61,0)]
        self.r=PaperRuntime(self.root,LAB/'paper_config_v3.json',client=self.client,clock_ms=lambda:self.client.now,fixture=True)
        return self.r

    def test_no_delay_needs_no_refresh_and_restart_preserves_window(self):
        r=self.open_v3();s=r.poll();start=s['research']['strategy_start_ms'];deadline=s['research']['deadline_ms']
        self.assertIsNone(s['latest_error'])
        self.assertEqual(sum(e.endswith('bookTicker') for e,p in self.client.calls),2)
        r.close();self.r=None;self.client.now+=1000
        r=self.open_v3();s=r.poll()
        self.assertEqual(s['research']['strategy_start_ms'],start)
        self.assertEqual(s['research']['deadline_ms'],deadline)
        self.assertEqual(s['fills_count'],0)

    def test_persistent_slow_both_symbols_fail_closed_without_refresh_loop(self):
        r=self.open_v3();original=self.client.get
        def get(e,p=None):
            if e.endswith('depth') and (p or {}).get('symbol')=='XAUUSDT': self.client.now+=16000
            return original(e,p)
        self.client.get=get;s=r.poll()
        self.assertIn('stale source',s['latest_error'])
        self.assertFalse(s['paper_trading_enabled'])
        self.assertEqual(s['fills_count'],0)
        self.assertEqual(sum(e.endswith('bookTicker') for e,p in self.client.calls),4)
        events=[json.loads(p) for p, in r.db.execute('SELECT payload FROM audit')]
        self.assertEqual(sum(e['type']=='batch_quote_refresh' for e in events),1)

    def test_source_already_stale_at_receipt_is_not_retried_or_dispatched(self):
        r=self.open_v3();self.client.stale=True;s=r.poll()
        self.assertIn('market source',s['latest_error'])
        self.assertFalse(s['paper_trading_enabled'])
        self.assertEqual(sum(e.endswith('bookTicker') for e,p in self.client.calls),1)
        self.assertEqual(s['fills_count'],0)

    def test_refresh_with_old_source_fails_closed_and_retains_original_timestamp(self):
        r=self.open_v3();original=self.client.get;source=self.client.now-2000
        def get(e,p=None):
            if e.endswith('depth') and (p or {}).get('symbol')=='XAUUSDT': self.client.now+=14000
            q=original(e,p)
            if e.endswith('bookTicker') and (p or {}).get('symbol')=='ETHUSDT':
                q['source_timestamp_ms']=source;q['payload']['time']=source
            return q
        self.client.get=get;s=r.poll()
        self.assertIsNotNone(s['latest_error'])
        self.assertFalse(s['paper_trading_enabled'])
        self.assertEqual(s['fills_count'],0)
        receipts=[json.loads(p)['receipt'] for p, in r.db.execute("SELECT payload FROM audit") if json.loads(p)['type']=='public_receipt']
        tickers=[q for q in receipts if q['endpoint'].endswith('bookTicker') and q['params']['symbol']=='ETHUSDT']
        self.assertEqual([q['source_timestamp_ms'] for q in tickers],[source,source])

    def test_eth_refresh_that_ages_xau_refreshes_xau_once_too(self):
        r=self.open_v3();original=self.client.get;eth_tickers=0;xau_depths=0
        def get(e,p=None):
            nonlocal eth_tickers,xau_depths
            symbol=(p or {}).get('symbol')
            if e.endswith('bookTicker') and symbol=='ETHUSDT': eth_tickers+=1
            if e.endswith('depth') and symbol=='XAUUSDT':
                xau_depths+=1
                if xau_depths==1:self.client.now+=14000
            if e.endswith('depth') and symbol=='ETHUSDT' and eth_tickers==2:self.client.now+=2500
            q=original(e,p)
            if e.endswith('bookTicker') and symbol=='ETHUSDT' and eth_tickers==1:
                q['source_timestamp_ms']-=2000;q['payload']['time']-=2000
            return q
        self.client.get=get;s=r.poll()
        self.assertIsNone(s['latest_error'])
        self.assertEqual([p['symbol'] for e,p in self.client.calls if e.endswith('bookTicker')],['ETHUSDT','XAUUSDT','ETHUSDT','XAUUSDT'])
        self.assertEqual(s['fills_count'],0)
        self.assertTrue(all(0<=self.client.now-ts<=15000 for m in s['markets'] for ts in m['source_timestamps_ms'].values()))

    def test_batch_refresh_receipt_does_not_turn_prearrival_depth_into_fill(self):
        from sim_broker import Intent
        from decimal import Decimal as D
        r=self.open_v3();self.assertIsNone(r.poll()['latest_error']);start=self.client.now
        spec=r.broker.instruments['ETHUSDT']
        order=r.broker.submit(Intent('ARTIFICIAL-entry','ETHUSDT','BUY',D('.3'),D('99.5'),start,spec.quantity_step,spec.tick,spec.min_notional,spec.max_quantity,'TAKER',False))
        self.assertEqual(order['status'],'PENDING');self.assertEqual(order['arrival_ts'],start+2000)
        self.client.now+=1001
        original=self.client.get;eth_tickers=0
        def get(e,p=None):
            nonlocal eth_tickers
            symbol=(p or {}).get('symbol')
            if e.endswith('depth') and symbol=='XAUUSDT':self.client.now+=14000
            q=original(e,p)
            if e.endswith('bookTicker') and symbol=='ETHUSDT':
                eth_tickers+=1
                if eth_tickers==1:q['source_timestamp_ms']-=2000;q['payload']['time']-=2000
            if e.endswith('depth') and symbol=='ETHUSDT':
                q['source_timestamp_ms']=start+1000;q['payload']['E']=start+1000
            return q
        self.client.get=get;s=r.poll()
        self.assertIsNone(s['latest_error'])
        self.assertEqual(s['fills_count'],0)
        self.assertEqual(r.broker.orders[order['order_id']]['status'],'PENDING')
        self.assertEqual(r.config['execution_model']['latency_ms'],2000)

    def test_slow_xau_refreshes_only_aged_eth_once_before_decision(self):
        self.client.bars=[[BASE+i*60000,'100','100','100','100','10',BASE+(i+1)*60000-1] for i in range(-61,0)]
        self.r=PaperRuntime(self.root,LAB/'paper_config_v3.json',client=self.client,clock_ms=lambda:self.client.now,fixture=True)
        original=self.client.get
        first_eth=True
        def get(e,p=None):
            nonlocal first_eth
            if e.endswith('depth') and (p or {}).get('symbol')=='XAUUSDT':
                self.client.now+=14000
            receipt=original(e,p)
            if first_eth and e.endswith('bookTicker') and (p or {}).get('symbol')=='ETHUSDT':
                first_eth=False
                receipt['source_timestamp_ms']-=2000
                receipt['payload']['time']-=2000
            return receipt
        self.client.get=get
        s=self.r.poll()
        self.assertIsNone(s['latest_error'],'ETH fresh at receipt must refresh after slow XAU, not bypass 15s gate')
        eth=next(m for m in s['markets'] if m['symbol']=='ETHUSDT')
        self.assertGreaterEqual(min(eth['source_timestamps_ms'].values()),BASE+16000)
        tickers=[p['symbol'] for e,p in self.client.calls if e.endswith('bookTicker')]
        self.assertEqual(tickers,['ETHUSDT','XAUUSDT','ETHUSDT'])
        self.assertEqual(s['fills_count'],0)
        self.assertEqual(self.r.config['execution_model']['max_source_age_ms'],15000)

class BriefFallbackTests(unittest.TestCase):
    def run_brief(self, change, *, age=0):
        import brief_learning_v3 as brief
        with tempfile.TemporaryDirectory(dir='/tmp/perp-desk-tests') as t:
            r=PaperRuntime(Path(t)/'account',LAB/'paper_config_v3.json',clock_ms=lambda:BASE,fixture=True)
            s=r.snapshot();r.close();s['fixture']=False
            s['markets']=[dict(symbol='ETHUSDT',category='crypto',bid='99',ask='100',mark_price='100',source_timestamps_ms={'bookTicker':BASE,'depth5':BASE,'premiumIndex':BASE})]
            s['feed']['last_success_at']=s['updated_at'];s['feed']['connected']=True
            change(s)
            p=Path(t)/'s.json';p.write_text(json.dumps(s))
            with patch('sys.argv',['brief','--status',str(p)]),patch.object(brief.time,'time',return_value=(BASE+age)/1000),redirect_stdout(io.StringIO()) as stdout:
                rc=brief.main()
            return rc,stdout.getvalue()

    def test_disconnected_feed_is_not_presented_as_healthy(self):
        rc,text=self.run_brief(lambda s:s['feed'].update(connected=False))
        self.assertEqual(rc,1)
        self.assertIn('非即時',text)
        self.assertIn('feed',text)

    def test_error_text_with_newlines_still_produces_eight_lines(self):
        rc,text=self.run_brief(lambda s:s.update(latest_error='timeout\noriginal detail',blockers=['timeout\noriginal detail']))
        self.assertEqual(rc,1)
        self.assertEqual(len(text.splitlines()),8)
        self.assertIn('timeout',text)
        self.assertIn('0/30',text)

    def test_stale_snapshot_keeps_metrics_without_live_claim(self):
        rc,text=self.run_brief(lambda s:None,age=60001)
        self.assertEqual(rc,1)
        self.assertIn('stale/future snapshot',text)
        self.assertIn('最後已驗證快照',text)

    def test_corrupt_account_fixture_missing_and_wrong_version_have_no_metrics(self):
        for change in (lambda s:s.update(equity_usdt='101'),lambda s:s.update(fixture=True),lambda s:s['engine'].update(version_id='H1-PAPER-002'),lambda s:s.pop('research')):
            rc,text=self.run_brief(change)
            self.assertEqual(rc,1)
            self.assertIn('簡報不可用',text)
            self.assertNotIn('權益',text)

    def test_fresh_snapshot_remains_success(self):
        rc,text=self.run_brief(lambda s:None)
        self.assertEqual(rc,0)
        self.assertIn('尚未證明',text)
        self.assertEqual(len(text.splitlines()),8)

    def test_stale_source_cli_keeps_verified_last_known_metrics(self):
        import brief_learning_v3 as brief
        with tempfile.TemporaryDirectory(dir='/tmp/perp-desk-tests') as t:
            r=PaperRuntime(Path(t)/'account',LAB/'paper_config_v3.json',clock_ms=lambda:BASE,fixture=True)
            s=r.snapshot();r.close();s['fixture']=False
            s['markets']=[dict(symbol='ETHUSDT',category='crypto',bid='99',ask='100',mark_price='100',source_timestamps_ms={'bookTicker':BASE-16000})]
            p=Path(t)/'s.json';p.write_text(json.dumps(s))
            with patch('sys.argv',['brief','--status',str(p)]),patch.object(brief.time,'time',return_value=BASE/1000),redirect_stdout(io.StringIO()) as stdout:
                rc=brief.main()
            text=stdout.getvalue()
            self.assertIn('最後已驗證快照',text)
            self.assertIn('本金 100',text)
            self.assertIn('權益 100',text)
            self.assertIn('0/30',text)
            self.assertIn('stale/future source',text)
            self.assertEqual(len(text.splitlines()),8)
            self.assertEqual(rc,1,'reporting fallback must not return healthy success')
