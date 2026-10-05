#!/usr/bin/env python3
"""Policy-compliant owner escalation adapter for Issue #26.

Uses the officially supported `hermes send` CLI for one-shot, zero-LLM delivery.
Delivery is not owner receipt, execution, or completion. The durable gate state
tracks those later stages separately.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

from operator_event_gate import Gate, GateError, canonical, load_config

def owner_message(event):
    return (
        "Perp Desk owner action required\n"
        f"event_id={event['event_id']}\n"
        f"kind={event['kind']}\n"
        "transition=policy_capability_blocked\n"
        f"evidence_ref={event['evidence_ref']}\n"
        f"permitted_next_action={event['permitted_next_action']}\n"
        "state=awaiting_owner\n"
        "Delivery of this message is not owner receipt, execution, or completion. "
        "Use the documented owner-receive / owner-start / owner-complete readback flow."
    )

def _real_transport(target,message):
    try:
        proc=subprocess.run(
            ['hermes','send','--to',target,'--json'],
            input=message,text=True,capture_output=True,timeout=30,check=False)
    except (OSError,subprocess.TimeoutExpired):
        return False,None,'transport'
    if proc.returncode!=0:
        return False,None,'notification'
    digest=hashlib.sha256(proc.stdout.encode('utf-8')).hexdigest()[:24]
    return True,'hermes-send:'+digest,None

def deliver_once(config_path,*,now_ms=None,transport=None):
    cfg=load_config(config_path)
    if not cfg['owner_handoff']['enabled']:
        return dict(attempted=False,reason='owner_handoff_disabled')
    gate=Gate(cfg,now_ms=now_ms)
    event=gate.claim_owner_escalation()
    if event is None:
        return dict(attempted=False,reason='no_due_owner_handoff')
    message=owner_message(event)
    sender=_real_transport if transport is None else transport
    try:
        success,handle,failure=sender(cfg['owner_handoff']['target'],message)
    except Exception:
        success,handle,failure=False,None,'interrupted'
    gate=Gate(cfg,now_ms=now_ms)
    gate.finish_owner_escalation(event['escalation_token'],success=bool(success),
                                 delivery_handle=handle if success else None,
                                 failure_kind=None if success else failure)
    return dict(attempted=True,event_id=event['event_id'],success=bool(success),
                delivery_handle=handle if success else None,
                failure_kind=None if success else failure)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--engineering-fixture',action='store_true')
    p.add_argument('--fixture-result',choices=('success','notification-fail','interrupted'))
    p.add_argument('--now-ms',type=int)
    a=p.parse_args(argv)
    if (a.fixture_result is not None or a.now_ms is not None) and not a.engineering_fixture:
        p.error('--fixture-result/--now-ms are engineering-fixture only')
    transport=None
    if a.engineering_fixture:
        mode=a.fixture_result or 'success'
        def fixture_transport(_target,_message):
            if mode=='success':
                return True,'fixture-delivery:accepted',None
            if mode=='notification-fail':
                return False,None,'notification'
            raise RuntimeError('synthetic interrupted delivery')
        transport=fixture_transport
    result=deliver_once(a.config,now_ms=a.now_ms,transport=transport)
    print(canonical(result))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
