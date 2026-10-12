"""Pure read-only projection of already-parsed dashboard v2 sources.

A scorecard is not an independent service, clock, or scheduler heartbeat.
No input is mutated, and no I/O or time source is used.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import math


MONEY = ('pnl', 'fees', 'max_dd', 'loss_limit')
COUNTS = ('days_in', 'days_left', 'signals', 'blocked', 'fills',
          'round_trips', 'open_positions', 'feed_errors', 'feed_gaps')
OTHER_NUMBERS = ('stale_min',)
JOURNAL_FIELDS = ('id', 'at', 'kind', 'title', 'body', 'verdict')
COMMITMENT_FIELDS = ('id', 'text', 'state', 'due')
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utc_microseconds(stamp):
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp)
        if moment.utcoffset() != timedelta(0):
            return None
        delta = moment - EPOCH
        return ((delta.days * 86400 + delta.seconds) * 1000000
                + delta.microseconds)
    except (TypeError, ValueError, OverflowError):
        return None


def _money(value):
    if not isinstance(value, str):
        return {'exact': None, 'display': '未知'}
    try:
        number = Decimal(value)
        if not number.is_finite():
            raise InvalidOperation
        with localcontext() as ctx:
            ctx.prec = max(28, number.adjusted() + 4)
            rounded = number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return {'exact': value, 'display': format(rounded, 'f')}
    except (InvalidOperation, ValueError, OverflowError):
        return {'exact': None, 'display': '未知'}


def _nonnegative_number(value):
    if type(value) in (int, float):
        if value >= 0 and (type(value) is int or math.isfinite(value)):
            return value
    elif isinstance(value, str):
        try:
            number = Decimal(value)
            if number.is_finite() and number >= 0:
                return value
        except InvalidOperation:
            pass
    return None


def _unknown_arm(identifier=None):
    row = {'arm': identifier}
    row.update({name: None for name in COUNTS + OTHER_NUMBERS})
    row.update({name: {'exact': None, 'display': '未知'} for name in MONEY})
    return row


def _project_arm(source, index, reasons):
    if not isinstance(source, dict):
        reasons.append(f'第{index}臂資料格式錯誤')
        return _unknown_arm()
    identifier = source.get('arm')
    if not isinstance(identifier, str) or not identifier.strip():
        identifier = None
        reasons.append(f'第{index}臂識別碼無效')
    row = _unknown_arm(identifier)
    for name in COUNTS:
        value = source.get(name)
        if type(value) is int and value >= 0:
            row[name] = value
        else:
            reasons.append(f'第{index}臂 {name} 無效')
    for name in OTHER_NUMBERS:
        row[name] = _nonnegative_number(source.get(name))
        if row[name] is None:
            reasons.append(f'第{index}臂 {name} 無效')
    for name in MONEY:
        row[name] = _money(source.get(name))
        if row[name]['exact'] is None:
            reasons.append(f'第{index}臂 {name} 無效')
    return row


def _select_strings(source, names):
    return {name: source.get(name) if isinstance(source.get(name), str)
            else None for name in names}


def build_model(scorecard, journal, ledger, *, now_ms, max_age_ms):
    """Return JSON-safe dashboard data; no liveness inference or totals."""
    if type(now_ms) is not int:
        raise ValueError('now_ms 必須是整數')
    if type(max_age_ms) is not int or max_age_ms < 0:
        raise ValueError('max_age_ms 必須是非負整數')

    reasons = []
    scorecard_at = scorecard.get('at') if isinstance(scorecard, dict) else None
    if not isinstance(scorecard_at, str):
        scorecard_at = None
    state = 'unknown'
    arms = []
    if scorecard is None:
        reasons.append('記分卡來源缺失')
    elif not isinstance(scorecard, dict):
        reasons.append('記分卡格式無效')
    else:
        blocked = False
        stamp_us = _utc_microseconds(scorecard_at)
        if stamp_us is None:
            reasons.append('記分卡時間無效')
            blocked = True
        elif stamp_us > now_ms * 1000:
            reasons.append('記分卡時間在未來')
            blocked = True
        elif now_ms * 1000 - stamp_us > max_age_ms * 1000:
            reasons.append('記分卡已過期')
            blocked = True
        source_arms = scorecard.get('arms')
        if not isinstance(source_arms, list):
            reasons.append('記分卡臂資料缺失或無效')
            blocked = True
        else:
            seen = set()
            for item in source_arms:
                if isinstance(item, dict):
                    identifier = item.get('arm')
                    if isinstance(identifier, str) and identifier.strip():
                        if identifier in seen:
                            reasons.append('記分卡臂識別碼重複')
                            blocked = True
                            break
                        seen.add(identifier)
        if not blocked:
            state = 'known'
            arms = [_project_arm(source, index, reasons)
                    for index, source in enumerate(source_arms, 1)]

    entries = []
    if journal is None:
        reasons.append('研究日誌來源缺失')
    elif not isinstance(journal, list):
        reasons.append('研究日誌格式無效')
    else:
        for item in reversed(journal):
            if isinstance(item, dict):
                entries.append(_select_strings(item, JOURNAL_FIELDS))
                if len(entries) == 5:
                    break
            else:
                reasons.append('研究日誌項目格式無效')

    commitments = []
    if ledger is None:
        reasons.append('承諾帳本來源缺失')
    elif not isinstance(ledger, dict) or not isinstance(ledger.get('items'), list):
        reasons.append('承諾帳本格式無效')
    else:
        for item in ledger['items']:
            if not isinstance(item, dict):
                reasons.append('承諾帳本項目格式無效')
            elif item.get('state') not in ('VERIFIED', 'DROPPED'):
                commitments.append(_select_strings(item, COMMITMENT_FIELDS))

    return {
        'scorecard_state': state,
        'reasons': reasons,
        'scorecard_at': scorecard_at,
        'arms': arms,
        'journal': entries,
        'commitments': commitments,
        'health': {
            'liveness': 'unknown',
            'clock': 'unknown',
            'scheduler': 'unknown',
            'reasons': ['缺少獨立服務存活證據', '缺少獨立時鐘驗證',
                        '缺少獨立排程證據'],
        },
    }
