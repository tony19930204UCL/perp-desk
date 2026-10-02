"""Scheduler candidate: collect PAPER operational state, stdout only on change."""
from pathlib import Path
import sys
ROOT=Path('/home/chihcheng/.hermes/profiles/perp-desk/lab')
sys.path.insert(0,str(ROOT))
from health_watchdog import main
if __name__=='__main__':
    raise SystemExit(main(['--once']))
