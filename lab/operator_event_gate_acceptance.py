#!/usr/bin/env python3
"""Focused public-safe Issue #22 scheduler-adapter acceptance artifact."""
import json
import sys
import unittest
from pathlib import Path

LAB=Path(__file__).resolve().parent
sys.path.insert(0,str(LAB))
sys.path.insert(0,str(LAB/'tests'))
import test_operator_event_gate as t

CASES=[
    'test_normal_ticks_are_byte_stable_and_zero_model_calls',
    'test_operator_confirmed_storage_hold_is_not_normal_and_never_autorestart_wake',
    'test_ready_backlog_survives_claim_interruption_lease_and_backoff',
    'test_provider_and_notification_failures_requeue_without_completion',
    'test_deadline_checkpoint_derive_only_from_configured_active_authority',
    'test_subprocess_scheduler_adapter_isolated_and_public_safe',
]

def main():
    suite=unittest.TestSuite(t.OperatorEventGateTests(name) for name in CASES)
    result=unittest.TextTestRunner(stream=sys.stderr,verbosity=2).run(suite)
    payload={
        'label':'ARTIFICIAL ISSUE-22 SCHEDULER ADAPTER - NOT DEPLOYMENT',
        'ok':result.wasSuccessful(),
        'tests_run':result.testsRun,
        'normal_tick_model_calls':0,
        'live_cron_changed':False,
        'health_writer_changed':False,
        'trading_state_changed':False,
        'public_network_required':False,
        'cases':CASES,
        'failures':[case.id() for case,_ in result.failures],
        'errors':[case.id() for case,_ in result.errors],
    }
    print(json.dumps(payload,sort_keys=True,separators=(',',':')))
    return 0 if payload['ok'] else 1

if __name__=='__main__':
    raise SystemExit(main())
