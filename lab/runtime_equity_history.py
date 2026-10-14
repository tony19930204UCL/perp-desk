"""Transactional, append-only original PAPER equity history (PERSIST-1 B1).

B2 integration contract (caller owns the sqlite3.Connection and transaction):

    db.execute('BEGIN')
    history = EquityHistoryStore(db)
    history.prepare()                         # once per connection, in a transaction
    migrated_now = history.migrate_legacy(legacy_list)  # ordered, once ever
    # If migrated_now, remove history from the singleton JSON *in this transaction*.
    history.append_snapshot(point)            # suppresses only adjacent equal ts
    db.execute('UPDATE state SET payload=? WHERE id=1', (new_payload,))
    db.commit()                               # on ANY error incl. COMMIT: db.rollback()

After initialization, recent() and count() are read-only and do not require a
transaction. Re-run prepare() after rolling back initial schema creation.
The migration marker and every point are in the same database as the singleton;
this module NEVER commits, rolls back, changes other tables, or deletes points.
Call migrate_legacy([]) for a fresh runtime before appending new snapshots.
"""

import sqlite3

RECENT_LIMIT = 2048

_HISTORY = """CREATE TABLE runtime_equity_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT CHECK(seq > 0),
    ts TEXT NOT NULL CHECK(typeof(ts) = 'text' AND length(ts) > 0),
    equity_usdt TEXT NOT NULL CHECK(typeof(equity_usdt) = 'text' AND length(equity_usdt) > 0),
    valuation_stale INTEGER NOT NULL CHECK(typeof(valuation_stale) = 'integer' AND valuation_stale IN (0, 1))
)"""
_MIGRATION = """CREATE TABLE runtime_equity_migration (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    complete INTEGER NOT NULL CHECK(typeof(complete) = 'integer' AND complete = 1)
)"""
_SCHEMAS = {'runtime_equity_history': _HISTORY, 'runtime_equity_migration': _MIGRATION}



def _point(point):
    if not isinstance(point, dict) or set(point) != {'ts', 'equity_usdt', 'valuation_stale'}:
        raise ValueError('equity point must have exactly ts, equity_usdt, valuation_stale')
    ts, equity, stale = (point[k] for k in ('ts', 'equity_usdt', 'valuation_stale'))
    if not isinstance(ts, str) or not ts or not isinstance(equity, str) or not equity or type(stale) is not bool:
        raise ValueError('invalid equity point types/values')
    return ts, equity, int(stale)


