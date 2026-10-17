#!/usr/bin/env python3
"""Commitment ledger: a statement of future work is only allowed to say "dispatched" if something is running.

Pure stdlib, file based, no model calls. Works for any agent or profile.

A commitment is registered BEFORE it is promised in a message. Registering binds it to a handle (the thing that
will actually do the work) and, ideally, a machine-checkable acceptance test. The command prints a stamp such as
    [C-0007 已派工 pid:4242 驗收:file_exists 到期:11-01 12:00]
that the author pastes next to the sentence. `lint` refuses sentences about future work that carry no valid stamp,
and `check` (run by a scheduler) raises an alert when a commitment has no handle, a dead handle, is overdue, or
sits unverified. Verification is automatic when an acceptance test exists, otherwise it stays "待驗收".
"""
import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

OPEN, DISPATCHED, VERIFIED, BLOCKED, DROPPED = 'OPEN', 'DISPATCHED', 'VERIFIED', 'BLOCKED', 'DROPPED'
TERMINAL = {VERIFIED, DROPPED}
OPEN_GRACE_H = float(os.environ.get('COMMIT_OPEN_GRACE_H', 24))
STALE_H = float(os.environ.get('COMMIT_STALE_H', 72))
REPEAT_H = float(os.environ.get('COMMIT_REPEAT_H', 6))
CAS_REFUSED = 3

FUTURE = re.compile(
    r"(我會|我將|我要|我來|我去|接下來|下一步|之後會|稍後|待會|隨後|馬上|下次|會自動|等.{0,6}再|"
    r"\bI(?:'ll| will)\b|\bnext step\b|\bnext,|\bwill\b|\blater\b|\bgoing to\b|\bafter that\b)", re.I)
STAMP = re.compile(r"\[(C-\d{4,})\s+(已派工|已完成|未派工)[^\]]*\]")


def home():
    # Inside the multi-profile gateway the process env points at the default home; the active profile is a
    # per-turn override that only hermes_constants.get_hermes_home() sees. In a plain CLI the env var is right.
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get('HERMES_HOME') or (Path.home() / '.hermes'))


def default_ledger():
    return Path(os.environ.get('COMMIT_LEDGER') or (home() / 'commitments' / 'ledger.json'))


