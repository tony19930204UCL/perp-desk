"""ARTIFICIAL observer analytics, not performance."""
import unittest,importlib.util,json,copy
from pathlib import Path
from test_paper_presentation import render_html,dom_text,paper_fixture
LAB=Path(__file__).resolve().parents[1]

def trades_fixture():
    fills=[dict(fill_id=f'f{i}',order_id=f'o{i}',symbol='BTCUSDT',side=side,qty='1',price=price,fee='.01',ts=ts) for i,(side,price,ts) in enumerate([('BUY','100',1000),('BUY','102',2000),('SELL','102',4000),('SELL','104',6000)])]
    ledger=[dict(type='fee',fill_id=f['fill_id'],amount='-.01',ts=f['ts']) for f in fills]
    ledger += [dict(type='realized',order_id='o2',amount='1',ts=4000),dict(type='realized',order_id='o3',amount='3',ts=6000),dict(type='funding',symbol='BTCUSDT',amount='-.01',qty='2',ts=3000)]
    return dict(fills=fills,cost_ledger=ledger,research=dict(strategy_start_ms=500,deadline_ms=10000,target_complete_round_trips=30,complete_round_trips=1,signals_count=3,blocked_signals_count=3,rejection_categories={'insufficient_reward_after_costs':3}))

class ObserverAnalyticsTests(unittest.TestCase):
    def view(self,s,now=7000):
        self.assertTrue((LAB/'observer_analytics.py').exists(),'read-only round-trip analytics missing')
        from observer_analytics import analyze
        return analyze(s,now_ms=now)

    def test_duplicate_ledger_and_funding_event_ids_are_unavailable(self):
        for kind in ['realized','funding','funding-event']:
            s=trades_fixture()
            for i,c in enumerate(s['cost_ledger']):c['ledger_id']='ledger:'+str(i)
            row=next(c for c in s['cost_ledger'] if c['type']==('funding' if kind.startswith('funding') else kind))
            row['event_id']='native-event';duplicate=copy.deepcopy(row)
            if kind=='funding-event':duplicate['ledger_id']='another-ledger-id'
            s['cost_ledger'].append(duplicate)
            with self.subTest(kind=kind):self.assertFalse(self.view(s)['available'],'duplicate cost must not alter trusted performance')

    def test_account_cost_summaries_reconcile_full_ledger_including_open_fees(self):
        s=trades_fixture();s.update(gross_realized_pnl_usdt='4',fees_usdt='.04',funding_pnl_usdt='-.01',realized_pnl_usdt='3.95')
        self.assertTrue(self.view(s)['available'])
        for name in ['gross_realized_pnl_usdt','fees_usdt','funding_pnl_usdt','realized_pnl_usdt']:
            bad=copy.deepcopy(s);bad[name]='999'
            with self.subTest(name=name):self.assertFalse(self.view(bad)['available'],'account/ledger mismatch must not be trusted')
        s['fills'].append(dict(fill_id='open',order_id='open-order',symbol='BTCUSDT',side='BUY',qty='1',price='105',fee='.01',ts=6500))
        s['cost_ledger'].append(dict(type='fee',fill_id='open',amount='-.01',ts=6500))
        s.update(fees_usdt='.05',realized_pnl_usdt='3.94')
        a=self.view(s);self.assertTrue(a['available']);self.assertEqual(a['closed_trades'][0]['net_pnl_usdt'],'3.95')

    def test_account_validation_rejects_contradictory_cost_ledger(self):
        import dashboard
        s=paper_fixture();s.update(trades_fixture(),positions=[],fills_count=4,unrealized_pnl_usdt='0',cash_usdt='103.95',equity_usdt='103.95',total_pnl_usdt='3.95',realized_pnl_usdt='3.95',gross_realized_pnl_usdt='4',fees_usdt='.04',funding_pnl_usdt='-.01')
        dashboard.validate_snapshot(s)
        s['gross_realized_pnl_usdt']='999'
        with self.assertRaises(ValueError):dashboard.validate_snapshot(s)

    def test_missing_research_window_cannot_be_labelled_window_performance(self):
        s=trades_fixture();s.pop('research')
        self.assertFalse(self.view(s)['available'],'lifetime trades cannot masquerade as window samples')

    def test_empty_open_short_multi_symbol_and_missing_costs_are_honest(self):
        empty=dict(fills=[],cost_ledger=[],research=dict(strategy_start_ms=500,deadline_ms=10000,complete_round_trips=0))
        a=self.view(empty);self.assertTrue(a['available']);self.assertIsNone(a['statistics']['win_rate_percent'])
        s=trades_fixture();s['research']['complete_round_trips']=0;s['fills']=s['fills'][:2];s['cost_ledger']=s['cost_ledger'][:2]
        a=self.view(s);self.assertTrue(a['available']);self.assertEqual(a['open_episodes'],1);self.assertEqual(a['closed_trades'],[])
        s=trades_fixture()
        for f in s['fills']:f['side']='SELL' if f['side']=='BUY' else 'BUY'
        for c in s['cost_ledger']:
            if c['type']=='realized':c['amount']='-'+c['amount']
        a=self.view(s);self.assertEqual(a['closed_trades'][0]['direction'],'SHORT');self.assertEqual(a['statistics']['wins'],0);self.assertIsNone(a['statistics']['fee_to_gross_percent'])
        s=trades_fixture();second=copy.deepcopy(s)
        for f in second['fills']:f.update(symbol='ETHUSDT',fill_id='eth-'+f['fill_id'],order_id='eth-'+f['order_id'])
        for c in second['cost_ledger']:
            if 'fill_id' in c:c['fill_id']='eth-'+c['fill_id']
            if 'order_id' in c:c['order_id']='eth-'+c['order_id']
            if 'symbol' in c:c['symbol']='ETHUSDT'
        s['fills']=sorted(s['fills']+second['fills'],key=lambda f:f['ts']);s['cost_ledger']+=second['cost_ledger'];s['research']['complete_round_trips']=2
        a=self.view(s);self.assertTrue(a['available']);self.assertEqual(a['statistics']['samples'],2)
        s=trades_fixture();s['cost_ledger'].pop(0);self.assertFalse(self.view(s)['available'])
        s=trades_fixture();s['research']['complete_round_trips']=2;self.assertFalse(self.view(s)['available'])
        s=trades_fixture();s['fills'][0]['price']='NaN';self.assertFalse(self.view(s)['available'])

    def test_malformed_container_is_unavailable_not_http_crash(self):
        for value in [None,[],42,'bad']:
            with self.subTest(value=value):self.assertFalse(self.view(dict(fills=[value],cost_ledger=[]))['available'])

    def test_dynamic_capital_and_third_symbol_preserve_asset_invariants(self):
        import dashboard
        s=paper_fixture();s.update(initial_equity_usdt='250',cash_usdt='249.70',equity_usdt='251.95')
        s['markets'].append(dict(s['markets'][0],symbol='BTCUSDT'))
        try:dashboard.validate_snapshot(s)
        except ValueError as exc:self.fail('presentation must not hardcode100 or ETH/XAU: '+str(exc))
        s['equity_usdt']='251.96'
        with self.assertRaises(ValueError):dashboard.validate_snapshot(s)

    def test_partials_make_one_closed_round_with_actual_net_costs(self):
        s=trades_fixture();before=copy.deepcopy(s);a=self.view(s)
        self.assertTrue(a['available']);self.assertEqual(s,before)
        self.assertEqual(len(a['closed_trades']),1)
        t=a['closed_trades'][0];self.assertEqual(t['direction'],'LONG')
        self.assertEqual(t['entry_price'],'101');self.assertEqual(t['exit_price'],'103')
        self.assertEqual(t['net_pnl_usdt'],'3.95');self.assertEqual(t['holding_ms'],5000)
        self.assertEqual(a['statistics']['win_rate_percent'],'100')
        self.assertEqual(a['statistics']['mean_net_pnl_usdt'],'3.95')
        self.assertEqual(a['statistics']['fee_to_gross_percent'],'1.00')
        self.assertEqual(a['remaining_ms'],3000)

