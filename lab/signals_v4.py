"""Closed one-hour PAPER research detector; inherited H1 contracts unchanged."""
from decimal import Decimal
from signals_v3 import Detector as MinuteDetector


class Detector(MinuteDetector):
    interval_ms = 3600000
    interval_name = "1h"
    downside_sigma = Decimal("1.5")
    volume_multiple = Decimal("1.2")
