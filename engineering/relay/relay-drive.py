"""Watcher for one relay job: loops queue_runner step, auto-approves GitHub allow cards within scope, stops at a state that needs the operator.

usage: relay-drive.py JOB_DIR [MAX_STEPS=40]     (JOB_DIR contains job.json)
Stops (exit 0) on REVIEW_REQUIRED, ALL_DONE, BLOCKED_OWNER, STEP_ERR, or ENGINEER_IDLE_NO_HANDBACK (chat idle after 8+ incomplete steps: the engineer replied without delivering); prints one line per step. Two identical scope
blocks in a row also stop it (a card the scope check refuses must reach a human, not be retried silently).
Read-only GitHub errors ('error connecting to api.github.com') are retried up to 12 times.
Keep this file in the skill: copies in scratch directories get pruned.
"""
import json, subprocess, sys, time
from pathlib import Path
SK = '<OPERATOR_HOME>/.hermes/profiles/perp-desk/skills/chatgpt-engineering-relay/scripts'
sys.path.insert(0, SK)
import engineering_instruction_carrier as c
from main_verdict import atomic

S = Path(sys.argv[1])
maxs = int(sys.argv[2]) if len(sys.argv) > 2 else 40
j = json.load(open(S / 'job.json'))


def tr(req):
    p = subprocess.run(j['transport_command'], input=json.dumps(req), capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr[-200:])
    return json.loads(p.stdout)


snap = lambda: tr({'op': 'snapshot'})
ev = lambda e: tr({'op': 'evaluate', 'expression': e})


def approve():
    evs = []
    try:
        ok = c.permission(snap(), cdp_fn=ev, snapshot_fn=snap, repo=j['repository'], branch=j['branch'], mailbox=j['mailbox'],
                          allowed=set(j['write_allowlist']), pr_number=j['pr_number'],
                          save_fn=lambda name, obj: atomic(S / name, obj),
                          event_fn=lambda kind, **kw: evs.append((kind, kw)), valid_until=j.get('valid_until'))
        return ok, evs
    except Exception as e:
        return False, str(e)[:200]


scope_blocks = 0
net_retries = 0
incomplete = 0
IDLE_AFTER = 8   # consecutive INCOMPLETE_HANDBACK steps (about 5 minutes) before checking whether the engineer is idle
for i in range(maxs):
    p = subprocess.run(['python3', SK + '/queue_runner.py', 'step', '--job', str(S / 'job.json')], capture_output=True, text=True, timeout=200)
    try:
        r = json.loads(p.stdout)
    except Exception:
        r = {'status': 'STEP_ERR', 'err': p.stderr[-200:]}
    print(time.strftime('%X'), i, json.dumps(r)[:200], flush=True)
    st = r.get('status')
    if st == 'STEP_ERR' and 'connecting to api.github.com' in str(r.get('err')) and net_retries < 12:
        net_retries += 1
        time.sleep(15)
        continue
    if st == 'BLOCKED_AUTH':
        ok, info = approve()
        print('  approve:', (ok, info), flush=True)
        if ok:
            scope_blocks = 0
        else:
            scope_blocks += 1
            if scope_blocks >= 2:
                print('  scope refused twice: stopping for a human', flush=True)
                break
        time.sleep(5)
        continue
    if st == 'WAITING' and r.get('detail') == 'INCOMPLETE_HANDBACK':
        incomplete += 1
        if incomplete >= IDLE_AFTER and incomplete % 4 == 0:
            try:
                if not snap().get('busy'):
                    print('  ENGINEER_IDLE_NO_HANDBACK: the chat is idle but no complete handback arrived (read the last reply, it may say the work was not done)', flush=True)
                    break
            except Exception as e:
                print('  idle check failed:', str(e)[:100], flush=True)
    else:
        incomplete = 0
    if st in ('REVIEW_REQUIRED', 'ALL_DONE', 'BLOCKED_OWNER', 'STEP_ERR'):
        break
    time.sleep(30)
