#!/usr/bin/env python3
"""Tests for installed_auto_accept.py.

Loaded via importlib.util – does NOT import the package engineering.workflow_pipeline.
"""
import ast
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Load module under test by file path
# ---------------------------------------------------------------------------
_MOD_PATH = (Path(__file__).resolve().parents[1] / 'installed_auto_accept.py')


def _load_module(home=None, accept=None, fake_commit=None):
    """Load a fresh copy of the module with patched HOME / ACCEPT / c."""
    spec = importlib.util.spec_from_file_location('installed_auto_accept', str(_MOD_PATH))
    mod = importlib.util.module_from_spec(spec)
    # Patch sys.path so that the module's own sys.path.insert doesn't break anything
    spec.loader.exec_module(mod)
    if home is not None:
        mod.HOME = home
    if accept is not None:
        mod.ACCEPT = accept
    if fake_commit is not None:
        mod.c = fake_commit
    return mod


def _load_fresh(home, accept, fake_c):
    return _load_module(home=home, accept=accept, fake_commit=fake_c)


# ---------------------------------------------------------------------------
# Fake commit module factory
# ---------------------------------------------------------------------------

DISPATCHED = 'DISPATCHED'


def make_fake_commit(items=None, handle_statuses=None):
    """Return a fake 'commit' stand-in namespace.

    handle_statuses: optional dict mapping handle value -> (status, info) tuple.
    If provided, overrides default logic for those handles.
    Default: 'finished_handle' -> ('finished', {}), anything else -> ('pending', {}).
    """
    fc = types.SimpleNamespace()
    fc.DISPATCHED = DISPATCHED

    class FakeLedger:
        def __init__(self):
            self.data = {'items': list(items or [])}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    fc.Ledger = FakeLedger

    _extra = dict(handle_statuses) if handle_statuses else {}

    def handle_status(handle):
        if handle in _extra:
            return _extra[handle]
        if handle == 'finished_handle':
            return ('finished', {})
        return ('pending', {})

    fc.handle_status = handle_status
    return fc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_log(log_path, content):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(content)


def _make_fake_accept(path):
    """Write a tiny accept_unit.py that writes a PASS result json."""
    path.write_text(
        '#!/usr/bin/env python3\n'
        'import sys, json, pathlib\n'
        'job = pathlib.Path(sys.argv[1])\n'
        'out_idx = sys.argv.index("--out")\n'
        'out = pathlib.Path(sys.argv[out_idx + 1])\n'
        'out.write_text(json.dumps({"verdict": "PASS"}))\n'
    )
    path.chmod(0o755)


# ---------------------------------------------------------------------------
# Test: resolve_home
# ---------------------------------------------------------------------------

class TestResolveHome(unittest.TestCase):

    def test_script_location_wins(self):
        """If script is in <profile>/scripts/, and commitments/ exists, return profile dir."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            scripts = td / 'scripts'
            scripts.mkdir()
            (td / 'commitments').mkdir()
            fake_script = scripts / 'auto_accept.py'
            fake_script.write_text('')
            mod = _load_module()
            result = mod.resolve_home(script_path=str(fake_script), environ={})
            self.assertEqual(result, td.resolve())

    def test_env_fallback(self):
        """HERMES_HOME with commitments/ is used when script location does not qualify."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / 'commitments').mkdir()
            mod = _load_module()
            # Script in a non-scripts dir
            result = mod.resolve_home(script_path=str(td / 'other' / 'auto_accept.py'),
                                      environ={'HERMES_HOME': str(td)})
            self.assertEqual(result, td.resolve())

    def test_none_when_neither(self):
        """Returns None when no qualifying location and HERMES_HOME not set."""
        with tempfile.TemporaryDirectory() as td:
            mod = _load_module()
            result = mod.resolve_home(script_path=str(Path(td) / 'auto_accept.py'),
                                      environ={})
            self.assertIsNone(result)

    def test_no_hermes_home_fallback(self):
        """No fallback to ~/.hermes – None if env missing and script location wrong."""
        mod = _load_module()
        result = mod.resolve_home(script_path='/tmp/auto_accept.py', environ={})
        # Must be None (no ~/.hermes fallback)
        self.assertIsNone(result)

    def test_script_location_wins_over_env(self):
        """Script install location takes priority over HERMES_HOME env var."""
        with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
            td1, td2 = Path(td1), Path(td2)
            scripts = td1 / 'scripts'
            scripts.mkdir()
            (td1 / 'commitments').mkdir()
            (td2 / 'commitments').mkdir()
            fake_script = scripts / 'auto_accept.py'
            fake_script.write_text('')
            mod = _load_module()
            result = mod.resolve_home(script_path=str(fake_script),
                                      environ={'HERMES_HOME': str(td2)})
            self.assertEqual(result, td1.resolve())

    def test_env_without_commitments_rejected(self):
        """HERMES_HOME directory that lacks commitments/ is not used."""
        with tempfile.TemporaryDirectory() as td:
            mod = _load_module()
            result = mod.resolve_home(script_path=None,
                                      environ={'HERMES_HOME': td})
            self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Test: parse_marker
