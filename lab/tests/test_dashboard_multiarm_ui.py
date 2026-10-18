"""Static multiarm UI contracts; no Node, Chromium or external services."""
import json
import sys
import unittest
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
HTML = (LAB / 'dashboard_multiarm.html').read_text(encoding='utf-8')
JS = (LAB / 'dashboard_multiarm_ui.js').read_text(encoding='utf-8')


class UiTests(unittest.TestCase):
    def test_paper(self):
        self.assertIn('PAPER', HTML)
    def test_readonly(self):
        self.assertIn('READ-ONLY', HTML)
    def test_zh(self):
        self.assertIn('lang="zh-Hant-TW"', HTML)
    def test_local_js(self):
        self.assertIn('src="dashboard_multiarm_ui.js"', HTML)
    def test_no_remote_html(self):
        self.assertNotIn('https://', HTML)
        self.assertNotIn('http://', HTML)
    def test_no_remote_js(self):
        self.assertNotIn('https://', JS)
        self.assertNotIn('http://', JS)
    def test_no_markup_sinks(self):
        for term in ('innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write'):
            self.assertNotIn(term, JS)
    def test_no_write_fetch(self):
        for term in ("method:'POST'", "method:'PUT'", "method:'DELETE'", "method:'PATCH'"):
            self.assertNotIn(term, JS)
    def test_no_forms(self):
        self.assertNotIn('<form', HTML)
    def test_no_trade_actions(self):
        for term in ('下單', '買入', '賣出', '平倉按鈕'):
            self.assertNotIn('>'+term+'<', HTML)
    def test_five_tabs(self):
        for key in ('positions', 'closed', 'open_orders', 'orders', 'fills'):
            self.assertIn("'"+key+"'", JS)
    def test_twelve_arms(self):
        self.assertIn("'w1-xrpusdt'", JS)
        self.assertIn("'t1-xrpusdt'", JS)
    def test_unknown(self):
        self.assertIn('未知', JS)
    def test_stale(self):
        self.assertIn('STALE', JS)
    def test_refresh_interval(self):
        self.assertIn('setInterval(refresh,15000)', JS)
    def test_selection_retained(self):
        self.assertIn('let selected=', JS)
        self.assertIn('let selected=ids[0],tab=', JS)
    def test_scroll_retained(self):
        self.assertIn('window.scrollY', JS)
        self.assertIn('window.scrollTo(0,top)', JS)
    def test_failed_fetch_clears(self):
        self.assertIn("unknown('讀取失敗 · 未知')", JS)
    def test_api(self):
        self.assertIn("fetch('/api/multiarm'", JS)
    def test_text_safe(self):
        self.assertIn('textContent=label', JS)
    def test_tooltips(self):
        self.assertIn('value.title=exact', JS)
    def test_min_mobile(self):
        self.assertIn('@media(max-width:600px)', HTML)
    def test_no_external_fonts(self):
        self.assertNotIn('@import', HTML)
        self.assertNotIn('fonts.googleapis', HTML)
    def test_no_modelled_margin(self):
        for name in ('保證金','可用保證金','強平價','ROE'):
            self.assertIn(name, JS)
    def test_csp(self):
        self.assertIn("connect-src 'self'", HTML)


if __name__ == '__main__':
    unittest.main()
