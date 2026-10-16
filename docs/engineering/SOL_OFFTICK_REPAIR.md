# SO1-r0: SOL off-tick fail-closed provenance (engineering; not deployed)

## Evidence and actual code path

Immutable fixture: `lab/tests/fixtures/sol_offtick_original.json`, not modified. Its depth receipt /fapi/v1/depth (audit 135971, received 2026-10-16T04:22:31.153082Z, E=1792124551070, T=1792124551061) has ask level index 2 (zero-based) `105.5290` quantity `1.00`. Against the *original* PRICE_FILTER tickSize string `0.0100`, exact Decimal remainder is `0.0090`. The observed bookTicker bid `105.5000`/ask `105.5100` is independently legal; the mark `105.51000000` is valuation, not execution depth.

Existing dispatch path: `paper_runtime_v5.collect_markets` (inherited by v7) calls `paper_market.book_event` for structural/source checking, then `validate_sources`, then `paper_runtime_v2.deliver_markets` -> `emit` -> `SimBroker.on_event` -> `SimBroker._validate_event`. The last validator visits every bid and ask, checks exact Decimal price modulo instrument tick, and rejects on the *first* illegal price. Existing generic error `ValueError: off-tick quote` was the throw-site. This is a static code-path conclusion: the original fixture's adjacent poll_error is NOT a stack trace proving this specific level generated audit 135974. The new runtime diagnostic is emitted only from an actual broker throw and matches the complete persisted receipt's original bids/asks, symbol, and source time (or says no match).

The independently re-fetched public reference reported unchanged `0.0100`, so old-cache age does not establish stale filters. Reference record received 2026-10-15T21:54:22.336399Z; neither exchangeInfo serverTime nor source timestamp is substituted for local receive time.

## Minimal changes

- Broker rejection identifies validator, side, zero-based level, original price string, exact tick and remainder. The same ValueError still rejects the full event, without rounding, deleting levels or changing valid-path calculations.
- V2 runtime `emit`, also inherited by the SOL runtime, records a causal `broker_offtick_rejection` audit only when the broker actually rejects. It includes dispatch/receipt/source timing, source age at dispatch, cached reference receipt/age and canonical SHA256, and optional exact matching previously persisted depth receipt timing/hash. Hashes identified as `canonical_runtime_receipt_sha256` are **not** claimed equal to the original external fixture's `payload_sha256` or `receipt_sha256`; those represent different envelopes/canonicalization.
- The unchanged invalid book and unchanged filter remain blocked. No reference-refresh or depth-recollection path is added absent evidence that it can produce legal source data safely. Existing bounded stale-source refresh and all freshness/future/arrival/risk/fee controls remain as before.

## Verification boundaries

`python3 -m unittest -v tests.test_sol_offtick_repair` from `lab/` is the focused test command. The tests exercise original-golden Decimal arithmetic, deeper levels, legal-depth unchanged acceptance, changed filter arithmetic (not permission to trade), non-executable off-grid mark, source/future/stale gates and runtime throw-site audit association. Remaining acceptance requires independent old/new valid-path fill/cost/risk/ledger differential, full repository CI at precise final head and isolated public probe with genuine original hashes, receipt and dispatch timing. None is inferred merely from fixture replay.

No account state, strategy, deployed runtime, source fixture, private API or credentials altered. Main CIO owns separate acceptance and deployment gates.
