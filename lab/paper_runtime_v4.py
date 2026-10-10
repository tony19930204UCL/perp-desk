"""Isolated H2-PAPER-004 one-hour PAPER candidate; no deployment side effects."""
import argparse
import time
from pathlib import Path

from paper_runtime_v3 import PaperRuntime as BaseRuntime, publish, canonical, research_metrics
from signals_v4 import Detector


class PaperRuntime(BaseRuntime):
    CONFIG_HASH = "e27b4b4b05ec66a2a88b9d4dd4eb502dd0bc329d87003346da55707ee0a7405c"
    DETECTOR = Detector
    EXTRA_SOURCES = BaseRuntime.EXTRA_SOURCES + ("paper_runtime_v4.py", "signals_v4.py")
    INTERVAL_MS = 3600000
    INTERVAL_NAME = "1h"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        start = self.state["strategy_start_ms"]
        deadline = start + self.config["research"]["window_ms"]
        # The v3 parent initializes a 48-hour deadline for new state.
        # Preserve a previously persisted H2 deadline across restarts.
        if self.state.get("research_window_version") != self.config["version_id"]:
            self.state["research_deadline_ms"] = deadline
            self.state["research_window_version"] = self.config["version_id"]
            self.save()

    def risk_blockers(self):
        return super().risk_blockers()

    def snapshot(self):
        result = super().snapshot()
        research = result["research"]
        research["target_complete_round_trips"] = self.config["research"]["target_complete_round_trips"]
        research["target_not_guarantee"] = self.config["research"]["target_not_guarantee"]
        result["engine"]["candidate_implementation"] = (
            "paper-engine-v4" if self.state.get("deployment") else "paper-engine-v4-candidate"
        )
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    lab = Path(__file__).resolve().parent
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=lab / "paper_config_v4.json")
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
