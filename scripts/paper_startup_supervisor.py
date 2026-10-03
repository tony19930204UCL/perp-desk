"""Operator entrypoint for bounded PAPER v3 startup/recovery. Does not install a service."""
from pathlib import Path
import sys

LAB=Path(__file__).resolve().parents[1]/'lab'
sys.path.insert(0,str(LAB))
from startup_recovery import main

if __name__=='__main__':
    raise SystemExit(main())
