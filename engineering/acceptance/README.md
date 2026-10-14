# Progress-Aware Acceptance Runner & Helper Integration

Dedicated engineering acceptance tooling for deterministic test execution monitoring without premature termination of progressing suites.

> [!IMPORTANT]
> **Engineering Only — Not Live Deployment**: Source files under `engineering/acceptance/` are copies staged for engineering validation. They do not alter live trading engines, databases, deployment environments, or production branches. Cron remains paused; no automated deployment or merge to main occurs.

---

## 1. Background & Integrated Architecture

The full lab test suite comprises 621–625 unit tests taking 900–1232 seconds. A fixed 14-minute (`subprocess.run(timeout=840)`) hard timeout prematurely terminates healthy test runs that are making steady progress.

This engineering unit integrates the standalone progress-aware runner into the acceptance helpers (`accept_unit.py` and `auto_accept.py`):
- **Observational Soft Checkpoints (`--soft-seconds 840`, `--checkpoint-seconds 300`)**: An observation window (never a cutoff line). When tests are completing, execution continues under recurring observation windows with the same child PID and start identity. When no tests have completed in a window, stall is suspected and thread stacks and kernel wait channels (`wchan`) are recorded, but the child is **not** terminated.
- **Hard Suite Ceiling (`--hard-seconds 2400`)**: Enforces an absolute 40-minute ceiling across the entire suite. If reached, the process group is cleanly terminated with explicit `TIMEOUT` and diagnostic traces (NEVER generic `FAIL`).
- **Explicit Incomplete Verdicts**: Test failures report `FAIL`, hard timeouts report `TIMEOUT`, inactivity/item stalls report `STALL`, and process cancellations report `CANCELLED`. Incomplete runs never masquerade as generic assertion failures.
- **Process Group Isolation & Bounded Cleanup**: Linux process groups (`os.setsid`) ensure bounded termination of the runner, worker, and fixture descendants on timeout, stall, or cancellation. `stop_process_group` reaps owned `Popen` handles during grace so zombie states never delay termination.
- **Continuous Unbuffered Streams**: Worker runs unbuffered (`python3 -u`) and writes real-time JSONL events (`events.jsonl`) with flush and `os.fsync`. Raw `stdout.log` and `stderr.log` stream continuously to disk.
- **Faulthandler Diagnostics**: `faulthandler` is enabled on `sys.stderr` and registered to `SIGUSR1`. On inactivity or item timeout, stack traces of all threads are dumped to `stderr.log` for immediate diagnosis.
- **Flock Deduplication & Restart Recovery**: Persists exact configuration keys (canonical SHA256 of head, suite, cwd, and budgets). Concurrent invocations with the same key wait and reuse the running handle; completed results with the exact key are reused; changed head/suite/budget never silently reuses.
- **Pending CI Reuse Without Retesting**: When CI is still in progress, `accept_unit.py` runs tests, records the passed results in the persistent state directory, and reports `PENDING_CI`. On the next tick when CI has completed, `accept_unit.py` reuses the completed exact-head test verdict without retesting the entire 40-minute suite.
- **Immutable Clean Clone Location & Clone Lock**: Clone locations are keyed by canonical `repo@head` hash (`scratch_base/accept-<sha256(repo@head)[:16]>/clone`) with exclusive concurrency protection via `clone.lock`. Active clones for existing heads are never mutated or deleted under running workers (no `rmtree` or `git checkout` on active clones). Existing cached clones with head mismatches fail closed without deleting or mutating existing directories. Fails closed if the profile scratch path is inaccessible, refusing silent fallback into global system temp (`/tmp`).
- **Strict Runner Report Parsing & Reconciliation**: `accept_unit.py` never maps a process exit code 0 to `PASS` without complete, valid runner artifacts. Successful verdict requires exact head, working directory, suite, and actual requested configuration key (including budgets) agreement; typed, non-empty, unique discovered and completed test ID lists; exact count lengths and equality; `tests.ran == completed_count`; full passed + failed + skipped reconciliation; and real `suite_completed` lifecycle event proof with successful verdict and ran agreement. Stale `runner_out.json` artifacts from earlier configurations are purged prior to execution. Missing, malformed, or invalid artifacts fail closed with `FAIL` (while `TIMEOUT` remains `TIMEOUT`).
- **Global Serialization & Active Marker Scanning in `auto_accept.py`**: Before launching any worker, `auto_accept.py` pre-scans all registered job markers across all ledger items, even when the ledger watcher item has transitioned to `VERIFIED`, `OPEN`, or other states. An alive process matching exact PID and start identity remains a global blocker even beyond 2400s elapsed. Unknown process identities and malformed markers fail closed by preserving markers and blocking duplicate dispatch. Launch identity check failures retain quarantine markers rather than starttime 0, preventing repeated launches. Proven recycled PIDs with measured different start times and definitely dead workers are cleaned up.
- **Safe Log Parsing**: `auto_accept.job_dir_from_log` parses both single-line and pretty-printed `REVIEW_REQUIRED` JSON blocks safely via regex and text inspection without executing arbitrary log content.

---

