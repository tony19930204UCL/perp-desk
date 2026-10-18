# C0182 dispatch_next — source-only candidate

This is a source mirror, not an installed or authorized runtime. No host deployment, scheduler, trading or browser automation is included.

## Explicit entrypoints and invocation

Use `python3 /absolute/path/to/dispatch_next.py --config /absolute/path/to/config.json`. The configuration is a JSON object requiring absolute existing paths for `preflight`, `queue`, `commit`, `job`, plus absolute `job_dir`, `state_dir`, `receipt_path`; explicit `repository`, `branch`, `task_id`, `instruction_id`, `ledger_text`, `exact_target_approval_owner`, `observer_argv`, and `observer_send_incapable_verified=true`. No implicit home, profile, model or branch selection. The supplied observer must be independently verified as send-incapable. Do not configure relay-drive.py as an observer: it can send. relay-poll.py is non-sending but can invoke allow-once and requires its existing exact-target approval ownership and contracts.

## Behavior and receipt

The candidate invokes the installed preflight CLI using its explicit path, then at most one queue `step --job`. It writes an exclusive intent receipt before the send-capable call and a separate immutable `.outcome.json` after it. Repeated invocation against the same receipt path is rejected. `DISPATCHED` alone is not delivery; a matching `send_<instruction>.json` must be `CONFIRMED` with exact-rendered-bubble evidence. Unknown or blocked send states are not retried. On confirmed delivery it attempts a single `commit.py add TEXT --run ... --accept manual --due +2h` and retains stdout/stderr/return code. Registration failure never causes resend. Registration is not completion.

Possible receipt statuses: PRECHECK_FAILED, UNSENT, SEND_UNKNOWN, CONFIRMED_DELIVERY, REGISTRATION_FAILED, WATCH_REGISTERED. The source-only marker remains true. Do not interpret local process launch as recipient delivery.

## Boundaries and known gaps

Installed `dispatch_preflight.py` and `commit.py` are not in this source tree. Their host contracts must be verified before installation. Installed `--check-model` is High-only, so an Instant-authorized task cannot use that check; do not suppress checks or change model authorization. Approval owner and observer contracts cannot be inferred from filenames. The exact ledger stamp grammar and process start identity need independent installed-host acceptance. No completion receiver is installed by this patch. In particular, a successful detached ledger launch is not proof of watcher readiness or handback. This is not an unattended completion solution.

## Installation / rollback mapping (documentation only)

Installer, if separately authorized, must map source dispatch_next.py to the intended host path; map explicit installed preflight/queue/commit/observer entrypoints and confirm their CLI behavior, identity, permissions and existing approval owner. Verify process handles and exact ledger stamps on host, then independently test fail-closed outcomes before enabling anything. Rollback: disable invocation at the caller, retain receipts and ledger audit evidence, and restore the prior host configuration; never delete evidence or resend unknown deliveries.

## Tests

Requested command: `(cd lab && python3 -m unittest -v tests.test_dispatch_next)`. The source is designed for temporary-file and mocked-subprocess tests. CI and installed-host execution must be reported separately.
