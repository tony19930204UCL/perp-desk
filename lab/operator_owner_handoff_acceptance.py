#!/usr/bin/env python3
"""Focused public-safe Issue #26 owner-resumption acceptance artifact."""
import json
import sys
import unittest
from pathlib import Path

LAB=Path(__file__).resolve().parent
sys.path.insert(0,str(LAB))
sys.path.insert(0,str(LAB/'tests'))
import test_owner_handoff as t

CASES=[
    'test_policy_denial_becomes_awaiting_owner_not_completed',
    'test_external_prerequisite_block_stays_terminal_and_has_no_owner_escalation',
    'test_delivery_is_not_owner_receipt_execution_or_completion',
    'test_owner_receipt_without_execution_remains_non_active',
    'test_executing_requires_owner_handle_and_evidence_and_interruption_reverts',
    'test_owner_handoff_suppresses_duplicate_cron_worker_when_source_goes_overdue',
    'test_owner_terminal_result_requires_authoritative_work_completion',
    'test_delivery_claim_interruption_and_failure_retry_are_bounded',
    'test_subprocess_escalation_adapter_is_public_safe_and_not_completion',
    'test_normal_ticks_still_zero_model_with_owner_handoff_enabled',
]

def main():
    suite=unittest.TestSuite(t.OwnerHandoffTests(name) for name in CASES)
    result=unittest.TextTestRunner(stream=sys.stderr,verbosity=2).run(suite)
    payload={
        'label':'ARTIFICIAL ISSUE-26 OWNER HANDOFF - NOT LIVE OWNER RESUMPTION',
        'ok':result.wasSuccessful(),
        'tests_run':result.testsRun,
        'normal_tick_model_calls':0,
        'official_escalation_transport':'hermes send',
        'automatic_authorized_owner_session_resume_claimed':False,
        'delivery_counts_as_owner_receipt':False,
        'receipt_counts_as_execution':False,
        'execution_requires_handle_and_evidence':True,
        'live_cron_changed':False,
        'approval_policy_changed':False,
        'health_writer_changed':False,
        'trading_state_changed':False,
        'cases':CASES,
        'failures':[case.id() for case,_ in result.failures],
        'errors':[case.id() for case,_ in result.errors],
    }
    print(json.dumps(payload,sort_keys=True,separators=(',',':')))
    return 0 if payload['ok'] else 1

if __name__=='__main__':
    raise SystemExit(main())
