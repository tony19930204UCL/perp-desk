#!/usr/bin/env python3
"""no_agent cron (silent): run the deterministic acceptance for relay units that were delivered, without any model.

For every ledger item that is DISPATCHED with a finished handle whose watcher log ends in REVIEW_REQUIRED, find the job directory from the review packet path,
and start `accept_unit.py JOB --out JOB/auto_accept-<ID>.json` detached (one at a time). The supervisor agent is held back by ledger_wake.py until that file exists
(or 25 minutes passed), so the model starts with facts and never waits for a 6 minute test run.
Prints nothing.
Copy to <profile>/scripts/ (ledger_wake.py imports it from there) and keep accept_unit.py at <profile>/skills/relay-backlog-operator/scripts/.
"""
import fcntl, json, os, re, subprocess, sys, time
from pathlib import Path

HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))).resolve()
sys.path.insert(0, str(HOME / 'plugins' / 'commitment-gate'))
import commit as c
ACCEPT = HOME / 'skills' / 'relay-backlog-operator' / 'scripts' / 'accept_unit.py'


def job_dir_from_log(item_id):
    log = HOME / 'commitments' / 'runs' / (item_id + '.log')
    if not log.exists(): return None, None
    tail = log.read_text(errors='ignore').splitlines()[-6:]
    for l in reversed(tail):
        m = re.search(r'REVIEW_REQUIRED.*?"packet": "([^"]+review_packet\.json)', l)
        if m: return Path(m.group(1)).parent, log
    return None, log


def main():
    (HOME / 'lab' / 'journal').mkdir(parents=True, exist_ok=True)
    lock = open(HOME / 'lab' / 'journal' / 'auto_accept.lock', 'w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: return
    with c.Ledger() as led:
        items = [i for i in led.data['items'] if i['state'] == c.DISPATCHED and c.handle_status(i.get('handle'))[0] == 'finished']
    for it in items:
        jd, log = job_dir_from_log(it['id'])
        if jd is None or not (jd / 'job.json').exists(): continue
        out = jd / ('auto_accept-%s.json' % it['id']); running = jd / ('auto_accept-%s.running' % it['id'])
        if out.exists():
            try: pending = json.load(open(out)).get('verdict') == 'PENDING_CI' and time.time() - out.stat().st_mtime > 180
            except Exception: pending = False
            if not pending: continue
            out.unlink(); (jd / ('auto_accept-%s.running' % it['id'])).unlink(missing_ok=True)    # CI had not finished when this ran: do it again
        if running.exists():
            try:
                pid = int(running.read_text().split()[0])
                if os.path.exists('/proc/%d' % pid) and time.time() - running.stat().st_mtime < 1200: return    # one at a time
            except Exception: pass
        p = subprocess.Popen([sys.executable, str(ACCEPT), str(jd), '--out', str(out)], stdout=subprocess.DEVNULL, stderr=open(str(jd / ('auto_accept-%s.err' % it['id'])), 'w'), start_new_session=True)
        running.write_text('%d %s' % (p.pid, time.strftime('%F %T')))
        return    # at most one start per tick


if __name__ == '__main__':
    main()
