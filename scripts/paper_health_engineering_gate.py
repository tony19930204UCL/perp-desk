"""Operator handoff helper: report health incidents for GitHub Issue triage; never wakes repair."""
from pathlib import Path
import sys
import json
ROOT=Path('/home/chihcheng/.hermes/profiles/perp-desk/lab')
sys.path.insert(0,str(ROOT))
from health_watchdog import once, issue_handoff, load_monitor_config
if __name__=='__main__':
    cfg=load_monitor_config(ROOT/'shared/health_monitor_config.json')
    once(ROOT,runtime_root=cfg['runtime_root'],state_dir=cfg['state_dir'],
         status_path=cfg['status_path'],runtime_script=cfg['runtime_script'])
    print(json.dumps(issue_handoff(ROOT),sort_keys=True,ensure_ascii=False))
