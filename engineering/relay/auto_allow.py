#!/usr/bin/env python3
"""Bounded watcher: shared transaction lock, no nested approval subprocess."""
import datetime
import json
import os
from pathlib import Path
import sys
import time
from approval_transaction import approve, profile_root

HOME=Path(os.environ.get("HERMES_HOME",str(Path.home()/".hermes/profiles/perp-desk"))).expanduser().resolve()
SC=HOME/"cache"/"scratch"
STATE=HOME/"lab"/"journal"/"auto_allow_state.json"
ONCE="--once" in sys.argv

def active_job():
    best=None
    for p in SC.glob("relay-*/active_instruction.json"):
        try:
            job=json.loads((p.parent/"job.json").read_text())
            exp=datetime.datetime.fromisoformat(job["valid_until"])
            if exp.tzinfo is None or exp <= datetime.datetime.now(datetime.timezone.utc):
                continue
            m=p.stat().st_mtime
            if time.time()-m<14*3600 and (best is None or m>best[0]):
                best=(m,p.parent)
        except (OSError,ValueError,TypeError,KeyError):
            continue
    return best[1] if best else None

def main():
    state=json.loads(STATE.read_text()) if STATE.exists() else {}
    end=time.monotonic()+(0 if ONCE else 50)
    while True:
        job=active_job()
        if job is not None:
            result=approve(job,mode="auto")
            status=result["status"]
            if status=="APPROVED":
                state["clicked"]=state.get("clicked",0)+1
                print("ALLOWED_ONCE",result.get("fingerprint",""),flush=True)
            elif status not in ("NO_PENDING",):
                print("APPROVAL_"+status,result.get("reason",""),flush=True)
        if time.monotonic()>=end: break
        time.sleep(5)
    STATE.parent.mkdir(parents=True,exist_ok=True)
    STATE.write_text(json.dumps(state))

if __name__=="__main__":
    main()
