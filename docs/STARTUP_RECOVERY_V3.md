# PAPER v3 startup, stop and cold-recovery contract

Status: operator-deployable candidate. This PR does not install or activate a service.

## Scope

This contract owns startup coordination for three already accepted components:

1. PAPER v3 runtime;
2. localhost read-only observer dashboard;
3. deterministic read-only health monitor.

It preserves the existing split-root topology:

- monitor/dashboard root and runtime root may differ;
- the runtime keeps the existing explicit `state_dir` and `status_path`;
- the runtime keeps the accepted `--storage-policy`;
- the health monitor keeps its accepted explicit runtime/state/status identity configuration.

It does not change strategy, trading risk, execution assumptions, account baseline, forward start, research deadline or storage thresholds.

## Important limitation

"Autostart" here means **when the supported WSL user systemd environment is itself started**.

This contract does not make Windows start WSL after a host reboot, does not restart the host, and does not install a Windows Scheduled Task. If Windows is down, WSL is not started, the user systemd manager is unavailable, or the Hermes/gateway/Telegram path is offline, PAPER monitoring and Telegram delivery are **not guaranteed**.

The durable PAPER namespace remains the source of recovery truth across such outages.

## Operator-local configuration

Create an operator-owned JSON file outside mirror-owned source files:

```json
{
  "schema_version": 1,
  "runtime_python": "/absolute/path/to/python",
  "runtime_root": "/absolute/path/to/runtime-worktree/lab",
  "runtime_config": "/absolute/path/to/runtime-worktree/lab/paper_config_v3.json",
  "state_dir": "/absolute/path/to/shared/data/paper-v2",
  "status_path": "/absolute/path/to/shared/shared/paper_v2_live.json",
  "storage_policy": "/absolute/path/to/operator/storage-policy.json",
  "monitor_root": "/absolute/path/to/observer-profile/lab",
  "health_config": "/absolute/path/to/observer-profile/lab/shared/health_monitor_config.json",
  "dashboard_html": "/absolute/path/to/observer-profile/lab/dashboard.html",
  "dashboard_port": 18767,
  "health_interval_seconds": 120,
  "startup_health_attempts": 3,
  "startup_health_interval_seconds": 5
}
```

The values above are path placeholders. They are not live machine paths or recommended capacity values.

All path fields must be absolute. Startup health attempts are bounded to 1..5. The supervisor never loops indefinitely waiting for recovery.

The existing health configuration must name the **same** runtime root, state directory and status path. A mismatch blocks startup.

## Read-only preflight

Before activating anything:

```sh
python scripts/paper_startup_supervisor.py --config /path/to/startup.json --check
```

Preflight is read-only. It requires the already deployed durable namespace and verifies:

- runtime/state/monitor roots and required source/config files exist;
- storage policy file exists;
- health identity exactly matches runtime root/state/status;
- `runtime.sqlite3` is readable, SQLite `quick_check` succeeds, and state/audit tables exist;
- durable config hash matches the configured v3 config;
- deployment marker is `H1-PAPER-003`;
- original `forward_start_ms`, `strategy_start_ms`, `research_deadline_ms`, fill baseline and ledger baseline are present;
- `signals.sqlite3` and `broker.sqlite3` are readable and structurally present;
- open positions, pending orders, fills, ledger and audit counts are observed without rewriting them;
- zero exact matching runtimes means ready to start;
- one exact matching runtime means already running and is **not** adopted by a second supervisor;
- more than one exact matching runtime is an explicit duplicate-runtime block.

A missing/corrupt durable database is never initialized, replaced, reset or repaired automatically.

## Single-engine rule

The supervisor refuses ownership if an exact runtime already exists for the configured runtime cwd + `paper_runtime_v3.py` + state namespace + status path.

The existing runtime namespace lock remains a second authoritative race guard inside PAPER itself.

No fuzzy process matching is used. A second engine is never started intentionally for recovery.

## Cold-start behavior

The supervisor starts the read-only dashboard and then exactly one PAPER v3 process with the accepted arguments:

```sh
python -u paper_runtime_v3.py \
  --state-dir "$STATE_DIR" \
  --status "$STATUS_PATH" \
  --config "$RUNTIME_CONFIG" \
  --storage-policy "$STORAGE_POLICY"
```

It does not create a new account namespace.

During bounded startup acceptance it runs the same health monitor against the explicit split-root identity. PAPER is considered recovered only after health reports current operational evidence.

If data is unavailable/stale, the existing runtime source/gap rules remain authoritative. The supervisor does not backfill missed trades, retimestamp old data, synthesize confirmation fills, force a close or extend the research window. If health does not become current within the configured bounded attempts, the supervisor stops its owned children and waits for operator action.

