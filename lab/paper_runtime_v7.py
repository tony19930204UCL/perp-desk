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
