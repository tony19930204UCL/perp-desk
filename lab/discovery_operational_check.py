#!/usr/bin/env python3
"""Focused synthetic acceptance for the three operator-blocking Issue #23 gaps."""
import io, json, sys, unittest
from pathlib import Path

LAB=Path(__file__).resolve().parent
sys.path.insert(0,str(LAB));sys.path.insert(0,str(LAB/'tests'))
import test_discovery_lab as t

CASES=[
    (t.RunnerSubprocessTests,'test_operator_cli_prepare_activate_run_report_stop_restart',
     'staged public-source CLI prepare/explicit-activate/shared-run/report/orderly-stop/restart'),
    (t.DiscoveryLabTests,'test_runner_source_gap_scope_is_durable_and_fail_closed',
     'aggTrade transport gaps isolate to maker arm while shared-source gaps fail closed all arms and persist restart state'),
    (t.DiscoveryLabTests,'test_queue_2x_same_event_waiting_then_partial_vs_primary_fill',
     'same-event primary-1x fill versus shadow-2x waiting/nonfill then partial sensitivity'),
    (t.RetentionEvidenceTests,'test_raw_rollover_keeps_compact_causal_reconstruction_and_storage_exit',
     'raw retention rollover causal reconstruction plus 32MiB entry-stop/protective-exit'),
]

def main():
    suite=unittest.TestSuite(cls(name) for cls,name,_ in CASES)
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    payload=dict(
        label='ARTIFICIAL ISSUE-23 OPERATIONAL ACCEPTANCE - NOT DEPLOYMENT OR MARKET PERFORMANCE',
        ok=result.wasSuccessful(),tests_run=result.testsRun,
        deployed=False,live_orders=False,public_network_required=False,
        cases=[dict(test=cls.__name__+'.'+name,claim=claim) for cls,name,claim in CASES],
        failures=[case.id() for case,_ in result.failures],
        errors=[case.id() for case,_ in result.errors])
    print(json.dumps(payload,sort_keys=True,separators=(',',':')))
    return 0 if payload['ok'] else 1

if __name__=='__main__':
    raise SystemExit(main())
