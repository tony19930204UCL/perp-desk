#!/usr/bin/env python3
"""Scoped diff-clean allow-once. The lock spans policy and final readback."""
import sys
from approval_transaction import approve
if __name__ == "__main__":
    if len(sys.argv) not in (2, 3): raise SystemExit("usage: relay-allow-if-diff-clean.py JOB_DIR [MAX_CHANGED_LINES]")
    result = approve(sys.argv[1], mode="diff", max_lines=int(sys.argv[2]) if len(sys.argv) == 3 else 4)
    print(result["status"], result.get("reason", result.get("path", "")))
    raise SystemExit(0 if result["status"] in ("APPROVED", "NO_PENDING") else 2)
