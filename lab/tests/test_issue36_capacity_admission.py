import unittest
from issue36_capacity_admission import run_profiles,ENTRY_STOP,WINDOW_MINUTES,MAX_RAW_EVENTS

class Issue36CapacityAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result=run_profiles()
        cls.by={x['profile']:x for x in cls.result['profiles']}

    def test_valid_profile_covers_full_forward_48h_and_restarts(self):
        p=self.by['continuous_valid_source']
        self.assertEqual(p['completed_minutes'],WINDOW_MINUTES)
        self.assertEqual(p['counters']['closed_bars'],WINDOW_MINUTES)
        self.assertEqual(p['counters']['poll_failures'],0)
        self.assertEqual(p['feed']['retained_events'],MAX_RAW_EVENTS)
        self.assertGreater(p['feed']['events_evicted'],0)
        self.assertEqual(p['restart_readback_causal_head'],p['evidence'].get('head_hash',p['restart_readback_causal_head']))
        self.assertLess(p['size_after_reopen']['total_bytes'],ENTRY_STOP)
        self.assertTrue(p['capacity_pass'])

    def test_invalid_outage_profile_is_fail_closed_and_capacity_decision_is_honest(self):
        p=self.by['heavy_late_invalid_transport_outage_recovery']
        self.assertGreater(p['counters']['late_invalid'],0)
        self.assertGreater(p['counters']['gaps'],0)
        self.assertGreaterEqual(p['counters']['poll_failures'],1)
        self.assertEqual(p['feed']['retained_events'],MAX_RAW_EVENTS)
        self.assertGreater(p['evidence']['kinds'].get('source_unknown',0),0)
        self.assertIsNotNone(p['gate_minute'])
        self.assertLess(p['gate_minute'],WINDOW_MINUTES)
        self.assertFalse(p['capacity_pass'])
        self.assertEqual(self.result['admission'],'NOT_FEASIBLE')

    def test_all_store_sizes_and_source_receipt_dispatch_are_reported(self):
        for p in self.result['profiles']:
            stores=p['size_after_reopen']['stores']
            self.assertIn('shared-feed.sqlite3',stores)
            self.assertIn('causal_evidence.sqlite3',stores)
            for arm in 'ABC':self.assertIn('arm-'+arm+'.sqlite3',stores)
            sample=p['source_receipt_dispatch']
            self.assertIsNotNone(sample['last'])
            self.assertIn('source_ts',sample['last'])
            self.assertIn('ts',sample['last'])

    def test_contract_is_admission_only_not_activation(self):
        self.assertFalse(self.result['operator_root_touched'])
        self.assertFalse(self.result['new_window_started'])
        self.assertFalse(self.result['deployed'])
        self.assertIn('immutable_causal',self.result['preserved_contracts'])
        self.assertIn('no_synthetic_fills',self.result['preserved_contracts'])

if __name__=='__main__':unittest.main()
