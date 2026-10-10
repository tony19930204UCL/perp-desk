"""Regression coverage for root-level paper config export rules."""
from pathlib import Path
import sys
import tempfile
import unittest

AUTOMATION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AUTOMATION))
import review_sync


class ConfigAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'lab').mkdir()

    def put(self, name, content='{}'):
        path = self.root / 'lab' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def exported(self):
        return review_sync.collect(self.root)

    def test_base_config_exports(self):
        self.put('paper_config.json')
        self.assertIn('lab/paper_config.json', self.exported())

    def test_versioned_config_exports(self):
        self.put('paper_config_v4.json')
        self.assertIn('lab/paper_config_v4.json', self.exported())

    def test_multisegment_config_exports(self):
        self.put('paper_config_f1h_btcusdt.json')
        self.assertIn('lab/paper_config_f1h_btcusdt.json', self.exported())

    def test_existing_explicit_names_export(self):
        names = ('paper_config_v2.json', 'paper_config_v3.json',
                 'operator_event_gate_config.example.json',
                 'discovery_config_v1.json', 'discovery_config_v2.json',
                 'research_dashboard_config.example.json')
        for name in names:
            self.put(name)
        self.assertEqual(set(self.exported()), {'lab/' + name for name in names})

    def test_nested_configs_excluded(self):
        for name in ('shared/paper_config_v9.json',
                     'evidence/paper_config_v9.json',
                     'data/x/paper_config_v9.json'):
            self.put(name)
        self.assertEqual(self.exported(), {})

    def test_near_miss_names_excluded(self):
        for name in ('paper_config_v4.json.bak', 'paper_configv4.json',
                     'Paper_Config_v4.json', 'paper_config_v4.JSON'):
            self.put(name)
        self.assertEqual(self.exported(), {})

    def test_token_pattern_is_blocked(self):
        token = 'gh' + 'p_' + 'x' * 30
        self.put('paper_config_v4.json', '{"value":"' + token + '"}')
        with self.assertRaises(review_sync.Blocked):
            self.exported()

    def test_matching_symlink_is_blocked(self):
        outside = self.root / 'outside'
        outside.write_text('{}')
        (self.root / 'lab/paper_config_v4.json').symlink_to(outside)
        with self.assertRaises(review_sync.Blocked):
            self.exported()

    def test_fixed_profile_exact_export_set(self):
        for name in ('paper_config.json', 'paper_config_v4.json',
                     'paper_config_f1h_solusdt.json', 'discovery_config_v2.json',
                     'shared/paper_config_v9.json', 'paper_configv4.json',
                     'staging/probe/paper_config_v3.json',
                     'fixtures/demo/sample.json', 'tests/test_demo.py'):
            self.put(name, 'pass\n' if name.endswith('.py') else '{}')
        expected = {'lab/' + name for name in
                    ('paper_config.json', 'paper_config_v4.json',
                     'paper_config_f1h_solusdt.json', 'discovery_config_v2.json',
                     'staging/probe/paper_config_v3.json',
                     'fixtures/demo/sample.json', 'tests/test_demo.py')}
        self.assertEqual(set(self.exported()), expected)

    def test_skipped_directory_remains_skipped(self):
        self.put('__pycache__/paper_config_v4.json')
        self.assertEqual(self.exported(), {})


if __name__ == '__main__':
    unittest.main()