## Runtime exit policy: no restart loop

The supervisor does **not** continuously restart the runtime.

### Exit code 2 / storage protection

The accepted v3 runtime uses exit code 2 for intentional storage protection stop / `StorageProtectionHalt`.

On that outcome the supervisor:

1. performs at most one final read-only health observation;
2. does not start a replacement runtime;
3. stops its owned dashboard process;
4. exits in `operator-hold`.

Capacity/configuration must be corrected and explicitly re-accepted by the operator before another start.

This prevents a service manager from repeatedly consuming exit reserve or overwriting the final stop evidence.

### Other runtime/dashboard exits

An unexpected runtime or dashboard exit also moves the supervisor to `operator-hold`. There is no automatic in-process retry.

The service template below therefore uses `Restart=no`.

## Normal stop

SIGTERM/SIGINT to the supervisor is the supported stop path.

The supervisor terminates only the runtime/dashboard children it launched. It does not:

- flatten positions;
- cancel/fabricate fills solely because the service is stopping;
- rewrite broker/account state;
- alter the research deadline;
- reset the account;
- start an alternate namespace.

Unfilled/pending orders and positions remain durable according to the existing broker/runtime persistence contract and are recovered on the next accepted cold start.

## Offline interval and recovery semantics

An outage creates no synthetic trading history.

On recovery:

- existing fills/ledger/audit remain the prefix;
- existing account/risk baselines remain unchanged;
- original forward start and research deadline remain unchanged;
- persisted open positions and pending orders are loaded as they were durably recorded;
- the runtime's existing gap handling, source freshness, pending-entry and reduce-only exit rules decide what happens after fresh data resumes;
- no "missed" offline trade is reconstructed;
- no forced flatten occurs merely because there was an outage.

## Health monitor ownership

The supervisor performs the accepted health tick at `health_interval_seconds`.

During cutover, the operator must ensure only one periodic health owner remains. If an older standalone scheduler is still active, disable it **only after** the supervisor has passed preflight/startup acceptance and its health output has been read back. The health configuration file itself is retained unchanged.

Health remains read-only toward trading state and never starts repair.

## Example user-systemd service template

This is a template only. The coding PR does not write it into `~/.config/systemd/user`, run `systemctl`, enable linger or change the host.

```ini
[Unit]
Description=Perp Desk PAPER v3 bounded supervisor
After=default.target

[Service]
Type=simple
ExecStart=/absolute/path/to/python /absolute/path/to/scripts/paper_startup_supervisor.py --config /absolute/path/to/startup.json
Restart=no
KillSignal=SIGTERM
TimeoutStopSec=10

[Install]
WantedBy=default.target
```

Do not set `Restart=always` or `Restart=on-failure`; intentional storage/config holds require operator review.

## Operator deployment

1. Confirm exact PR head and green CI.
2. Apply accepted files through the normal local workflow; do not change the trading namespace.
3. Create the operator-local startup JSON using the **already deployed** runtime root, state/status namespace, storage policy and health config.
4. Run `--check`. Do not continue if it reports missing/corrupt state, identity mismatch or an already-running runtime.
5. Stop the currently manually managed runtime/dashboard using the existing operator procedure. Verify the exact runtime PID count is zero. Do not kill a process based only on a substring match.
6. Run the supervisor manually once. Verify:
   - exactly one runtime;
   - dashboard responds locally;
   - health shows the same exact runtime/state/status identity;
   - forward start/research deadline/account baselines match preflight;
   - no reset/backfill occurred.
7. Stop the manual supervisor cleanly and repeat `--check`; durable prefixes/baselines must be unchanged except legitimate runtime events already recorded before stop.
8. If replacing an existing standalone health scheduler, disable it only now, after supervisor health has been verified.
9. Create the user-systemd unit from the template and run a manual `systemctl --user start` / status readback. **Do not reboot the host for acceptance.**
10. Verify `/api/status`, `/api/health`, `/api/work` and exact process count.
11. Enable the unit only after those readbacks pass.

## Rollback

1. `systemctl --user stop` the supervisor unit.
2. Disable/remove only the new supervisor unit/configuration.
3. Verify no supervisor-owned runtime remains.
4. Re-enable the previously accepted standalone health schedule if it was disabled.
5. Resume the previous manual runtime/dashboard procedure only after exact process count is zero and the same state/status/storage-policy configuration is used.
6. Do not restore an older account copy, reset the namespace or move the research deadline.

Rollback is process-management rollback, **not trading-state rollback**.

## Mirror export

This document and the startup supervisor entrypoint are part of the mechanical mirror export contract. Operator-local startup/storage/health JSON files remain excluded.