## 2. Helper Contracts & External Fields

### `accept_unit.py` CLI Options & Flags
Preserves all legacy arguments while adding optional progress budget controls:
```text
accept_unit.py JOB_DIR [OPTIONS]

Options:
  --tests SUITE            Unittest args relative to lab (default: "discover -s tests")
  --skip-tests             Skip running tests (evaluates head, scope, ci only)
  --out PATH               Path to write final JSON report
  --soft-seconds SECONDS   Initial soft checkpoint deadline in seconds (default: 840)
  --checkpoint-seconds SEC Checkpoint observation interval in seconds (default: 300)
  --hard-seconds SECONDS   Absolute total suite hard deadline in seconds (default: 2400)
  --item-seconds SECONDS   Optional explicit budget for a single test item
  --stall-seconds SECONDS  Optional inactivity budget before suspecting stall
  --state-dir PATH         Explicit state directory for runner state and logs
```

### Output JSON External Contract Fields
Every run emits a structured JSON object preserving all external contract fields:
- `job`: Absolute path to the job directory.
- `repository`: Repository owner/name.
- `branch`: Branch name under evaluation.
- `head`: Observed head commit SHA.
- `head_ok`: Boolean; true if head exists and matches `review_packet.json` (if present).
- `scope_ok`: Boolean; true if changed files are strictly within `write_allowlist`.
- `ci_ok`: Boolean; true if all CI runs completed with conclusion `success` and at least one run exists.
- `ci_pending`: Boolean; true if any workflow run is still in progress.
- `tests_ok`: Boolean or `null` (if `--skip-tests`); true if all discovered tests passed with complete lifecycle.
- `verdict`: One of `PASS`, `PASS_WITHOUT_TESTS`, `PENDING_CI`, `FAIL`, `TIMEOUT`, `STALL`, `CANCELLED`.
- `mailbox`: Mailbox status, instruction ID, blocker details, and claims.
- `ci_runs`: Formatted `event:status:conclusion` run records.
- `tests`: Detailed test metrics dictionary:
  - `ran`: Total count of tests executed.
  - `seconds`: Elapsed run time in seconds.
  - `ok`: Boolean indicating test suite success.
  - `failed`: Boolean indicating test failures or errors.
  - `skipped`: Count of skipped tests.
  - `tail`: Trailing log lines from standard streams.

---

## 3. Staged Preflight Proof & Dependencies

### Standard Library Only
All modules use **strictly Python 3 standard library** packages:
- `argparse`, `fcntl`, `hashlib`, `json`, `os`, `pathlib`, `re`, `shutil`, `signal`, `subprocess`, `sys`, `tempfile`, `time`, `unittest`, `faulthandler`
No third-party or PyPI dependencies (`pip`) are required or imported.

### Preflight Verification Commands

1. **Verify Staged Acceptance Source File Hashes**:
   Generate sha256 checksums to verify file integrity:
   ```bash
   sha256sum engineering/acceptance/accept_unit.py \
             engineering/acceptance/auto_accept.py \
             engineering/acceptance/fixture_suites.py \
             engineering/acceptance/progress_runner.py \
             engineering/acceptance/unittest_worker.py
   ```

2. **Verify Pinned Head Matches Target Review Commit**:
   ```bash
   git rev-parse HEAD
   ```

3. **Verify Staged Test Suite Passes (Offline & Fast)**:
   Runs 38 tests (21 progress runner fixture tests + 17 integration compatibility and regression tests):
   ```bash
   python3 -m unittest lab/tests/test_acceptance_progress_runner.py -v
   ```

4. **Verify No Live Deployment or Cron Activation**:
   Verify cron schedule remains paused:
   ```bash
   # Confirm no active auto_accept cron entry in crontab
   crontab -l | grep -E "auto_accept" || echo "Cron correctly paused"
   ```

---

## 4. Full Real Lab Acceptance Run (840s Soft / 2400s Hard)

To execute the complete lab test suite under the 40-minute hard ceiling and 14-minute soft checkpoint on the pinned head:

```bash
python3 engineering/acceptance/progress_runner.py \
  --cwd lab \
  --head $(git rev-parse HEAD) \
  --suite "discover -s tests" \
  --state-dir .scratch/lab-progress-state \
  --soft-seconds 840 \
  --checkpoint-seconds 300 \
  --hard-seconds 2400 \
  --out .scratch/lab-progress-state/verdict.json
```

---

## 5. Rollback Plan

If operational rollback of the staged acceptance tooling is required:
1. **Restore Previous Helper Copies**:
   Discard or revert staged changes to `accept_unit.py` and `auto_accept.py` using git:
   ```bash
   git checkout HEAD~1 -- engineering/acceptance/accept_unit.py engineering/acceptance/auto_accept.py
   ```
2. **Keep Cron Paused**:
   Do **not** enable or resume the `no_agent` cron job.
3. **Clean Scratch State**:
   Remove any scratch progress directories:
   ```bash
   rm -rf .scratch/*-progress-state .progress_runner
   ```
4. **No External State**:
   No database migrations, ledger schema changes, or background system services were created.
