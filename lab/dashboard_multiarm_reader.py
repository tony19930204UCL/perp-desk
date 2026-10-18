"""Read just the fixed broker singleton in twelve fixed namespaces, never write."""
import json
import os
from pathlib import Path
import sqlite3
import stat
from urllib.parse import quote
from dashboard_multiarm_model import ARMS, project, unknown

MAX_BYTES = 32 * 1024 * 1024


def read_one(root, dirname, *, max_bytes=MAX_BYTES):
    path = root / dirname / 'broker.sqlite3'
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return None, 'NOT_REGULAR'
        if info.st_size > max_bytes + 4096:
            return None, 'TOO_LARGE'
        # Read-only immutable snapshots must not silently overlook active WAL.
        if any(Path(str(path) + ext).exists() for ext in ('-wal', '-shm', '-journal')):
            return None, 'ACTIVE_SIDECAR'
        uri = 'file:' + quote(str(path), safe='/') + '?mode=ro'
        connection = sqlite3.connect(uri, uri=True, timeout=0.15)
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.execute('PRAGMA busy_timeout=150')
            row = connection.execute(
                'SELECT payload FROM sim_broker_state WHERE singleton=?', (1,)).fetchone()
            if row is None:
                return None, 'MISSING_ROW'
            payload = row[0]
            if type(payload) is not str:
                return None, 'BAD_PAYLOAD'
            if len(payload.encode('utf-8')) > max_bytes:
                return None, 'TOO_LARGE'
            data = json.loads(payload, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            return data, None
        finally:
            connection.close()
    except FileNotFoundError:
        return None, 'MISSING_FILE'
    except (OSError, sqlite3.Error):
        return None, 'DB_UNAVAILABLE'
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None, 'BAD_JSON'


def read_all(root, *, now_ms, stale_after_s=90, max_bytes=MAX_BYTES):
    if type(now_ms) is not int or type(stale_after_s) is not int or stale_after_s < 0:
        raise ValueError('invalid clock')
    root = Path(root)
    if not root.is_absolute():
        raise ValueError('absolute root required')
    rows = []
    for arm, dirname in ARMS:
        data, error = read_one(root, dirname, max_bytes=max_bytes)
        rows.append(unknown(arm, error) if error else
                    project(arm, data, now_ms=now_ms, stale_after_s=stale_after_s))
    return dict(kind='PAPER', read_only=True, now_ms=now_ms, tick_seconds=15, arms=rows)
