"""Bounded, explicit, read-only local sources for the frozen dashboard model."""
from datetime import datetime, timezone
import json
import math
import os
import stat

from dashboard_v2_model import build_model


def _read_at_most(fd, limit):
    chunks = []
    while limit:
        part = os.read(fd, min(limit, 65536))
        if not part:
            break
        chunks.append(part)
        limit -= len(part)
    return b''.join(chunks)


def _bounded_file(path, max_bytes, *, tail):
    """Return (bytes, tail_start, problem); never open non-regular files."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None, 0, '不是一般檔案'
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                return None, 0, '不是一般檔案'
            if tail:
                start = max(0, info.st_size - max_bytes)
                offset = start - 1 if start else 0
                os.lseek(fd, offset, os.SEEK_SET)
                return _read_at_most(fd, info.st_size - offset), start, None
            return _read_at_most(fd, max_bytes + 1), 0, None
        finally:
            os.close(fd)
    except FileNotFoundError:
        return None, 0, '檔案缺失'
    except OSError:
        return None, 0, '檔案無法讀取'


def _complete_lines(data, start):
    # A single byte before the tail distinguishes a full first line
    # from a cut fragment. An unfinished last line is never considered.
    if start:
        if data[:1] == b'\n':
            data = data[1:]
        else:
            boundary = data.find(b'\n')
            if boundary < 0:
                return None, '完整紀錄超出大小上限'
            data = data[boundary + 1:]
    end = data.rfind(b'\n')
    if end < 0:
        return None, '沒有完整紀錄'
    try:
        lines = data[:end + 1].decode('utf-8', errors='strict').split('\n')
    except UnicodeDecodeError:
        return None, '完整紀錄編碼無效'
    rows = [line for line in lines if line.strip()]
    if not rows:
        return None, '沒有有效紀錄'
    return rows, None


def _reject_constant(_value):
    raise ValueError('nonstandard JSON constant')


def _json_dict(line):
    value = json.loads(line, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError('JSON object required')
    return value


def _read_jsonl(path, max_bytes, *, scorecard):
    label = '記分卡' if scorecard else '研究日誌'
    raw, start, issue = _bounded_file(path, max_bytes, tail=True)
    if issue:
        return None, [label + issue]
    lines, issue = _complete_lines(raw, start)
    if issue:
        return None, [label + issue]
    if scorecard:
        try:
            return _json_dict(lines[-1]), []
        except (ValueError, RecursionError):
            return None, ['記分卡最新完整紀錄無效']
    entries = []
    invalid = False
    for line in lines:
        try:
            entries.append(_json_dict(line))
        except (ValueError, RecursionError):
            invalid = True
    reasons = ['研究日誌略過無效完整紀錄'] if invalid else []
    if not entries:
        return None, reasons + ['研究日誌沒有有效紀錄']
    return entries[-5:], reasons


def _iso_due(value):
    if isinstance(value, str):
        return value
    if type(value) not in (int, float) or value < 0:
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _read_ledger(path, max_bytes):
    raw, _, issue = _bounded_file(path, max_bytes, tail=False)
    if issue:
        return None, ['承諾帳本' + issue]
    if len(raw) > max_bytes:
        return None, ['承諾帳本超出大小上限']
    try:
        parsed = _json_dict(raw.decode('utf-8', errors='strict'))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, ['承諾帳本格式或編碼無效']
    if not isinstance(parsed.get('items'), list):
        return None, ['承諾帳本項目格式無效']
    items = []
    for item in parsed['items']:
        if not isinstance(item, dict):
            items.append(None)
            continue
        items.append({'id': item.get('id'), 'text': item.get('text'),
                      'state': item.get('state'), 'due': _iso_due(item.get('due'))})
    return {'items': items}, []


def read_dashboard(scorecard_path, journal_path, ledger_path, *,
                   now_ms, max_age_ms, max_bytes=1048576):
    """Read only the three caller-selected files; append sanitized diagnostics."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError('max_bytes 必須是正整數')
    if type(now_ms) is not int:
        raise ValueError('now_ms 必須是整數')
    if type(max_age_ms) is not int or max_age_ms < 0:
        raise ValueError('max_age_ms 必須是非負整數')

    scorecard, score_reasons = _read_jsonl(scorecard_path, max_bytes,
                                           scorecard=True)
    journal, journal_reasons = _read_jsonl(journal_path, max_bytes,
                                         scorecard=False)
    ledger, ledger_reasons = _read_ledger(ledger_path, max_bytes)
    result = build_model(scorecard, journal, ledger, now_ms=now_ms,
                         max_age_ms=max_age_ms)
    result['reasons'].extend(score_reasons + journal_reasons + ledger_reasons)
    return result
