# Maker X2 accounting handback
Engineering only; not deployed. Do not merge or push main: main is a one-way local mirror.
The operator transports the existing external ChatGPT candidate verbatim, and is not its author.
The seeded accounting.py is NOT accepted and has no executed test evidence.
API: replay(initial_cash, events), LedgerError; offline stdlib Decimal short-fill ledger.
Fees: maker 0.0002, taker 0.0005. Funding uses remaining signed quantity, actual mark and rate.
Required tests: split short entries at different prices; half exit and remaining basis;
per-leg fees; positive and negative short funding; funding after half exit on remaining qty;
same-timestamp funding before versus after exit; flat cash; open positions not fake-closed;
duplicate event and settlement IDs; seq gaps/backwards timestamps; over-close/nonpositive qty;
nonfinite and float rejection; gap exit uses actual fill price.
Expected values must be independent explicit Decimal literals, not production-derived summaries.
Run from this directory: python3 -m unittest discover -s tests -v
Only accounting.py, tests/test_accounting.py, README.md and .relay/mailbox/maker-v3-x2.json are writable.
Only fix accounting.py for demonstrated bugs in the original specification, with a diff.
A01-A16 and the full X2 engine remain unverified. No 24-grid research, paper or live deployment.
