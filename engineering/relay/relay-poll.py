#!/usr/bin/env python3
"""Fallback watcher that does not depend on the carrier's send record.

usage: relay-poll.py JOB_DIR INSTRUCTION_ID [MAX_MINUTES=90]

Every RELAY_POLL_SECONDS seconds (default 30; the always-on auto_allow.py job handles cards fast):
  1. runs relay-allow-card.py JOB_DIR (strict allow-once: operation, repo, branch and file allowlist are checked there)
  2. reads the branch head and the mailbox file on the branch
Stops (exit 0) when the mailbox carries INSTRUCTION_ID with status READY_FOR_REVIEW, WAITING_CI or BLOCKED (prints HANDBACK <status> <head>),
or when the chat is idle for 8 minutes with no new commit and no card (prints ENGINEER_IDLE_NO_PROGRESS), or after MAX_MINUTES (prints TIMEOUT).
Read-only apart from the allow-once click. It never sends anything to the chat.
"""
import json, subprocess, sys, time
from pathlib import Path

JD = Path(sys.argv[1]); IID = sys.argv[2]; MAXM = int(sys.argv[3]) if len(sys.argv) > 3 else 90
j = json.load(open(JD / 'job.json'))
HERE = Path(__file__).resolve().parent
repo, branch, mailbox = j['repository'], j['branch'], j['mailbox']


def gh(path, raw=False):
    cmd = ['gh', 'api'] + (['-H', 'Accept: application/vnd.github.raw'] if raw else []) + [path]
    for _ in range(3):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if p.returncode == 0: return p.stdout
        time.sleep(5)
    return None


def snap():
    for _ in range(3):
        p = subprocess.run(j['transport_command'], input=json.dumps({'op': 'snapshot'}), capture_output=True, text=True, timeout=80)
        if p.returncode == 0:
            try: return json.loads(p.stdout)
            except Exception: pass
    return {}


start = time.time(); last_head = None; last_change = time.time(); cards = 0
while time.time() - start < MAXM * 60:
    from approval_transaction import approve
    ar = approve(JD, mode="auto")
    if ar["status"] == "APPROVED":
        cards += 1; last_change = time.time()
        print(time.strftime("%X"), "ALLOWED_ONCE", ar.get("fingerprint", ""), flush=True)
    elif ar["status"] not in ("NO_PENDING",):
        print(time.strftime("%X"), "APPROVAL", ar["status"], ar.get("reason", ""), flush=True)
    head = (gh('repos/%s/git/ref/heads/%s' % (repo, branch)) or '')
    try: head = json.loads(head)['object']['sha']
    except Exception: head = None
    if head and head != last_head:
        if last_head is not None: last_change = time.time(); print(time.strftime('%X'), 'new commit', head[:10], flush=True)
        last_head = head
        raw = gh('repos/%s/contents/%s?ref=%s' % (repo, mailbox, head), raw=True)
        try:
            mb = json.loads(raw)
            if mb.get('instruction_id') == IID and mb.get('status') in ('READY_FOR_REVIEW', 'WAITING_CI', 'BLOCKED'):
                print('HANDBACK', mb['status'], head, flush=True); sys.exit(0)
        except Exception:
            pass
    s = snap()
    if s.get('busy'): last_change = time.time()
    if time.time() - last_change > 480:
        print('ENGINEER_IDLE_NO_PROGRESS (chat idle 8 minutes, no commit, no card). Read the last reply.', flush=True); sys.exit(0)
    time.sleep(int(__import__("os").environ.get("RELAY_POLL_SECONDS", "30")))
print('TIMEOUT after %d minutes' % MAXM, flush=True)