# ---------------------------------------------------------------------------

class TestParseMarker(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.mod = _load_module()

    def tearDown(self):
        self._td.cleanup()

    def _marker(self, text):
        p = self.td / 'auto_accept-x.running'
        p.write_text(text)
        return p

    def test_valid_full(self):
        p = self._marker('12345 999 1700000000 2024-01-01 00:00:00')
        pid, ident, epoch, valid = self.mod.parse_marker(p)
        self.assertTrue(valid)
        self.assertEqual(pid, 12345)
        self.assertEqual(ident, (12345, 999))
        self.assertAlmostEqual(epoch, 1700000000.0, delta=1)

    def test_quarantine_blocks(self):
        p = self._marker('12345 quarantine 1700000000 2024-01-01 00:00:00')
        pid, ident, epoch, valid = self.mod.parse_marker(p)
        self.assertFalse(valid)

    def test_malformed_blocks(self):
        p = self._marker('notanumber foo')
        pid, ident, epoch, valid = self.mod.parse_marker(p)
        self.assertFalse(valid)

    def test_empty_invalid(self):
        p = self._marker('')
        pid, ident, epoch, valid = self.mod.parse_marker(p)
        self.assertFalse(valid)

    def test_pid_one_invalid(self):
        p = self._marker('1 999 1700000000 x')
        pid, ident, epoch, valid = self.mod.parse_marker(p)
        self.assertFalse(valid)

    def test_legacy_two_fields(self):
        p = self._marker('9999 8888')
        pid, ident, epoch, valid = self.mod.parse_marker(p)
        self.assertTrue(valid)
        self.assertEqual(ident, (9999, 8888))
        # epoch comes from file mtime
        self.assertIsNotNone(epoch)


# ---------------------------------------------------------------------------
# Test: check_marker_liveness
# ---------------------------------------------------------------------------

class TestCheckMarkerLiveness(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        self.mod = _load_module()

    def tearDown(self):
        self._td.cleanup()

    def _marker(self, text):
        p = self.td / 'auto_accept-x.running'
        p.write_text(text)
        return p

    def test_missing_file_returns_false(self):
        p = self.td / 'nonexistent.running'
        self.assertFalse(self.mod.check_marker_liveness(p))

    def test_dead_pid_removes_marker(self):
        # Use a PID that definitely doesn't exist (e.g. from /proc max + something)
        dead_pid = 99999999
        p = self._marker(f'{dead_pid} 12345 1700000000 x')
        result = self.mod.check_marker_liveness(p)
        self.assertFalse(result)
        self.assertFalse(p.exists())

    def test_recycled_pid_removes_marker(self):
        """Live PID with different identity -> recycled -> removed."""
        # Use our own PID but give a wrong starttime
        own_pid = os.getpid()
        own_ident = self.mod.get_proc_identity(own_pid)
        if own_ident is None:
            self.skipTest("Cannot read own identity")
        wrong_starttime = own_ident[1] + 999999
        p = self._marker(f'{own_pid} {wrong_starttime} 1700000000 x')
        result = self.mod.check_marker_liveness(p)
        self.assertFalse(result)
        self.assertFalse(p.exists())

    def test_live_matching_identity_blocks(self):
        """Live PID with correct identity -> returns True."""
        own_pid = os.getpid()
        own_ident = self.mod.get_proc_identity(own_pid)
        if own_ident is None:
            self.skipTest("Cannot read own identity")
        p = self._marker(f'{own_pid} {own_ident[1]} 1700000000 x')
        result = self.mod.check_marker_liveness(p)
        self.assertTrue(result)
        self.assertTrue(p.exists())

    def test_unreadable_identity_blocks(self):
        """Live PID but get_proc_identity returns None -> fail closed, blocks."""
        own_pid = os.getpid()
        own_ident = self.mod.get_proc_identity(own_pid)
        if own_ident is None:
            self.skipTest("Cannot read own identity")
        p = self._marker(f'{own_pid} {own_ident[1]} 1700000000 x')
        with patch.object(self.mod, 'get_proc_identity', return_value=None):
            result = self.mod.check_marker_liveness(p)
        self.assertTrue(result)
        self.assertTrue(p.exists())

    def test_eperm_blocks_and_keeps_marker(self):
        """EPERM from os.kill -> preserves marker and blocks."""
        own_pid = os.getpid()
        own_ident = self.mod.get_proc_identity(own_pid)
        if own_ident is None:
            self.skipTest("Cannot read own identity")
        p = self._marker(f'{own_pid} {own_ident[1]} 1700000000 x')
        import errno as _errno
        eperm = OSError(_errno.EPERM, "Operation not permitted")
        eperm.errno = _errno.EPERM
        with patch('os.kill', side_effect=eperm):
            result = self.mod.check_marker_liveness(p)
        self.assertTrue(result)
        self.assertTrue(p.exists())

    def test_legacy_live_marker_blocks(self):
        """Legacy marker (only pid, no starttime) with live PID -> preserves and blocks."""
        own_pid = os.getpid()
        # Legacy: only one digit field after pid
        p = self._marker(f'{own_pid} 9999999')
        # own_pid is alive; but ident from marker would be (own_pid, 9999999)
        # which has expected_ident set, so we need truly legacy (no second digit or just one field)
        # Actually parse_marker needs exactly 1 numeric field after pid for legacy:
        # len(parts) < 3 and parts[1].isdigit() -> expected_ident = (pid, int(parts[1]))
        # That IS expected_ident set, so it will check identity.
        # For a true legacy marker, let's write just "pid\n" (only 1 part)
        p.write_text(f'{own_pid}\n')
        result = self.mod.check_marker_liveness(p)
        # expected_ident is None (only 1 part), PID is live -> legacy block
        self.assertTrue(result)
        self.assertTrue(p.exists())

    def test_quarantine_marker_blocks_and_kept(self):
        p = self._marker('12345 quarantine 1700000000 x')
        result = self.mod.check_marker_liveness(p)
        self.assertTrue(result)
        self.assertTrue(p.exists())

    def test_malformed_marker_blocks_and_kept(self):
        p = self._marker('garbage!!!!')
        result = self.mod.check_marker_liveness(p)
        self.assertTrue(result)
        self.assertTrue(p.exists())


# ---------------------------------------------------------------------------
# Test: job_dir_from_log
# ---------------------------------------------------------------------------

class TestJobDirFromLog(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        (self.td / 'commitments' / 'runs').mkdir(parents=True)
        (self.td / 'lab' / 'journal').mkdir(parents=True)
        (self.td / 'plugins').mkdir()
        self.mod = _load_module(home=self.td)

    def tearDown(self):
        self._td.cleanup()

    def _log(self, item_id, content):
        p = self.td / 'commitments' / 'runs' / f'{item_id}.log'
        p.write_text(content)
        return p

    def test_single_line(self):
        job = self.td / 'jobs' / 'abc'
        job.mkdir(parents=True)
        self._log('item1', f'REVIEW_REQUIRED {{"packet": "{job}/review_packet.json"}}\n')
        jd, log = self.mod.job_dir_from_log('item1')
        self.assertEqual(jd, job)

    def test_pretty_printed(self):
        job = self.td / 'jobs' / 'abc'
        job.mkdir(parents=True)
        content = (
            'some stuff\n'
            'REVIEW_REQUIRED\n'
            '{\n'
            f'  "packet": "{job}/review_packet.json",\n'
            '  "other": "val"\n'
            '}\n'
        )
        self._log('item2', content)
        jd, log = self.mod.job_dir_from_log('item2')
        self.assertEqual(jd, job)

    def test_last_occurrence_wins(self):
        job1 = self.td / 'jobs' / 'first'
        job2 = self.td / 'jobs' / 'second'
        job1.mkdir(parents=True)
        job2.mkdir(parents=True)
        content = (
            f'REVIEW_REQUIRED {{"packet": "{job1}/review_packet.json"}}\n'
            'more stuff\n'
            f'REVIEW_REQUIRED {{"packet": "{job2}/review_packet.json"}}\n'
        )
        self._log('item3', content)
        jd, log = self.mod.job_dir_from_log('item3')
        self.assertEqual(jd, job2)

    def test_no_packet(self):
        self._log('item4', 'No review required here\n')
        jd, log = self.mod.job_dir_from_log('item4')
        self.assertIsNone(jd)

    def test_outside_home_rejected(self):
        """Packet path outside HOME is rejected."""
        with tempfile.TemporaryDirectory() as other:
            other = Path(other)
            self._log('item5', f'REVIEW_REQUIRED {{"packet": "{other}/review_packet.json"}}\n')
            jd, log = self.mod.job_dir_from_log('item5')
            self.assertIsNone(jd)
            self.assertIsNotNone(log)

    def test_symlink_escape_rejected(self):
        """Symlink pointing outside HOME is rejected."""
        with tempfile.TemporaryDirectory() as outside:
            outside = Path(outside)
            link_dir = self.td / 'jobs' / 'escape_link'
            link_dir.parent.mkdir(parents=True, exist_ok=True)
            link_dir.symlink_to(outside)
            self._log('item6', f'REVIEW_REQUIRED {{"packet": "{link_dir}/review_packet.json"}}\n')
            jd, log = self.mod.job_dir_from_log('item6')
            self.assertIsNone(jd)

    def test_missing_log(self):
        jd, log = self.mod.job_dir_from_log('nonexistent')
        self.assertIsNone(jd)
        self.assertIsNone(log)

    def test_home_none_returns_none_none(self):
        mod = _load_module(home=None)
        mod.HOME = None
        jd, log = mod.job_dir_from_log('anything')
        self.assertIsNone(jd)
        self.assertIsNone(log)


# ---------------------------------------------------------------------------
# Test: main()
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.td = Path(self._td.name)
        # Profile structure
        (self.td / 'commitments' / 'runs').mkdir(parents=True)
        (self.td / 'lab' / 'journal').mkdir(parents=True)
        (self.td / 'plugins').mkdir()
        # Fake accept_unit.py
        self.accept = self.td / 'skills' / 'relay-backlog-operator' / 'scripts' / 'accept_unit.py'
        self.accept.parent.mkdir(parents=True)
        _make_fake_accept(self.accept)

    def tearDown(self):
        self._td.cleanup()

    def _job(self, item_id):
        job = self.td / 'jobs' / item_id
        job.mkdir(parents=True, exist_ok=True)
        (job / 'job.json').write_text('{}')
        return job

    def _log(self, item_id, job):
        log = self.td / 'commitments' / 'runs' / f'{item_id}.log'
        log.write_text(f'REVIEW_REQUIRED {{"packet": "{job}/review_packet.json"}}\n')

    def _item(self, item_id, state=DISPATCHED, handle='finished_handle'):
        return {'id': item_id, 'state': state, 'handle': handle}

    def _make_mod(self, items):
        fc = make_fake_commit(items)
        mod = _load_fresh(self.td, self.accept, fc)
        return mod

    def _wait_for_file(self, path, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                return True
            time.sleep(0.05)
        return False

    def test_launches_once_with_marker(self):
        """main() launches accept_unit once and writes a marker."""
        job = self._job('i1')
        self._log('i1', job)
        mod = self._make_mod([self._item('i1')])
        mod.main()
        running = job / 'auto_accept-i1.running'
        self.assertTrue(running.exists(), "Marker should exist after launch")
        # Wait for result
        out = job / 'auto_accept-i1.json'
        self._wait_for_file(out)
        # Reap child
        out_content = out.read_text() if out.exists() else '{}'
        data = json.loads(out_content)
        self.assertEqual(data.get('verdict'), 'PASS')

    def test_second_run_while_alive_does_not_relaunch(self):
        """While marker is live, second main() call does not start a new process."""
        job = self._job('i2')
        self._log('i2', job)
        mod = self._make_mod([self._item('i2')])
        mod.main()
        # Write our own identity as marker (simulate live worker)
        running = job / 'auto_accept-i2.running'
        self.assertTrue(running.exists())
        # Second call
        err_before = list(job.glob('*.err'))
        mod.main()
        err_after = list(job.glob('*.err'))
        # No new err files created (only one Popen call total)
        self.assertEqual(len(err_before), len(err_after))

    def test_at_most_one_start_per_tick_two_candidates(self):
        """With two ready candidates, only one is started per tick."""
        job1 = self._job('j1')
        job2 = self._job('j2')
        self._log('j1', job1)
        self._log('j2', job2)
        fc = make_fake_commit([self._item('j1'), self._item('j2')])
        mod = _load_fresh(self.td, self.accept, fc)
        mod.main()
        r1 = (job1 / 'auto_accept-j1.running').exists()
        r2 = (job2 / 'auto_accept-j2.running').exists()
        # Exactly one marker was written
        self.assertEqual(r1 + r2, 1)

    def test_global_serialization_across_two_jobs(self):
        """If a live worker exists for job1, main() does not start job2."""
        job1 = self._job('g1')
        job2 = self._job('g2')
        self._log('g1', job1)
        self._log('g2', job2)
        # Place a live marker for job1 (using our own PID)
        own_pid = os.getpid()
        mod0 = _load_module()
        own_ident = mod0.get_proc_identity(own_pid)
        if own_ident is None:
            self.skipTest("Cannot read own identity")
        running1 = job1 / 'auto_accept-g1.running'
        running1.write_text(f'{own_pid} {own_ident[1]} {int(time.time())} 2024-01-01 00:00:00')

        fc = make_fake_commit([self._item('g1'), self._item('g2')])
        mod = _load_fresh(self.td, self.accept, fc)
        mod.main()
        self.assertFalse((job2 / 'auto_accept-g2.running').exists())

    def test_lock_already_held_nothing(self):
        """If lock is held by another process, main() returns without doing anything."""
        import fcntl as _fcntl
        job = self._job('lk1')
        self._log('lk1', job)
        mod = self._make_mod([self._item('lk1')])
        lock_path = self.td / 'lab' / 'journal' / 'auto_accept.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_f = open(str(lock_path), 'w')
        _fcntl.flock(lock_f, _fcntl.LOCK_EX)
        try:
            mod.main()
        finally:
            lock_f.close()
        self.assertFalse((job / 'auto_accept-lk1.running').exists())

    def test_non_dispatched_skipped(self):
        """Non-DISPATCHED items are ignored."""
        job = self._job('nd1')
        self._log('nd1', job)
        mod = self._make_mod([self._item('nd1', state='OPEN')])
        mod.main()
        self.assertFalse((job / 'auto_accept-nd1.running').exists())

    def test_handle_not_finished_skipped(self):
        """Item with non-finished handle is not launched."""
        job = self._job('hn1')
        self._log('hn1', job)
        mod = self._make_mod([self._item('hn1', handle='pending_handle')])
        mod.main()
        self.assertFalse((job / 'auto_accept-hn1.running').exists())

    def test_pass_result_no_relaunch(self):
        """PASS result json -> no relaunch."""
        job = self._job('pr1')
        self._log('pr1', job)
        out = job / 'auto_accept-pr1.json'
        out.write_text(json.dumps({'verdict': 'PASS'}))
        mod = self._make_mod([self._item('pr1')])
        mod.main()
        # No new .running marker
        self.assertFalse((job / 'auto_accept-pr1.running').exists())

    def test_fail_result_no_relaunch(self):
        """FAIL result json -> no relaunch."""
        job = self._job('fr1')
        self._log('fr1', job)
        out = job / 'auto_accept-fr1.json'
        out.write_text(json.dumps({'verdict': 'FAIL'}))
        mod = self._make_mod([self._item('fr1')])
        mod.main()
        self.assertFalse((job / 'auto_accept-fr1.running').exists())

    def test_pending_ci_young_no_relaunch(self):
        """PENDING_CI younger than 180s -> no relaunch."""
        job = self._job('pc1')
        self._log('pc1', job)
        out = job / 'auto_accept-pc1.json'
        out.write_text(json.dumps({'verdict': 'PENDING_CI'}))
        # file is fresh (just written)
        mod = self._make_mod([self._item('pc1')])
        mod.main()
        self.assertFalse((job / 'auto_accept-pc1.running').exists())

    def test_pending_ci_old_reruns_and_removes_result(self):
        """PENDING_CI older than 180s -> rerun, old result and attempts file removed."""
        job = self._job('pc2')
        self._log('pc2', job)
        out = job / 'auto_accept-pc2.json'
        out.write_text(json.dumps({'verdict': 'PENDING_CI'}))
        # Set mtime to 200 seconds ago
        old_time = time.time() - 200
        os.utime(str(out), (old_time, old_time))
        attempts = job / 'auto_accept-pc2.attempts'
        attempts.write_text('1')
        mod = self._make_mod([self._item('pc2')])
        mod.main()
        # old result should be removed and new launch happened
        self.assertTrue((job / 'auto_accept-pc2.running').exists())
        # old attempts file was removed (PENDING_CI rerun resets crash counter),
        # then a fresh one (=1) was written at launch time
        if attempts.exists():
            self.assertEqual(int(attempts.read_text().strip()), 1)

    def test_dead_worker_crash_budget_3(self):
        """Dead worker without result relaunches up to 3 times then stops."""
        job = self._job('cb1')
        self._log('cb1', job)
        mod = self._make_mod([self._item('cb1')])
        running = job / 'auto_accept-cb1.running'
        out = job / 'auto_accept-cb1.json'
        attempts = job / 'auto_accept-cb1.attempts'

        # Each call when attempts < MAX_DEAD_RETRIES (3) should launch.
        # attempts is incremented before launch, so after 3 launches attempts=3 -> blocked.
        for launch_num in range(1, 4):
            # No running marker, no result file (simulates dead worker from previous launch)
            if running.exists():
                running.unlink()
            if out.exists():
                out.unlink()
            mod.main()
            count = int(attempts.read_text().strip())
            self.assertEqual(count, launch_num, f"Expected attempts={launch_num} after launch {launch_num}")
            self.assertTrue(running.exists(), f"Should have launched (attempt {launch_num})")

        # Now attempts=3: kill the marker to simulate another death, then call again
        if running.exists():
            running.unlink()
        if out.exists():
            out.unlink()
        mod.main()
        # Budget exhausted: no new launch, no result file
        self.assertFalse(running.exists(), "No launch after crash budget exhausted")
        self.assertFalse(out.exists(), "No result file ever fabricated")
        # attempts stays at 3 (not incremented since no launch)
        self.assertEqual(int(attempts.read_text().strip()), 3)

    def test_no_result_file_fabricated_after_budget(self):
        """After crash budget, no result json is ever written."""
        job = self._job('cb2')
        self._log('cb2', job)
        attempts = job / 'auto_accept-cb2.attempts'
        attempts.write_text('3')
        running = job / 'auto_accept-cb2.running'
        # running marker already cleaned (marker removed by previous death)
        mod = self._make_mod([self._item('cb2')])
        mod.main()
        self.assertFalse((job / 'auto_accept-cb2.json').exists())
        self.assertFalse(running.exists())

    def test_accept_missing_nothing_no_marker(self):
        """If ACCEPT does not exist, main() launches nothing and writes no marker."""
        job = self._job('am1')
        self._log('am1', job)
        fc = make_fake_commit([self._item('am1')])
        missing_accept = self.td / 'nonexistent_accept.py'
        mod = _load_fresh(self.td, missing_accept, fc)
        mod.main()
        self.assertFalse((job / 'auto_accept-am1.running').exists())

    def test_c_none_nothing(self):
        """c=None -> main() returns silently."""
        job = self._job('cn1')
        self._log('cn1', job)
        mod = _load_fresh(self.td, self.accept, None)
        mod.c = None
        mod.main()
        self.assertFalse((job / 'auto_accept-cn1.running').exists())

    def test_home_none_nothing(self):
        """HOME=None -> main() returns silently."""
        fc = make_fake_commit([self._item('hn2')])
        mod = _load_fresh(self.td, self.accept, fc)
        mod.HOME = None
        mod.main()
        # No files created in any job dir

    def test_stdout_stderr_silent(self):
        """main() produces no output on stdout or stderr."""
        job = self._job('ss1')
        self._log('ss1', job)
        # Run as subprocess to capture output
        script = self.td / 'run_main.py'
        script.write_text(
            f'import importlib.util, pathlib\n'
            f'spec = importlib.util.spec_from_file_location("m", r"{_MOD_PATH}")\n'
            f'mod = importlib.util.module_from_spec(spec)\n'
            f'spec.loader.exec_module(mod)\n'
            f'import types, json\n'
            f'fc = types.SimpleNamespace()\n'
            f'fc.DISPATCHED = "DISPATCHED"\n'
            f'class L:\n'
            f'    def __init__(self): self.data = {{"items": [{json.dumps(self._item("ss1"))}]}}\n'
            f'    def __enter__(self): return self\n'
            f'    def __exit__(self, *a): pass\n'
            f'fc.Ledger = L\n'
            f'fc.handle_status = lambda h: ("finished", {{}})\n'
            f'mod.HOME = pathlib.Path(r"{self.td}")\n'
            f'mod.ACCEPT = pathlib.Path(r"{self.accept}")\n'
            f'mod.c = fc\n'
            f'mod.main()\n'
        )
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        )
        self.assertEqual(result.stdout, '', f"Unexpected stdout: {result.stdout!r}")
        self.assertEqual(result.stderr, '', f"Unexpected stderr: {result.stderr!r}")

    def test_ledger_data_not_mutated(self):
        """Ledger data is unchanged after main() runs."""
        items = [self._item('lm1')]
        import copy
        original = copy.deepcopy(items)
        fc = make_fake_commit(items)
        job = self._job('lm1')
        self._log('lm1', job)
        mod = _load_fresh(self.td, self.accept, fc)
        mod.main()
        # The items list in the fake commit was not mutated
        self.assertEqual(items, original)

    # ------------------------------------------------------------------
    # Global-scan-only tests (catch the "pass vs return" mutation)
    # ------------------------------------------------------------------

    def test_live_marker_of_non_candidate_item_blocks_other_job(self):
        """Global scan: live marker on a VERIFIED item (never a launch candidate)
        must block a separate DISPATCHED+finished item B from starting."""
        # Item A: VERIFIED state (not DISPATCHED -> never a candidate), but has a
        # log and job dir so the global scan visits it, and a live .running marker.
        job_a = self._job('ga_nc')
        self._log('ga_nc', job_a)
        own_pid = os.getpid()
        mod0 = _load_module()
        own_ident = mod0.get_proc_identity(own_pid)
        if own_ident is None:
            self.skipTest("Cannot read own process identity")
        running_a = job_a / 'auto_accept-ga_nc.running'
        running_a.write_text(
            f'{own_pid} {own_ident[1]} {int(time.time())} 2024-01-01 00:00:00'
        )

        # Item B: DISPATCHED with finished handle - would be a launch candidate.
        job_b = self._job('gb_nc')
        self._log('gb_nc', job_b)

        fc = make_fake_commit([
            self._item('ga_nc', state='VERIFIED', handle='finished_handle'),
            self._item('gb_nc', state=DISPATCHED, handle='finished_handle'),
        ])
        mod = _load_fresh(self.td, self.accept, fc)
        mod.main()

        # B must NOT have started: no .running, no .attempts, no .err
        self.assertFalse(
            (job_b / 'auto_accept-gb_nc.running').exists(),
            "B's marker must not exist when A's global marker is live",
        )
        self.assertFalse(
            (job_b / 'auto_accept-gb_nc.attempts').exists(),
            "B's attempts file must not exist",
        )
        self.assertFalse(
            (job_b / 'auto_accept-gb_nc.err').exists(),
            "B's err file must not exist (no Popen call was made)",
        )

    def test_live_marker_of_item_with_unfinished_handle_blocks_other_job(self):
        """Global scan: live marker on a DISPATCHED item whose handle is still
        running (so it is not a launch candidate) must block item B from starting."""
        # Item A: DISPATCHED but handle status is 'running', not 'finished'.
        # It has a log, job dir and a live .running marker.
        job_a = self._job('ga_uh')
        self._log('ga_uh', job_a)
        own_pid = os.getpid()
        mod0 = _load_module()
        own_ident = mod0.get_proc_identity(own_pid)
        if own_ident is None:
            self.skipTest("Cannot read own process identity")
        running_a = job_a / 'auto_accept-ga_uh.running'
        running_a.write_text(
            f'{own_pid} {own_ident[1]} {int(time.time())} 2024-01-01 00:00:00'
        )

        # Item B: DISPATCHED with finished handle.
        job_b = self._job('gb_uh')
        self._log('gb_uh', job_b)

        # Use extended fake commit so item A's handle returns ('running', {}).
        fc = make_fake_commit(
            items=[
                self._item('ga_uh', state=DISPATCHED, handle='running_handle'),
                self._item('gb_uh', state=DISPATCHED, handle='finished_handle'),
            ],
            handle_statuses={'running_handle': ('running', {})},
        )
        mod = _load_fresh(self.td, self.accept, fc)
        mod.main()

        self.assertFalse(
            (job_b / 'auto_accept-gb_uh.running').exists(),
            "B must not start while A's global marker is live",
        )
        self.assertFalse(
            (job_b / 'auto_accept-gb_uh.attempts').exists(),
            "B's attempts file must not exist",
        )
        self.assertFalse(
            (job_b / 'auto_accept-gb_uh.err').exists(),
            "B's err file must not exist (no Popen call was made)",
        )

    def test_dead_marker_of_non_candidate_item_does_not_block(self):
        """Global scan: dead marker on a VERIFIED item is cleaned up and does not
        prevent item B (DISPATCHED+finished) from starting."""
        # Item A: VERIFIED, has log+job dir, but marker uses a nonexistent PID.
        job_a = self._job('ga_dm')
        self._log('ga_dm', job_a)
        dead_pid = 99999999
        running_a = job_a / 'auto_accept-ga_dm.running'
        running_a.write_text(
            f'{dead_pid} 12345 1700000000 2024-01-01 00:00:00'
        )

        # Item B: launch candidate.
        job_b = self._job('gb_dm')
        self._log('gb_dm', job_b)

        fc = make_fake_commit([
            self._item('ga_dm', state='VERIFIED', handle='finished_handle'),
            self._item('gb_dm', state=DISPATCHED, handle='finished_handle'),
        ])
        mod = _load_fresh(self.td, self.accept, fc)
        mod.main()

        # A's dead marker must have been removed by check_marker_liveness.
        self.assertFalse(
            running_a.exists(),
            "Dead marker for A must be cleaned up by the global scan",
        )
        # B must have started: its .running marker must exist.
        self.assertTrue(
            (job_b / 'auto_accept-gb_dm.running').exists(),
            "B must start when only a dead (cleaned) marker was in the global scan",
        )
        # Wait for B's result and reap the worker.
        out_b = job_b / 'auto_accept-gb_dm.json'
        self._wait_for_file(out_b, timeout=5.0)
        if out_b.exists():
            data = json.loads(out_b.read_text())
            self.assertEqual(data.get('verdict'), 'PASS')

    def test_first_launch_records_attempt_one(self):
        """After the very first launch (no prior marker, no attempts file) the
        attempts file must contain exactly the integer 1."""
        job = self._job('fl1')
        self._log('fl1', job)
        mod = self._make_mod([self._item('fl1')])
        # Verify no pre-existing attempts file
        attempts = job / 'auto_accept-fl1.attempts'
        self.assertFalse(attempts.exists(), "Pre-condition: no attempts file before first launch")

        mod.main()

        self.assertTrue(attempts.exists(), "attempts file must be created on first launch")
        self.assertEqual(
            attempts.read_text().strip(),
            '1',
            "attempts file must contain exactly '1' after first launch",
        )
        # Reap the worker process.
        out = job / 'auto_accept-fl1.json'
        self._wait_for_file(out, timeout=5.0)
        if out.exists():
            data = json.loads(out.read_text())
            self.assertEqual(data.get('verdict'), 'PASS')


# ---------------------------------------------------------------------------
# Static / AST tests
# ---------------------------------------------------------------------------

class TestStaticAnalysis(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.source = _MOD_PATH.read_text()
        cls.tree = ast.parse(cls.source)

    def test_allowed_imports_only(self):
        """Only allowed modules are imported at top level (direct module body statements)."""
        allowed = {'errno', 'fcntl', 'json', 'os', 'pathlib', 're', 'subprocess', 'sys', 'time'}
        # Walk only direct children of the module body (not nested inside if/try/etc.)
        imported = set()
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split('.')[0])
        self.assertTrue(imported.issubset(allowed),
                        f"Disallowed imports: {imported - allowed}")

    def test_exactly_one_popen_call(self):
        """There is exactly one Popen call site in the module."""
        count = self.source.count('subprocess.Popen')
        self.assertEqual(count, 1, f"Expected 1 Popen call, found {count}")

    def _strip_comments(self, src):
        """Remove # comments and string literals for keyword checks."""
        # Remove # comments
        lines = []
        for line in src.splitlines():
            code_part = line.split('#')[0]
            lines.append(code_part)
        return '\n'.join(lines)

    def test_no_forbidden_words(self):
        """Forbidden words must not appear anywhere in the file (case-insensitive)."""
        forbidden = ['allow_once', 'click', 'card', 'browser', 'cdp', 'bridge',
                     'chatgpt', 'github', 'token', 'secret', 'password']
        lower_src = self.source.lower()
        for word in forbidden:
            self.assertNotIn(word, lower_src, f"Forbidden word found: {word!r}")


if __name__ == '__main__':
    unittest.main()
