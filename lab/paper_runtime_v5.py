"""Symbol-parameterized F1H paper runtime with configurable observation.

Differs from paper_runtime_v4.py apart from traded symbol:
- Parameterized Traded Symbol: Strategy symbol bound to config strategy.symbol
  (BTCUSDT, SOLUSDT, XRPUSDT, or ETHUSDT for tests) rather than hardcoded ETHUSDT.
- Detector Binding: signals_v5.Detector registered with the single traded symbol.
- Config Hash Allowlist: Accepts canonical byte hashes of the three F1H configs
  (BTC/SOL/XRP) and paper_config_v4.json (ETH equivalence), rejecting modified configs.
- Configurable XAUUSDT Observation: v4 always observes XAUUSDT; F1H makes XAU
  configurable via observe_xau and OFF by default.
- Symbol-Isolated Signal Routing: route_signals only processes signals for traded symbol.
- Preserved Risk/Validation Gates: 8-hour funding validation, 15000ms late signal gate,
  2x cost gate, and 43200000ms max holding exit preserved without relaxation.
- Candidate Engine Identification: Engine snapshot reports paper-engine-v5 candidate.
"""
import argparse, fcntl, hashlib, json, os, sqlite3, time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from paper_market import decimal_string, fresh_source, instrument, book_event, mark_event, finalized_funding, receipt_ms
from paper_runtime_v2 import RuntimeClient
from paper_runtime_v3 import publish, canonical
from paper_runtime_v4 import PaperRuntime as BaseRuntime
from paper_sizing import size_long
from signals_v5 import Detector
from sim_broker import SimBroker, InstrumentSettings, ExecutionModel, RiskContract, Intent
from storage_protection import StorageGuard, load_policy

D = Decimal


def _canonical_config_hashes():
    lab = Path(__file__).resolve().parent
    names = ("paper_config_f1h_btcusdt.json", "paper_config_f1h_solusdt.json",
             "paper_config_f1h_xrpusdt.json", "paper_config_v4.json")
    return {n: hashlib.sha256((lab / n).read_bytes()).hexdigest() for n in names if (lab / n).exists()}


