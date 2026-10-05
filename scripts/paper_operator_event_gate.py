#!/usr/bin/env python3
"""Hermes cron pre-check wrapper for Issue #22.

Install under the profile's scripts/ directory. The local, operator-owned config
lives under lab/shared and is excluded from mirror export.
"""
from pathlib import Path
import sys

PROFILE=Path(__file__).resolve().parents[1]
LAB=PROFILE/'lab'
sys.path.insert(0,str(LAB))

from operator_event_gate import scheduler_control
from operator_owner_handoff import deliver_once

if __name__=='__main__':
    config=LAB/'shared/operator_event_gate_config.json'
    # Deterministic zero-LLM owner escalation. Delivery is tracked separately
    # from owner receipt/execution/completion inside the gate namespace.
    deliver_once(config)
    print(scheduler_control(config))
