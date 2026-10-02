# Mechanical private GitHub synchronization

Source profile: `/home/chihcheng/.hermes/profiles/perp-desk`.
Mirror: `repo_sync/mirror`. Source documents: `repo_sync/docs`.
The mirror is disposable review output, never the live deployment source.

A profile-specific systemd user timer will execute a plain Python tick (no LLM).
Changed source fingerprint must settle at least 15 seconds. Every tick rechecks private remote identity, scans working tree and Git history with gitleaks, commits only changed owned files, pushes without force, then reads exact remote SHA.
No change produces no commit. Network/auth/scanner failures record `blocked` with last success preserved and retry on later ticks. Remote-main divergence requires manual reconciliation, never overwrite.
`repo_sync/status.json` is local telemetry excluded from upload.
Exclusions: credentials, gateway settings, chat/session files, runtime DBs, data/shared/probe namespaces and unreviewed lab/evidence.
No WSL shutdown or machine-off execution guarantee. Enabled user timer runs when its WSL user manager is active. This is not a trading autostart mechanism.

Manual immediate export uses the exact same mechanical script with `--force` (bypasses settling only, never security gates).
Do not write into mirror directly. Edit source docs/code, then the timer uploads the allowlisted change. A snapshot commit is version control, not test acceptance or deployment approval.
