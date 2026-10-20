#!/usr/bin/env python3
"""Scoped strict allow-once; NO_PENDING is not an approval."""
import sys
from approval_transaction import approve
if __name__ == "__main__":
    if len(sys.argv) != 2: raise SystemExit("usage: relay-allow-card.py JOB_DIR")
    result = approve(sys.argv[1], mode="strict")
    print(result["status"], result.get("reason", result.get("path", "")))
    raise SystemExit(0 if result["status"] in ("APPROVED", "NO_PENDING") else 2)
