# Relay reliability spec (acceptance = observable behaviour under fake-transport drills)
Root causes observed 2026-10-25 (real incident, PR82 R4 graph builder):
1. Long instruction renders as a FOLDED user bubble ("…顯示較多"); exact-bubble readback never matches, so the send is recorded SEND_UNKNOWN although it was delivered. Everything downstream (approve, release_revise, next instruction) then refuses.
2. The worker flow never clicks the GitHub "Allow once" card. The task sat ~3h blocked on one card; the tracker then expired silently.
3. release_revise requires phase SENT_CONFIRMED, so a SEND_UNKNOWN claim can never be released for a REVISE; PRODUCT_ALREADY_CLAIMED follows.
4. Tracker expiry/pending-card are not surfaced anywhere an agent is woken by.
5. A job needs a hand-made job.json with an explicit chat id.
Required fixes (each with offline unittest using fake transport/page snapshots; mutants must be killed):
A. Positive reconciliation of folded bubbles: confirm only if exactly one nonzero-size turn-bound bubble whose full message subtree (not UI controls like the show-more button) equals the complete normalized prompt; never trim arbitrary suffixes; never resend.
B. Scoped single-use card approval inside the tracker loop (reuse the semantics of chatgpt_card_approver.py: expand details, require named allowlisted path or repo+branch, no secrets, no always/remember, one click, no retry, readback). Refuse and surface CARD_REFUSED otherwise.
C. release_revise/advance works after a positively reconciled send; keep all existing guards (generation, verdict binding, no release while approval intent is open).
D. Tracker state machine: phases WAIT_LOGIN/SENT/CARD_PENDING/CARD_APPROVED/HANDBACK/EXPIRED; EXPIRED and CARD_PENDING write a machine-readable file that scripts/engineering_wake.py-style monitors can read (state file with phase, since, instruction id).
E. make_job helper creating a valid job.json for an explicit chat url, repo, branch, PR, allowlist, expiry.
F. A drill test that runs: send (folded bubble) -> reconcile -> card appears -> auto-approve -> mailbox handback -> REVISE release -> second instruction, all with fakes, plus negative cases (secret in card, path outside allowlist, second card, click ack lost).
