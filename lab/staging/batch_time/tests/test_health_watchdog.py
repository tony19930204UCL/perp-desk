"""Standalone operational monitoring; fixtures never enter trading state."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)

def module():
    spec = importlib.util.spec_from_file_location('health_watchdog', ROOT / 'health_watchdog.py')
    if not spec or not spec.loader or not (ROOT / 'health_watchdog.py').exists():
        return None
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def snapshot():
    d = json.loads((ROOT / 'shared/paper_v2_live.json').read_text())
    d['updated_at'] = NOW.isoformat()
    d['feed'].update(connected=True, last_success_at=NOW.isoformat(), errors_count=0)
    d['latest_error'] = None; d['blockers'] = []
    for m in d['markets']:
        m['last_received_at'] = NOW.isoformat()
        m['source_timestamps_ms'] = dict.fromkeys(('bookTicker','depth5','premiumIndex'), int(NOW.timestamp()*1000))
    return d

class HealthTests(unittest.TestCase):
    def test_work_and_storage_are_independent_and_do_not_claim_execution(self):
        mod = module(); d = snapshot()
        work = {'tasks': [dict(id='old', state='completed', updated_at='2026-10-01T06:00:00Z', current_step='done'),
                          dict(id='repair', state='running', updated_at='2026-10-02T05:50:00Z', current_step='watching'),
                          dict(id='later', state='queued', updated_at='2026-10-02T04:00:00Z', current_step='planned')]}
        before = json.dumps(work, sort_keys=True)
        r = mod.evaluate(d, work, NOW, True, dict(used_bytes=600000000, budget_bytes=536870912, free_bytes=100000000, min_free_bytes=1073741824))
        self.assertIn('work-overdue', r['faults']); self.assertIn('storage-capacity', r['faults'])
        self.assertEqual(r['pending_work'][0]['id'], 'repair')
        self.assertTrue(r['pending_work'][0]['activity_unconfirmed'])
        self.assertNotIn('executing', r['pending_work'][0])
        self.assertEqual(json.dumps(work, sort_keys=True), before)
        self.assertIn('work-unavailable', mod.evaluate(d, None, NOW, True)['faults'])
        self.assertIn('work-overdue', mod.evaluate(None, work, NOW, True)['faults'])
        self.assertIn('not verified', r['service_restart_limitation'])

    def test_schema_identity_and_raw_timestamp_gates(self):
        mod = module(); good = snapshot()
        self.assertTrue(mod.evaluate(good, {'tasks': []}, NOW, True)['operational_healthy'])
        cases = [({}, 'snapshot-unavailable'),
                 ({'candidate_not_deployed': True}, 'snapshot-unavailable'),
                 ({'updated_at': 'invalid'}, 'snapshot-unavailable'),
                 ({'updated_at': '2026-10-02T05:58:00Z'}, 'heartbeat-stale'),
                 ({'updated_at': '2026-10-02T06:00:01Z'}, 'heartbeat-stale')]
        for changes, key in cases:
            d = snapshot() if changes else {}; d.update(changes)
            self.assertIn(key, mod.evaluate(d, {'tasks': []}, NOW, True)['faults'])
        for stamp in (int(NOW.timestamp()*1000)-61000, int(NOW.timestamp()*1000)+1, None, True):
            d = snapshot(); d['markets'][0]['source_timestamps_ms']['bookTicker'] = stamp
            self.assertIn('source-freshness', mod.evaluate(d, {'tasks': []}, NOW, True)['faults'])
        d = snapshot(); d['engine']['version_id'] = 'wrong'
        self.assertIn('snapshot-unavailable', mod.evaluate(d, {'tasks': []}, NOW, True)['faults'])
        d = snapshot(); d['markets'][0]['last_received_at'] = '2026-10-02T05:58:00Z'
        self.assertIn('source-freshness', mod.evaluate(d, {'tasks': []}, NOW, True)['faults'])
        d = snapshot(); d['feed']['connected'] = False; d['latest_error'] = 'ValueError: batch source age exceeded'
        r = mod.evaluate(d, {'tasks': []}, NOW, False)
        for key in ('runtime-absent','feed-disconnected','runtime-error'):
            self.assertIn(key, r['faults'])
        self.assertEqual(r['evidence']['latest_error'], d['latest_error'])

    def test_missing_snapshot_cannot_be_healthy(self):
        mod = module()
        self.assertIsNotNone(mod, 'operational watchdog missing')
        report = mod.evaluate(None, None, NOW, process_present=False)
        self.assertFalse(report['operational_healthy'])
        self.assertIn('snapshot-unavailable', report['faults'])

if __name__ == '__main__':
    unittest.main()