class ObserverUiTests(unittest.TestCase):
    def test_failed_reconstruction_suppresses_unverified_progress(self):
        from observer_analytics import analyze
        s=paper_fixture();data=trades_fixture();data['research']['complete_round_trips']=8
        s.update(data,feed_stale=False,feed_age_seconds=1)
        s['observer']=analyze(data,now_ms=7000)
        self.assertFalse(s['observer']['available'])
        nodes=render_html(s)
        self.assertNotIn('8 / 30',dom_text(nodes['roundTrips']))
        self.assertIn('未確認',dom_text(nodes['roundTrips']))
        self.assertEqual(nodes['sampleProgress']['attributes']['value'],'0')

    def test_rejection_totals_must_reconcile_without_unproven_dedup_claim(self):
        s=paper_fixture();s.update(feed_stale=False,feed_age_seconds=1,research=dict(strategy_start_ms=500,deadline_ms=10000,complete_round_trips=0,target_complete_round_trips=30,signals_count=5,blocked_signals_count=4,rejection_categories={'risk_per_trade':400}))
        nodes=render_html(s)
        self.assertEqual(dom_text(nodes['rejectionDistribution']),'')
        self.assertIn('未確認',dom_text(nodes['rejectionNote']))
        s['research']['rejection_categories']={'risk_per_trade':4}
        nodes=render_html(s)
        self.assertIn('4',dom_text(nodes['rejectionDistribution']))
        self.assertNotIn('去重',dom_text(nodes['rejectionNote']))

    def test_readable_decimal_display_preserves_exact_value_without_float_rounding(self):
        s=paper_fixture();s.update(feed_stale=False,feed_age_seconds=1,equity_usdt='100.0000000000000000',total_pnl_usdt='0E-16',cash_usdt='12345678901234567890.1234500')
        nodes=render_html(s)
        self.assertEqual(dom_text(nodes['equity']),'100')
        self.assertEqual(dom_text(nodes['total']),'0')
        self.assertEqual(dom_text(nodes['cash']),'12345678901234567890.12345')
        self.assertEqual(nodes['cash']['attributes']['title'],s['cash_usdt'])

    def test_curve_has_real_time_axes_dynamic_baseline_and_stale_segment_break(self):
        s=paper_fixture();s.update(feed_stale=True,feed_age_seconds=1,initial_equity_usdt='250')
        s['equity_history']=[dict(ts='2026-10-02T12:00:00Z',equity_usdt='250',valuation_stale=False),dict(ts='2026-10-02T12:00:01Z',equity_usdt='251',valuation_stale=True),dict(ts='2026-10-02T12:00:10Z',equity_usdt='249',valuation_stale=False)]
        nodes=render_html(s);chart=nodes['chart']
        text=dom_text(chart)
        self.assertIn('250 USDT',text,'dynamic baseline label missing')
        self.assertIn('UTC',text,'real time x axis missing')
        self.assertIn('USDT',text,'y axis units missing')
        paths=[e for e in chart['children'] if e['attributes'].get('data-role')=='equity-path']
        self.assertEqual(len(paths),1)
        self.assertEqual(paths[0]['attributes']['d'].count('M'),2,'stale valuation must not be connected as fresh')
        self.assertIn('過期',dom_text(nodes['health']))

    def test_priority_progress_closed_trade_and_rejection_distribution_render_actual_data(self):
        from observer_analytics import analyze
        s=paper_fixture();s.update(trades_fixture(),feed_stale=False,feed_age_seconds=1,fixture=True,positions=[],fills_count=4,unrealized_pnl_usdt='0',cash_usdt='103.95',equity_usdt='103.95',total_pnl_usdt='3.95',realized_pnl_usdt='3.95',gross_realized_pnl_usdt='4',fees_usdt='.04',funding_pnl_usdt='-.01')
        s['observer']=analyze(s,now_ms=7000)
        nodes=render_html(s)
        self.assertNotIn('error',nodes)
        self.assertIn('roundTrips',nodes,'research progress panel missing')
        self.assertIn('1 / 30',dom_text(nodes['roundTrips']))
        self.assertIn('closedTrades',nodes,'closed round-trip table missing')
        self.assertIn('BTCUSDT',dom_text(nodes['closedTrades']))
        self.assertIn('3.95',dom_text(nodes['closedTrades']))
        self.assertIn('rejectionDistribution',nodes,'rejection categories missing')
        self.assertIn('insufficient_reward_after_costs',dom_text(nodes['rejectionDistribution']))
        self.assertIn('3',dom_text(nodes['rejectionDistribution']))
        self.assertIn('100',dom_text(nodes['winRate']))
        html=(LAB/'dashboard.html').read_text()
        self.assertLess(html.index('id="equity"'),html.index('背景工作 / 下一步'))
        self.assertLess(html.index('id="positionList"'),html.index('id="roundTrips"'))
        self.assertIn('<details id="proofDetails">',html)
        self.assertNotIn('100 USDT',html)
        self.assertIn('UTC',html)

class ObserverHttpTests(unittest.TestCase):
    def test_api_adds_read_only_analytics_without_mutating_snapshot(self):
        import tempfile,threading
        from http.client import HTTPConnection
        import dashboard
        from test_dashboard import fixture
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as td:
            p=Path(td)/'status.json';s=fixture();s.update(fills=[],cost_ledger=[],research=dict(strategy_start_ms=500,deadline_ms=10000,complete_round_trips=0,target_complete_round_trips=30))
            raw=json.dumps(s);p.write_text(raw)
            server=dashboard.make_server(0,p,LAB/'dashboard.html');thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                c=HTTPConnection('127.0.0.1',server.server_port);c.request('GET','/api/status');response=c.getresponse();d=json.loads(response.read());c.close()
                self.assertEqual(response.status,200)
                self.assertIn('observer',d,'derived observer analytics missing from actual HTTP response')
                self.assertTrue(d['observer']['available']);self.assertIsNone(d['observer']['statistics']['win_rate_percent'])
                self.assertEqual(p.read_text(),raw)
            finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
