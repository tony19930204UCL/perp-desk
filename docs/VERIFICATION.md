# Reproducible verification

Python stdlib unittest and Node.js are used. No private exchange credentials needed.
Run only in a fresh clone or isolated test workspace, not against production data.

```bash
python3 -m unittest discover -s automation/tests -v
cd lab
python3 -m unittest discover -s tests -v
```

The first fresh export ran all 155 tests and failed (two missing public-receipt evidence fixtures and one scheduler assertion tied incorrectly to clone path). Actual RED log: `evidence/sync/export_paper_suite.txt`. The two exact public-source fixtures were inspected before adding a narrow allowlist. The scheduler test now verifies the literal deployed profile binding, independent of clone location, without changing scheduler scripts or runtime core. All three regressions passed in isolation (`export_portability_green.txt`). A final complete export-suite acceptance still requires a fresh rerun.
Staged health uses relative dependencies via the exported lab layout. Staged batch_time suites have their own source/tests and may need fixture path support.
Raw live DB/audit exports are deliberately excluded. Historical version references may point to locally retained evidence that is not published. Missing repo evidence is a known review limit, not a successful acceptance.
CI runs the same commands from a clone, read-only token, no deployment and no trading.
`evidence/sync/red*.txt` and green logs are actual TDD executions, not fabricated outcomes.
