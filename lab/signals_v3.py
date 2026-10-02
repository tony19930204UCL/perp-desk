"""Preregistered one-minute PAPER detector, reusing durable H1 contracts."""
from decimal import Decimal
from signals_v2 import Detector as BaseDetector

class Detector(BaseDetector):
    interval_ms = 60000
    interval_name = "1m"
    downside_sigma = Decimal("1.5")
    volume_multiple = Decimal("1.2")