class Ledger:
    """Locked read-modify-write view of the ledger file."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_ledger()

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = open(str(self.path) + '.lock', 'a+')
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        self.data = json.loads(self.path.read_text('utf-8')) if self.path.exists() else {'next': 1, 'items': []}
        return self

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), 'utf-8')
                os.replace(tmp, self.path)
        finally:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()

    def get(self, cid):
        return next((i for i in self.data['items'] if i['id'] == cid), None)


# ---------------------------------------------------------------- handles
def _pid_start(pid):
    try:
        return int(Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()[19])
    except Exception:
        return None


def handle_status(h):
    """-> (state, detail). state in alive | finished | dead | unknown."""
    if not h:
        return 'dead', 'no handle'
    kind = h['kind']
    if kind == 'pid':
        ef = h.get('exitfile')
        if ef and Path(ef).exists():
            return 'finished', Path(ef).read_text().strip() or '?'
        if _pid_start(h['pid']) == h.get('start') and h.get('start') is not None:
            return 'alive', 'pid %d' % h['pid']
        return 'dead', 'pid %d is gone' % h['pid']
    if kind == 'cron':
        try:
            d = json.loads((home() / 'cron' / 'jobs.json').read_text('utf-8'))
            jobs = d.get('jobs', d)
            j = next((x for x in jobs if x.get('id') == h['ref']), None)
        except Exception as e:
            return 'unknown', 'cannot read cron table: %s' % str(e)[:60]
        if not j:
            return 'dead', 'cron job %s not found' % h['ref']
        if not j.get('enabled', True) or j.get('state') == 'paused':
            return 'dead', 'cron job %s is paused or disabled' % h['ref']
        if j.get('state') == 'completed':
            return 'finished', 'cron job %s completed (last_status=%s)' % (h['ref'], j.get('last_status'))
        return 'alive', 'cron %s next=%s' % (h['ref'], j.get('next_run_at'))
    return 'unknown', h['kind']  # manual, file: no liveness signal


# ---------------------------------------------------------------- acceptance
def accept_ok(a, item=None):
    """-> (ok, evidence)."""
    if not a:
        return False, 'no acceptance test'
    kind, _, arg = a.partition(':')
    if kind == 'file_exists':
        p = Path(arg)
        return (p.exists() and p.stat().st_size > 0), 'file %s' % arg
    if kind == 'file_fresh':
        path, _, minutes = arg.rpartition(':')
        p = Path(path)
        ok = p.exists() and (time.time() - p.stat().st_mtime) <= float(minutes) * 60
        return ok, 'file %s newer than %s min' % (path, minutes)
    if kind == 'cmd':
        try:
            r = subprocess.run(arg, shell=True, capture_output=True, text=True, timeout=30)
            return r.returncode == 0, 'cmd exit %d: %s' % (r.returncode, arg[:60])
        except Exception as e:
            return False, 'cmd error %s' % str(e)[:60]
    if kind == 'exit0':
        st, detail = handle_status((item or {}).get('handle'))
        return (st == 'finished' and detail == '0'), 'exit code %s' % detail
    return False, 'acceptance kind %s is manual' % kind


# ---------------------------------------------------------------- blockers
# A commitment that is not dispatched may only say WHY in one of these checkable forms. Free text is not accepted:
#   item:C-0007     waits for another ledger item to be VERIFIED (a real dependency)
#   cmd:SHELL       waits until the command exits 0; it must fail right now (else nothing blocks you)
#   file:PATH       waits until the file exists; it must not exist right now
#   user:QUESTION   needs a decision only the human can give; the question is sent with the stamp
# or an owned choice, --defer "what I decide not to do now" with --due (a re-decision date). A choice is labelled
# as a choice, never as an obstacle.
def blocker_state(led, bb):
    """-> (satisfied, detail). satisfied True means the block is gone and the item must be dispatched."""
    kind, _, arg = bb.partition(':')
    if kind == 'item':
        it = led.get(arg)
        if not it:
            return False, 'item %s does not exist' % arg
        if it['state'] == VERIFIED:
            return True, '%s verified' % arg
        if it['state'] == DROPPED:
            return False, '%s was dropped' % arg
        return False, '%s is %s' % (arg, it['state'])
    if kind == 'cmd':
        try:
            r = subprocess.run(arg, shell=True, capture_output=True, text=True, timeout=30)
            return r.returncode == 0, 'cmd exit %d' % r.returncode
        except Exception as e:
            return False, 'cmd error %s' % str(e)[:50]
    if kind == 'file':
        return Path(arg).exists(), 'file %s %s' % (arg, 'exists' if Path(arg).exists() else 'missing')
    if kind == 'user':
        return False, 'waiting for the user'
    raise SystemExit('refused: unknown blocker kind %r (use item:, cmd:, file:, user:)' % kind)


# ---------------------------------------------------------------- helpers
def parse_due(s):
    if not s:
        return None
    m = re.fullmatch(r'\+(\d+(?:\.\d+)?)([mhd])', s)
    if m:
        return time.time() + float(m.group(1)) * {'m': 60, 'h': 3600, 'd': 86400}[m.group(2)]
    return datetime.fromisoformat(s).timestamp()


def fmt_t(ts):
    return datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')


def stamp(item):
    if item['state'] == OPEN:
        if item.get('blocked_by'):
            return '[%s 未派工:等 %s]' % (item['id'], item['blocked_by'][:60])
        if item.get('defer'):
            return '[%s 未派工:我選擇延後（不是阻擋）重評:%s]' % (item['id'], fmt_t(item['due']) if item.get('due') else '?')
        return '[%s 未派工:無有效理由，須派工、阻擋或延後]' % item['id']
    label = '已完成' if item['state'] == VERIFIED else '已派工'
    h = item.get('handle') or {}
    parts = [item['id'], label, '%s:%s' % (h.get('kind', '?'), h.get('ref') or h.get('pid', ''))]
    if item.get('accept'):
        parts.append('驗收:' + item['accept'].split(':')[0])
    if item.get('due'):
        parts.append('到期:' + fmt_t(item['due']))
    return '[' + ' '.join(parts) + ']'


def touch(item, note):
    item['updated'] = time.time()
    item.setdefault('history', []).append([time.time(), note])


# ---------------------------------------------------------------- operations
def add(led, text, handle=None, run=None, accept=None, due=None, verifier='script', blocked_by=None, defer=None, source='agent'):
    if not text.strip():
        raise SystemExit('refused: empty commitment text')
    if not (handle or run or blocked_by or defer):
        raise SystemExit('refused: a commitment needs an executor (--run CMD or --handle KIND:REF), or a checkable blocker '
                         '(--blocked-by item:C-0007 | cmd:SHELL | file:PATH | user:QUESTION), or an owned choice '
                         '(--defer "what I decide not to do now" --due +3d). "not started" and free-text reasons are not accepted.')
    if blocked_by:
        sat, detail = blocker_state(led, blocked_by)
        if sat:
            raise SystemExit('refused: nothing blocks this (%s). Dispatch it now with --run or --handle.' % detail)
        if blocked_by.startswith(('item:',)) and 'does not exist' in detail or 'dropped' in detail:
            raise SystemExit('refused: blocker invalid (%s)' % detail)
        if blocked_by.startswith('user:') and len(blocked_by) < 12:
            raise SystemExit('refused: user blocker needs the concrete question')
    if defer:
        if not due:
            raise SystemExit('refused: --defer needs --due (the date I must decide again, at most +14d)')
        if parse_due(due) - time.time() > 14 * 86400 + 60:
            raise SystemExit('refused: a deferral may last at most 14 days')
    cid = 'C-%04d' % led.data['next']
    led.data['next'] += 1
    item = dict(id=cid, text=text.strip(), created=time.time(), updated=time.time(), state=OPEN, verifier=verifier,
                source=source, accept=accept, due=parse_due(due), handle=None, reason=None, blocked_by=blocked_by, defer=defer, history=[])
    if run:
        logdir = led.path.parent / 'runs'
        logdir.mkdir(parents=True, exist_ok=True)
        exitfile = logdir / (cid + '.exit')
        log = open(logdir / (cid + '.log'), 'ab')
        p = subprocess.Popen(['bash', '-c', '( %s ); echo $? > %s' % (run, shlex.quote(str(exitfile)))],
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
        time.sleep(0.2)
        item['handle'] = dict(kind='pid', pid=p.pid, start=_pid_start(p.pid), exitfile=str(exitfile), ref=str(p.pid))
        item['accept'] = accept or 'exit0'
    elif handle:
        kind, _, ref = handle.partition(':')
        h = dict(kind=kind, ref=ref)
        if kind == 'pid':
            h.update(pid=int(ref), start=_pid_start(int(ref)))
            if h['start'] is None:
                raise SystemExit('refused: pid %s is not running, so it cannot be a handle' % ref)
        if kind == 'cron' and handle_status(h)[0] in ('dead',):
            raise SystemExit('refused: %s' % handle_status(h)[1])
        item['handle'] = h
    if item['handle']:
        item['state'] = DISPATCHED
    touch(item, 'registered %s' % item['state'])
    led.data['items'].append(item)
    return item


def evaluate(led, now=None):
    """Update verifications, return alert lines (deduplicated by REPEAT_H)."""
    now = now or time.time()
    alerts = []
    for it in led.data['items']:
        if it['state'] in TERMINAL:
            continue
        problem = None
        hs, detail = handle_status(it.get('handle')) if it['state'] == DISPATCHED else ('dead', 'no handle')
        if it['state'] == DISPATCHED and it.get('accept') and it['accept'] != 'manual':
            ok, ev = accept_ok(it['accept'], it)
            if ok:
                it['state'] = VERIFIED
                it['evidence'] = ev
                touch(it, 'auto-verified: ' + ev)
                continue
        if it['state'] == OPEN:
            if it.get('blocked_by'):
                sat, detail = blocker_state(led, it['blocked_by'])
                if sat:
                    problem = '阻擋已解除（%s）但還沒派工' % detail
                elif 'dropped' in detail or 'does not exist' in detail:
                    problem = '阻擋無效（%s），須重新決定' % detail
                elif it['blocked_by'].startswith('user:') and now - it['created'] > OPEN_GRACE_H * 3600:
                    problem = '等使用者回答已超過 %.0f 小時：%s' % ((now - it['created']) / 3600, it['blocked_by'][5:80])
            elif it.get('defer'):
                if it.get('due') and now > it['due']:
                    problem = '延後到期，須重新決定：派工、再延後（附新理由）或放棄'
            else:
                problem = '沒有有效理由（舊式免證據理由）：須派工、掛上可檢查的阻擋、或明說延後'
        elif it['state'] == BLOCKED:
            problem = '被擋住：%s' % (it.get('reason') or '無原因')
        elif it['state'] == DISPATCHED:
            if hs == 'dead':
                problem = 'handle 已消失（%s）而驗收未通過' % detail
            elif hs == 'finished':
                problem = 'handle 已結束（%s）但驗收未通過或待人工驗收' % detail
            elif it.get('due') and now > it['due']:
                problem = '已逾期（到期 %s），handle 狀態：%s' % (fmt_t(it['due']), hs)
            elif now - it['updated'] > STALE_H * 3600:
                problem = '%.0f 小時沒有任何進展紀錄' % ((now - it['updated']) / 3600)
        if problem:
            sig = problem.split('（')[0]
            if it.get('alert_sig') == sig and now - it.get('alerted', 0) < REPEAT_H * 3600:
                continue
            it['alert_sig'], it['alerted'] = sig, now
            alerts.append('%s %s：%s' % (it['id'], it['text'][:50], problem))
    return alerts


def attention(led):
    """Deterministic (no timestamps) list of what needs a human or agent decision."""
    out = []
    for it in led.data['items']:
        if it['state'] in TERMINAL:
            continue
        hs = handle_status(it.get('handle'))[0] if it['state'] == DISPATCHED else '-'
        if it['state'] in (OPEN, BLOCKED) or hs in ('dead', 'finished'):
            out.append('%s %s handle=%s %s' % (it['id'], it['state'], hs, it['text'][:60]))
    return out


def lint(text, led):
    """-> list of (line_no, line, reason). Empty list = every future-work sentence is backed by the ledger."""
    bad = []
    in_code = False
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith('```'):
            in_code = not in_code
            continue
        if not s or in_code or s.startswith('>'):
            continue
        stamps = [(m.group(1), m.group(2), m.group(0)) for m in STAMP.finditer(line)]
        for cid, label, full in stamps:
            it = led.get(cid)
            if it and full != stamp(it):
                bad.append((n, s, '%s 的標記文字與帳本不符（自己加字或已過期），應為 %s' % (cid, stamp(it))))
                continue
            if not it:
                bad.append((n, s, '%s 不在帳本裡（偽造的標記）' % cid))
            elif label == '已派工' and not (it['state'] == DISPATCHED and handle_status(it.get('handle'))[0] != 'dead'):
                bad.append((n, s, '%s 標「已派工」但帳本狀態是 %s，handle %s' % (cid, it['state'], handle_status(it.get('handle'))[0])))
            elif label == '已完成' and it['state'] != VERIFIED:
                bad.append((n, s, '%s 標「已完成」但沒有通過驗收（%s）' % (cid, it['state'])))
            elif label == '未派工' and it['state'] != OPEN:
                bad.append((n, s, '%s 標「未派工」但帳本狀態是 %s' % (cid, it['state'])))
            elif label == '未派工' and not (it.get('blocked_by') or it.get('defer')):
                bad.append((n, s, '%s 標「未派工」但沒有可檢查的阻擋或延後決定' % cid))
            elif label == '未派工' and it.get('blocked_by') and blocker_state(led, it['blocked_by'])[0]:
                bad.append((n, s, '%s 的阻擋已解除，不能再說未派工' % cid))
        if not stamps and FUTURE.search(line):
            bad.append((n, s, '提到未來動作，沒有帳本標記'))
    return bad


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--ledger')
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('add')
    a.add_argument('text')
    a.add_argument('--handle', help='pid:N | cron:JOBID | manual:WHO | file:PATH')
    a.add_argument('--run', help='shell command to start detached now; its pid becomes the handle, acceptance defaults to exit0')
    a.add_argument('--blocked-by', dest='blocked_by', help='item:C-0007 | cmd:SHELL | file:PATH | user:QUESTION (checked, must be blocking now)')
    a.add_argument('--defer', help='an owned choice not to do it now (labelled as a choice); needs --due, at most +14d')
    a.add_argument('--accept', help='file_exists:PATH | file_fresh:PATH:MIN | cmd:COMMAND | exit0 | manual')
    a.add_argument('--due', help='+2h, +3d or an ISO local time')
    a.add_argument('--verifier', default='script', choices=['script', 'agent', 'user'])
    sub.add_parser('list').add_argument('--all', action='store_true')
    sub.add_parser('check')
    sub.add_parser('attention')
    li = sub.add_parser('lint')
    li.add_argument('file', nargs='?', default='-')
    for name in ('done', 'block', 'drop', 'show', 'stamp'):
        p = sub.add_parser(name)
        p.add_argument('id')
        if name != 'show' and name != 'stamp':
            p.add_argument('--evidence' if name == 'done' else '--reason', default='')
            p.add_argument('--expect-state', choices=[OPEN, DISPATCHED, VERIFIED, BLOCKED, DROPPED])
    args = ap.parse_args(argv)
    led = Ledger(args.ledger)

    if args.cmd == 'lint':
        text = sys.stdin.read() if args.file == '-' else Path(args.file).read_text('utf-8')
        with led:
            bad = lint(text, led)
        for n, line, why in bad:
            print('line %d: %s\n    -> %s' % (n, line[:90], why))
        return 1 if bad else 0
    with led:
        if args.cmd == 'add':
            it = add(led, args.text, args.handle, args.run, args.accept, args.due, args.verifier, args.blocked_by, args.defer)
            print(stamp(it))
        elif args.cmd == 'list':
            for it in led.data['items']:
                if args.all or it['state'] not in TERMINAL:
                    print('%s %-10s %s | handle=%s | %s' % (it['id'], it['state'], stamp(it), handle_status(it.get('handle'))[0] if it['state'] == DISPATCHED else '-', it['text'][:70]))
        elif args.cmd == 'check':
            for line in evaluate(led):
                print(line)
        elif args.cmd == 'attention':
            for line in attention(led):
                print(line)
        elif args.cmd in ('done', 'block', 'drop'):
            it = led.get(args.id)
            if not it:
                raise SystemExit('unknown id')
            def current():
                hs = handle_status(it.get('handle'))[0] if it['state'] == DISPATCHED else '-'
                hist = (it.get('history') or [[None, 'none']])[-1]
                print('%s state=%s handle=%s history=%s' % (it['id'], it['state'], hs, hist[1]))
            if args.expect_state and it['state'] != args.expect_state:
                current()
                raise SystemExit(CAS_REFUSED)
            if args.cmd == 'block' and it['state'] == VERIFIED:
                current()
                raise SystemExit(CAS_REFUSED)
            if args.cmd == 'done':
                if not args.evidence:
                    raise SystemExit('refused: done needs --evidence (what you read back)')
                if it['state'] == VERIFIED:
                    current()
                    if it.get('evidence') == args.evidence:
                        raise SystemExit(0)
                    raise SystemExit(CAS_REFUSED)
                it['state'], it['evidence'] = VERIFIED, args.evidence
            elif args.cmd == 'block':
                it['state'], it['reason'] = BLOCKED, args.reason or 'blocked'
            else:
                if not args.reason:
                    raise SystemExit('refused: drop needs --reason')
                it['state'], it['reason'] = DROPPED, args.reason
            touch(it, args.cmd)
            print(stamp(it))
        elif args.cmd in ('show', 'stamp'):
            it = led.get(args.id)
            print(json.dumps(it, ensure_ascii=False, indent=1) if args.cmd == 'show' else stamp(it))
    return 0


if __name__ == '__main__':
    sys.exit(main())
