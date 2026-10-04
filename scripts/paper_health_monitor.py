"""Scheduler candidate: collect PAPER operational state, stdout only on change."""
from pathlib import Path
import sys
ROOT=Path('/home/chihcheng/.hermes/profiles/perp-desk/lab')
sys.path.insert(0,str(ROOT))
from health_watchdog import once, delivery, load_monitor_config
if __name__=='__main__':
    cfg=load_monitor_config(ROOT/'shared/health_monitor_config.json')
    report=once(ROOT,runtime_root=cfg['runtime_root'],state_dir=cfg['state_dir'],
                status_path=cfg['status_path'],runtime_script=cfg['runtime_script'])
    message=delivery(report)
    if message: print(message)
