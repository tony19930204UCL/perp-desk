#!/usr/bin/env python3
"""Deterministic Issue #34 transport/capacity evidence (isolated fixture only)."""
from __future__ import annotations
import json,sys,tempfile
from pathlib import Path
from urllib.error import URLError

LAB=Path(__file__).resolve().parent
sys.path.insert(0,str(LAB))
from discovery_feed import SharedFeed,MAX_RAW_EVENTS
from discovery_runner import DiscoveryRunner

BUDGET=32*1024*1024
ENTRY_STOP=int(BUDGET*0.9)

def main():
    with tempfile.TemporaryDirectory(prefix='issue34-') as td:
        path=Path(td)/'shared-feed.sqlite3'
        feed=SharedFeed(path,forward_start_ms=1_000_000)
        total=MAX_RAW_EVENTS*2
        for i in range(total):
            source=1_000_001+i
            # Deliberately stale relative to dispatch: retained and counted invalid,
            # never suitable for strategy dispatch.
            feed.ingest('book',dict(source_ts=source,receipt_ts=source,
                        bids=[['100','1']],asks=[['100.1','1']]),
                        received_ms=source+20_000)
        before=feed.snapshot();size_before=path.stat().st_size
        reopened=SharedFeed(path,forward_start_ms=1_000_000)
        after=reopened.snapshot();size_after=path.stat().st_size
        evidence=dict(
            label='ISSUE34_ISOLATED_FIXTURE_NOT_OPERATOR_ROOT',
            budget_bytes=BUDGET,entry_stop_bytes=ENTRY_STOP,
            generated_invalid_events=total,raw_retention_limit=MAX_RAW_EVENTS,
            retained_events=after['retained_events'],
            retained_payload_bytes=after['retained_payload_bytes'],
            events_persisted=after['events_persisted'],
            events_evicted=after['events_evicted'],
            source_invalid=after['source_invalid'],
            sqlite_bytes_before_reopen=size_before,sqlite_bytes_after_reopen=size_after,
            below_entry_stop=size_after<ENTRY_STOP,
            same_root_reopen=True,
            transport=dict(
                reference_dns_retryable=DiscoveryRunner._retryable_failure('reference',URLError('temporary DNS')),
                reference_timeout_retryable=DiscoveryRunner._retryable_failure('reference',TimeoutError('timeout')),
                reference_validation_retryable=DiscoveryRunner._retryable_failure('reference',ValueError('filters changed'))),
            causal_evidence_policy='separate immutable causal store; raw replay rollover only',
            claim_48h_feasible=False,
            operator_root_touched=False,deployed=False)
        if after['retained_events']!=MAX_RAW_EVENTS or after['events_evicted']!=total-MAX_RAW_EVENTS:
            raise AssertionError('raw rollover mismatch')
        if not evidence['below_entry_stop']:
            raise AssertionError('isolated raw window exceeds 90% entry stop')
        if evidence['transport']!={'reference_dns_retryable':True,'reference_timeout_retryable':True,
                                   'reference_validation_retryable':False}:
            raise AssertionError('transport lifecycle mismatch')
        print(json.dumps(evidence,sort_keys=True,separators=(',',':')))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
