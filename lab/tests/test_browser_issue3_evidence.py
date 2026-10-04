"""Parser regressions for real-Chrome Issue #3 layout evidence."""
import unittest

from browser_issue3_acceptance import inspect_view


class BrowserEvidenceParserTests(unittest.TestCase):
    def view(self,attrs):
        dom='<html '+attrs+'><body>Beta high precision · 唯讀帳本 SOLUSDT '+\
            '<select id="ledgerSelect"></select><span data-exact="123.456"></span></body></html>'
        return inspect_view(dom,ledger_label='Beta high precision',symbol='SOLUSDT',
                            exact_values=['123.456'])

    def test_ready_nonoverflow_serialized_attributes_are_read(self):
        d=self.view('data-layout-scroll-width="1351" data-layout-client-width="1351" '
                    'data-layout-overflow="false" data-layout-phase="sync" data-render-ready="true"')
        self.assertEqual(d['failures'],[])
        self.assertEqual(d['scroll_width'],'1351')
        self.assertEqual(d['client_width'],'1351')
        self.assertEqual(d['phase'],'sync')

    def test_missing_measurement_is_distinct_from_real_overflow(self):
        missing=self.view('')
        self.assertIn('render-not-ready',missing['failures'])
        self.assertIn('layout-measurement-missing',missing['failures'])
        self.assertNotIn('root-layout-overflow',missing['failures'])

        overflow=self.view('data-layout-scroll-width="1400" data-layout-client-width="1351" '
                           'data-layout-overflow="true" data-layout-phase="sync" data-render-ready="true"')
        self.assertNotIn('render-not-ready',overflow['failures'])
        self.assertNotIn('layout-measurement-missing',overflow['failures'])
        self.assertIn('root-layout-overflow',overflow['failures'])


if __name__=='__main__':
    unittest.main()
