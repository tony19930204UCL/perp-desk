#!/usr/bin/env python3
"""no_agent cron (silent): run the deterministic acceptance for relay units that were delivered, without any model.

For every ledger item that is DISPATCHED with a finished handle whose watcher log ends in REVIEW_REQUIRED, find the job directory from the review packet path,
and start `accept_unit.py JOB --out JOB/auto_accept-<ID>.json` detached (one at a time). The supervisor agent is held back by ledger_wake.py until that file exists
(or 25 minutes passed), so the model starts with facts and never waits for a 6 minute test run.
Prints nothing.
Copy to <profile>/scripts/ (ledger_wake.py imports it from there) and keep accept_unit.py at <profile>/skills/relay-backlog-operator/scripts/.
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

MAX_DEAD_RETRIES = 3


def resolve_home(script_path=None, environ=None):
    """Resolve the profile home directory.

    Order:
    1. Script install location: if script_path.resolve().parent.name == 'scripts'
       and (parent.parent / 'commitments') is a directory, return parent.parent.
    2. env HERMES_HOME if it is a directory containing 'commitments'.
    3. None. No fallback to ~/.hermes.
    """
    if script_path is not None:
        try:
            resolved = Path(script_path).resolve()
            parent = resolved.parent
            if parent.name == 'scripts':
                candidate = parent.parent
                if (candidate / 'commitments').is_dir():
                    return candidate
        except Exception:
            pass

    if environ is None:
        environ = os.environ
    hermes_home = environ.get('HERMES_HOME')
    if hermes_home:
        p = Path(hermes_home)
        if p.is_dir() and (p / 'commitments').is_dir():
            return p.resolve()

    return None


HOME = resolve_home(script_path=__file__)

if HOME is not None:
    sys.path.insert(0, str(HOME / 'plugins' / 'commitment-gate'))
    try:
        import commit as c
    except ImportError:
        c = None
else:
    c = None

_sibling_candidate = Path(__file__).resolve().with_name('accept_unit.py')
if HOME is not None and _sibling_candidate.exists() and _sibling_candidate.is_file():
    ACCEPT = _sibling_candidate
elif HOME is not None:
    ACCEPT = HOME / 'skills' / 'relay-backlog-operator' / 'scripts' / 'accept_unit.py'
else:
    ACCEPT = None


def get_proc_identity(pid: int):
    """Return (pid, starttime) tuple uniquely identifying a Linux process instance."""
    if not pid or pid <= 1:
        return None
    stat_file = Path(f"/proc/{pid}/stat")
    if not stat_file.exists():
        return None
    try:
        raw = stat_file.read_text()
        rparen = raw.rfind(")")
        if rparen != -1:
            rest = raw[rparen + 1 :].split()
            # rest[19] is field 22 (starttime)
            return (pid, int(rest[19]))
    except Exception:
        pass
    try:
        return (pid, Path(f"/proc/{pid}").stat().st_mtime_ns)
    except Exception:
        return None


def is_proc_alive(pid: int, expected_ident=None) -> bool:
    """Check if process pid is running and matches expected start identity."""
    if not pid or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return False
    if expected_ident is not None:
        curr = get_proc_identity(pid)
        if curr is None:
            # Cannot prove identity -> fail closed, do not declare healthy without proof
            return False
        return tuple(expected_ident) == curr
    return True


def parse_marker(running_file: Path):
    """Parse marker file returning (pid, expected_ident, start_epoch, is_valid)."""
    try:
        raw = running_file.read_text().strip()
    except Exception:
        return None, None, None, False
    if not raw or "quarantine" in raw.lower():
        return None, None, None, False
    parts = raw.split()
    if not parts or not parts[0].isdigit():
        return None, None, None, False
    pid = int(parts[0])
    if pid <= 1:
        return None, None, None, False
    expected_ident = None
    start_epoch = None
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        expected_ident = (pid, int(parts[1]))
        start_epoch = float(parts[2])
    elif len(parts) >= 2 and parts[1].isdigit():
        expected_ident = (pid, int(parts[1]))
    if start_epoch is None:
        try:
            start_epoch = running_file.stat().st_mtime
        except Exception:
            start_epoch = time.time()
    return pid, expected_ident, start_epoch, True


def check_marker_liveness(running_file: Path) -> bool:
    """Check if marker represents a live running worker. If dead, unlinks marker.

    Returns True if worker is alive, active, or uncertain ownership (a blocker).
    Returns False if dead, recycled PID, or unlinked.
    """
    if not running_file.exists():
        return False
    pid, expected_ident, start_epoch, is_valid = parse_marker(running_file)
    if not is_valid:
        # Malformed marker or quarantine marker preserves and blocks!
        # Do not unlink uncertain ownership; fail closed.
        return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        # Process is definitely dead -> clean up running marker
        running_file.unlink(missing_ok=True)
        return False
    except OSError as e:
        if getattr(e, 'errno', None) == errno.ESRCH:
            # Process is definitely dead -> clean up running marker
            running_file.unlink(missing_ok=True)
            return False
        # PermissionError (EPERM) or unknown OSError: does not prove dead!
        # Preserve marker and block duplicate dispatch (fail closed).
        return True

    # Process is alive (os.kill succeeded)
    if expected_ident is not None:
        curr = get_proc_identity(pid)
        if curr is None:
            # Cannot read identity for live PID: fail closed!
            # Preserve marker and block duplicate dispatch.
            return True
        if tuple(expected_ident) != curr:
            # PID exists but identity measured DIFFERENT (recycled PID) -> clean up marker
            running_file.unlink(missing_ok=True)
            return False
        # Proven live worker matching expected identity
        return True
    else:
        # Legacy marker (no expected_ident recorded) and PID is live.
        # Uncertain legacy identity: preserve and block!
        return True


def job_dir_from_log(item_id):
    """Find job directory from watcher log packet without executing arbitrary content.

    Supports both single-line and pretty-printed REVIEW_REQUIRED JSON blocks.
    Returns (None, None) if HOME is None.
    Returns (None, log) if the resolved directory is outside HOME.
    """
    if HOME is None:
        return None, None
    log = HOME / 'commitments' / 'runs' / (item_id + '.log')
    if not log.exists():
        return None, None
    try:
        content = log.read_text(errors='ignore')
    except Exception:
        return None, log

    home_resolved = HOME.resolve()

    idx = content.rfind('REVIEW_REQUIRED')
    if idx != -1:
        sub = content[idx:]
        m = re.search(r'"packet"\s*:\s*"([^"]+review_packet\.json)"', sub)
        if not m:
            m = re.search(r'"packet"\s*:\s*"([^"]+)"', sub)
        if m:
            candidate = Path(m.group(1)).parent
            try:
                resolved = candidate.resolve()
            except Exception:
                return None, log
            try:
                resolved.relative_to(home_resolved)
            except ValueError:
                return None, log
            return candidate, log

    # Fallback to tail lines
    tail = content.splitlines()[-20:]
    for l in reversed(tail):
        m = re.search(r'REVIEW_REQUIRED.*?"packet":\s*"([^"]+)"', l)
        if m:
            candidate = Path(m.group(1)).parent
            try:
                resolved = candidate.resolve()
            except Exception:
                return None, log
            try:
                resolved.relative_to(home_resolved)
            except ValueError:
                return None, log
            return candidate, log
    return None, log


def main():
    if HOME is None:
        return
    if c is None:
        return
    if ACCEPT is None or not ACCEPT.is_file():
        return

    (HOME / 'lab' / 'journal').mkdir(parents=True, exist_ok=True)
    lock = open(HOME / 'lab' / 'journal' / 'auto_accept.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return

    try:
        with c.Ledger() as led:
            all_items = list(led.data.get('items', []))

        # Global scan: check all registered job markers for any active live worker,
        # even when ledger watcher became VERIFIED/OPEN/etc if log resolves job and marker live.
        seen_dirs = set()
        for it in all_items:
            item_id = it.get('id')
            if not item_id:
                continue
            jd, log = job_dir_from_log(item_id)
            if jd is None or not (jd / 'job.json').exists():
                continue
            resolved_str = str(jd.resolve())
            if resolved_str in seen_dirs:
                continue
            seen_dirs.add(resolved_str)
            for rf in jd.glob('auto_accept-*.running'):
                if check_marker_liveness(rf):
                    return  # Another worker is still alive; globally serialize (one at a time)

        # Evaluate items for launch: only DISPATCHED items with finished handle
        job_candidates = []
        for it in all_items:
            if it.get('state') == c.DISPATCHED:
                status, _ = c.handle_status(it.get('handle'))
                if status == 'finished':
                    item_id = it.get('id')
                    jd, log = job_dir_from_log(item_id)
                    if jd is not None and (jd / 'job.json').exists():
                        job_candidates.append((it, jd))

        # No worker is active anywhere: evaluate items for launch
        for it, jd in job_candidates:
            item_id = it['id']
            out = jd / ('auto_accept-%s.json' % item_id)
            running = jd / ('auto_accept-%s.running' % item_id)
            attempts_file = jd / ('auto_accept-%s.attempts' % item_id)

            if out.exists():
                try:
                    with open(out) as out_f:
                        pending = json.load(out_f).get('verdict') == 'PENDING_CI' and time.time() - out.stat().st_mtime > 180
                except Exception:
                    pending = False
                if not pending:
                    continue
                # PENDING_CI older than 180s: rerun, remove result and attempts (not a crash)
                out.unlink()
                running.unlink(missing_ok=True)
                attempts_file.unlink(missing_ok=True)

            # Check liveness of per-item marker (may still exist if not caught by global scan)
            if running.exists():
                if check_marker_liveness(running):
                    return

            # Crash budget: attempts_file records how many launches have completed without a
            # result (each Popen call increments it just before launch).  If the file exists
            # now with no marker and no out, the previous worker crashed.
            if not out.exists() and attempts_file.exists():
                crashes = 0
                try:
                    crashes = int(attempts_file.read_text().strip())
                except Exception:
                    crashes = 0
                if crashes >= MAX_DEAD_RETRIES:
                    # Budget exhausted: do not launch, write nothing
                    continue

            err_path = jd / ('auto_accept-%s.err' % item_id)
            # Increment attempts counter before launch; checked on next invocation if no result
            launch_num = 0
            try:
                launch_num = int(attempts_file.read_text().strip())
            except Exception:
                launch_num = 0
            attempts_file.write_text(str(launch_num + 1))
            with open(str(err_path), 'w') as err_f:
                p = subprocess.Popen(
                    [sys.executable, str(ACCEPT), str(jd), '--out', str(out)],
                    stdout=subprocess.DEVNULL,
                    stderr=err_f,
                    start_new_session=True
                )
            ident = get_proc_identity(p.pid)
            if ident:
                running.write_text('%d %d %d %s' % (p.pid, ident[1], int(time.time()), time.strftime('%F %T')))
            else:
                # launch get_proc_identity failure retains quarantine marker not starttime0 leading repeat launch
                running.write_text('%d quarantine %d %s' % (p.pid, int(time.time()), time.strftime('%F %T')))
            return  # at most one start per tick
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == '__main__':
    main()
