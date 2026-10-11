"""T1 daily trend PAPER detector runtime; decisions only, no order routing."""
import argparse
import hashlib
import time
from pathlib import Path

import paper_runtime_v5 as v5
from trend_signals import TrendDetector

D = v5.D


def _canonical_config_hashes():
    lab = Path(__file__).resolve().parent
    names = ("paper_config_t1_" + symbol + ".json"
             for symbol in ("btcusdt", "ethusdt", "solusdt", "xrpusdt"))
    return {name: hashlib.sha256((lab / name).read_bytes()).hexdigest()
            for name in names}


class PaperRuntime(v5.PaperRuntime):
    CONFIG_HASHES = _canonical_config_hashes()
    ALLOWED_CONFIG_HASHES = set(CONFIG_HASHES.values())
    EXTRA_SOURCES = v5.PaperRuntime.EXTRA_SOURCES + (
        "paper_runtime_v7.py", "trend_signals.py", "trend_sizing.py")
    INTERVAL_MS = 86400000
    INTERVAL_NAME = "1d"

    def __init__(self, root, config_path, **kwargs):
        def configured_detector(store_path, version_id, forward_start, symbols):
            strategy = self.config["strategy"]
            detector = TrendDetector(self.symbol, strategy["entry_lookback"],
                                     strategy["exit_lookback"])
            # v2 dashboard snapshots expect this interface on the detector.
            detector.manifest = lambda: dict(
                strategy="T1", symbol=detector.symbol,
                position=detector.position, last_close_ms=detector.last,
                seeded=detector.last is not None)
            return detector

        self.DETECTOR = configured_detector
        super().__init__(root, config_path, **kwargs)
        try:
            if "t1_detector" in self.state:
                self.detector.restore_state(self.state["t1_detector"])
            self.state.setdefault("t1_armed", None)
            self.state.setdefault("window_void", False)
            self.state.setdefault("t1_decisions", [])
            self.state.setdefault("t1_detector", self.detector.export_state())
            self.save()
        except Exception:
            self.close()
            raise

    def ensure_broker(self, spec):
        """Reuse the v5 PAPER broker contract without querying hourly signals."""
        c = self.config
        settings = v5.InstrumentSettings(
            spec["symbol"], D(spec["maker_fee"]), D(spec["taker_fee"]),
            D(spec["qty_step"]), D(spec["tick_size"]), D(spec["min_notional"]),
            D(spec["max_qty"]), D(spec["min_qty"]), spec["category"])
        if self.broker:
            if self.broker.instruments[self.symbol] != settings:
                raise ValueError("public instrument filters changed; new review required")
            return
        e = c["execution_model"]
        self.broker = v5.SimBroker(
            self.root / "broker.sqlite3", initial_cash=D(c["initial_equity_usdt"]),
            instruments=[settings],
            execution=v5.ExecutionModel(
                e["latency_ms"], e["exit_slippage_ticks"], None,
                e["entry_slippage_ticks"], e["exit_slippage_ticks"]),
            risk=v5.RiskContract(
                True, c["risk_version"], D(c["max_loss_per_trade_usdt"]),
                D(c["max_daily_loss_usdt"]), D(c["max_effective_exposure_x"]),
                c["max_positions"], D(c["total_loss_limit_usdt"]), True),
            version_id=self.state.get("account_version_id", c["version_id"]),
            forward_start=self.state["forward_start_ms"])
        if self.state.get("gap_open"):
            self.cancel_entries_for_gap()
        self.save()

    def _bar(self, row, now):
        if (not isinstance(row, (list, tuple)) or len(row) < 7
                or type(row[0]) is not int or type(row[6]) is not int
                or row[0] % self.INTERVAL_MS
                or row[6] != row[0] + self.INTERVAL_MS - 1):
            raise ValueError("invalid API closed-bar timestamps")
        if row[6] >= now:
            return None
        values = {key: D(v5.decimal_string(row[i], key != "volume"))
                  for key, i in (("open", 1), ("high", 2), ("low", 3),
                                 ("close", 4), ("volume", 5))}
        return dict(symbol=self.symbol, open_time_ms=row[0],
                    close_time_ms=row[6], **values)

    def _void(self, reason):
        self.state["window_void"] = True
        self.state["latest_error"] = "ValueError: " + reason
        self.state["gaps"] += int("gap" in reason)
        self.state["t1_detector"] = self.detector.export_state()
        self.audit(dict(type="t1_window_void", reason=reason,
                        cursor=self.state.get("cursor")))
        self.save()
        raise ValueError(reason)

    def bootstrap(self):
        if self.state["t1_armed"] is not None or self.state["window_void"]:
            return
        cutoff = self.state.get("decision_cutoff_ms", self.state["forward_start_ms"])
        first = cutoff // self.INTERVAL_MS * self.INTERVAL_MS
        receipt = self.fetch("/fapi/v1/klines", dict(
            symbol=self.symbol, interval=self.INTERVAL_NAME,
            startTime=first - 60 * self.INTERVAL_MS,
            endTime=first - 1, limit=60))
        rows = receipt["payload"]
        if len(rows) != 60:
            self._void("bootstrap requires 60 contiguous closed daily bars")
        for index, row in enumerate(rows):
            try:
                bar = self._bar(row, self.clock())
            except (ValueError, IndexError, TypeError) as exc:
                self._void("invalid bootstrap bar: " + str(exc))
            if bar is None or bar["open_time_ms"] != first - (60 - index) * self.INTERVAL_MS:
                self._void("bootstrap gap or unclosed bar")
            result = self.detector.process(bar)
            if result["diagnostic"].startswith("reject"):
                self._void(result["diagnostic"])
        self.state["cursor"] = first
        self.state["t1_armed"] = self.detector.position == 0
        self.state["warmup"] = 60
        self.state["diagnostic"] = "context_ready_waiting_next_closed_bar"
        self.state["t1_detector"] = self.detector.export_state()
        self.audit(dict(type="t1_bootstrap", cursor=first,
                        armed=self.state["t1_armed"],
                        detector_position=self.detector.position))
        self.save()

    def collect(self):
        if self.state["window_void"]:
            raise ValueError("T1 research window void; manual review required")
        ref = self.reference()
        spec = v5.instrument(ref, self.symbol, "crypto",
                             self.config["fee_assumptions"]["crypto"])
        self.funding(spec)
        markets, spec = self.collect_markets(ref)
        self.state["markets"] = markets
        self.bootstrap()
        start = self.state["cursor"]
        if start + self.INTERVAL_MS <= self.clock():
            receipt = self.fetch("/fapi/v1/klines", dict(
                symbol=self.symbol, interval=self.INTERVAL_NAME,
                startTime=start, limit=1000))
            rows = receipt["payload"]
        else:
            rows = []
        for row in rows:
            try:
                bar = self._bar(row, self.clock())
            except (ValueError, IndexError, TypeError) as exc:
                self._void("invalid daily bar: " + str(exc))
            if bar is None:
                continue
            if bar["open_time_ms"] < self.state["cursor"]:
                continue
            if bar["open_time_ms"] != self.state["cursor"]:
                self._void("daily bar gap")
            result = self.detector.process(bar)
            diagnostic = result["diagnostic"]
            self.audit(dict(type="t1_detector", diagnostic=diagnostic,
                            close_time_ms=bar["close_time_ms"]))
            if diagnostic.startswith("reject"):
                self._void(diagnostic)
            if diagnostic == "entry":
                if self.state["t1_armed"]:
                    self._decision("entry", bar)
                else:
                    self.audit(dict(type="t1_suppressed_unarmed_entry",
                                    close_time_ms=bar["close_time_ms"]))
            elif diagnostic == "exit":
                if not self.state["t1_armed"]:
                    self.state["t1_armed"] = True
                    self.audit(dict(type="t1_armed_after_exit",
                                    close_time_ms=bar["close_time_ms"]))
                elif self._position_may_exist():
                    self._decision("exit", bar)
            self.state["diagnostic"] = diagnostic
            self.state["cursor"] = bar["open_time_ms"] + self.INTERVAL_MS
            self.state["t1_detector"] = self.detector.export_state()
            self.save()
        age = self.config["execution_model"]["max_source_age_ms"]
        if any(not 0 <= self.clock() - ts <= age
               for market in markets
               for ts in market["source_timestamps_ms"].values()):
            markets, spec = self.collect_markets(ref)
            self.state["markets"] = markets
        self.validate_sources(markets, "decision after candle work")
        self.route_signals(spec)
        self.state["last_success_ms"] = self.clock()
        self.state["latest_error"] = None
        self.save()

    def _position_may_exist(self):
        return (any(d["kind"] == "entry" for d in self.state["t1_decisions"])
                or bool(self.broker and self.broker.positions))

    def _decision(self, kind, bar):
        item = dict(id="t1:%s:%s:%s" % (
            self.symbol, bar["close_time_ms"], kind),
            kind=kind, close_time_ms=bar["close_time_ms"],
            close=str(bar["close"]), status="new")
        if not any(d["id"] == item["id"] for d in self.state["t1_decisions"]):
            self.state["t1_decisions"].append(item)
            self.audit(dict(type="t1_decision", decision=item))

    def route_signals(self, spec):
        """No orders; T1-U2B2 implements decision execution."""
        return None

    def exit_policy(self, now, mark):
        """No take-profit or max-hold; T1-U2B2 implements decision execution."""
        return None

    def handle_gap(self):
        """Daily candle continuity is checked in collect, not by hourly heartbeat."""
        return None

    def snapshot(self):
        result = super().snapshot()
        result["engine"]["candidate_implementation"] = (
            "paper-engine-v7" if self.state.get("deployment")
            else "paper-engine-v7-candidate")
        result["research"].update(
            t1_armed=self.state["t1_armed"],
            window_void=self.state["window_void"],
            detector_position=self.detector.position,
            pending_decisions=sum(d["status"] == "new"
                                  for d in self.state["t1_decisions"]))
        # Detection-only candidates cannot submit paper orders.
        result["paper_trading_enabled"] = False
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    lab = Path(__file__).resolve().parent
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--config", type=Path,
                        default=lab / "paper_config_t1_btcusdt.json")
    parser.add_argument("--storage-policy", type=Path)
    args = parser.parse_args(argv)
    runtime = PaperRuntime(args.state_dir, args.config,
                           storage_policy=args.storage_policy)
    try:
        while True:
            snapshot = runtime.poll()
            v5.publish(snapshot, args.status)
            print(v5.canonical(snapshot), flush=True)
            if snapshot.get("storage_protection", {}).get("stop_after_publish"):
                return 2
            if args.once:
                return 1 if snapshot["latest_error"] else 0
            time.sleep(runtime.config["execution_model"]["poll_interval_seconds"])
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
