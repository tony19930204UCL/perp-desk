"""Isolated ARTIFICIAL timing regressions, never performance."""
import json
import unittest
from pathlib import Path
import test_paper_v2 as v2
from test_paper_runtime import LAB, BASE, iso

class FreshnessTests(unittest.TestCase):
    setUp = v2.V2Tests.setUp
    tearDown = v2.V2Tests.tearDown
    fill_next = v2.V2Tests.fill_next

    def open(self):
        from paper_runtime_v2 import PaperRuntime
        self.r = PaperRuntime(self.root, LAB/'paper_config_v2.json', client=self.client,
                              clock_ms=lambda:self.client.now, fixture=True)
        return self.r

    def test_actual_delay_refreshes_real_inputs_before_decision(self):
        r=self.open()
        self.assertIsNone(r.poll()['latest_error'])
        # Keep heartbeat continuous until a close is due (no backfilled trades).
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000; r.poll()
        self.client.now=BASE+301000
        actual=json.loads((LAB/'evidence/freshness_actual_failure.json').read_text())
        quotes=[x['receipt'] for x in actual if x.get('receipt',{}).get('source_timestamp_ms') is not None]
        from paper_market import receipt_ms
        end=actual[-1]['at_ms']
        self.assertEqual(end-quotes[0]['source_timestamp_ms'],15198)
        # Replay the measured relative source/receipt offsets with a controlled
        # fixture clock. No captured historical book is submitted to a broker.
        start=self.client.now
        first=receipt_ms(quotes[0])
        original=self.client.get
        initial_quotes=iter(quotes)
        count=0
        def get(e,p=None):
            nonlocal count
            if e in ('/fapi/v1/ticker/bookTicker','/fapi/v1/depth','/fapi/v1/premiumIndex') and count<6:
                q=next(initial_quotes); count+=1
                self.client.now=start+receipt_ms(q)-first
                receipt=original(e,p)
                source=start+q['source_timestamp_ms']-first
                receipt['source_timestamp_ms']=source
                if 'time' in receipt['payload']: receipt['payload']['time']=source
                if 'E' in receipt['payload']: receipt['payload']['E']=source
                return receipt
            if e.endswith('klines'):
                self.client.now=start+end-first
            return original(e,p)
        self.client.get=get
        s=r.poll()
        self.assertIsNone(s['latest_error'], 'slow candles must recover using newly fetched inputs, not age tolerance')
        self.assertEqual(s['fills_count'],0)
        eth=next(m for m in s['markets'] if m['symbol']=='ETHUSDT')
        self.assertGreater(min(eth['source_timestamps_ms'].values()),start+end-first-1)
        self.assertEqual(r.config['execution_model']['max_source_age_ms'],15000)

    def test_xau_failure_cannot_block_gap_exit_on_actual_eth_book(self):
        r=self.open(); self.fill_next(r)
        self.client.now+=61000
        original=self.client.get
        def get(e,p=None):
            if (p or {}).get('symbol')=='XAUUSDT':
                raise OSError('ARTIFICIAL unavailable observation market')
            return original(e,p)
        self.client.get=get
        s=r.poll()
        self.assertIsNotNone(s['latest_error'])
        exits=[o for o in r.broker.orders.values() if o['intent']['reduce_only'] and o['status']=='PENDING']
        self.assertEqual(len(exits),1,'ETH exit must precede XAU observation request')
        self.client.now+=3000
        s=r.poll()
        self.assertEqual(s['positions'],[])
        self.assertEqual(s['fills_count'],2)
        self.assertEqual(s['fills'][-1]['observed_price'],self.client.bid)
        self.assertFalse(s['paper_trading_enabled'])

    def test_no_new_close_skips_klines_across_restart(self):
        r=self.open(); r.poll()
        account=r.snapshot()
        cursor=r.state['cursor']; cutoff=r.state['forward_start_ms']
        self.client.calls=[]; self.client.now+=5000
        r.poll()
        self.assertFalse(any(e.endswith('klines') for e,p in self.client.calls),
                         'quote cycle must not repeatedly fetch the same unclosed candle')
        r.close(); self.r=None
        r=self.open(); self.client.calls=[]; s=r.poll()
        self.assertFalse(any(e.endswith('klines') for e,p in self.client.calls))
        self.assertEqual(r.state['cursor'],cursor)
        self.assertEqual(r.state['forward_start_ms'],cutoff)
        self.assertEqual(s['cash_usdt'],account['cash_usdt'])
        self.assertEqual(s['fills_count'],0)
        self.assertEqual(s['signals_count'],0)

    def test_refreshed_sources_still_obey_arrival_latency_and_restart(self):
        r=self.open(); r.poll()
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000; r.poll()
        self.client.closed_bar(0,'95','21')
        self.client.price='95'; self.client.bid='94.99'; self.client.ask='95.01'
        self.client.kline_delay_ms=1000
        s=r.poll()
        self.assertEqual(s['signals_count'],1)
        self.assertEqual(s['fills_count'],0)
        original=self.client.get
        fixed_source=self.client.now
        def get(e,p=None):
            receipt=original(e,p)
            if e.endswith('depth') and (p or {}).get('symbol')=='ETHUSDT':
                receipt['source_timestamp_ms']=fixed_source
                receipt['payload']['E']=fixed_source
            return receipt
        self.client.get=get
        self.client.now+=1000
        self.assertEqual(r.poll()['fills_count'],0)
        self.client.now+=2000
        self.assertEqual(r.poll()['fills_count'],0,'receipt arrival cannot turn an older source book into a delayed fill')
        self.client.get=original
        s=r.poll()
        self.assertEqual(s['fills_count'],1)
        cash=s['cash_usdt']; start=r.state['forward_start_ms']
        r.close(); self.r=None; r=self.open()
        s=r.poll()
        self.assertEqual(s['cash_usdt'],cash)
        self.assertEqual(s['fills_count'],1)
        self.assertEqual(s['signals_count'],1)
        self.assertEqual(r.state['forward_start_ms'],start)
        self.assertEqual(s['fills'][0]['observed_price'],'95.01')

    def test_late_signal_after_candle_delay_is_rejected_not_backfilled(self):
        r=self.open(); r.poll()
        while self.client.now+50000<BASE+301000:
            self.client.now+=50000; r.poll()
        self.client.closed_bar(0,'95','21')
        self.client.price='95'; self.client.bid='94.99'; self.client.ask='95.01'
        self.client.kline_delay_ms=16000
        s=r.poll()
        self.assertIsNone(s['latest_error'])
        self.assertEqual(s['signals_count'],1)
        self.assertEqual(s['rejections'][0]['reason'],'late_closed_bar_signal')
        self.assertEqual(s['fills_count'],0)
        self.assertEqual(s['orders'],[])

if __name__=='__main__': unittest.main()
