"""W1 paper runtime, retaining F1H execution and frozen sizing.

The inherited sizing reads W1 risk limits directly; no frozen-file override is
necessary. The detector factory injects the immutable config thresholds after
the parent validates the W1-only byte hash.
"""
import hashlib
from pathlib import Path

from paper_runtime_v5 import PaperRuntime as F1HRuntime
from signals_v6 import Detector


def _canonical_config_hashes():
    lab = Path(__file__).resolve().parent
    names = tuple("paper_config_w1_" + s + ".json"
                  for s in ("ethusdt", "btcusdt", "solusdt", "xrpusdt"))
    return {name: hashlib.sha256((lab / name).read_bytes()).hexdigest()
            for name in names}


class PaperRuntime(F1HRuntime):
    CONFIG_HASHES = _canonical_config_hashes()
    ALLOWED_CONFIG_HASHES = set(CONFIG_HASHES.values())
    DETECTOR = Detector
    EXTRA_SOURCES = F1HRuntime.EXTRA_SOURCES + ("paper_runtime_v6.py", "signals_v6.py")

    def __init__(self, root, config_path, **kwargs):
        def configured_detector(store_path, version_id, forward_start, symbols):
            strategy = self.config["strategy"]
            return Detector(store_path, version_id, forward_start, symbols,
                            downside_sigma=strategy["downside_sigma"],
                            volume_multiple=strategy["volume_multiple"])

        self.DETECTOR = configured_detector
        super().__init__(root, config_path, **kwargs)
