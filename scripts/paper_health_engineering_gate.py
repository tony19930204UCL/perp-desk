"""Scheduler candidate: gated engineering wake. Never spawns or claims repair work."""
from pathlib import Path
import sys
import json
ROOT=Path('/home/chihcheng/.hermes/profiles/perp-desk/lab')
sys.path.insert(0,str(ROOT))
from health_watchdog import once, engineering_gate
if __name__=='__main__':
    once(ROOT)
    print(json.dumps(engineering_gate(ROOT),sort_keys=True,ensure_ascii=False))
