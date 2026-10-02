"""Synthetic PAPER presentation fixtures only; never runtime/config data."""
import copy
import json
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

import dashboard
import brief

LAB = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 1, 0, 0, 10, tzinfo=timezone.utc)


def paper_fixture():
    stamp = '2026-10-01T00:00:00Z'
    ms = int(datetime.fromisoformat(stamp).timestamp() * 1000)
    return dict(schema_version=1, mode='paper', initial_equity_usdt='100',
                cash_usdt='99.70', equity_usdt='101.95', realized_pnl_usdt='-0.30',
                unrealized_pnl_usdt='2.25', total_pnl_usdt='1.95',
                paper_trading_enabled=True, live_trading_enabled=False,
                positions=[dict(symbol='ETHUSDT', side='BUY', qty='0.01',
                                entry_price='100', mark_price='325', stop_price='95',
                                unrealized_pnl_usdt='2.25')],
                fills_count=1, signals_count=3, blocked_signals_count=2, blockers=[],
                engine=dict(implementation='paper-engine-v1', version_id='SYNTHETIC-TEST',
                            forward_start_at=stamp, model_sha256='a' * 64, status='running'),
                risk=dict(approved=True, risk_version='SYNTHETIC-RISK', risk_config_sha256='b' * 64),
                updated_at=stamp, feed=dict(transport='REST polling', connected=True,
                    last_success_at=stamp, errors_count=0, gaps_count=0),
                markets=[dict(symbol=symbol, category=category, bid='100', ask='101',
                              mark_price='100.5', last_received_at=stamp,
                              source_timestamps_ms=dict(bookTicker=ms, premiumIndex=ms, depth5=ms))
                         for symbol, category in [('ETHUSDT', 'crypto'), ('XAUUSDT', 'TradFi')]],
                versions=[], equity_history=[], latest_error=None)


