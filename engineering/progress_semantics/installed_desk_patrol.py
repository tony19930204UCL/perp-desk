#!/usr/bin/env python3
"""Desk patrol gate: no model. Prints nothing when all is well; prints ANOMALY / FINDING lines when the owner should look.
Used as the monitor script of a cron job whose agent then investigates, fixes what its whitelist allows, and reports.

Checks (all local files and systemctl, read-only):
  MACHINE_GAP    time since the previous patrol run exceeds 30 minutes (the machine or the scheduler was down); names which windows exceed the 3 hour void limit
  SERVICE        a perp-paper-* unit is not active, or its restart counter rose since the last run
  STALE_STATUS   a live status file has not been rewritten for 180 seconds
  POLL_ERRORS    poll errors / (poll errors + validated receipts) in the newest 2000 audit rows of an arm exceeds 5%
  WRITE_RATE     all paper processes together wrote more than 600 KB/s since the last run (needs the previous sample); target of the PERSIST-1 work
  DISK_FREE      free space on the data disk under 100 GB
  FINDING        lines from heartbeat_gate.py (positive experiment, large paper swing, daily digest)
Each key is repeated at most once per 6 hours unless its text changes. State: lab/journal/patrol_state.json
"""
import json, os, shutil, sqlite3, subprocess, sys, time
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes/profiles/perp-desk'))).resolve()
LAB = HOME / 'lab'; STATE = LAB / 'journal' / 'patrol_state.json'; PAUSES = LAB / 'journal' / 'paused_units.json'
UNITS = ['h2', 'f1h-btcusdt', 'f1h-solusdt', 'f1h-xrpusdt', 'w1-btcusdt', 'w1-ethusdt', 'w1-solusdt', 'w1-xrpusdt', 't1-btcusdt', 't1-ethusdt', 't1-solusdt', 't1-xrpusdt']
LIVE = {'h2': 'paper_v4_live.json'}
DATA = {'h2': 'paper-v4'}
WRITE_LIMIT = 600 * 1024; REPEAT = 6 * 3600; GAP_MIN = 30 * 60; VOID = 180 * 60


def sysd(unit, prop):
    return subprocess.run(['systemctl', '--user', 'show', 'perp-paper-%s.service' % unit, '-p', prop, '--value'], capture_output=True, text=True, timeout=20).stdout.strip()


def wbytes(pid):
    try:
        for l in open('/proc/%d/io' % pid):
            k, v = l.split(':')
            if k == 'write_bytes': return int(v)
    except Exception:
        return None


def poll_rate(unit):
    p = LAB / 'data' / DATA.get(unit, 'paper-' + unit) / 'runtime.sqlite3'
    try:
        c = sqlite3.connect('file:%s?mode=ro' % p, uri=True, timeout=10)
        err = ok = 0
        for (t,) in c.execute("select json_extract(payload,'$.type') from (select payload from audit order by id desc limit 2000)"):
            if t == 'poll_error': err += 1
            elif t == 'source_validation': ok += 1
        c.close()
        return err, ok
    except Exception:
        return None, None



def pause_markers(now):
    if not PAUSES.exists():
        return {}, {}
    try:
        data = json.loads(PAUSES.read_text('utf-8'))
        if data.get('schema_version') != 1 or not isinstance(data.get('units'), dict):
            raise ValueError('schema')
        valid = {}; notices = {}
        for unit, row in data['units'].items():
            if unit not in UNITS:
                notices['PAUSE_UNKNOWN_UNIT ' + str(unit)] = 'marker names a unit not monitored by patrol'
                continue
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ('reason','authorized_by','authorized_at','review_by')):
                raise ValueError('unit record')
            auth = datetime.fromisoformat(row['authorized_at'])
            review = datetime.fromisoformat(row['review_by'])
            if auth.tzinfo is None or review.tzinfo is None:
                raise ValueError('offset required')
            if review.timestamp() <= now:
                notices['PAUSE_EXPIRED ' + unit] = 'owner pause review_by has passed'
            else:
                valid[unit] = row
        return valid, notices
    except Exception as exc:
        return {}, {'PAUSE_MARKER_INVALID': 'paused_units.json invalid: %s' % str(exc)[:80]}


BROKER_ACTIVE_STATUSES = frozenset(('PENDING', 'RESTING'))
BROKER_TERMINAL_STATUSES = frozenset(('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'))

