"""Operator handoff helper: report health incidents for GitHub Issue triage; never wakes repair."""
from pathlib import Path
import sys
import json
ROOT=Path('/home/chihcheng/.hermes/profiles/perp-desk/lab')
sys.path.insert(0,str(ROOT))
from health_watchdog import once, issue_handoff
if __name__=='__main__':
    once(ROOT)
    print(json.dumps(issue_handoff(ROOT),sort_keys=True,ensure_ascii=False))
