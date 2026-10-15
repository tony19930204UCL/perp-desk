# Collapsed approval transaction candidate

This directory is an **engineering source candidate**, not deployed ChatGPT profile code. The read-only sanitized references under `docs/engineering/collapsed-approval-source/` map to the same relative filenames here. Main owns verification, source deployment, schedule/CLI readback and any authenticated-live observation. There is **no live folded-card proof** and no approval of the paused AP3 unknown send.

## Integration and non-goals

`approval_transaction.approve(job_dir, mode="strict"|"diff"|"auto")` acquires a shared advisory flock at `<profile>/cache/approval-transaction.lock`. `HERMES_HOME` explicitly selects the profile root; otherwise root is derived from `cache/scratch` ancestry, or the user-home default. The profile must be a trusted local directory. The old `auto_allow.lock` is deliberately not held by the watcher; all approval callers use the transaction lock instead. Transport/request-file locks remain inside the transaction. Approver code never sends, navigates or edits user input.

All UI reads require an exact confirmed send receipt, active instruction, matching task and pinned `https://chatgpt.com/c/<id>` URL plus a timezone-aware, unexpired deadline. The DOM selector assumptions are deliberately conservative: assistant/user `data-message-author-role`, card `[data-approval-card]` or `[role=group]`, `data-invocation-id` or `data-tool-call-id`, known exact Traditional Chinese labels and card-local allow-once action. These selectors **have not been verified against an authenticated live folded card**. Unknown locale, ambiguous cards or controls, missing invocation, detached/replaced nodes, unowned dialogs and unexpected state fail closed. The details portal requires explicit `aria-controls` linking to the originating card. No global approval button index or whole-conversation grant is permitted.

`auto` performs strict followed by diff **under the same lock**, with no unscoped fallback. The admission, fingerprint and expiry are reread before the click; readback must prove a same-card completed state. Unknown clicks and failed readbacks are `UNKNOWN`, never retried in that transaction. The receipt contains a SHA256 fingerprint, not content.

## Deployment mapping

* `engineering/relay/approval_transaction.py`: new shared transaction dependency (must be deployed alongside all callers)
* `engineering/relay/relay-allow-card.py`, `relay-allow-if-diff-clean.py`, `auto_allow.py`, `relay-poll.py`, `relay-drive.py`, `profile_runner.py`, `engineering_instruction_carrier.py`: runtime helper/entrypoint counterparts
* `engineering/relay/legacy_cdp_bridge.py`: retained baseline transport with per-request lock
* `engineering/relay/engineering_github_inbox.py`, `engineering_handback_contract.py`, `main_verdict.py`, `queue_runner.py`: retained reference implementations

**Open blocking verification:** the full C01–C28 executable candidate-DOM matrix, process/race tests, actual CLI integration, independent CI and authenticated-live observation were not executed in this session. These gaps prohibit treating this candidate as accepted or deployable. Existing test file covers only initial policy/admission regression cases. Never equate a fixture PASS with live approval proof.