class EquityHistoryStore:
    """Storage facade with no transaction ownership and no whole-history reads on hot paths."""

    def __init__(self, connection):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError('sqlite3.Connection required')
        self.db = connection

    def _writer(self):
        if not self.db.in_transaction:
            raise RuntimeError('caller must BEGIN a transaction before history writes')

    def prepare(self):
        """Create or strictly validate owned tables and existing data, once per open.

        Call inside an explicit BEGIN. Incompatible/partial schemas are refused
        before executing any DDL; other database objects remain untouched.
        An existing archive is scanned only at this opening validation boundary.
        """
        self._writer()
        existing = {name: (kind, sql) for name, kind, sql in self.db.execute(
            "SELECT name, type, sql FROM sqlite_master WHERE name IN (?, ?)", tuple(_SCHEMAS))}
        if existing and set(existing) != set(_SCHEMAS):
            raise ValueError('partial equity history schema')
        for name, (kind, sql) in existing.items():
            if kind != 'table' or sql != _SCHEMAS[name]:
                raise ValueError('incompatible equity history schema: ' + name)
        if existing:
            unexpected = self.db.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name IN (?, ?) AND type IN ('trigger', 'index')",
                tuple(_SCHEMAS)).fetchall()
            if unexpected:
                raise ValueError('unexpected equity history index/trigger')
        else:
            for sql in _SCHEMAS.values():
                self.db.execute(sql)
        # SQLite can be opened with CHECK constraints disabled; validate preexisting
        # data as well as table declarations instead of trusting PRAGMA settings.
        for seq, ts, equity, stale in self.db.execute(
                'SELECT seq, ts, equity_usdt, valuation_stale FROM runtime_equity_history'):
            if type(seq) is not int or seq <= 0 or type(ts) is not str or not ts or type(equity) is not str or not equity or type(stale) is not int or stale not in (0, 1):
                raise ValueError('invalid stored equity point')
        markers = self.db.execute('SELECT id, complete FROM runtime_equity_migration').fetchall()
        if any(type(i) is not int or i != 1 or type(v) is not int or v != 1 for i, v in markers) or len(markers) > 1:
            raise ValueError('invalid equity migration marker')
        if not markers and self.db.execute('SELECT 1 FROM runtime_equity_history LIMIT 1').fetchone():
            raise ValueError('unmarked equity archive')

    def migration_complete(self):
        """Durable marker, readable inside the current caller transaction."""
        return self.db.execute(
            'SELECT complete FROM runtime_equity_migration WHERE id=1').fetchone() == (1,)

    def migrate_legacy(self, points):
        """Import an ordered legacy list exactly once; return True only on first import.

        Must share the caller's transaction with *removal* of legacy singleton
        history; a failed insert or failed caller COMMIT must be rolled back by
        the caller, then the entire transaction can be retried without duplicates.
        """
        self._writer()
        if not isinstance(points, list):
            raise ValueError('legacy history must be an ordered list')
        if self.migration_complete():
            return False
        if self.db.execute('SELECT 1 FROM runtime_equity_history LIMIT 1').fetchone():
            raise ValueError('cannot migrate into unmarked nonempty archive')
        for point in points:
            self.db.execute('INSERT INTO runtime_equity_history(ts,equity_usdt,valuation_stale) VALUES(?,?,?)', _point(point))
        self.db.execute('INSERT INTO runtime_equity_migration(id,complete) VALUES(1,1)')
        return True

    def append(self, point):
        """Append one original point unconditionally; separated repeats are valid."""
        self._writer()
        values = _point(point)
        if not self.migration_complete():
            raise RuntimeError('migrate legacy history before appending')
        self.db.execute('INSERT INTO runtime_equity_history(ts,equity_usdt,valuation_stale) VALUES(?,?,?)', values)

    def append_snapshot(self, point):
        """Inherited snapshot policy: skip only if the immediately prior ts matches.

        Return True iff inserted. The last-row lookup is indexed by seq; no
        archive scan and no suppression of nonconsecutive repeated timestamps.
        """
        self._writer()
        values = _point(point)
        if not self.migration_complete():
            raise RuntimeError('migrate legacy history before appending')
        last = self.db.execute('SELECT ts FROM runtime_equity_history ORDER BY seq DESC LIMIT 1').fetchone()
        if last is not None and last[0] == values[0]:
            return False
        self.db.execute('INSERT INTO runtime_equity_history(ts,equity_usdt,valuation_stale) VALUES(?,?,?)', values)
        return True

    def recent(self):
        """Return at most the latest 2048 *original* points, oldest first."""
        rows = self.db.execute(
            'SELECT ts,equity_usdt,valuation_stale FROM runtime_equity_history '
            'ORDER BY seq DESC LIMIT ?', (RECENT_LIMIT,)).fetchall()
        return [dict(ts=ts, equity_usdt=equity, valuation_stale=bool(stale))
                for ts, equity, stale in reversed(rows)]

    def count(self):
        """Total immutable archive row count, not the recent-window length."""
        return self.db.execute('SELECT COUNT(*) FROM runtime_equity_history').fetchone()[0]


# PERSIST-1 B3A: an independent, complete, immutable reference receipt store.
# This is storage only; callers decide freshness and own all transactions.
import hashlib
import json
import math


_EXACT_RECEIPTS = """CREATE TABLE runtime_exact_receipts (
    digest TEXT PRIMARY KEY NOT NULL CHECK(typeof(digest) = 'text' AND length(digest) = 64 AND digest NOT GLOB '*[^0-9a-f]*'),
    receipt_json TEXT NOT NULL CHECK(typeof(receipt_json) = 'text' AND length(receipt_json) > 0)
)"""
_HEX = frozenset('0123456789abcdef')


