#!/usr/bin/env python3
"""Deterministic acceptance of one engineering unit. The model reads the JSON verdict; it does not judge these facts itself.

usage: accept_unit.py JOB_DIR [--tests "unittest args relative to lab"] [--skip-tests] [--out FILE]
Reads job.json (repository, branch, mailbox, write_allowlist). Checks, all by machine:
  head        branch head SHA (gh api) and that it still matches review_packet.json when one exists
  scope       files changed since the last accepted head (from the newest verdict-*.json with status ACCEPTED_SCOPE, else base of the PR) are all inside write_allowlist
  ci          exact-head workflow runs: every run completed with conclusion success, at least one run exists
  mailbox     mailbox on the branch: instruction_id, status, and whether it claims tests 'not run' (reported, not trusted)
  tests       clean `gh repo clone --depth 1` of the branch and the CI command run under progress-aware runner
              with 14 minute soft observation and 40 minute hard budget; result parsed with full lifecycle verification.
Prints one JSON object and writes it to --out. verdict is PASS only when head, scope, ci and tests all pass.
Takes minutes: start it in the background and poll the --out file (a foreground tool call is cut off after about 7 minutes).
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

try:
    from engineering.acceptance.progress_runner import compute_config_key
except ImportError:
    import importlib.util
    _pr_file = Path(__file__).resolve().with_name('progress_runner.py')
    _spec = importlib.util.spec_from_file_location('progress_runner', str(_pr_file))
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    compute_config_key = _mod.compute_config_key

ap = argparse.ArgumentParser(description="Deterministic acceptance of one engineering unit.")
ap.add_argument('job', help="Path to job directory")
ap.add_argument('--tests', default='discover -s tests', help="Unittest args relative to lab")
ap.add_argument('--skip-tests', action='store_true', help="Skip running tests")
ap.add_argument('--out', help="Path to output JSON verdict file")
ap.add_argument('--soft-seconds', type=float, default=840.0, help="Soft checkpoint budget in seconds (default: 840)")
ap.add_argument('--checkpoint-seconds', type=float, default=300.0, help="Checkpoint interval in seconds (default: 300)")
ap.add_argument('--hard-seconds', type=float, default=2400.0, help="Absolute hard deadline in seconds (default: 2400)")
ap.add_argument('--item-seconds', type=float, default=None, help="Explicit item budget in seconds")
ap.add_argument('--stall-seconds', type=float, default=None, help="Inactivity budget in seconds")
ap.add_argument('--state-dir', default=None, help="Explicit state directory for runner")

a = ap.parse_args()
JD = Path(a.job).resolve()
with open(JD / 'job.json') as _jf:
    j = json.load(_jf)
repo, branch, mailbox, allow = j['repository'], j['branch'], j['mailbox'], set(j['write_allowlist'])
res = dict(job=str(JD), repository=repo, branch=branch, at=time.strftime('%F %T UTC', time.gmtime()))


def gh(*args, raw=False):
    cmd = ['gh', 'api'] + (['-H', 'Accept: application/vnd.github.raw'] if raw else []) + list(args)
    for _ in range(3):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if p.returncode == 0:
            return p.stdout
        time.sleep(4)
    return None


head = (gh('repos/%s/git/ref/heads/%s' % (repo, branch), '--jq', '.object.sha') or '').strip()
res['head'] = head
res['head_ok'] = bool(head)
pk = JD / 'review_packet.json'
if pk.exists():
    try:
        with open(pk) as _pkf:
            ph = json.load(_pkf).get('head_sha')
        res['review_packet_head'] = ph
        res['head_ok'] = res['head_ok'] and (ph == head)
    except Exception:
        res['head_ok'] = False

# Scope: since last accepted head
base = None
for f in sorted(JD.glob('verdict-*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
    try:
        with open(f) as _vf:
            v = json.load(_vf)
        if v.get('status') == 'ACCEPTED_SCOPE':
            base = v['head_sha']
            break
    except Exception:
        pass

if base is None:
    # branches are stacked on earlier work, so the PR base (main) would list other units' files.
    # Use the parent of the first commit that touched this job's mailbox.
    first = (gh('repos/%s/commits?sha=%s&path=%s&per_page=100' % (repo, branch, mailbox), '--jq', '.[-1].parents[0].sha') or '').strip()
    res['scope_base_source'] = 'parent of first mailbox commit' if first else 'pr base (fallback)'
    base = first or (gh('repos/%s/pulls/%s' % (repo, j['pr_number']), '--jq', '.base.sha') or '').strip()

res['scope_base'] = base
files = (gh('repos/%s/compare/%s...%s' % (repo, base, head), '--jq', '.files[].filename') or '').split()
outside = [f for f in files if f not in allow]
res['changed_files'] = files
res['outside_allowlist'] = outside
res['scope_ok'] = bool(files) and not outside

# CI runs
runs = json.loads(gh('repos/%s/actions/runs?head_sha=%s' % (repo, head)) or '{"workflow_runs":[]}')['workflow_runs']
res['ci_runs'] = ['%s:%s:%s' % (r['event'], r['status'], r.get('conclusion') or '-') for r in runs]
res['ci_ok'] = bool(runs) and all(r['status'] == 'completed' and r.get('conclusion') == 'success' for r in runs)
res['ci_pending'] = any(r['status'] != 'completed' for r in runs)

# Mailbox
try:
    mb = json.loads(gh('repos/%s/contents/%s?ref=%s' % (repo, mailbox, head), raw=True))
    tests_val = mb.get('tests')
    res['mailbox'] = dict(
        status=mb.get('status'),
        instruction_id=mb.get('instruction_id'),
        claims_tests_not_run=any('not run' in str(t.get('result', '')).lower() for t in (tests_val if isinstance(tests_val, list) else [])),
        blockers=mb.get('blockers')
    )
except Exception as e:
    res['mailbox'] = dict(error=str(e)[:100])

runner_status = None

if a.skip_tests:
    res['tests'] = dict(skipped=True)
    res['tests_ok'] = None
    runner_status = 'SKIPPED'
else:
    hermes_home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))).resolve()
    scratch_base = hermes_home / 'cache' / 'scratch'
    scratch_ok = True
    try:
        scratch_base.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        scratch_ok = False
        res['tests'] = dict(
            ran=0,
            seconds=0,
            ok=False,
            failed=True,
            skipped=0,
            tail=[],
            error=f'Scratch path inaccessible: {scratch_base}: {e}'
        )
        res['tests_ok'] = False
        res['clone_head_matches'] = False
        runner_status = 'FAIL'

    if scratch_ok:
        repo_head = f"{repo}@{head}"
        clone_key = hashlib.sha256(repo_head.encode('utf-8')).hexdigest()[:16]
        clone_root = scratch_base / ('accept-' + clone_key)
        clone_dir = clone_root / 'clone'
        clone_lock_path = clone_root / 'clone.lock'
        clone_root.mkdir(parents=True, exist_ok=True)

        lock_fd = open(clone_lock_path, 'a+')
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
            clone_needed = True
            if (clone_dir / '.git').exists():
                try:
                    cp = subprocess.run(['git', '-C', str(clone_dir), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=10)
                    if cp.returncode == 0 and cp.stdout.strip() == head:
                        clone_needed = False
                        res['clone_head_matches'] = True
                    else:
                        cloned_head = cp.stdout.strip() if cp.returncode == 0 else 'unreadable'
                        res['tests'] = dict(
                            error=f'existing cached clone head mismatch: expected {head}, got {cloned_head}',
                            ran=0, seconds=0, ok=False, failed=True, skipped=0, tail=[]
                        )
                        res['tests_ok'] = False
                        res['clone_head_matches'] = False
                        runner_status = 'FAIL'
                        clone_needed = False
                except Exception as e:
                    res['tests'] = dict(
                        error=f'existing cached clone check error: {e}',
                        ran=0, seconds=0, ok=False, failed=True, skipped=0, tail=[]
                    )
                    res['tests_ok'] = False
                    res['clone_head_matches'] = False
                    runner_status = 'FAIL'
                    clone_needed = False

            if clone_needed:
                if clone_dir.exists():
                    res['tests'] = dict(
                        error='clone directory exists and is not a valid git repository',
                        ran=0, seconds=0, ok=False, failed=True, skipped=0, tail=[]
                    )
                    res['tests_ok'] = False
                    res['clone_head_matches'] = False
                    runner_status = 'FAIL'
                else:
                    clone_dir.parent.mkdir(parents=True, exist_ok=True)
                    c = subprocess.run(['gh', 'repo', 'clone', repo, str(clone_dir), '--', '--depth', '1', '-b', branch, '-q'], capture_output=True, text=True, timeout=240)
                    if c.returncode != 0:
                        res['tests'] = dict(error='clone failed: ' + c.stderr[-150:], ran=0, seconds=0, ok=False, failed=True, skipped=0, tail=[])
                        res['tests_ok'] = False
                        res['clone_head_matches'] = False
                        runner_status = 'FAIL'
                    else:
                        cloned = subprocess.run(['git', '-C', str(clone_dir), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
                        if cloned == head:
                            res['clone_head_matches'] = True
                        else:
                            res['tests'] = dict(error='clone head mismatch: expected %s, got %s' % (head, cloned), ran=0, seconds=0, ok=False, failed=True, skipped=0, tail=[])
                            res['tests_ok'] = False
                            res['clone_head_matches'] = False
                            runner_status = 'FAIL'
        finally:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                lock_fd.close()
            except Exception:
                pass

    if res.get('clone_head_matches'):
        test_cwd = clone_dir / 'lab' if (clone_dir / 'lab').is_dir() else clone_dir

        if a.state_dir:
            state_dir = Path(a.state_dir).resolve()
        else:
            state_dir = (JD / '.progress_runner').resolve()
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            state_dir = scratch_base / ('runner-' + hashlib.sha256(str(JD).encode()).hexdigest()[:16])
            state_dir.mkdir(parents=True, exist_ok=True)

        runner_script = Path(__file__).resolve().with_name('progress_runner.py')
        runner_out = state_dir / 'runner_out.json'
        # Guard stale runner_out from earlier config on runner conflict/crash
        if runner_out.exists():
            try:
                runner_out.unlink()
            except Exception:
                pass

        expected_config = {
            "checkpoint_seconds": float(a.checkpoint_seconds),
            "cwd": str(test_cwd.resolve()),
            "hard_seconds": float(a.hard_seconds),
            "head": str(head).strip(),
            "item_seconds": (
                float(a.item_seconds) if a.item_seconds is not None else None
            ),
            "soft_seconds": float(a.soft_seconds),
            "stall_seconds": (
                float(a.stall_seconds)
                if a.stall_seconds is not None
                else None
            ),
            "suite": str(a.tests).strip(),
        }
        expected_config_key = compute_config_key(expected_config)

        runner_cmd = [
            sys.executable,
            str(runner_script),
            '--cwd', str(test_cwd),
            '--head', str(head),
            '--suite', str(a.tests),
            '--state-dir', str(state_dir),
            '--soft-seconds', str(a.soft_seconds),
            '--checkpoint-seconds', str(a.checkpoint_seconds),
            '--hard-seconds', str(a.hard_seconds),
            '--out', str(runner_out)
        ]
        if a.item_seconds is not None:
            runner_cmd.extend(['--item-seconds', str(a.item_seconds)])
        if a.stall_seconds is not None:
            runner_cmd.extend(['--stall-seconds', str(a.stall_seconds)])

        t0 = time.time()
        r = subprocess.run(runner_cmd, capture_output=True, text=True)

        runner_data = {}
        if runner_out.exists():
            try:
                runner_data = json.loads(runner_out.read_text())
            except Exception:
                runner_data = {}
        if (not isinstance(runner_data, dict) or not runner_data) and r.stdout.strip():
            try:
                runner_data = json.loads(r.stdout)
            except Exception:
                runner_data = {}
        if not isinstance(runner_data, dict):
            runner_data = {}

        runner_status = None
        runner_error = None

        raw_status = runner_data.get('status') if isinstance(runner_data, dict) else None
        if r.returncode == 2 or raw_status == 'TIMEOUT':
            runner_status = 'TIMEOUT'
        elif r.returncode == 3 or raw_status in ('STALL', 'STALL_SUSPECTED'):
            runner_status = 'STALL'
        elif r.returncode == 4 or raw_status == 'CANCELLED':
            runner_status = 'CANCELLED'
        elif r.returncode != 0:
            runner_status = 'FAIL'
            runner_error = f'runner exited with code {r.returncode}'
        else:
            if not isinstance(runner_data, dict) or not runner_data:
                runner_status = 'FAIL'
                runner_error = 'missing or malformed runner artifacts'
            else:
                rep_config_key = runner_data.get('config_key')
                if not isinstance(rep_config_key, str) or rep_config_key != expected_config_key:
                    runner_status = 'FAIL'
                    runner_error = f'runner report config_key mismatch: expected {expected_config_key}, got {rep_config_key}'
                elif runner_data.get('head') != head:
                    runner_status = 'FAIL'
                    runner_error = f"runner report head mismatch: expected {head}, got {runner_data.get('head')}"
                elif runner_data.get('cwd') != str(test_cwd.resolve()):
                    runner_status = 'FAIL'
                    runner_error = f"runner report cwd mismatch: expected {test_cwd.resolve()}, got {runner_data.get('cwd')}"
                elif runner_data.get('suite') != str(a.tests).strip():
                    runner_status = 'FAIL'
                    runner_error = f"runner report suite mismatch: expected {a.tests}, got {runner_data.get('suite')}"
                else:
                    discovered_ids = runner_data.get('discovered_ids')
                    completed_ids = runner_data.get('completed_ids')
                    discovered_count = runner_data.get('discovered_count')
                    completed_count = runner_data.get('completed_count')

                    if not isinstance(discovered_ids, list) or not isinstance(completed_ids, list):
                        runner_status = 'FAIL'
                        runner_error = 'discovered_ids and completed_ids must be lists'
                    elif len(discovered_ids) == 0 or len(completed_ids) == 0:
                        runner_status = 'FAIL'
                        runner_error = 'discovered_ids or completed_ids is empty'
                    elif not all(isinstance(x, str) and len(x) > 0 for x in discovered_ids):
                        runner_status = 'FAIL'
                        runner_error = 'discovered_ids must contain non-empty strings'
                    elif not all(isinstance(x, str) and len(x) > 0 for x in completed_ids):
                        runner_status = 'FAIL'
                        runner_error = 'completed_ids must contain non-empty strings'
                    elif len(discovered_ids) != len(set(discovered_ids)):
                        runner_status = 'FAIL'
                        runner_error = 'discovered_ids contains duplicate IDs'
                    elif len(completed_ids) != len(set(completed_ids)):
                        runner_status = 'FAIL'
                        runner_error = 'completed_ids contains duplicate IDs'
                    elif not isinstance(discovered_count, int) or isinstance(discovered_count, bool) or not isinstance(completed_count, int) or isinstance(completed_count, bool):
                        runner_status = 'FAIL'
                        runner_error = 'discovered_count and completed_count must be integers'
                    elif discovered_count != len(discovered_ids):
                        runner_status = 'FAIL'
                        runner_error = f'discovered_count ({discovered_count}) != len(discovered_ids) ({len(discovered_ids)})'
                    elif completed_count != len(completed_ids):
                        runner_status = 'FAIL'
                        runner_error = f'completed_count ({completed_count}) != len(completed_ids) ({len(completed_ids)})'
                    elif discovered_count != completed_count:
                        runner_status = 'FAIL'
                        runner_error = f'reconciliation failed: {discovered_count} discovered != {completed_count} completed'
                    elif set(discovered_ids) != set(completed_ids):
                        runner_status = 'FAIL'
                        runner_error = 'discovered_ids and completed_ids sets do not match'
                    elif runner_data.get('status') != 'PASS' or runner_data.get('verdict') != 'PASS' or runner_data.get('exit_code') != 0 or runner_data.get('tests_ok') is not True:
                        runner_status = 'FAIL'
                        runner_error = 'runner report status/verdict/exit_code is not PASS/0'
                    else:
                        t_info = runner_data.get('tests')
                        if not isinstance(t_info, dict):
                            runner_status = 'FAIL'
                            runner_error = 'tests metric field must be a dictionary'
                        elif not t_info.get('ok') or t_info.get('failed', 0) != 0:
                            runner_status = 'FAIL'
                            runner_error = 'tests dictionary indicates test failure or not ok'
                        else:
                            t_ran = t_info.get('ran')
                            t_passed = t_info.get('passed')
                            t_failed = t_info.get('failed')
                            t_skipped = t_info.get('skipped')

                            if not isinstance(t_ran, int) or isinstance(t_ran, bool) or t_ran != completed_count:
                                runner_status = 'FAIL'
                                runner_error = f'tests.ran ({t_ran}) != completed_count ({completed_count})'
                            elif not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (t_passed, t_failed, t_skipped)):
                                runner_status = 'FAIL'
                                runner_error = 'tests passed, failed, skipped must be non-negative integers'
                            elif t_passed + t_failed + t_skipped != completed_count:
                                runner_status = 'FAIL'
                                runner_error = f'tests passed ({t_passed}) + failed ({t_failed}) + skipped ({t_skipped}) != completed_count ({completed_count})'
                            else:
                                suite_ev = runner_data.get('suite_completed_event')
                                if not isinstance(suite_ev, dict):
                                    runner_status = 'FAIL'
                                    runner_error = 'missing suite_completed_event proof'
                                elif suite_ev.get('event') != 'suite_completed':
                                    runner_status = 'FAIL'
                                    runner_error = 'suite_completed_event has invalid event'
                                elif suite_ev.get('was_successful') is not True:
                                    runner_status = 'FAIL'
                                    runner_error = 'suite_completed_event was_successful is not True'
                                elif suite_ev.get('ran') != completed_count:
                                    runner_status = 'FAIL'
                                    runner_error = f"suite_completed_event ran ({suite_ev.get('ran')}) != completed_count ({completed_count})"
                                else:
                                    runner_status = 'PASS'

        t_info = runner_data.get('tests', {}) if isinstance(runner_data.get('tests'), dict) else {}
        total_sec = runner_data.get('total_seconds', round(time.time() - t0))
        res['tests'] = dict(
            ran=t_info.get('ran', 0),
            seconds=int(total_sec),
            ok=(runner_status == 'PASS'),
            failed=(runner_status == 'FAIL' or bool(t_info.get('failed'))),
            skipped=t_info.get('skipped', 0),
            tail=t_info.get('tail', r.stderr.strip().splitlines()[-4:] or r.stdout.strip().splitlines()[-4:] or [])
        )
        if runner_status in ('TIMEOUT', 'STALL', 'CANCELLED'):
            diag_reason = None
            if isinstance(runner_data.get('diagnostics'), dict):
                diag_reason = runner_data['diagnostics'].get('reason')
            res['tests']['error'] = diag_reason or runner_status.lower()
        elif runner_status == 'FAIL' and runner_error:
            res['tests']['error'] = runner_error

        res['tests_ok'] = (runner_status == 'PASS')
        if 'diagnostics' in runner_data:
            res['diagnostics'] = runner_data['diagnostics']
        if 'checkpoints' in runner_data:
            res['checkpoints'] = runner_data['checkpoints']

# Verdict determination:
# Add TIMEOUT/STALL/CANCELLED explicit incomplete verdicts, NEVER generic FAIL for timeout.
# Head / scope failure fails closed.
if not res['head_ok'] or not res['scope_ok']:
    res['verdict'] = 'FAIL'
elif runner_status == 'TIMEOUT':
    res['verdict'] = 'TIMEOUT'
elif runner_status in ('STALL', 'STALL_SUSPECTED'):
    res['verdict'] = 'STALL'
elif runner_status == 'CANCELLED':
    res['verdict'] = 'CANCELLED'
elif a.skip_tests:
    if not res['ci_ok'] and not res['ci_pending']:
        res['verdict'] = 'FAIL'
    elif res['ci_pending']:
        res['verdict'] = 'PENDING_CI'
    else:
        res['verdict'] = 'PASS_WITHOUT_TESTS'
elif not res['tests_ok']:
    res['verdict'] = 'FAIL'
elif not res['ci_ok'] and not res['ci_pending']:
    res['verdict'] = 'FAIL'
elif res['ci_pending']:
    res['verdict'] = 'PENDING_CI'
else:
    res['verdict'] = 'PASS'

out = json.dumps(res, ensure_ascii=False, indent=1)
if a.out:
    Path(a.out).write_text(out + '\n')
print(out)