def render_html(snapshot, unavailable=False):
    """Execute actual inline JS against a minimal DOM, without HTTP/server."""
    script = (LAB / 'dashboard.html').read_text().split('<script>')[1].split('</script>')[0]
    script = script.replace('refresh();setInterval(refresh,5000);', '')
    harness = r'''
const vm = require('node:vm'), fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(){this.children=[]; this.textContent=''; this.className=''; this.attributes={}; this.classList={remove(){},add(){}};}
 appendChild(e){this.children.push(e);}
 replaceChildren(){this.children=[]; this.textContent='';}
 setAttribute(k,v){this.attributes[k]=v;}
}
const elements={};
const document={getElementById(id){return elements[id]??=(new Element());},createElement(){return new Element();},createElementNS(){return new Element();}};
const context={document, console}; vm.createContext(context);
try {vm.runInContext(input.script,context); context.s=input.snapshot;
 vm.runInContext('render(s);'+(input.unavailable?'unavailable("synthetic invalid");':''),context);
 console.log(JSON.stringify(elements));}
catch(e){console.log(JSON.stringify({error:e.message}));}
'''
    result = subprocess.run(['node', '-e', harness], input=json.dumps(dict(script=script, snapshot=snapshot,
                            unavailable=unavailable)), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def dom_text(element):
    return element['textContent'] + ''.join(dom_text(child) for child in element['children'])


class PaperPresentationTests(unittest.TestCase):
    def test_api_reads_paper_and_nested_malformed_fields_fail_closed_without_server(self):
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        scratch = tempfile.gettempdir()
        with tempfile.TemporaryDirectory(dir=scratch, prefix='paper-presentation-') as directory:
            path = Path(directory) / 'synthetic-status.json'
            with patch.object(dashboard, 'ThreadingHTTPServer') as constructor:
                dashboard.make_server(0, path, LAB / 'dashboard.html')
            handler_class = constructor.call_args.args[1]
            handler = handler_class.__new__(handler_class)
            handler.path = '/api/status'
            handler.headers = {'Host': '127.0.0.1:8767'}
            handler.server = SimpleNamespace(server_port=8767)
            replies = []
            handler.send = lambda status, body, content_type: replies.append((status, json.loads(body)))
            snapshot = paper_fixture()
            path.write_text(json.dumps(snapshot))
            handler.do_GET()
            self.assertEqual(replies[-1][0], 200)
            self.assertEqual(replies[-1][1]['equity_usdt'], '101.95')
            self.assertEqual(replies[-1][1]['positions'], snapshot['positions'])
            self.assertTrue(replies[-1][1]['feed_stale'], 'historical synthetic source must be stale now')
            for field, invalid in [('symbol', []), ('source_timestamps_ms', [])]:
                bad = copy.deepcopy(snapshot)
                bad['markets'][0][field] = invalid
                path.write_text(json.dumps(bad))
                handler.do_GET()
                self.assertEqual(replies[-1][0], 503)
                self.assertNotIn('equity_usdt', replies[-1][1])

    def test_optional_market_history_money_and_unknown_categories_fail_closed(self):
        snapshot = paper_fixture()
        for field in ('funding_rate', 'min_notional'):
            bad = copy.deepcopy(snapshot)
            bad['markets'][0][field] = 'NaN'
            with self.subTest(field=field), self.assertRaises(ValueError):
                dashboard.validate_snapshot(bad)
        bad = copy.deepcopy(snapshot)
        bad['equity_history'] = [dict(ts=bad['updated_at'], equity_usdt='NaN')]
        with self.assertRaises(ValueError):
            dashboard.validate_snapshot(bad)
        bad = copy.deepcopy(snapshot)
        bad['markets'][0].update(symbol='UNKNOWN', category=None)
        with self.assertRaises(ValueError):
            dashboard.validate_snapshot(bad)
        snapshot['markets'][0].update(funding_rate='-0.0001', min_notional='5')
        snapshot['equity_history'] = [dict(ts=snapshot['updated_at'], equity_usdt='101.95')]
        self.assertIsNone(dashboard.validate_snapshot(snapshot))

    def test_optional_accounting_is_displayed_without_subtracting_costs_twice(self):
        snapshot = paper_fixture()
        snapshot.update(gross_realized_pnl_usdt='0.10', fees_usdt='0.35', funding_pnl_usdt='-0.05',
                        feed_stale=False, feed_age_seconds=10)
        report = brief.render(snapshot, now=NOW)
        self.assertIn('費用 0.35', report)
        self.assertIn('funding -0.05', report)
        self.assertIn('毛已實現 0.10', report)
        self.assertIn('淨已實現 -0.30', report)
        self.assertIn('現金 99.70', report)
        self.assertLessEqual(len(report.splitlines()), 8)
        elements = render_html(snapshot)
        self.assertIn('費用 0.35', dom_text(elements['accounting']))
        self.assertIn('funding -0.05', dom_text(elements['accounting']))
        unavailable = render_html(snapshot, unavailable=True)
        self.assertNotIn('0.35', dom_text(unavailable['accounting']))
        self.assertNotIn('未交易不等於有優勢', (LAB / 'dashboard.html').read_text())
        for field in ('fees_usdt', 'funding_pnl_usdt', 'gross_realized_pnl_usdt'):
            bad = copy.deepcopy(snapshot)
            bad[field] = 'NaN'
            with self.subTest(field=field), self.assertRaises(ValueError):
                dashboard.validate_snapshot(bad)
            self.assertIn('資料缺失或格式錯誤', brief.render(bad, now=NOW))

    def test_html_renders_actual_paper_positions_status_and_clears_invalid_data(self):
        snapshot = paper_fixture()
        snapshot.update(feed_stale=False, feed_age_seconds=10)
        snapshot['engine']['version_id'] = '<img src=x onerror=alert(1)>'
        snapshot['positions'][0]['symbol'] = '<script>bad()</script>'
        elements = render_html(snapshot)
        self.assertNotIn('error', elements)
        self.assertEqual(dom_text(elements['equity']), '101.95')
        self.assertEqual(dom_text(elements['realized']), '-0.30')
        self.assertIn('運行', dom_text(elements['health']))
        self.assertIn('已交易', dom_text(elements['health']))
        self.assertIn('BUY', dom_text(elements['positionList']))
        self.assertIn('0.01', dom_text(elements['positionList']))
        self.assertIn('<script>bad()</script>', dom_text(elements['positionList']))
        self.assertIn('<img src=x onerror=alert(1)>', dom_text(elements['versions']))
        self.assertIn('SYNTHETIC-RISK', dom_text(elements['versions']))
        self.assertIn('無', dom_text(elements['blockers']))
        self.assertEqual(len(elements['chart']['children']), 0, 'must not invent a PNL plot')
        snapshot['positions'] = []
        self.assertNotIn('未交易', dom_text(render_html(snapshot)['empty']))
        snapshot['feed_stale'] = True
        self.assertIn('過期', dom_text(render_html(snapshot)['health']))
        snapshot['engine'].update(status='warmup', warmup_received=12, warmup_required=61)
        snapshot['fills_count'] = 0
        self.assertIn('12/61', dom_text(render_html(snapshot)['health']))
        elements = render_html(snapshot, unavailable=True)
        self.assertEqual(elements['positionList']['children'], [])
        self.assertEqual(dom_text(elements['equity']), '—')
        html = (LAB / 'dashboard.html').read_text()
        for forbidden in ('innerHTML', 'https://', '<button', '<form', '<iframe'):
            self.assertNotIn(forbidden, html)

    def test_paper_brief_reports_net_pnl_states_and_fails_closed(self):
        snapshot = paper_fixture()
        report = brief.render(snapshot, now=NOW)
        self.assertNotIn('資料缺失或格式錯誤', report)
        for phrase in ('PAPER', '100 USDT', '實際錢包未知', '新鮮', 'REST polling',
                       '99.70', '101.95', '淨已實現 -0.30', '未實現 2.25', '合計 1.95',
                       '持倉 1', '訊號 3', '成交 1', '阻擋 2', 'SYNTHETIC-TEST', '已交易', '實盤停用'):
            self.assertIn(phrase, report)
        self.assertNotIn('未交易', report)
        self.assertNotIn('未實作', report)
        self.assertLessEqual(len(report.splitlines()), 8)
        snapshot['engine']['status'] = 'halted'
        snapshot['paper_trading_enabled'] = False
        snapshot['blockers'] = ['funding_incomplete\nsecond line']
        report = brief.render(snapshot, now=NOW)
        self.assertIn('停止', report)
        self.assertIn('已交易', report)
        self.assertNotIn('未交易', report)
        self.assertLessEqual(len(report.splitlines()), 8)
        snapshot.update(positions=[], fills_count=0, realized_pnl_usdt='0',
                        unrealized_pnl_usdt='0', total_pnl_usdt='0', cash_usdt='100', equity_usdt='100')
        snapshot['engine'].update(status='warmup', warmup_received=12, warmup_required=61)
        report = brief.render(snapshot, now=NOW)
        self.assertIn('暖機', report)
        self.assertIn('12/61', report)
        self.assertIn('未交易', report)
        self.assertIn('過期', brief.render(snapshot, now=datetime(2026, 10, 1, 0, 5, tzinfo=timezone.utc)))
        for field, invalid in [('live_trading_enabled', True), ('cash_usdt', '99'), ('risk', {})]:
            bad = copy.deepcopy(snapshot)
            bad[field] = invalid
            report = brief.render(bad, now=NOW)
            self.assertIn('資料缺失或格式錯誤', report)
            self.assertNotIn('現金 99', report)
        snapshot['engine']['version_id'] = 'SYNTHETIC\nINJECTION'
        snapshot['latest_error'] = 'test\nerror'
        self.assertLessEqual(len(brief.render(snapshot, now=NOW).splitlines()), 8)

    def test_exact_currency_invariants_fail_closed_but_net_fees_allow_positive_cash(self):
        from decimal import localcontext
        snapshot = paper_fixture()
        for field in ('cash_usdt', 'equity_usdt', 'total_pnl_usdt'):
            bad = copy.deepcopy(snapshot)
            bad[field] = '102'
            with self.subTest(field=field), self.assertRaises(ValueError):
                dashboard.validate_snapshot(bad)
        # Exact long-decimal equality must not depend on ambient precision.
        snapshot.update(realized_pnl_usdt='-0.300000000000000001',
                        cash_usdt='99.699999999999999999',
                        equity_usdt='101.949999999999999999',
                        total_pnl_usdt='1.949999999999999999')
        with localcontext() as context:
            context.prec = 6
            self.assertIsNone(dashboard.validate_snapshot(snapshot))
            snapshot['equity_usdt'] = '101.949999999999999998'
            with self.assertRaises(ValueError):
                dashboard.validate_snapshot(snapshot)

    def test_dashboard_accepts_proven_paper_positions_and_rejects_invalid_proof(self):
        snapshot = paper_fixture()
        self.assertIsNone(dashboard.validate_snapshot(snapshot))
        disabled = copy.deepcopy(snapshot)
        disabled['paper_trading_enabled'] = False
        self.assertIsNone(dashboard.validate_snapshot(disabled))
        corruptions = [('live_trading_enabled', True), ('mode', 'live'),
                       ('paper_trading_enabled', 1), ('fills_count', True),
                       ('signals_count', -1), ('equity_usdt', 'NaN')]
        for field, invalid in corruptions:
            bad = copy.deepcopy(snapshot)
            bad[field] = invalid
            with self.subTest(field=field), self.assertRaises((ValueError, ArithmeticError)):
                dashboard.validate_snapshot(bad)
        for field, invalid in [('approved', False), ('risk_version', ''),
                               ('risk_config_sha256', None), ('risk_config_sha256', 'B' * 64)]:
            bad = copy.deepcopy(snapshot)
            bad['risk'][field] = invalid
            with self.subTest(risk=field), self.assertRaises(ValueError):
                dashboard.validate_snapshot(bad)
        for field, invalid in [('implementation', 'unknown'), ('version_id', ''),
                               ('forward_start_at', '2026-10-01T00:00:00'),
                               ('forward_start_at', '2026-10-01T00:00:00+02:00'),
                               ('model_sha256', 'bad'), ('status', 'live')]:
            bad = copy.deepcopy(snapshot)
            bad['engine'][field] = invalid
            with self.subTest(engine=field), self.assertRaises(ValueError):
                dashboard.validate_snapshot(bad)
        for field, invalid in [('side', 'LONG'), ('qty', '0'), ('qty', True),
                               ('entry_price', '-1'), ('mark_price', 'Infinity'),
                               ('stop_price', '0'), ('unrealized_pnl_usdt', 'NaN')]:
            bad = copy.deepcopy(snapshot)
            bad['positions'][0][field] = invalid
            with self.subTest(position=field), self.assertRaises((ValueError, ArithmeticError)):
                dashboard.validate_snapshot(bad)
        for field in ('risk', 'engine'):
            bad = copy.deepcopy(snapshot)
            del bad[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                dashboard.validate_snapshot(bad)
