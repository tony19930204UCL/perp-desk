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

## r1 native binding correction (supersedes initial illustrative results layout)

The real Maker PR75 `BoundedReport` writes `cells/00_strategy.json`
and `cells/00_matched.json` through index 47, while delay2 and cost1_5x
are separate; `results.json` and `manifest.json` are emitted only by
`finish()`. No native `attempt_id` is present in manifest. A trusted
private `results.json` *binding document*, not the native producer output,
must provide `root` as a relative directory **directly containing** native
artifact files; `attempt` is an independent authenticated launch identifier;
`expected` has the canonical producer `input_manifest_sha256`,
`config_sha256`, and full `source_sha256` filename-to-hash map recorded
by the authorized launch. If these expectations are not independently
available, metrics are unknown, not an invented 0/48. Never move or rewrite
Maker artifact files. Partially written complete JSON cell pairs can be
observed only under the same trusted expected identity.

Directive is an authoritative document, not a manually invented summary:
`active_owners` contains entries with `item`, `actor`, `scope`,
`product` and `pending_decisions` is either an authoritative integer or
list of decisions with status `PENDING`/`WAITING_HUMAN`.
Stage has `products` keyed by maker/collectors/relay. Each route step's
`verified_steps` and `evidence` are installation/acceptance evidence,
not source-candidate CI; `active_step` alone cannot animate without a
matching product-specific authenticated process.

Every launch and adoption entry must associate `product` with the
canonical route ID. A `wrapper:true` entry is not counted as a product
child. Existing authenticated operator baseline may establish expected
PID/start/boot/argv/cwd; live `/proc` may only *compare*, never supply,
those expectations. The live green notification consumer can be labelled
waiting on the official scheduler; earlier retired heatmap agents cannot
be promoted into active capture workers. No installed collector evidence
implies fourteen days of effective coverage.

### Browser entrypoint

`OWNER_METRO_CHROME=/path/to/existing/chromium cd lab && python3 -m unittest -v tests.test_owner_metro_ui.MetroUI.test_real_chromium`

If using an environment assignment, set it before the Python process, e.g.
`cd lab && OWNER_METRO_CHROME=/path/to/existing/chromium python3 -m unittest -v tests.test_owner_metro_ui.MetroUI.test_real_chromium`.
This is an actual CDP browser runner, not a static substring check.

## r2 exact CI regression and acceptance entrypoints

The previous head `e7c6498261de0bba9626ef7e6a24040b31a1533f`
had one focused failure: `MetroUnit.test_large_ledger_and_source`
expected seven pending decisions from an *undated* directive. The fixture
now supplies `updated_at:1792065600` for the frozen test clock.
Undated or aged directives remain unavailable for current decisions.

Additional isolated commands from repository root:

```sh
cd lab && python3 -m unittest -v tests.test_owner_metro_proc tests.test_owner_metro_cells
cd lab && python3 -m unittest -v tests.test_owner_metro tests.test_owner_metro_live tests.test_owner_metro_ui
cd lab && python3 -m unittest discover -s tests -v
python3 -m unittest discover -s automation/tests -v
```

The first three fixture JSON documents under `lab/tests/fixtures/`
are synthetic report *data*, not operator bindings. They are loaded by
`test_owner_metro_cells`, which synthesizes actual native
`cells/NN_strategy.json`, `cells/NN_matched.json`, `results.json`
and `manifest.json` inside a temporary root. They must not be used as
production sources or interpreted as trading acceptance.

Browser test `tests.test_owner_metro_ui.MetroUI.test_real_chromium`
searches the explicit `OWNER_METRO_CHROME` or `CHROMIUM_PATH` executable,
then existing `~/.cache/ms-playwright/chromium-*` binaries and native
Chrome/Chromium names. Missing browsers cleanly SKIP. A SKIP is never an
accepted viewport proof; operator must record genuine 1280 and 390 CDP
evidence separately and verify final exact-HEAD push and PR CI success.
No update to `.github/workflows/ci.yml` is needed: its existing
`python3 -m unittest discover -s tests -v` discovers `test_owner_metro*.py`.
