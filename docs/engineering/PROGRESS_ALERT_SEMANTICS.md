# Progress alert semantics (PA1)
Pure stdlib module. Import `from engineering.progress_semantics import evaluate, parse_output`.

`parse_output(text: str) -> tuple[bool,str]` reads only the first nonempty Response line for failure; `[SILENT]` alone is successful.

`evaluate(item: dict, now: aware datetime|ISO timestamp, schedule: dict, evidence: dict, state: dict, cooldown_hours: float=6) -> dict`.

Schedule: timezone (IANA), frequency daily/weekly/hourly/every_n_minutes, hour, minute, weekday (Monday=0), minutes for intervals, job_id, grace_minutes. Evidence: runs with job_id, at, ref, content, status, script_hash; files with name/content/at; journal list. State: coverage='not_implemented', required_artifacts list, expected_hash, disabled, state_stopped, window_invalidated, last_alerts map keyed item-id:reason-code with timestamp. Caller persists alert history only after actual delivery. All inputs remain unchanged.

Result: state, reason_code, evidence_ref, evidence_age_hours, alert, suppressed_by_cooldown, suggested_kind. Reclassification is advisory only. No plugin installation, state writes or owner acceptance implied.

Limitations: journal is carried as an input but not independently validated for per-item coverage; explicit coverage state must be supplied. DST and missing previous-slot evidence require local integration validation. The repository CI does not discover this module's tests; run `python3 -m unittest discover -s engineering/progress_semantics/tests -v` from repo root.
