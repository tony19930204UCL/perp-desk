# Mechanical authorized public GitHub synchronization

Source profile: `/home/chihcheng/.hermes/profiles/perp-desk`.
Mirror: `repo_sync/mirror`. Source documents: `repo_sync/docs`.
Authorized identity: `tony19930204UCL/perp-desk`, exact HTTPS remote `https://github.com/tony19930204UCL/perp-desk.git`.
The mirror is read-only review output, never the live deployment source. Main belongs to mechanical synchronization; proposals belong in separate branches/issues.

## Authorization and acceptance

The exporter defaults to PRIVATE and rejects public repositories. Public export requires BOTH `--visibility public` and `--authorize-public-repo` equal to `--owner-repo`. The profile wrapper explicitly pins this authorization to the identity above. Metadata must be a mapping with exact full_name and a genuine boolean private=false for public mode; private mode requires private=true. A missing, malformed or changed identity/visibility fails closed. No repository visibility is changed by this tool.

As of this candidate preparation, profile cron `2d2db10436e9` is PAUSED pending independent parent acceptance. This patch does not resume cron, push GitHub, deploy live code or restart trading. The minute schedule with no_agent=true is configured, not claimed active. No systemd timer or machine-off execution is guaranteed. Parent must verify the exact job before any explicit resumption.

## Security and transaction

Changed source fingerprint must settle at least 15 seconds. Every tick rechecks the exact authorized visibility/identity and fetch/push URLs, fetches all advertised refs into a scanner-only namespace (never local main), scans current exported bytes and local/all-ref/reflog history using real gitleaks default rules, commits only changed owned files, pushes without force, then reads back the exact remote SHA.
Scanner policy is outside the export. `--gitleaks-ignore-path /dev/null`, `--ignore-gitleaks-allow` and sanitized GITLEAKS environment prevent repository-level suppression. Git hooks, config, environment, ownership/permission, symlink/hardlink and full-tree byte checks remain enforced.
No change produces no commit. Failures record blocked with last success preserved. Remote-main divergence or unowned history requires manual review, never reset, overwrite or force-push.

## Export/privacy policy

`repo_sync/status.json` and ownership ledger are local telemetry excluded from upload. Credentials, gateway settings, chat/session files, runtime DBs, data/shared/probe namespaces, raw snapshots and unreviewed lab/evidence remain excluded. No allowlist destinations were expanded for public mode.
Public mode omits context/SOUL.md and obsolete context/SPEC.md, and replaces only the personal/account/environment section of context/AGENTS.md with an explicit omission marker. Engineering rules, PAPER constraints, versions, AI review guide, source, tests and reviewed public-receipt fixtures remain available. Source profile originals are untouched. The manifest hashes the actual public, privacy-filtered bytes, not the omitted originals.
Removing metadata from a new snapshot does NOT remove it from preexisting Git history. Credential scanning does not certify absence of personal or operational information; contextual history and review limitations require separate parent assessment.

## Parent-only immediate synchronization

After independent review and current/history rescan, invoke automation/review_sync.py with the exact profile, mirror, owner, HTTPS URL and both public opt-in flags. `--force` bypasses settling ONLY, never security gates. The profile wrapper does not forward arbitrary CLI arguments. Do not run either entry during local-only preparation.
A snapshot commit is version control, not test acceptance, trading permission or deployment approval. Live trading is NOT deployed/enabled by this export; PAPER deployment history is separate and not live telemetry.
