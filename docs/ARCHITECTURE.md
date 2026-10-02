# Architecture

```text
Binance public REST
   -> paper_market.py (raw source / receipt / funding contracts)
   -> paper_runtime_v2.py (context, forward cutoff, freshness, orchestration)
       -> signals_v2.py (strategy, durable signal state)
       -> paper_sizing.py (quantity and risk checks)
       -> sim_broker.py (PAPER orders, fills, fees, funding, ledger)
       -> isolated live SQLite namespace + read-only snapshot
snapshot -> dashboard.py / brief.py
snapshot + process identity + work + capacity -> health_watchdog.py
source allowlist -> automation/review_sync.py -> private GitHub mirror
```

REST polling is not tick-complete websocket execution. Raw source and local receipt are different clocks.
Signal and broker namespaces are distinct. Review cross-store commit/restart windows and idempotency.
Public historical candles are indicator context, never fabricated forward fills.
PAPER is intended to prepare shared market, risk, lifecycle, accounting, audit and operations contracts for a future execution adapter.
No private exchange adapter is claimed complete. Real permission, private stream recovery, ambiguous execution, actual fees and reconciliation require separate acceptance.
GitHub sync is one-way export only. It does not deploy code, restart runtime, change risk, or write trading SQLite.
