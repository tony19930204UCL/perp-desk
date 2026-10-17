#!/usr/bin/env python3
"""Installed cron wrapper; behavior preserved from supplied source."""
import os
import subprocess
import sys
from pathlib import Path

home = os.environ.get("HERMES_HOME") or str(Path(__file__).resolve().parents[1])
env = dict(os.environ, HERMES_HOME=home)
r = subprocess.run(
    [sys.executable, os.path.join(home, "plugins/commitment-gate/commit.py"), "check"],
    capture_output=True, text=True, env=env, timeout=60)
out = r.stdout.strip()
if r.returncode != 0:
    out = (out + "\n" if out else "") + "承諾帳本檢查本身失敗：" + r.stderr.strip()[:200]
if out:
    print("承諾帳本需要處理：\n" + out)
