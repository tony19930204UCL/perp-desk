import unittest
from issue36_capacity_admission import run_profiles,ENTRY_STOP

class Issue36CapacityAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fast structural/persistence smoke only. The exact 48h load runs once in
        # the dedicated Issue36 CI step and validates its own full-window result.
        cls.result=run_profiles(window_minutes=3)
        cls.by={x['profile']:x for x in cls.result['profiles']}

    def test_smoke_profiles_use_real_persistence_and_restart_readback(self):
        for p in self.result['profiles']:
            stores=p['size_after_reopen']['stores']
            self.assertIn('shared-feed.sqlite3',stores)
            self.assertIn('causal_evidence.sqlite3',stores)
            for arm in 'ABC':self.assertIn('arm-'+arm+'.sqlite3',stores)
            self.assertEqual(p['restart_readback_storage_bytes'],p['size_after_reopen']['total_bytes'])
            self.assertLessEqual(p['completed_minutes'],3)

    def test_source_receipt_dispatch_and_causal_unknown_are_reported(self):
        valid=self.by['continuous_valid_source']
        bad=self.by['heavy_late_invalid_transport_outage_recovery']
        self.assertIsNotNone(valid['source_receipt_dispatch']['last'])
        self.assertIn('source_ts',valid['source_receipt_dispatch']['last'])
        self.assertIn('ts',valid['source_receipt_dispatch']['last'])
        self.assertGreater(bad['counters']['late_invalid'],0)
        self.assertGreater(bad['evidence']['kinds'].get('source_unknown',0),0)

    def test_fixed_budget_contract_is_unchanged(self):
        self.assertEqual(self.result['fixed_budget_bytes'],32*1024*1024)
        self.assertEqual(self.result['entry_stop_bytes'],ENTRY_STOP)
        self.assertFalse(self.result['operator_root_touched'])
        self.assertFalse(self.result['new_window_started'])
        self.assertFalse(self.result['deployed'])
        self.assertIn('immutable_causal',self.result['preserved_contracts'])
        self.assertIn('no_synthetic_fills',self.result['preserved_contracts'])

    def test_smoke_does_not_claim_real_market_or_force_full_admission(self):
        self.assertEqual(self.result['requested_window_minutes'],3)
        for p in self.result['profiles']:
            self.assertFalse(p['real_public_market_claim'])

if __name__=='__main__':unittest.main()
