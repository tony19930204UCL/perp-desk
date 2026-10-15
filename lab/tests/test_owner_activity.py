"""Synthetic-only Monitor projection, filesystem and HTTP regressions."""
import copy
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from owner_activity import build_activity, read_activity
from dashboard_v2_server import create_server

FIX = json.loads((ROOT / 'tests/fixtures/owner_activity_sanitized.json').read_text())
NOW = 1792065600000


def model(fixture=None):
    f = copy.deepcopy(fixture or FIX)
    return build_activity(f['ledger'], f['cron'], f['roadmap'], f['work'], f['journal'],
                          now_ms=NOW, execution_proofs=f.get('execution_proofs'),
                          result_proofs=f.get('result_proofs'))


def by_id(result, task):
    return next(x for x in result['tasks'] if x['id'] == task)


class Projection(unittest.TestCase):
    def test_running_vs_identity(self):
        m = model()
        self.assertEqual(by_id(m, 'C-0030')['state'], 'running_verified')
        self.assertEqual(by_id(m, 'C-0030')['activity'], 'verified_worker')
        for proof in ({'pid': 1234, 'start_identity': 'different', 'alive': True,
                       'observed_at': '2026-10-15T11:59:30Z'},
                      {'pid': 1234, 'start_identity': 'fixture-start', 'alive': False,
                       'observed_at': '2026-10-15T11:59:30Z'}, None):
            with self.subTest(proof=proof):
                f = copy.deepcopy(FIX)
                f['execution_proofs'] = {'C-0030': proof}
                self.assertEqual(by_id(model(f), 'C-0030')['state'], 'activity_unconfirmed')

    def test_idle_and_weekly_not_due(self):
        m = model()
        self.assertEqual(by_id(m, 'C-0028')['schedule']['run_state'], 'success_without_output')
        self.assertNotEqual(by_id(m, 'C-0028')['schedule']['run_state'], 'missed')
        self.assertEqual(by_id(m, 'C-0026')['schedule']['run_state'], 'result_proven')
        self.assertEqual(by_id(m, 'C-0026')['activity'], 'scheduled_idle')

    def test_missing_result_failure_and_missed(self):
        f = copy.deepcopy(FIX)
        f['result_proofs'] = {}
        self.assertEqual(by_id(model(f), 'C-0026')['schedule']['run_state'], 'success_without_output')
        f['cron']['jobs'][0]['last_status'] = 'failed'
        self.assertEqual(by_id(model(f), 'C-0026')['schedule']['run_state'], 'failed')
        f['cron']['jobs'][0]['next_run_at'] = '2026-10-14T00:00:00Z'
        f['cron']['jobs'][0]['last_run_at'] = '2026-10-13T00:00:00Z'
        self.assertEqual(by_id(model(f), 'C-0026')['schedule']['run_state'], 'missed')

    def test_resolved_blocker_and_overdue_recheck(self):
        row = by_id(model(), 'C-0031')
        self.assertEqual(row['blockers'], [])
        self.assertIn('recorded blocker resolved; ledger still blocked', row['warnings'])
        self.assertIn('deferral recheck overdue', row['warnings'])

    def test_conflicts_and_source_states(self):
        f = copy.deepcopy(FIX)
        f['work']['tasks'][0]['state'] = 'completed'
        f['ledger']['items'][2]['state'] = 'BLOCKED'
        self.assertIn('work/ledger conflict', by_id(model(f), 'C-0030')['warnings'])
        f['work'] = {'tasks': [], 'updated_at': '2026-10-17T00:00:00Z'}
        self.assertEqual(model(f)['sources']['work']['state'], 'conflict')
        f['work']['updated_at'] = '2026-01-01T00:00:00Z'
        self.assertEqual(model(f)['sources']['work']['state'], 'stale')

    def test_union_reconciles_sources(self):
        m = model()
        self.assertEqual(m['totals']['ledger'], len(FIX['ledger']['items']))
        self.assertEqual(m['totals']['cron'], len(FIX['cron']['jobs']))
        self.assertEqual(m['totals']['union'], len({t['id'] for t in m['tasks']}))
        self.assertEqual(m['totals']['union'], 5)
        self.assertEqual(m['stage_current'], None)
        self.assertTrue(all(t['deployed'] == 'unconfirmed' for t in m['tasks']))

    def test_read_missing_corrupt_symlink_and_oversize(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for parts in (('commitments',), ('cron',), ('lab', 'journal'), ('lab', 'shared')):
                (root.joinpath(*parts)).mkdir(parents=True, exist_ok=True)
            m = read_activity(str(root), now_ms=NOW)
            self.assertEqual(m['sources']['ledger']['state'], 'missing')
            target = root / 'commitments/ledger.json'
            target.write_text('{not-json')
            self.assertEqual(read_activity(str(root), now_ms=NOW)['sources']['ledger']['state'], 'invalid')
            target.unlink()
            target.symlink_to(root / 'cron/jobs.json')
            self.assertEqual(read_activity(str(root), now_ms=NOW)['sources']['ledger']['state'], 'invalid')
            target.unlink()
            target.write_text('x' * 500)
            self.assertEqual(read_activity(str(root), now_ms=NOW, max_bytes=128)['sources']['ledger']['state'], 'invalid')

    def test_unsafe_text_not_elevated_to_markup(self):
        f = copy.deepcopy(FIX)
        f['ledger']['items'][0]['text'] = '<img src=x onerror=alert(1)>'
        self.assertIn('<img', by_id(model(f), 'C-0026')['title'])


class API(unittest.TestCase):
    def test_monitor_opt_in_host_origin_methods_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for parts in (('commitments',), ('cron',), ('lab', 'journal'), ('lab', 'shared')):
                root.joinpath(*parts).mkdir(parents=True, exist_ok=True)
            root.joinpath('commitments/ledger.json').write_text(json.dumps(FIX['ledger']))
            root.joinpath('cron/jobs.json').write_text(json.dumps(FIX['cron']))
            root.joinpath('lab/journal/roadmap.json').write_text(json.dumps(FIX['roadmap']))
            root.joinpath('lab/shared/work_status.json').write_text(json.dumps(FIX['work']))
            root.joinpath('lab/journal/entries.jsonl').write_text(
                ''.join(json.dumps(x) + '\n' for x in FIX['journal']))
            cfg = dict(scorecard_path=str(root/'s'), journal_path=str(root/'j'),
                       ledger_path=str(root/'l'), clock_ms=lambda: NOW)
            httpd = create_server(**cfg)
            thread = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            thread.start()
            try:
                def req(method, path, headers=None):
                    con = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
                    try:
                        con.request(method, path, headers=headers or {})
                        r = con.getresponse()
                        return r.status, r.read()
                    finally:
                        con.close()
                self.assertEqual(req('GET', '/api/owner-activity')[0], 404)
                httpd.configure_monitor(profile_root=str(root))
                code, data = req('GET', '/api/owner-activity')
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(data)['totals']['ledger'], 5)
                self.assertEqual(req('GET', '/owner_activity.html')[0], 200)
                self.assertEqual(req('GET', '/owner_activity.js')[0], 200)
                self.assertEqual(req('POST', '/api/owner-activity')[0], 405)
                self.assertEqual(req('GET', '/api/owner-activity?file=x')[0], 404)
                self.assertEqual(req('GET', '/api/owner-activity', {'Host':'evil.example'})[0],403)
                self.assertEqual(req('GET', '/api/owner-activity', {'Origin':'https://evil.example'})[0],403)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