def _receipt_json(receipt):
    """Canonical *complete* JSON; refuse Python types JSON would silently coerce."""
    if type(receipt) is not dict:
        raise ValueError('exact receipt must be a JSON object')

    active = set()

    def check(value):
        kind = type(value)
        if kind is dict or kind is list:
            if id(value) in active:
                raise ValueError('cyclic exact receipt')
            active.add(id(value))
            try:
                if kind is dict:
                    for key, item in value.items():
                        if type(key) is not str:
                            raise ValueError('exact receipt object keys must be strings')
                        check(item)
                else:
                    for item in value:
                        check(item)
            finally:
                active.remove(id(value))
        elif kind is float:
            if not math.isfinite(value):
                raise ValueError('nonfinite exact receipt number')
        elif value is not None and kind not in (str, int, bool):
            raise ValueError('unsupported exact receipt value')

    check(receipt)
    try:
        return json.dumps(receipt, sort_keys=True, separators=(',', ':'),
                          allow_nan=False, ensure_ascii=True)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError('invalid exact receipt JSON') from exc


def _receipt_digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _check_receipt_digest(digest):
    if type(digest) is not str or len(digest) != 64 or not set(digest) <= _HEX:
        raise ValueError('malformed exact receipt digest')


def _decode_receipt(digest, text):
    """Validate a single stored row, including canonical spelling and identity."""
    _check_receipt_digest(digest)
    if type(text) is not str or not text:
        raise ValueError('invalid stored exact receipt JSON')
    try:
        receipt = json.loads(text)
        if _receipt_json(receipt) != text or _receipt_digest(text) != digest:
            raise ValueError('stored exact receipt identity/content mismatch')
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid stored exact receipt identity/content') from exc
    return receipt


class ExactReceiptStore:
    """Append-only SHA256 address of an entire canonical receipt, never payload only.

    prepare() must run inside a caller-owned BEGIN, and is the only method that
    scans owned rows. put() also requires a caller transaction. Neither method
    commits or rolls back. get() and hot-path put() use the primary-key lookup.
    """

    def __init__(self, connection):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError('sqlite3.Connection required')
        self.db = connection

    def _writer(self):
        if not self.db.in_transaction:
            raise RuntimeError('caller must BEGIN a transaction before exact receipt writes')

    def prepare(self):
        self._writer()
        existing = self.db.execute(
            "SELECT type,sql FROM main.sqlite_master WHERE name='runtime_exact_receipts'"
        ).fetchone()
        if existing is not None and existing != ('table', _EXACT_RECEIPTS):
            raise ValueError('incompatible exact receipt schema')
        if existing is not None:
            objects = self.db.execute(
                "SELECT name,type,sql FROM main.sqlite_master "
                "WHERE tbl_name='runtime_exact_receipts' AND type IN ('index','trigger')"
            ).fetchall()
            if any(name != 'sqlite_autoindex_runtime_exact_receipts_1'
                   or kind != 'index' or sql is not None
                   for name, kind, sql in objects):
                raise ValueError('unexpected exact receipt index/trigger')
        else:
            self.db.execute(_EXACT_RECEIPTS)
        # Existing rows are validated only at the opening boundary. CHECK can
        # have been disabled by a prior writer, so constraints alone are not proof.
        for digest, text in self.db.execute(
                'SELECT digest,receipt_json FROM main.runtime_exact_receipts'):
            _decode_receipt(digest, text)

    def put(self, receipt):
        """Insert if absent; return digest. Identical complete receipts are idempotent."""
        self._writer()
        text = _receipt_json(receipt)
        digest = _receipt_digest(text)
        row = self.db.execute(
            'SELECT receipt_json FROM main.runtime_exact_receipts WHERE digest=?',
            (digest,)).fetchone()
        if row is not None:
            _decode_receipt(digest, row[0])
            if row[0] != text:
                raise ValueError('exact receipt digest collision')
        else:
            self.db.execute(
                'INSERT INTO main.runtime_exact_receipts(digest,receipt_json) VALUES(?,?)',
                (digest, text))
        return digest

    def get(self, digest):
        """Return a newly decoded full receipt, or fail closed for missing/corrupt data."""
        _check_receipt_digest(digest)
        row = self.db.execute(
            'SELECT receipt_json FROM main.runtime_exact_receipts WHERE digest=?',
            (digest,)).fetchone()
        if row is None:
            raise KeyError('unknown exact receipt digest')
        return _decode_receipt(digest, row[0])
