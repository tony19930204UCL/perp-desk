import unittest
from issue36_capacity_admission import run_profiles,ENTRY_STOP

class Issue36CapacityAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Fast same-schema materialization + actual lifecycle smoke. The dedicated
        # focused CI step runs the full 2,880-minute engineering load.
        cls.result=run_profiles(minutes=10)
        cls.by={x['profile']:x for x in cls.result['profiles']}

    def test_profiles_materialize_all_actual_store_paths_and_restart_raw(self):
        for p in self.result['profiles']:
            stores=p['size_after_materialization']['stores']
            for required in ('shared-feed.sqlite3','causal_evidence.sqlite3',
                             'arm-a/broker.sqlite3','arm-b/broker.sqlite3','arm-c/broker.sqlite3'):
                self.assertIn(required,stores)
            self.assertEqual(p['restart_readback']['retained_events'],p['raw']['retained_events'])
            self.assertEqual(p['restart_readback']['events_persisted'],p['raw']['events_persisted'])
            self.assertTrue(p['accelerated_bulk_same_schema'])

    def test_actual_lifecycle_smoke_preserves_source_receipt_dispatch_and_restart(self):
        s=self.result['lifecycle_smoke']
        self.assertTrue(s['valid_source_receipt_dispatch']['source_valid'])
        self.assertFalse(s['late_source_receipt_dispatch']['source_valid'])
        self.assertEqual(s['valid_source_receipt_dispatch']['source_ts'],s['valid_source_receipt_dispatch']['receipt_ts'])
        self.assertGreaterEqual(s['source_gaps'],1)
        self.assertGreaterEqual(s['unknown_inputs'],1)
        self.assertEqual(s['causal_head_before'],s['causal_head_after_restart'])
        self.assertTrue(s['restart_readback'])

    def test_invalid_profile_accounts_for_each_unknown_causal_record(self):
        p=self.by['invalid']
        self.assertEqual(p['evidence']['kinds'].get('source_unknown'),p['raw_total_events'])
        self.assertEqual(p['raw']['source_invalid'],p['raw_total_events'])
        self.assertGreater(p['evidence']['payload_bytes']['source_unknown'],0)

    def test_fixed_budget_and_scope_contract_remain_unchanged(self):
        self.assertEqual(self.result['fixed_budget_bytes'],32*1024*1024)
        self.assertEqual(self.result['entry_stop_bytes'],ENTRY_STOP)
        self.assertFalse(self.result['operator_root_touched'])
        self.assertFalse(self.result['new_window_started'])
        self.assertFalse(self.result['deployed'])
        self.assertIn('immutable_causal',self.result['preserved_contracts'])
        self.assertIn('sticky_stop_unchanged',self.result['preserved_contracts'])
        self.assertIn('PR33_readonly_UI_unchanged',self.result['preserved_contracts'])
        for p in self.result['profiles']:self.assertFalse(p['real_public_market_claim'])

if __name__=='__main__':unittest.main()
