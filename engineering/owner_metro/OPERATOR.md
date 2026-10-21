# Owner Metro · isolated, uninstalled source handback

**Not deployed, not accepted, not a trading control.** The original Dashboard V2
loopback host is extended, not replaced. Source is branch-only until Main CIO
verifies the exact immutable release and its protected-process identities.

## Fixed local binding contract

Configure only `--profile-root` (absolute trusted non-symlink directory).
Place `owner_metro/bindings.json` **privately** under that root. It contains
`{"schema_version":1,"sources":{"directive":"owner_metro/directive.json","stage":"owner_metro/stage.json","heartbeat":"owner_metro/heartbeat.json","launch":"owner_metro/launch.json","adoption":"owner_metro/adoption.json","results":"owner_metro/results.json","liquidations":"owner_metro/liq.json"}}`.
These names are illustrative synthetic relative paths, not machine paths;
no binding is accepted from an API query. Each component is strictly
alphanumeric, underscore, dash or dot, forbids dot/dot-dot, symlinks and
traversal. Missing documents remain unknown, never success.

- `directive`: `{"owner":"PUBLIC_ALIAS","pending_decisions":2}`.
  Owner and pending count come from this source, not from hardcoded identities.
- `stage`: `{"maker_phase":"research","lease_status":"unknown","output_status":"pending","products":{"maker":{"active_step":1,"current":"research","next":"witness","verified_steps":[],"evidence":{}}}}`.
  A step is Done only if its indexed evidence has `{"accepted":true,"ref":"SAFE_REF"}`.
  Engineering CI is not installed runtime verification.
- `heartbeat`: `{"at":"2026-10-21T12:00:00Z","status":"observed"}`.
  No timestamp is changed by this monitor. `liquidations` is
  `{"event_count":0}` only if the genuine source explicitly recorded zero.
- `launch` and `adoption`: each has a `processes` array of launch identities
  `{id,pid,start_ticks,kind,boot_id,argv_sha256,cwd}`, with independent
  adoption of matching id/PID/start-ticks. These trusted expected values must
  be written by authorized launch/adoption machinery, **never copied from
  observed /proc by the monitor**. It reads /proc stat, boot identity,
  raw cmdline SHA256 and cwd for equality. Invalid/missing/mismatched
  expectations never establish liveness. No argv bytes are returned.
- `results`: `{"root":"owner_metro/research_outputs","attempt":"SYNTHETIC_ATTEMPT"}`.
  The output root must be inside trusted profile. Attempt subdirectory
  contains Maker PR75 `manifest.json`, `results.json` and `cells/*`
  checksummed artifacts. Manifest must independently identify attempt via
  `attempt_id`, source hash map, input manifest hash and config hash.
  This explicit attempt identity is an *operator-side wrapper extension*;
  the shipped Maker reporter does not emit `attempt_id` itself. Without a
  trustworthy adoption wrapper there is **no admitted completed cell**.

## Native isolated tests / staged browser checks

From repository root: `python3 -m unittest discover -s automation/tests -v`.
From `lab`: `python3 -m unittest discover -s tests -v`.
Focused: `cd lab && python3 -m unittest -v tests.test_owner_metro tests.test_owner_metro_live tests.test_owner_activity tests.test_owner_activity_browser`.
The existing `test_owner_activity_browser` runs installed Chromium if available.
Desktop 1280 and mobile 390 computed CSS/DOM evidence still requires an actual
Chromium run. No screenshot or CI was performed by this handback.

Stage a complete immutable release on a spare loopback port, retaining original
`dashboard_v2_reader.py` and `dashboard_v2_model.py`. Run native Linux and
Windows GET/HEAD/host/origin/traversal/CSP and 1280/390 viewport checks.
Record all source hashes, before/after protected PID + start identities,
backup/rollback invocation and both exact-head push and PR CI run IDs.
Only operator may switch the dedicated dashboard listener after acceptance.
No restart of trading, collector, lease or the existing shared dashboard.

## Explicit remaining acceptance gaps

At source delivery: real Chromium 1280/390 screenshots, source restoration
through two 3-second poll intervals, and actual process/lease cross-platform
proof have not been independently run. Source summaries cannot establish
historical 14-day coverage or unattended-loop acceptance by themselves.
