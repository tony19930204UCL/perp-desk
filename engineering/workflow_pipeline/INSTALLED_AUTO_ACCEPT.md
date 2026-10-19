# INSTALLED_AUTO_ACCEPT.md

## Purpose

`installed_auto_accept.py` is the repository copy of the profile script
`<profile>/scripts/auto_accept.py`.  It is a **silent, no-model cron launcher**
that runs every 5 minutes.  For every ledger item that is `DISPATCHED` with a
finished handle whose watcher log ends in `REVIEW_REQUIRED`, it locates the job
directory from the review-packet path and starts
`accept_unit.py JOB --out JOB/auto_accept-<ITEM>.json` detached
(at most one process at a time, globally).

## What It Is Not

This script is **not** an approval mechanism, a card clicker, an allow-once
dispatcher, or any form of interactive decision maker.  It only starts the
deterministic `accept_unit.py` worker and writes a marker file.  It never
changes ledger data (it only reads items); note that the commitment library's
`Ledger` context manager re-serializes the ledger file on exit with identical
content, which is inherited from the previous script and unchanged.  It never
touches external APIs and produces no output.

## Install Mapping

| Repository path | Installed path |
|---|---|
| `engineering/workflow_pipeline/installed_auto_accept.py` | `<profile>/scripts/auto_accept.py` |

> [!IMPORTANT]
> **Installation is NOT performed here and is NOT part of this deliverable.**
> The cron schedule change and any unattended proof are likewise out of scope.

### Required Sibling / Paths

After installation the following must exist:

| Purpose | Path |
|---|---|
| Worker script (preferred) | `<profile>/scripts/accept_unit.py` (sibling) |
| Worker script (fallback) | `<profile>/skills/relay-backlog-operator/scripts/accept_unit.py` |
| Commitment gate plugin | `<profile>/plugins/commitment-gate/commit.py` |
| Commitments run logs | `<profile>/commitments/runs/<ITEM>.log` |
| Lock file directory | `<profile>/lab/journal/` |

### Backup-First, Byte-Readback, and Rollback Steps

```sh
# 1. Back up the old file
cp <profile>/scripts/auto_accept.py <profile>/scripts/auto_accept.py.pre-r7

# 2. Copy the new file
cp engineering/workflow_pipeline/installed_auto_accept.py \
   <profile>/scripts/auto_accept.py

# 3. Byte-level verification (sha256 must match)
sha256sum engineering/workflow_pipeline/installed_auto_accept.py \
          <profile>/scripts/auto_accept.py

# 4. Rollback if needed
cp <profile>/scripts/auto_accept.py.pre-r7 \
   <profile>/scripts/auto_accept.py
```

## Changes versus the Reference (A–E)

| ID | Area | Change |
|---|---|---|
| **A** | Profile resolution | Added `resolve_home(script_path, environ) -> Path\|None`. Order: (1) script install location if parent is `scripts/` and sibling `commitments/` exists; (2) `HERMES_HOME` env var if directory has `commitments/`; (3) `None`. **No fallback to `~/.hermes`**. Module-level `HOME = resolve_home()`. If `HOME` is `None`, `main()` and `job_dir_from_log()` return silently. `commit` module imported only when `HOME` is not `None`. |
| **B** | Job-dir containment | `job_dir_from_log` returns `(None, log)` when the resolved directory is not inside `HOME.resolve()`, blocking `..` traversal and symlink escapes. Supports single-line and pretty-printed (multi-line) `REVIEW_REQUIRED` JSON; uses last occurrence. |
| **C** | ACCEPT resolution | `ACCEPT` must be an existing regular file: sibling `accept_unit.py` (if present) else `HOME/skills/relay-backlog-operator/scripts/accept_unit.py`. If neither exists, `main()` launches nothing and writes no marker. |
| **D** | Crash budget | The attempts file (`JOB/auto_accept-<ID>.attempts`, plain integer) counts launches. The number is incremented just before each start. If the result json is still missing and the count is already 3 or more (`MAX_DEAD_RETRIES = 3`), nothing is started and no result file is written (so at most 3 starts per item without a result). A `PENDING_CI` rerun removes the attempts file before the new start, so the new start counts as 1. A finished result (`PASS` or `FAIL`) leaves the attempts file in place and no further start happens. |
| **E** | Everything else | Unchanged from reference: fcntl non-blocking lock, global live-worker scan, PID + `/proc` start-time identity, marker format, quarantine/malformed/legacy preservation, recycled-PID cleanup, stdout/stderr silent, ledger read-only, at most one start per tick, single `subprocess.Popen` call site. |

## Known Limits

1. A quarantine, malformed, legacy-live, EPERM, or unreadable-identity marker blocks all new starts until a human removes it (fail-closed).
2. `accept_unit.py` runs the delivered branch's tests from a clean clone, which is inherited behaviour.
3. No CI step runs these tests yet (workflow files are outside this change).

## Out of Scope

- Installation of the script into any profile directory.
- Changes to the cron schedule.
- Any unattended proof or end-to-end validation against a live profile.
