"""Configurable closed-hour research detector; no execution capability."""
from decimal import Decimal, InvalidOperation
from signals_v5 import Detector as V5Detector


def _threshold(value, name, lower, upper, allow_zero):
    if not isinstance(value, (str, Decimal)):
        raise ValueError(f'{name} must be a decimal string or Decimal')
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f'{name} must be a finite decimal') from exc
    if (not number.is_finite() or number > upper or
            number < lower or (not allow_zero and number == 0)):
        bracket = '[' if allow_zero else '('
        raise ValueError(f'{name} must be in {bracket}{lower}, {upper}]')
    return number


class Detector(V5Detector):
    """Use instance-level thresholds in the inherited signal and feature logic."""

    def __init__(self, store_path, version_id, forward_start, symbols, *,
                 downside_sigma='1.5', volume_multiple='1.2'):
        self.downside_sigma = _threshold(downside_sigma, 'downside_sigma',
                                         Decimal('0'), Decimal('5'), False)
        self.volume_multiple = _threshold(volume_multiple, 'volume_multiple',
                                          Decimal('0'), Decimal('10'), True)
        super().__init__(store_path, version_id, forward_start, symbols)
