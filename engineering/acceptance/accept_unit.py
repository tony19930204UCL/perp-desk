#!/usr/bin/env python3
"""Deterministic acceptance of one engineering unit. The model reads the JSON verdict; it does not judge these facts itself.

usage: accept_unit.py JOB_DIR [--tests "unittest args relative to lab"] [--skip-tests] [--out FILE]
Reads job.json (repository, branch, mailbox, write_allowlist). Checks, all by machine:
  head        branch head SHA (gh api) and that it still matches review_packet.json when one exists
  scope       files changed since the last accepted head (from the newest verdict-*.json with status ACCEPTED_SCOPE, else base of the PR) are all inside write_allowlist
  ci          exact-head workflow runs: every run completed with conclusion success, at least one run exists
  mailbox     mailbox on the branch: instruction_id, status, and whether it claims tests 'not run' (reported, not trusted)
  tests       clean `gh repo clone --depth 1` of the branch and the CI command (default: cd lab && python3 -m unittest discover -s tests) run in the background
              with a 14 minute budget; result parsed from the unittest summary (Ran N tests, OK / FAILED), skips and expectedFailure counted
Prints one JSON object and writes it to --out. verdict is PASS only when head, scope, ci and tests all pass.
Takes minutes: start it in the background and poll the --out file (a foreground tool call is cut off after about 7 minutes).
"""
import argparse, json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

ap = argparse.ArgumentParser(); ap.add_argument('job'); ap.add_argument('--tests', default='discover -s tests'); ap.add_argument('--skip-tests', action='store_true'); ap.add_argument('--out')
a = ap.parse_args(); JD = Path(a.job); j = json.load(open(JD / 'job.json'))
repo, branch, mailbox, allow = j['repository'], j['branch'], j['mailbox'], set(j['write_allowlist'])
res = dict(job=str(JD), repository=repo, branch=branch, at=time.strftime('%F %T UTC', time.gmtime()))


def gh(*args, raw=False):
    cmd = ['gh', 'api'] + (['-H', 'Accept: application/vnd.github.raw'] if raw else []) + list(args)
    for _ in range(3):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if p.returncode == 0: return p.stdout
        time.sleep(4)
    return None


head = (gh('repos/%s/git/ref/heads/%s' % (repo, branch), '--jq', '.object.sha') or '').strip()
res['head'] = head; res['head_ok'] = bool(head)
pk = JD / 'review_packet.json'
if pk.exists():
    ph = json.load(open(pk)).get('head_sha'); res['review_packet_head'] = ph; res['head_ok'] = res['head_ok'] and ph == head
# scope: since last accepted head
base = None
for f in sorted(JD.glob('verdict-*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
    try:
        v = json.load(open(f))
        if v.get('status') == 'ACCEPTED_SCOPE': base = v['head_sha']; break
    except Exception: pass
if base is None:
    # branches are stacked on earlier work, so the PR base (main) would list other units' files. Use the parent of the first commit that touched this job's mailbox (the placeholder push that started the job).
    first = (gh('repos/%s/commits?sha=%s&path=%s&per_page=100' % (repo, branch, mailbox), '--jq', '.[-1].parents[0].sha') or '').strip()
    res['scope_base_source'] = 'parent of first mailbox commit' if first else 'pr base (fallback)'
    base = first or (gh('repos/%s/pulls/%s' % (repo, j['pr_number']), '--jq', '.base.sha') or '').strip()
res['scope_base'] = base
files = (gh('repos/%s/compare/%s...%s' % (repo, base, head), '--jq', '.files[].filename') or '').split()
outside = [f for f in files if f not in allow]
res['changed_files'] = files; res['outside_allowlist'] = outside; res['scope_ok'] = bool(files) and not outside
runs = json.loads(gh('repos/%s/actions/runs?head_sha=%s' % (repo, head)) or '{"workflow_runs":[]}')['workflow_runs']
res['ci_runs'] = ['%s:%s:%s' % (r['event'], r['status'], r.get('conclusion') or '-') for r in runs]
res['ci_ok'] = bool(runs) and all(r['status'] == 'completed' and r.get('conclusion') == 'success' for r in runs)
try:
    mb = json.loads(gh('repos/%s/contents/%s?ref=%s' % (repo, mailbox, head), raw=True))
    tests = mb.get('tests'); res['mailbox'] = dict(status=mb.get('status'), instruction_id=mb.get('instruction_id'), claims_tests_not_run=any('not run' in str(t.get('result', '')).lower() for t in (tests if isinstance(tests, list) else [])), blockers=mb.get('blockers'))
except Exception as e:
    res['mailbox'] = dict(error=str(e)[:100])
if a.skip_tests:
    res['tests'] = dict(skipped=True); res['tests_ok'] = None
else:
    d = tempfile.mkdtemp(prefix='accept-', dir=str(Path(os.environ.get('HERMES_HOME', Path.home() / '.hermes')) / 'cache' / 'scratch'))
    c = subprocess.run(['gh', 'repo', 'clone', repo, d + '/clone', '--', '--depth', '1', '-b', branch, '-q'], capture_output=True, text=True, timeout=240)
    if c.returncode: res['tests'] = dict(error='clone failed: ' + c.stderr[-150:]); res['tests_ok'] = False
    else:
        cloned = subprocess.run(['git', '-C', d + '/clone', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        res['clone_head_matches'] = cloned == head
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, '-m', 'unittest'] + a.tests.split(), cwd=d + '/clone/lab', capture_output=True, text=True, timeout=840)
            txt = r.stderr + r.stdout; m = re.search(r'Ran (\d+) tests? in ([\d.]+)s', txt)
            res['tests'] = dict(ran=int(m.group(1)) if m else None, seconds=round(time.time() - t0), ok=bool(re.search(r'^OK', txt, re.M)), failed=bool(re.search(r'^FAILED', txt, re.M)),
                                skipped=len(re.findall(r'\.\.\. skipped', txt)), tail=txt.strip().splitlines()[-4:])
            res['tests_ok'] = res['tests']['ok'] and not res['tests']['failed'] and bool(res['clone_head_matches'])
        except subprocess.TimeoutExpired:
            res['tests'] = dict(error='timeout after 14 minutes'); res['tests_ok'] = False
res['ci_pending'] = any(r['status'] != 'completed' for r in runs)
res['verdict'] = 'PENDING_CI' if res['ci_pending'] else 'PASS' if (res['head_ok'] and res['scope_ok'] and res['ci_ok'] and res.get('tests_ok') in (True,)) else ('PASS_WITHOUT_TESTS' if (a.skip_tests and res['head_ok'] and res['scope_ok'] and res['ci_ok']) else 'FAIL')
out = json.dumps(res, ensure_ascii=False, indent=1)
if a.out: Path(a.out).write_text(out)
print(out)
