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


def _normalized(sql):
    return ''.join(sql.split()).lower()


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
            if kind != 'table' or _normalized(sql or '') != _normalized(_SCHEMAS[name]):
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
