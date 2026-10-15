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


## CA1-r1 delta (candidate; Main review required)

* Corrected `JSONDecoder.raw_decode()` end-offset handling; only whitespace or the precise card-local close label may follow the single parsed operation.
* Corrected post-click DOM readback to use the same retained node/turn/invocation without the pre-click pending gate; no second click during readback.
* Normal new-conversation jobs may retain the homepage transport URL. Positive active-instruction and `CONFIRMED` send receipt must both identify the identical exact conversation; live snapshot must still match it.
* Every invocation gets a profile-local, fsynced, atomically replaced SHA256-keyed ledger entry before the potential click. Any existing entry, including an unknown previous click or a previously verified approval, blocks reentry across processes. The global flock covers the ledger and strict-to-diff transaction. Environment variables are used only to resolve a profile root, not to claim lock ownership. The ledger is append-only in intent: do not clear an unresolved entry automatically.
* **Inherited policy limitations remain:** strict detection-rule exceptions for selected self-contained `re.compile` or `SECRET` lines are not proof of secret safety. The original self-regex `TOKEN_LIKE` rejection and diff-clean `DIFF_SIZE` for more than 200 changed lines remain. There is no third fallback, payload splitting or obfuscation.
* Tests `test_C01` … `test_C28` now exist and invoke actual `DOM_JS` in isolated Chromium via Python Playwright. These are unexecuted candidate tests in this workspace; current fixture driver JS file is a static source check, not a Node browser runner. Tests do not establish authenticated live DOM behavior. Main must execute all tests and inspect coverage gaps and platform prerequisites before acceptance.


## CA1-r2 measured fixes / verification boundary

* Collapsed inspect and bind both initialize `opText: null`. The first details read can now establish card-local details rather than failing on `undefined !== null`; identity checks and fingerprints remain unchanged.
* Candidate `evaluate` and `snapshot` normalize arbitrary adapter/browser/Playwright exceptions as `InfraError`, without exposing raw content-bearing JavaScript exceptions in transaction results. An error after the persisted click intent is `UNKNOWN`, never `NO_PENDING` or success.
* Browser tests launch Chromium using `COLLAPSED_APPROVAL_CHROMIUM`, platform PATH candidates, then the Playwright bundled executable. They fail loudly on missing browsers rather than skipping; the Playwright context manager is closed correctly on launch failure and teardown.
* The synthetic allow-once fixture mutates `this.closest('[data-approval-card]')` rather than the first card in the document; an older completed card cannot absorb the pending card's transition. Test faults are keyed to the substituted `action = "..."` assignment, not incidental code text in the full JS.
* Added matrix variants for inherited strict secrets and identifiers, diff size/sensitive lines, malformed tool payloads, admission and fingerprint identity, multiprocessing flock contention, error transport, and concurrent direct-helper/auto_allow CLI no-card behavior.
* **Still unverified/incomplete**: candidate test execution on this exact source head, C22 competing approval processes holding lock from fallback through readback, full C23 carrier/poll/drive/profile concurrent entrypoint competition, and authentication-live DOM. The no-card CLI smoke is not equivalent to a pending-card race. Main acceptance must remain separate from source completeness.

Strict self-regex `TOKEN_LIKE`, inherited detection-rule exceptions (which do not certify secret safety), and diff-clean `DIFF_SIZE` for more than 200 changed lines remain exactly bounded. No third fallback or content-splitting workaround is authorized.
