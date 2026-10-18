"""Stdlib-only contracts; importing this suite never launches Chromium."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB))
import browser_dashboard_v2_acceptance as acceptance


class D2BrowserHarnessContracts(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(acceptance.FIXTURE.read_text(encoding='utf-8'))

    def test_five_named_probes_are_distinct(self):
        self.assertEqual(len(acceptance.PROBES), 5)
        self.assertEqual(len(set(acceptance.PROBES)), 5)
        self.assertTrue(all(value.startswith('D2-BROWSER-') for value in acceptance.PROBES))

    def test_fixture_schema_and_synthetic_sources(self):
        self.assertIs(acceptance.fixture_contract(self.data), self.data)
        self.assertEqual(len(self.data['scorecard']['arms']), 2)
        self.assertEqual(self.data['scorecard']['arms'][0]['signals'], 0)
        self.assertIsNone(self.data['scorecard']['arms'][1]['signals'])
        self.assertIsNone(self.data['scorecard']['arms'][1]['pnl'])

    def test_fixture_hostility_is_literal_data(self):
        hostile = self.data['hostile']
        self.assertIn('<img', hostile)
        self.assertIn(hostile, self.data['scorecard']['arms'][1]['arm'])
        self.assertIn(hostile, self.data['journal'][0]['verdict'])
        self.assertIn(hostile, self.data['ledger']['items'][0]['text'])

    def test_fixture_bad_contract_rejected(self):
        for key, replacement in (('schema_version', 0), ('now_ms', True),
                                 ('journal', None), ('hostile', None),
                                 ('long_text', 'tiny')):
            with self.subTest(key=key):
                changed = dict(self.data, **{key: replacement})
                with self.assertRaises(ValueError):
                    acceptance.fixture_contract(changed)

    def test_synthetic_inputs_project_with_actual_reader(self):
        with tempfile.TemporaryDirectory() as td:
            paths = acceptance.write_sources(Path(td), self.data)
            result = acceptance.read_dashboard(*paths, now_ms=self.data['now_ms'],
                                              max_age_ms=1000)
        self.assertEqual(result['scorecard_state'], 'known')
        self.assertEqual(result['arms'][0]['signals'], 0)
        self.assertIsNone(result['arms'][1]['signals'])
        self.assertEqual(result['arms'][0]['pnl']['exact'], '0')
        self.assertEqual(result['arms'][1]['pnl']['display'], '未知')
        self.assertEqual(len(result['journal']), 1)
        self.assertEqual(len(result['commitments']), 1)

    def test_source_write_shapes_and_readonly_state(self):
        with tempfile.TemporaryDirectory() as td:
            sources = acceptance.write_sources(Path(td), self.data)
            before = acceptance.state({str(i): p for i, p in enumerate(sources)})
            self.assertEqual(json.loads(sources[0].read_text().splitlines()[0]),
                             self.data['scorecard'])
            self.assertEqual(json.loads(sources[2].read_text()), self.data['ledger'])
            self.assertEqual(before, acceptance.state({str(i): p for i, p in enumerate(sources)}))

    def test_allowed_origin_exact_match_only(self):
        base = 'http://127.0.0.1:54321'
        for path in ('/', '/dashboard_v2.html', '/dashboard_v2_ui.js', '/api/dashboard'):
            self.assertTrue(acceptance.permitted_origin(base + path, base))
        for url in ('https://example.com/', 'http://localhost:54321/',
                    base + '/?debug=1', base + '/other',
                    'http://127.0.0.1:54322/', base + '/%2e'):
            self.assertFalse(acceptance.permitted_origin(url, base))

    def test_report_exit_conditions(self):
        report = {'cleanup': 'CLEAN', 'probes': {
            name: {'status': 'PASS'} for name in acceptance.PROBES}}
        self.assertEqual(acceptance.exit_for(report), 0)
        for status, expected in (('FAIL', 1), ('TIMEOUT', 3), ('NOT_RUN', 2)):
            report['probes'][acceptance.PROBES[0]]['status'] = status
            self.assertEqual(acceptance.exit_for(report), expected)
        report['probes'][acceptance.PROBES[0]]['status'] = 'PASS'
        report['cleanup'] = 'FAIL'
        self.assertEqual(acceptance.exit_for(report), 1)

    def test_unavailable_browser_is_not_pass(self):
        with patch.object(acceptance.shutil, 'which', return_value=None):
            self.assertIsNone(acceptance.browser_binary(None))
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(acceptance.browser_binary(str(Path(td) / 'absent')))

    def test_hash_and_mtime_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'synthetic'
            f.write_bytes(b'example')
            first = acceptance.state({'synthetic': f})
            self.assertEqual(first['synthetic']['size'], 7)
            self.assertEqual(len(first['synthetic']['sha256']), 64)
            f.write_bytes(b'changed')
            self.assertNotEqual(first, acceptance.state({'synthetic': f}))

    def test_no_browser_in_default_unit_contracts(self):
        self.assertTrue(callable(acceptance.main))
        self.assertTrue(callable(acceptance.ChromeCDP))
        self.assertNotIn('playwright', sys.modules)


    def test_private_chromium_launch_hides_scrollbars(self):
        # Exercise the real launch argument construction without starting a browser.
        with tempfile.TemporaryDirectory() as td:
            with patch.object(acceptance.subprocess, 'Popen',
                              side_effect=RuntimeError('launch captured')) as launch:
                with self.assertRaisesRegex(RuntimeError, 'launch captured'):
                    acceptance.ChromeCDP('/synthetic/chromium', Path(td),
                                         'http://127.0.0.1:54321',
                                         acceptance.Deadline(3))
        launch.assert_called_once()
        flags = launch.call_args.args[0]
        self.assertIn('--headless=new', flags)
        self.assertEqual(flags.count('--hide-scrollbars'), 1)


if __name__ == '__main__':
    unittest.main()
