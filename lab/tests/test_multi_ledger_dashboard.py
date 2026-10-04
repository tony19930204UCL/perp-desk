"""Synthetic configured-ledger acceptance; no operator snapshots or account data."""
import copy
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from http.client import HTTPConnection
from pathlib import Path

import dashboard
from observer_analytics import analyze
from test_paper_presentation import paper_fixture
from test_observer_dashboard import trades_fixture

LAB=Path(__file__).resolve().parents[1]


def ledger_snapshot(*,capital,symbol,window_start,deadline,high_precision=False,partial=False,
                    missing_window=False,mismatch=False):
    s=paper_fixture()
    s.update(trades_fixture())
    s['initial_equity_usdt']=str(capital)
    s['positions']=[]
    s['fills_count']=4
    s['unrealized_pnl_usdt']='0'
    s['markets'].append(dict(s['markets'][0],symbol='SOLUSDT',category='crypto'))
    for fill in s['fills']:
        fill['symbol']=symbol
    for row in s['cost_ledger']:
        if row.get('symbol')=='BTCUSDT':row['symbol']=symbol
    if high_precision:
        s['fills'][0]['price']='100.123456789012345678901234567890'
        s['fills'][1]['price']='102.987654321098765432109876543210'
        s['fills'][2]['price']='102.333333333333333333333333333333'
        s['fills'][3]['price']='104.777777777777777777777777777777'
    if partial:
        # First two fills are entry partials, already part of the normal fixture.
        self_note='partial entry fixture'
        del self_note
    s['research']['strategy_start_ms']=window_start
    s['research']['deadline_ms']=deadline
    s['research']['target_complete_round_trips']=30 if capital=='250' else 12
    if missing_window:
        s.pop('research')
    # Full account ledger is +4 gross - .04 fees - .01 funding = 3.95.
    initial=str(capital)
    realized='3.95'
    with localcontext() as ctx:
        ctx.prec=80
        cash=str(Decimal(initial)+Decimal(realized))
    s.update(cash_usdt=cash,equity_usdt=cash,total_pnl_usdt=realized,realized_pnl_usdt=realized,
             gross_realized_pnl_usdt='4.000000000000000000000000000000',
             fees_usdt='0.040000000000000000000000000000',
             funding_pnl_usdt='-0.010000000000000000000000000000')
    if mismatch:
        s['gross_realized_pnl_usdt']='4.1'
    return s


class MultiLedgerHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name);self.root=root
        self.default_path=root/'alpha.json';self.beta_path=root/'nested'/'beta.json'
        self.beta_path.parent.mkdir()
        self.alpha=ledger_snapshot(capital='250',symbol='BTCUSDT',window_start=500,deadline=10000)
        self.beta=ledger_snapshot(capital='1000.123456789012345678901234567890',
                                  symbol='SOLUSDT',window_start=1500,deadline=22000,high_precision=True)
        self.default_path.write_text(json.dumps(self.alpha))
        self.beta_path.write_text(json.dumps(self.beta))
        self.config=root/'ledgers.json'
        self.config.write_text(json.dumps(dict(schema_version=1,root=str(root.resolve()),default='alpha',
            ledgers=[dict(id='alpha',label='Alpha 250',status='alpha.json'),
                     dict(id='beta',label='Beta high precision',status='nested/beta.json')])))
        self.server=dashboard.make_server(0,self.default_path,LAB/'dashboard.html',self.config)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()

    def get(self,path):
        c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        c.request('GET',path,headers={'Host':f'127.0.0.1:{self.server.server_port}'})
        r=c.getresponse();raw=r.read();c.close()
        return r.status,json.loads(raw) if raw.startswith(b'{') else raw.decode()

    def test_switching_ledgers_is_read_only_and_never_mixes_capital_window_trades_or_costs(self):
        before_a=self.default_path.read_bytes();before_b=self.beta_path.read_bytes()
        code,a1=self.get('/api/status?ledger=alpha')
        self.assertEqual(code,200);self.assertEqual(a1['ledger_view']['id'],'alpha')
        self.assertEqual(a1['initial_equity_usdt'],'250')
        self.assertEqual(a1['research']['strategy_start_ms'],500)
        self.assertEqual(a1['observer']['closed_trades'][0]['symbol'],'BTCUSDT')
        self.assertEqual(a1['observer']['statistics']['samples'],1)
        code,b=self.get('/api/status?ledger=beta')
        self.assertEqual(code,200);self.assertEqual(b['ledger_view']['id'],'beta')
        self.assertEqual(b['initial_equity_usdt'],'1000.123456789012345678901234567890')
        self.assertEqual(b['research']['strategy_start_ms'],1500)
        self.assertEqual(b['research']['deadline_ms'],22000)
        self.assertEqual(b['observer']['closed_trades'][0]['symbol'],'SOLUSDT')
        self.assertEqual(b['observer']['closed_trades'][0]['fees_usdt'],'0.04')
        self.assertEqual(b['observer']['closed_trades'][0]['funding_pnl_usdt'],'-0.01')
        self.assertTrue(any(m['symbol']=='SOLUSDT' for m in b['markets']))
        code,a2=self.get('/api/status?ledger=alpha')
        self.assertEqual(code,200);self.assertEqual(a2['initial_equity_usdt'],'250')
        self.assertEqual(a2['observer']['closed_trades'][0]['symbol'],'BTCUSDT')
        self.assertEqual(self.default_path.read_bytes(),before_a)
        self.assertEqual(self.beta_path.read_bytes(),before_b)

    def test_public_ledger_list_exposes_ids_labels_only_and_unknown_or_path_queries_fail_closed(self):
        code,d=self.get('/api/ledgers');self.assertEqual(code,200)
        self.assertEqual(d['default'],'alpha');self.assertEqual([x['id'] for x in d['ledgers']],['alpha','beta'])
        self.assertNotIn(str(self.root),json.dumps(d))
        for selector in ('unknown','../alpha.json','%2e%2e%2falpha.json','/tmp/account.json','beta/../../alpha'):
            code,body=self.get('/api/status?ledger='+selector)
            self.assertIn(code,(400,404));self.assertNotIn('initial_equity_usdt',body)

    def test_selected_missing_invalid_stale_and_observer_mismatch_never_fall_back(self):
        # Missing beta does not silently show alpha.
        self.beta_path.unlink()
        code,d=self.get('/api/status?ledger=beta');self.assertEqual(code,503)
        self.assertFalse(d['available'])
        self.assertNotIn('initial_equity_usdt',d)
        self.beta_path.write_text('{bad json')
        code,d=self.get('/api/status?ledger=beta');self.assertEqual(code,503)
        self.beta_path.write_text(json.dumps(self.beta))
        # Stale remains the selected beta last-known snapshot, not alpha.
        stale=copy.deepcopy(self.beta);stale['feed']['connected']=False
        self.beta_path.write_text(json.dumps(stale))
        code,d=self.get('/api/status?ledger=beta');self.assertEqual(code,200)
        self.assertEqual(d['ledger_view']['id'],'beta');self.assertTrue(d['feed_stale'])
        self.assertEqual(d['initial_equity_usdt'],self.beta['initial_equity_usdt'])
        # Observer reconciliation failure suppresses stats but does not borrow alpha stats.
        bad=copy.deepcopy(self.beta);bad['gross_realized_pnl_usdt']='4.1'
        self.beta_path.write_text(json.dumps(bad))
        code,d=self.get('/api/status?ledger=beta');self.assertEqual(code,200)
        self.assertFalse(d['observer']['available'])
        self.assertIn('account/ledger mismatch',d['observer']['error'])
        self.assertEqual(d['ledger_view']['id'],'beta')

    def test_missing_window_and_partial_open_episode_remain_honest(self):
        missing=ledger_snapshot(capital='300',symbol='ETHUSDT',window_start=500,deadline=10000,missing_window=True)
        self.beta_path.write_text(json.dumps(missing))
        code,d=self.get('/api/status?ledger=beta');self.assertEqual(code,200)
        self.assertFalse(d['observer']['available']);self.assertIn('research window missing',d['observer']['error'])
        partial=paper_fixture();partial.update(fills=trades_fixture()['fills'][:2],
            cost_ledger=trades_fixture()['cost_ledger'][:2],
            research=dict(strategy_start_ms=500,deadline_ms=10000,target_complete_round_trips=30,
                          complete_round_trips=0,signals_count=1,blocked_signals_count=0,rejection_categories={}),
            positions=[],fills_count=2,gross_realized_pnl_usdt='0',fees_usdt='.02',
            funding_pnl_usdt='0',realized_pnl_usdt='-.02',cash_usdt='99.98',
            equity_usdt='99.98',unrealized_pnl_usdt='0',total_pnl_usdt='-.02')
        self.beta_path.write_text(json.dumps(partial))
        code,d=self.get('/api/status?ledger=beta');self.assertEqual(code,200)
        self.assertTrue(d['observer']['available']);self.assertEqual(d['observer']['closed_trades'],[])
        self.assertEqual(d['observer']['open_episodes'],1)
        self.assertEqual(d['observer']['statistics']['samples'],0)


class LedgerConfigTests(unittest.TestCase):
    def test_single_ledger_default_is_backwards_compatible(self):
        with tempfile.TemporaryDirectory() as td:
            status=Path(td)/'status.json'
            config=dashboard.load_ledger_config(None,status)
            self.assertEqual(config['default'],'default')
            self.assertEqual(config['ledgers']['default']['status_path'],status.resolve())

    def test_config_rejects_unknown_default_absolute_relative_escape_duplicate_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);default=root/'alpha.json';default.write_text('{}');cfg=root/'cfg.json'
            cases=[
                dict(schema_version=1,root=str(root),default='missing',ledgers=[dict(id='alpha',label='A',status='alpha.json')]),
                dict(schema_version=1,root=str(root),default='alpha',ledgers=[dict(id='alpha',label='A',status='/tmp/x')]),
                dict(schema_version=1,root=str(root),default='alpha',ledgers=[dict(id='alpha',label='A',status='../x')]),
                dict(schema_version=1,root=str(root),default='alpha',ledgers=[dict(id='alpha',label='A',status='alpha.json'),dict(id='alpha',label='B',status='alpha.json')])
            ]
            for data in cases:
                cfg.write_text(json.dumps(data))
                with self.subTest(data=data),self.assertRaises(ValueError):
                    dashboard.load_ledger_config(cfg.resolve(),default)
            other=root/'other.json';other.write_text('{}')
            cfg.write_text(json.dumps(dict(schema_version=1,root=str(root),default='alpha',
                ledgers=[dict(id='alpha',label='A',status='other.json')])))
            with self.assertRaisesRegex(ValueError,'default must equal --status'):
                dashboard.load_ledger_config(cfg.resolve(),default)
            target=root/'target.json';target.write_text('{}');link=root/'linked.json';link.symlink_to(target)
            cfg.write_text(json.dumps(dict(schema_version=1,root=str(root),default='alpha',
                ledgers=[dict(id='alpha',label='A',status='alpha.json'),dict(id='linked',label='L',status='linked.json')])))
            with self.assertRaisesRegex(ValueError,'symlink'):
                dashboard.load_ledger_config(cfg.resolve(),default)


if __name__=='__main__':unittest.main()
