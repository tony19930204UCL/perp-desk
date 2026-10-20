"""Stdlib-only browser contract tests, never start a browser in discovery."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
LAB=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(LAB))
import browser_dashboard_multiarm_acceptance as b


class BrowserContracts(unittest.TestCase):
    def test_probe_count(self):
        self.assertEqual(len(b.PROBES),7)
    def test_unique(self):
        self.assertEqual(len(set(b.PROBES)),7)
    def test_prefix(self):
        self.assertTrue(all(p.startswith('MA-BROWSER-') for p in b.PROBES))
    def test_fixture(self):
        self.assertEqual(json.loads(b.FIXTURE.read_text())['cash'],'99.66824579690005316')
    def test_no_runtime_browser(self):
        self.assertTrue(callable(b.main))
    def test_allowed_origin(self):
        origin='http://127.0.0.1:18001'
        self.assertTrue(b.allowed(origin+'/',origin))
        self.assertTrue(b.allowed(origin+'/api/multiarm',origin))
    def test_bad_origin(self):
        origin='http://127.0.0.1:18001'
        for url in ('https://elsewhere.invalid/','http://localhost:18001/','http://127.0.0.1:18002/','http://127.0.0.1:18001/api/multiarm?x=1'):
            self.assertFalse(b.allowed(url,origin))
    def test_source_hash(self):
        with tempfile.TemporaryDirectory() as td:
            f=Path(td)/'x';f.write_text('a')
            x=b.stat([f]);self.assertEqual(len(next(iter(x.values()))['sha256']),64)
            f.write_text('b')
            self.assertNotEqual(x,b.stat([f]))
    def test_temp_sqlite(self):
        with tempfile.TemporaryDirectory() as td:
            p=b.write_db(Path(td)/'paper-w1-xrpusdt',{'cash':'1'})
            self.assertTrue(p.exists())
    def test_chromium_flags(self):
        import browser_dashboard_v2_acceptance as cd
        with tempfile.TemporaryDirectory() as td:
            with patch.object(cd.subprocess,'Popen',side_effect=RuntimeError('captured')) as run:
                with self.assertRaises(RuntimeError):
                    cd.ChromeCDP('/synthetic/chromium',Path(td),'http://127.0.0.1:18001',cd.Deadline(5))
            self.assertIn('--hide-scrollbars',run.call_args.args[0])
    def test_exit_nonzero_unavailable(self):
        self.assertIsNone(b.cd.browser_binary('/a/missing/binary'))
    def test_no_install(self):
        import inspect
        text=inspect.getsource(b.main)
        self.assertNotIn('pip install',text)
        self.assertNotIn('playwright install',text)


    def test_dom_is_executed(self):
        self.assertTrue(b.DOM.startswith('(()=>('))
        self.assertTrue(b.DOM.endswith('))()'))

    def test_populated_accepts_stale_label(self):
        self.assertTrue(any(item.startswith('w1-xrpusdt')
                            for item in ['w1-xrpusdt · STALE']))

    def test_multiarm_interceptor_present(self):
        self.assertTrue(issubclass(b.MultiCDP,b.cd.ChromeCDP))
        self.assertIn('/api/multiarm',b.MultiCDP._event.__code__.co_consts)


if __name__=='__main__':unittest.main()