def pause_exposure(unit):
    p = LAB / 'data' / DATA.get(unit, 'paper-' + unit) / 'broker.sqlite3'
    try:
        c = sqlite3.connect('file:%s?mode=ro' % p, uri=True, timeout=10)
        try:
            row = c.execute('select payload from sim_broker_state where singleton=1').fetchone()
        finally:
            c.close()
        if not row:
            return None
        payload = json.loads(row[0])
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    positions = payload.get('positions')
    orders = payload.get('orders')
    if not isinstance(positions, dict) or not isinstance(orders, dict):
        return None
    statuses = []
    for order in orders.values():
        if not isinstance(order, dict) or not isinstance(order.get('status'), str):
            return None
        statuses.append(order['status'])
    if positions:
        return True
    if any(status in BROKER_ACTIVE_STATUSES for status in statuses):
        return True
    if all(status in BROKER_TERMINAL_STATUSES for status in statuses):
        return False
    return None

def main():
    now = time.time(); st = json.loads(STATE.read_text()) if STATE.exists() else {}
    prev_run = st.get('last_run'); out = {}
    if prev_run and now - prev_run > GAP_MIN:
        gap = (now - prev_run) / 60
        out['MACHINE_GAP'] = 'no patrol for %.0f minutes (machine or scheduler was down)%s' % (gap, '; exceeds the 180 minute void limit for any window that was running' if now - prev_run > VOID else '')
    pauses, pause_notices = pause_markers(now); out.update(pause_notices)
    restarts = {}; pidw = {}; tot = 0
    for u in UNITS:
        act = subprocess.run(['systemctl', '--user', 'is-active', 'perp-paper-%s.service' % u], capture_output=True, text=True, timeout=20).stdout.strip()
        n = int(sysd(u, 'NRestarts') or 0); restarts[u] = n
        paused = u in pauses
        if paused:
            exposure = pause_exposure(u)
            if exposure is True: out['PAUSE_OPEN_EXPOSURE ' + u] = 'paused unit has open position or open orders'
            elif exposure is None: out['PAUSE_EXPOSURE_UNKNOWN ' + u] = 'paused unit exposure cannot be established from broker state'
            if act == 'active': out['PAUSE_UNIT_ACTIVE ' + u] = 'owner-paused unit is active; normal checks apply'
        if act != 'active':
            if not paused: out['SERVICE ' + u] = 'unit is %s' % act
        elif n > st.get('restarts', {}).get(u, n): out['SERVICE ' + u] = 'restarted %d time(s) since the last patrol' % (n - st['restarts'][u])
        f = LAB / 'shared' / LIVE.get(u, 'paper_%s_live.json' % u.replace('-', '_'))
        if f.exists() and now - f.stat().st_mtime > 180 and (not paused or act == 'active'): out['STALE_STATUS ' + u] = 'status file not rewritten for %.0f s' % (now - f.stat().st_mtime)
        e, o = poll_rate(u)
        if e is not None and e + o and e / (e + o) > 0.05: out['POLL_ERRORS ' + u] = '%.1f%% (%d errors, %d validated) in the newest 2000 audit rows' % (100 * e / (e + o), e, o)
        pid = int(sysd(u, 'MainPID') or 0); w = wbytes(pid) if pid else None
        if w is not None: pidw[str(pid)] = w
    prev_w = st.get('pidw', {}); dt = now - st.get('last_run', now)
    if dt >= 240:
        tot = sum(max(0, v - prev_w[k]) for k, v in pidw.items() if k in prev_w)
        if tot / dt > WRITE_LIMIT: out['WRITE_RATE'] = 'paper processes wrote %.0f KB/s on average over the last %.0f minutes (limit %d KB/s)' % (tot / dt / 1024, dt / 60, WRITE_LIMIT // 1024)
    free = shutil.disk_usage(str(LAB)).free
    if free < 100 * 2 ** 30: out['DISK_FREE'] = '%.0f GB free' % (free / 2 ** 30)
    try:
        g = subprocess.run([sys.executable, str(HOME / 'scripts' / 'heartbeat_gate.py'), '--commit'], capture_output=True, text=True, timeout=60).stdout.strip()
        for i, line in enumerate(l for l in g.splitlines() if l.startswith('TELL')): out['FINDING %d' % i] = line
    except Exception as ex:
        out['PATROL_ERROR'] = 'heartbeat_gate failed: %s' % ex
    sent = st.get('sent', {}); lines = []
    for k, v in out.items():
        key = k if not k.startswith('FINDING') else 'FINDING ' + v[:60]
        prior = sent.get(key)
        if prior and prior['text'] == v and now - prior['at'] < REPEAT: continue
        sent[key] = dict(text=v, at=now); lines.append('%s: %s' % (k.split(' ')[0] if k.startswith('FINDING') else k, v))
    sent = {k: v for k, v in sent.items() if now - v['at'] < 2 * REPEAT}
    STATE.write_text(json.dumps(dict(last_run=now, restarts=restarts, pidw=pidw, sent=sent)))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