class PaperRuntime(BaseRuntime):
    CONFIG_HASHES = _canonical_config_hashes()
    ALLOWED_CONFIG_HASHES = set(CONFIG_HASHES.values())
    DETECTOR = Detector
    EXTRA_SOURCES = BaseRuntime.EXTRA_SOURCES + ("paper_runtime_v5.py", "signals_v5.py")
    INTERVAL_MS = 3600000
    INTERVAL_NAME = "1h"

    def __init__(self, root, config_path, *, client=None, clock_ms=None, sleep=None,
                 monotonic=None, fixture=False, storage_policy=None, storage_probe=None,
                 observe_xau=None):
        self.root = Path(root).resolve()
        live = Path(__file__).resolve().parent / "data" / "paper"
        if self.root == live or live in self.root.parents:
            raise ValueError("reserved live namespace")
        self.root.mkdir(parents=True, exist_ok=True)

        self.storage_guard = None
        self._storage_status = None
        self._storage_stop_after_publish = False
        self._storage_cycle_start_bytes = None
        if storage_policy is not None:
            policy = load_policy(storage_policy) if isinstance(storage_policy, (str, Path)) else storage_policy
            self.storage_guard = StorageGuard(self.root, policy, probe=storage_probe)

        self.config_bytes = Path(config_path).read_bytes()
        self.config = json.loads(self.config_bytes)
        self.config_hash = hashlib.sha256(self.config_bytes).hexdigest()
        if self.config_hash not in self.ALLOWED_CONFIG_HASHES:
            raise ValueError("only immutable F1H configurations are implemented")

        c = self.config
        if c.get("approved") is not True or c.get("mode") != "paper":
            raise ValueError("explicit approved PAPER configuration required")

        self.symbol = c.get("strategy", {}).get("symbol")
        if not self.symbol or not self.symbol.endswith("USDT") or self.symbol not in ("BTCUSDT", "SOLUSDT", "XRPUSDT", "ETHUSDT"):
            raise ValueError(f"unsupported strategy symbol {self.symbol}")

        self.observe_xau = bool(observe_xau if observe_xau is not None else c.get("observe_xau", False))
        lab = Path(__file__).resolve().parent
        all_sources = ("paper_runtime_v2.py", "signals_v2.py", "paper_runtime.py", "signals.py",
                       "paper_market.py", "paper_sizing.py", "sim_broker.py", "perp_collector.py") + self.EXTRA_SOURCES
        self.source_hashes = {n: hashlib.sha256((lab / n).read_bytes()).hexdigest() for n in all_sources if (lab / n).exists()}

        self.clock = clock_ms or (lambda: int(time.time() * 1000))
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic
        self.client = client or RuntimeClient()
        self.fixture = fixture
        self.broker = None
        self.db = None
        self.fd = os.open(self.root / "runtime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError("PAPER runtime already locked") from None

        try:
            self.db = sqlite3.connect(self.root / "runtime.sqlite3")
            self.db.execute("CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL)")
            row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
            self.state = json.loads(row[0]) if row else dict(
                config_hash=self.config_hash, fixture=fixture, forward_start_ms=self.clock(),
                cursor=None, errors=0, gaps=0, last_success_ms=None, markets=[], latest_error=None,
                warmup=0, history=[], diagnostic="warmup")
            if self.state["config_hash"] != self.config_hash or self.state["fixture"] != fixture:
                raise ValueError("immutable config/fixture namespace conflict")

            self.state.setdefault("strategy_start_ms", self.state["forward_start_ms"])
            deadline = self.state["strategy_start_ms"] + c["research"]["window_ms"]
            if self.state.get("research_window_version") != c["version_id"]:
                self.state["research_deadline_ms"] = deadline
                self.state["research_window_version"] = c["version_id"]
            self.state.setdefault("research_deadline_ms", deadline)
            self.state.setdefault("strategy_fill_baseline", 0)
            self.state.setdefault("strategy_ledger_baseline", 0)
            self.state.setdefault("strategy_signal_baseline", [])
            self.save()

            self.detector = self.DETECTOR(
                self.root / "signals.sqlite3", c["version_id"],
                datetime.fromtimestamp(self.state.get("strategy_start_ms", self.state["forward_start_ms"]) / 1000, timezone.utc),
                {self.symbol: "crypto"})

            if (self.root / "broker.sqlite3").exists():
                with closing(sqlite3.connect(self.root / "broker.sqlite3")) as b_db:
                    b_row = b_db.execute("SELECT payload FROM sim_broker_state WHERE singleton=1").fetchone()
                if b_row:
                    saved = json.loads(b_row[0]); inst = saved["meta"]["instruments"][0]
                    self.ensure_broker(dict(symbol=inst["symbol"], maker_fee=inst["maker_fee"], taker_fee=inst["taker_fee"],
                                            qty_step=inst["quantity_step"], tick_size=inst["tick"], min_notional=inst["min_notional"],
                                            max_qty=inst["max_quantity"], min_qty=inst["min_quantity"], category=inst["category"]))
        except Exception:
            self.close()
            raise

    def ensure_broker(self, spec):
        c = self.config
        s = InstrumentSettings(spec["symbol"], D(spec["maker_fee"]), D(spec["taker_fee"]),
                              D(spec["qty_step"]), D(spec["tick_size"]), D(spec["min_notional"]),
                              D(spec["max_qty"]), D(spec["min_qty"]), spec["category"])
        if self.broker:
            if self.broker.instruments[self.symbol] != s:
                raise ValueError("public instrument filters changed; new review required")
            return
        e = c["execution_model"]
        self.broker = SimBroker(
            self.root / "broker.sqlite3", initial_cash=D(c["initial_equity_usdt"]), instruments=[s],
            execution=ExecutionModel(e["latency_ms"], e["exit_slippage_ticks"], None, e["entry_slippage_ticks"], e["exit_slippage_ticks"]),
            risk=RiskContract(True, c["risk_version"], D(c["max_loss_per_trade_usdt"]), D(c["max_daily_loss_usdt"]),
                              D(c["max_effective_exposure_x"]), c["max_positions"], D(c["total_loss_limit_usdt"]), True),
            version_id=self.state.get("account_version_id", c["version_id"]), forward_start=self.state["forward_start_ms"])
        if self.state.get("gap_open"):
            self.cancel_entries_for_gap()
        with closing(sqlite3.connect(self.root / "signals.sqlite3")) as db:
            intents = [json.loads(row[0]) for row in db.execute("SELECT intent FROM h1_signals WHERE version_id=?", (c["version_id"],))]
        handled = self.state.setdefault("handled_signals", {})
        for signal in intents:
            sid = signal["signal_id"]
            if sid not in handled and "order:" + sid in self.broker.orders:
                handled[sid] = dict(status="submitted", target=signal["reversion_target"])
                self.audit(dict(type="recovered_committed_submission", signal_id=sid))
        self.save()

    def funding(self, spec):
        self.ensure_broker(spec)
        now = self.clock()
        last = self.state.get("funding_checked_ms")
        if last is not None and 0 <= now - last < 30000:
            return
        adjustments = self.fetch("/fapi/v1/fundingInfo")
        matched = [row for row in adjustments["payload"] if row.get("symbol") == self.symbol]
        if len(matched) > 1 or (matched and matched[0].get("fundingIntervalHours") != 8):
            raise ValueError("unsupported funding interval requires explicit review")
        receipt = self.fetch("/fapi/v1/fundingRate", dict(symbol=self.symbol, limit=1000,
                             startTime=max(0, self.state.get("funding_cursor", self.state["forward_start_ms"] - 28800000))))
        rows = receipt["payload"]
        events = finalized_funding(receipt, self.symbol, self.state["forward_start_ms"])
        observed = receipt_ms(receipt)
        if not rows or len(rows) >= 1000:
            raise ValueError("missing/truncated finalized funding coverage")
        expected = observed // 28800000 * 28800000
        slots = [row["fundingTime"] // 28800000 * 28800000 for row in rows if row.get("rateType", "Regular") == "Regular"]
        anchor = self.state.get("funding_cursor", self.state["forward_start_ms"]) // 28800000 * 28800000
        if (not slots or slots[-1] != expected or slots[0] > anchor + 28800000 or any(b - a != 28800000 for a, b in zip(slots, slots[1:]))
                or any(not 0 <= row["fundingTime"] % 28800000 <= 5000 for row in rows if row.get("rateType", "Regular") == "Regular")):
            raise ValueError("incomplete regular finalized funding coverage")
        for event in events:
            row = next(r for r in rows if r["fundingTime"] == event["ts_ms"])
            self.emit(dict(type="funding", symbol=self.symbol, ts=observed, settlement_ts=event["ts_ms"],
                           rate=event["rate"], mark=event["mark_price"], finalized=True, rate_type=row.get("rateType", "Regular")))
        self.emit(dict(type="funding_status", symbol=self.symbol, ts=observed, complete=True, valid_until_ts=expected + 28800000))
        self.state["funding_cursor"] = rows[-1]["fundingTime"]
        self.state["funding_checked_ms"] = observed
        self.save()

    def route_signals(self, spec):
        with closing(sqlite3.connect(self.root / "signals.sqlite3")) as db:
            intents = [json.loads(row[0]) for row in db.execute("SELECT intent FROM h1_signals WHERE version_id=? ORDER BY rowid", (self.config["version_id"],))]
        handled = self.state.setdefault("handled_signals", {})
        market = next((m for m in self.state["markets"] if m["symbol"] == self.symbol), None)
        if not market:
            return
        for signal in intents:
            sid = signal["signal_id"]
            if sid in handled or (signal.get("symbol") and signal["symbol"] != self.symbol):
                continue
            if "order:" + sid in self.broker.orders:
                handled[sid] = dict(status="submitted", target=signal["reversion_target"])
                self.save()
                continue
            now = self.clock()
            if self.risk_blockers():
                result = dict(status="rejected", reason=",".join(self.risk_blockers()))
            elif now - int(signal["features"]["bar_close_ms"]) > self.config["execution_model"]["max_source_age_ms"]:
                result = dict(status="rejected", reason="late_closed_bar_signal")
            elif self.broker.positions or any(o["status"] in ("PENDING", "RESTING") for o in self.broker.orders.values()):
                result = dict(status="rejected", reason="single_position_or_pending")
            else:
                result = size_long(self.config, spec, equity=str(self.broker.equity), bid=market["bid"], ask=market["ask"], target=signal["reversion_target"])
                if result["status"] == "accepted":
                    s = self.broker.instruments[self.symbol]
                    order = self.broker.submit(Intent(
                        sid, self.symbol, "BUY", D(result["qty"]), D(result["stop_price"]), now, s.quantity_step, s.tick,
                        s.min_notional, s.max_quantity, "TAKER", False, expires_ts=now + self.config["execution_model"]["max_source_age_ms"],
                        risk_limited=True, risk_quantity_step=D(result["execution_qty_step"])))
                    result.update(status="submitted" if order["status"] == "PENDING" else "rejected", reason=order["reason"], target=signal["reversion_target"])
            handled[sid] = result
            self.audit(dict(type="signal_routing", signal_id=sid, result=result))
            self.save()

    def exit_policy(self, now, mark):
        p = self.broker.positions.get(self.symbol)
        if not p or any(o["intent"]["reduce_only"] and o["status"] in ("PENDING", "RESTING") for o in self.broker.orders.values()):
            return
        entry = next(f for f in reversed(self.broker.fills) if f["symbol"] == self.symbol and f["side"] == "BUY")
        signal = self.state.get("handled_signals", {}).get(entry["intent_id"])
        if not signal or "target" not in signal:
            raise ValueError("position lacks durable target registration")
        reason = ("data_gap" if self.state.get("gap_open")
                  else "take_profit" if D(mark) >= D(signal["target"])
                  else "max_hold" if now - p["opened_ts"] >= self.config["strategy"]["max_holding_ms"]
                  else None)
        if reason:
            s = self.broker.instruments[self.symbol]
            order = self.broker.submit(Intent(f"exit:{entry['intent_id']}:{reason}", self.symbol, "SELL",
                                              D(p["qty"]), None, now, s.quantity_step, s.tick, s.min_notional, s.max_quantity, "TAKER", True))
            self.audit(dict(type="exit_trigger", reason=reason, mark=mark, order=order))

    def collect_markets(self, ref, *, symbols=None, refresh_stale=True):
        batch_start_ms, batch_start_mono = self.clock(), self.monotonic()
        requested = list(symbols if symbols is not None else [(self.symbol, "crypto")] + ([("XAUUSDT", "TradFi")] if self.observe_xau else []))
        markets, events, spec_traded = [], [], None
        age = self.config["execution_model"]["max_source_age_ms"]
        for symbol, category in requested:
            spec = instrument(ref, symbol, category, self.config["fee_assumptions"][category])
            ticker = self.fetch("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
            depth = self.fetch("/fapi/v1/depth", {"symbol": symbol, "limit": 20})
            mark = self.fetch("/fapi/v1/premiumIndex", {"symbol": symbol})
            b, m = book_event(depth, symbol, age), mark_event(mark, symbol, age)
            ts, _ = fresh_source(ticker, symbol, age)
            if ticker["payload"]["symbol"] != symbol:
                raise ValueError("ticker symbol mismatch")
            bid, ask = decimal_string(ticker["payload"]["bidPrice"], True), decimal_string(ticker["payload"]["askPrice"], True)
            if D(bid) >= D(ask):
                raise ValueError("crossed ticker")
            markets.append(dict(symbol=symbol, category=category, bid=bid, ask=ask, mark_price=m["mark_price"],
                                min_notional=spec["min_notional"], last_received_at=mark["received_at"],
                                source_timestamps_ms=dict(bookTicker=ts, depth5=b["ts_ms"], premiumIndex=m["ts_ms"])))
            if symbol == self.symbol:
                deadline = mark["payload"].get("nextFundingTime")
                if type(deadline) is not int or deadline != self.broker.funding_status[self.symbol]["valid_until_ts"]:
                    raise ValueError("unverified or changed funding deadline")
                spec_traded = spec
                events = [dict(type="book", symbol=symbol, ts=b["observed_ms"], source_ts=b["ts_ms"], bids=b["bids"], asks=b["asks"]),
                          dict(type="mark", symbol=symbol, ts=m["observed_ms"], source_ts=m["ts_ms"], price=m["mark_price"])]
                ctx_label = "ETH batch before broker delivery" if self.symbol == "ETHUSDT" else f"{self.symbol} batch before broker delivery"
                self.validate_sources(markets, ctx_label, allow_wait=True)
                self.deliver_markets(markets, events, spec_traded)
        refreshed = set()
        for _ in range(len(markets) if refresh_stale else 0):
            now = self.clock()
            aged = [(m["symbol"], m["category"]) for m in markets
                    if m["symbol"] not in refreshed and any(type(ts) is int and now - ts > age for ts in m["source_timestamps_ms"].values())]
            if not aged:
                break
            self._timing_record(dict(kind="peer_aging_before_refresh", classification="engineering_observation_not_strategy_performance",
                                     observed_wall_ms=now, stale_symbols=[s for s, c in aged],
                                     peers=[dict(symbol=m["symbol"], source_ages_ms={n: now - ts for n, ts in m["source_timestamps_ms"].items()}) for m in markets]))
            self.audit(dict(type="batch_quote_refresh", at_ms=now, symbols=[s for s, c in aged],
                            sources_before=[dict(symbol=m["symbol"], sources=m["source_timestamps_ms"]) for m in markets]))
            fresh, refreshed_spec = self.collect_markets(ref, symbols=aged, refresh_stale=False)
            refreshed.update(s for s, c in aged)
            replacements = {m["symbol"]: m for m in fresh}
            markets = [replacements.get(m["symbol"], m) for m in markets]
            if refreshed_spec is not None:
                spec_traded = refreshed_spec
        decision_ms, decision_mono = self.clock(), self.monotonic()
        self._timing_record(dict(kind="batch_peer_aging", classification="engineering_observation_not_strategy_performance",
                                 requested_symbols=[s for s, c in requested], refresh_stale=bool(refresh_stale),
                                 start_wall_ms=batch_start_ms, end_wall_ms=decision_ms,
                                 start_monotonic_ms=batch_start_mono * 1000, end_monotonic_ms=decision_mono * 1000,
                                 wall_elapsed_ms=decision_ms - batch_start_ms, monotonic_elapsed_ms=(decision_mono - batch_start_mono) * 1000,
                                 wall_minus_monotonic_ms=(decision_ms - batch_start_ms) - (decision_mono - batch_start_mono) * 1000,
                                 peers=[dict(symbol=m["symbol"], receipt_age_ms=decision_ms - int(datetime.fromisoformat(m["last_received_at"].replace("Z", "+00:00")).timestamp() * 1000),
                                             source_ages_ms={n: decision_ms - ts for n, ts in m["source_timestamps_ms"].items()}) for m in markets]))
        self.validate_sources(markets, "batch before decision", allow_wait=True)
        return markets, spec_traded

    def collect(self):
        ref = self.reference()
        spec_traded = instrument(ref, self.symbol, "crypto", self.config["fee_assumptions"]["crypto"])
        self.funding(spec_traded)
        markets, spec_traded = self.collect_markets(ref)
        self.state["markets"] = markets
        age = self.config["execution_model"]["max_source_age_ms"]
        self.bootstrap()
        start = self.state["cursor"] if self.state["cursor"] is not None else self.state.get("decision_cutoff_ms", self.state["forward_start_ms"]) // self.INTERVAL_MS * self.INTERVAL_MS
        rows = []
        if start + self.INTERVAL_MS <= self.clock():
            receipt = self.fetch("/fapi/v1/klines", dict(symbol=self.symbol, interval=self.INTERVAL_NAME, startTime=start, limit=1000))
            rows = receipt["payload"]
        for row in rows:
            if type(row[0]) is not int or type(row[6]) is not int or row[6] != row[0] + self.INTERVAL_MS - 1:
                raise ValueError("invalid API closed-bar timestamps")
            if row[6] + 1 > self.clock():
                continue
            bar = dict(symbol=self.symbol, open_time_ms=row[0], close_time_ms=row[6] + 1, closed=True,
                       **{k: D(decimal_string(row[i], k != "volume")) for k, i in [("open", 1), ("high", 2), ("low", 3), ("close", 4), ("volume", 5)]})
            result = self.detector.process(bar, now=datetime.fromtimestamp(self.clock() / 1000, timezone.utc))
            self.audit(dict(type="detector", result=result, bar=bar))
            if result["diagnostic"].startswith("reject"):
                raise ValueError(result["diagnostic"])
            if result["diagnostic"] == "gap_reset":
                self.state["gaps"] += 1
                self.state["decision_cutoff_ms"] = self.clock()
                self.state["gap_open"] = True
                self.state["warmup"] = 0
                self.cancel_entries_for_gap()
                self.save()
                raise ValueError("bar gap requires context rebuild at new decision cutoff")
            self.state["diagnostic"] = result["diagnostic"]
            self.state["cursor"] = row[0] + self.INTERVAL_MS
            self.save()
        with closing(sqlite3.connect(self.root / "signals.sqlite3")) as db:
            row = db.execute("SELECT bars FROM h1_state WHERE version_id=? AND symbol=?", (self.config["version_id"], self.symbol)).fetchone()
        self.state["warmup"] = len(json.loads(row[0])) if row else 0
        if any(not 0 <= self.clock() - ts <= age for market in markets for ts in market["source_timestamps_ms"].values()):
            markets, spec_traded = self.collect_markets(ref)
            self.state["markets"] = markets
        self.validate_sources(markets, "decision after candle work")
        self.route_signals(spec_traded)
        self.state["last_success_ms"] = self.clock()
        self.state["latest_error"] = None

    def bootstrap(self):
        cutoff = self.state.get("decision_cutoff_ms", self.state["forward_start_ms"])
        if self.detector.seeded(cutoff):
            return
        end = (cutoff - 1) // self.INTERVAL_MS * self.INTERVAL_MS
        receipt = self.fetch("/fapi/v1/klines", dict(symbol=self.symbol, interval=self.INTERVAL_NAME,
                             startTime=end - 61 * self.INTERVAL_MS, endTime=end - 1, limit=61))
        manifest = self.detector.seed(receipt, cutoff_ms=cutoff, now_ms=self.clock())
        self.state["cursor"] = end
        self.state["warmup"] = 61
        self.state["diagnostic"] = "context_ready_waiting_next_closed_bar"
        self.audit(dict(type="indicator_context_only", manifest=manifest))
        self.save()

    def snapshot(self):
        result = super().snapshot()
        research = result["research"]
        research["target_complete_round_trips"] = self.config["research"]["target_complete_round_trips"]
        research["target_not_guarantee"] = self.config["research"]["target_not_guarantee"]
        result["engine"]["candidate_implementation"] = (
            "paper-engine-v5" if self.state.get("deployment") else "paper-engine-v5-candidate"
        )
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    lab = Path(__file__).resolve().parent
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=lab / "paper_config_f1h_btcusdt.json")
    parser.add_argument("--storage-policy", type=Path)
    args = parser.parse_args(argv)
    runtime = PaperRuntime(args.state_dir, args.config, storage_policy=args.storage_policy)
    try:
        while True:
            snapshot = runtime.poll()
            publish(snapshot, args.status)
            print(canonical(snapshot), flush=True)
            if snapshot.get("storage_protection", {}).get("stop_after_publish"):
                return 2
            if args.once:
                return 1 if snapshot["latest_error"] else 0
            time.sleep(runtime.config["execution_model"]["poll_interval_seconds"])
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
