#!/usr/bin/env python3
"""Wake gate for the supervisor agent: prints what needs a decision NOW, empty output when nothing does.

Deterministic apart from a 3 hour round marker (printed only while something is pending) so that an unresolved item wakes the agent again every 3 hours.
Actionable:
  START      (roadmap) a roadmap lint problem: planned with all dependencies done, active without a ledger holder, or untouched for days
  READY      a DISPATCHED item with a cmd: or file_exists: acceptance test whose test now passes while its handle is still alive (external event happened)
  VERIFY     a DISPATCHED item whose handle has finished or died (a relay watcher ends when the engineer delivered, gave up or timed out)
  START      an OPEN/BLOCKED item whose `item:` blocker is in a terminal state (it is no longer blocked)
  REEVALUATE an owned deferral whose due date has passed
Read-only.
"""
import os, sys, time
from pathlib import Path

HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes/profiles/perp-desk'))).resolve()
sys.path.insert(0, str(HOME / 'plugins' / 'commitment-gate'))
import commit as c
import json
sys.path.insert(0, str(HOME / 'scripts'))
try:
    import auto_accept as auto    # optional: only profiles that run the engineering relay have it
except ImportError:
    auto = None

led = c.Ledger.__new__(c.Ledger)
try:
    with c.Ledger() as led:
        items = list(led.data['items'])
except Exception as e:
    print('LEDGER_UNREADABLE', str(e)[:120]); sys.exit(0)
by_id = {i['id']: i for i in items}
now = time.time(); out = []
for it in items:
    st = it['state']
    if st in c.TERMINAL: continue
    if st == c.DISPATCHED:
        hs = c.handle_status(it.get('handle'))[0]
        if hs in ('dead', 'finished'):
            extra = ''
            jd, logp = auto.job_dir_from_log(it['id']) if auto else (None, None)
            if jd is not None:
                res = jd / ('auto_accept-%s.json' % it['id'])
                if res.exists():
                    try:
                        vd = json.load(open(res)).get('verdict')
                        if vd == 'PENDING_CI' and now - res.stat().st_mtime < 25 * 60: continue    # CI still running: auto_accept will redo it
                        extra = ' auto_accept=%s file=%s' % (vd, res)
                    except Exception: extra = ' auto_accept=unreadable file=%s' % res
                elif logp is not None and now - logp.stat().st_mtime < 25 * 60:
                    continue    # delivered a moment ago and the deterministic acceptance is still running: hold the agent back
                else: extra = ' auto_accept=missing'
            out.append('VERIFY %s handle=%s %s%s' % (it['id'], hs, it['text'][:70], extra))
        elif str(it.get('accept', '')).startswith(('cmd:', 'file_exists:')):
            # an external event the item was waiting for has happened (a reply arrived, a file appeared): the handle is still alive but the work is ready to be judged
            ok, ev = c.accept_ok(it['accept'], it)
            if ok: out.append('READY %s acceptance check passes (%s) %s' % (it['id'], ev, it['text'][:60]))
        continue
    b = it.get('blocked_by')
    if b and str(b).startswith('item:'):
        dep = by_id.get(str(b)[5:])
        if dep is None or dep['state'] in c.TERMINAL:
            out.append('START %s (blocker %s is %s) %s' % (it['id'], str(b)[5:], 'missing' if dep is None else dep['state'], it['text'][:70]))
    elif it.get('defer') and it.get('due') and float(it['due']) < now:
        out.append('REEVALUATE %s overdue deferral %s' % (it['id'], it['text'][:70]))
# the roadmap is a second source of work: an item that is planned with every dependency done, or active with nobody holding it, has no ledger entry and therefore no other way to wake anyone
import subprocess
RM = HOME / 'scripts' / 'roadmap.py'
if RM.exists() and (HOME / 'lab' / 'journal' / 'roadmap.json').exists():
    try:
        r = subprocess.run([sys.executable, str(RM), 'lint'], capture_output=True, text=True, timeout=30, env=dict(os.environ, HERMES_HOME=str(HOME)))
        for l in r.stdout.splitlines():
            l = l.strip()
            if l and l not in ('ok', 'PROBLEMS:'): out.append('START roadmap: ' + l.lstrip('- ')[:140])
    except Exception:
        pass
if out:
    print('\n'.join(out)); print('round %d' % (int(now) // 10800))
