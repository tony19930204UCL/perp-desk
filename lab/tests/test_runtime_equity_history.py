"""B1 isolated SQLite tests; no paper runtime mutation or outbound I/O."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from runtime_equity_history import EquityHistoryStore, RECENT_LIMIT


def point(ts, equity='100.000', stale=False):
    return dict(ts=str(ts), equity_usdt=equity, valuation_stale=stale)


class EquityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'runtime.sqlite3'
        self.connections = []
        self.db, self.history = self.open_store()

    def open_store(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        db.execute('PRAGMA foreign_keys=ON')
        self.connections.append(db)
        db.execute('BEGIN')
        history = EquityHistoryStore(db)
        history.prepare()
        db.execute('COMMIT')
        return db, history

    def tearDown(self):
        for db in self.connections:
            db.close()

    def migrate(self, points):
        self.db.execute('BEGIN')
        result = self.history.migrate_legacy(points)
        self.db.execute('COMMIT')
        return result

    def raw(self):
        return self.db.execute('SELECT seq,ts,equity_usdt,valuation_stale FROM runtime_equity_history ORDER BY seq').fetchall()

    def test_ordered_original_strings_and_exact_types(self):
        source = [point('2026-10-14T01:00:00+00:00', '1.000000000000000000009'),
                  point('2026-10-14T01:05:00+00:00', '-0.0000', True),
                  point('2026-10-14T01:10:00+00:00', '1e-25')]
        self.assertTrue(self.migrate(source))
        self.assertEqual(self.history.recent(), source)
        self.assertEqual(self.raw(), [(1, source[0]['ts'], source[0]['equity_usdt'], 0),
                                      (2, source[1]['ts'], source[1]['equity_usdt'], 1),
                                      (3, source[2]['ts'], source[2]['equity_usdt'], 0)])

    def test_snapshot_immediately_equal_timestamp_is_suppressed_only_once(self):
        self.migrate([])
        self.db.execute('BEGIN')
        self.assertTrue(self.history.append_snapshot(point('a', '01')))
        self.assertFalse(self.history.append_snapshot(point('a', '99')))
        self.assertTrue(self.history.append_snapshot(point('b', '02')))
        self.db.execute('COMMIT')
        self.assertEqual(self.history.recent(), [point('a', '01'), point('b', '02')])

    def test_separated_repeats_and_unconditional_append_survive(self):
        self.migrate([point('a'), point('b'), point('a', '30')])
        self.db.execute('BEGIN')
        self.assertFalse(self.history.append_snapshot(point('a', '40')))
        self.history.append(point('a', '50'))
        self.assertTrue(self.history.append_snapshot(point('c', '60')))
        self.assertTrue(self.history.append_snapshot(point('a', '70')))
        self.db.execute('COMMIT')
        self.assertEqual([x['ts'] for x in self.history.recent()], ['a', 'b', 'a', 'a', 'c', 'a'])

    def test_legacy_migration_reopen_retry_is_idempotent(self):
        legacy = [point('1', '1.00'), point('2', '2.00', True), point('1', '1.30')]
        self.db.execute('BEGIN')
        self.db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        self.db.execute('INSERT INTO state VALUES (1, ?)', (json.dumps({'history': legacy, 'other': 42}),))
        self.assertTrue(self.history.migrate_legacy(legacy))
        self.assertTrue(self.history.migration_complete())
        self.db.execute('UPDATE state SET payload=? WHERE id=1', (json.dumps({'other': 42}),))
        self.db.execute('COMMIT')
        _, reopened = self.open_store()
        self.assertTrue(reopened.migration_complete())
        reopened.db.execute('BEGIN')
        self.assertFalse(reopened.migrate_legacy(legacy))
        reopened.db.execute('COMMIT')
        self.assertEqual(reopened.count(), len(legacy))
        self.assertEqual(reopened.recent(), legacy)
        self.assertEqual(json.loads(self.db.execute('SELECT payload FROM state').fetchone()[0]), {'other': 42})

    def test_mid_migration_invalid_point_rollback_then_retry(self):
        legacy = [point('1'), point('2'), dict(ts='bad', equity_usdt='3', valuation_stale=1)]
        self.db.execute('BEGIN')
        with self.assertRaises(ValueError):
            self.history.migrate_legacy(legacy)
        self.assertEqual(self.history.count(), 2)
        self.db.execute('ROLLBACK')
        self.assertFalse(self.history.migration_complete())
        self.assertEqual(self.history.count(), 0)
        self.assertTrue(self.migrate(legacy[:2] + [point('3')]))
        self.assertEqual(self.history.count(), 3)

    def test_caller_rollback_and_real_deferred_fk_commit_failure_retry(self):
        self.db.execute('CREATE TABLE parent (id INTEGER PRIMARY KEY)')
        self.db.execute('CREATE TABLE child (parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)')
        self.db.execute('BEGIN')
        self.assertTrue(self.history.migrate_legacy([point('first')]))
        self.db.execute('INSERT INTO child VALUES (999)')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('COMMIT')
        self.assertTrue(self.db.in_transaction)
        self.db.execute('ROLLBACK')
        self.assertEqual(self.history.count(), 0)
        self.assertFalse(self.history.migration_complete())
        self.migrate([point('first')])
        self.db.execute('BEGIN')
        self.history.append_snapshot(point('second'))
        self.db.execute('ROLLBACK')
        self.assertEqual(self.history.recent(), [point('first')])
        self.db.execute('BEGIN')
        self.history.append_snapshot(point('second'))
        self.db.execute('COMMIT')
        self.assertEqual(self.history.count(), 2)

    def test_missing_constraints_refused_without_rewrite(self):
        self.db.execute('BEGIN')
        self.db.execute('DROP TABLE runtime_equity_history')
        self.db.execute('CREATE TABLE runtime_equity_history(seq INTEGER PRIMARY KEY, ts TEXT, equity_usdt TEXT, valuation_stale INTEGER)')
        self.db.execute('COMMIT')
        original = self.db.execute("SELECT sql FROM sqlite_master WHERE name='runtime_equity_history'").fetchone()[0]
        self.db.execute('BEGIN')
        with self.assertRaisesRegex(ValueError, 'incompatible'):
            self.history.prepare()
        self.db.execute('ROLLBACK')
        self.assertEqual(self.db.execute("SELECT sql FROM sqlite_master WHERE name='runtime_equity_history'").fetchone()[0], original)

    def test_invalid_preexisting_data_refused_unchanged(self):
        self.migrate([])
        self.db.execute('PRAGMA ignore_check_constraints=ON')
        self.db.execute('BEGIN')
        self.db.execute('INSERT INTO runtime_equity_history(ts,equity_usdt,valuation_stale) VALUES(?,?,?)', ('a', '1', 3))
        self.db.execute('COMMIT')
        self.db.execute('PRAGMA ignore_check_constraints=OFF')
        before = self.raw()
        self.db.execute('BEGIN')
        with self.assertRaisesRegex(ValueError, 'invalid stored equity'):
            self.history.prepare()
        self.db.execute('ROLLBACK')
        self.assertEqual(before, self.raw())

    def test_recent_cap_at_2049_and_8192_preserves_all_rows(self):
        for total in (2049, 8192):
            with self.subTest(total=total):
                db = sqlite3.connect(':memory:', isolation_level=None)
                self.connections.append(db)
                db.execute('BEGIN')
                history = EquityHistoryStore(db)
                history.prepare()
                original = [point(str(i), str(i) + '.000', bool(i % 2)) for i in range(total)]
                history.migrate_legacy(original)
                db.execute('COMMIT')
                self.assertEqual(history.count(), total)
                self.assertEqual(len(history.recent()), RECENT_LIMIT)
                self.assertEqual(history.recent(), original[-RECENT_LIMIT:])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM runtime_equity_history').fetchone()[0], total)

    def test_owned_tables_only_no_hidden_commit_and_require_transaction(self):
        self.db.execute('CREATE TABLE untouched(v TEXT)')
        self.db.execute("INSERT INTO untouched VALUES('untouched')")
        self.db.execute('BEGIN')
        self.history.migrate_legacy([point('x')])
        self.assertTrue(self.db.in_transaction)
        self.db.execute('ROLLBACK')
        self.assertEqual(self.db.execute('SELECT v FROM untouched').fetchone()[0], 'untouched')
        self.assertFalse(self.history.migration_complete())
        with self.assertRaises(RuntimeError):
            self.history.migrate_legacy([point('x')])
        with self.assertRaises(RuntimeError):
            self.history.append(point('y'))


    def _assert_changed_check_literal_refused(self, replacement):
        # Independent on-disk database; preserve the original canonical store.
        bad_path = Path(self.temp.name) / ('variant_' + replacement.replace(' ', '_') + '.sqlite3')
        db = sqlite3.connect(bad_path, isolation_level=None)
        self.connections.append(db)
        canonical = {name: sql for name, sql in self.db.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN (?, ?)",
            ('runtime_equity_history', 'runtime_equity_migration'))}
        self.assertEqual(len(canonical), 2)
        changed = canonical['runtime_equity_history'].replace(
            "typeof(ts) = 'text'", "typeof(ts) = '" + replacement + "'", 1)
        self.assertNotEqual(changed, canonical['runtime_equity_history'])
        db.execute(changed)
        db.execute(canonical['runtime_equity_migration'])
        db.execute('CREATE TABLE untouched (v TEXT)')
        db.execute("INSERT INTO untouched VALUES ('before')")
        before_schema = db.execute(
            "SELECT name, type, sql FROM sqlite_master ORDER BY name").fetchall()
        before_data = db.execute('SELECT * FROM runtime_equity_history').fetchall()
        before_marker = db.execute('SELECT * FROM runtime_equity_migration').fetchall()
        db.execute('BEGIN')
        db.execute("INSERT INTO untouched VALUES ('uncommitted')")
        with self.assertRaisesRegex(ValueError, 'incompatible equity history schema'):
            EquityHistoryStore(db).prepare()
        self.assertTrue(db.in_transaction, 'prepare must not commit or roll back')
        self.assertEqual(db.execute(
            "SELECT name, type, sql FROM sqlite_master ORDER BY name").fetchall(), before_schema)
        self.assertEqual(db.execute('SELECT * FROM runtime_equity_history').fetchall(), before_data)
        self.assertEqual(db.execute('SELECT * FROM runtime_equity_migration').fetchall(), before_marker)
        db.execute('ROLLBACK')
        self.assertEqual(db.execute('SELECT * FROM untouched').fetchall(), [('before',)])
        self.assertEqual(db.execute(
            "SELECT name, type, sql FROM sqlite_master ORDER BY name").fetchall(), before_schema)

    def test_schema_quoted_typeof_case_change_refused(self):
        self._assert_changed_check_literal_refused('TEXT')

    def test_schema_quoted_typeof_whitespace_change_refused(self):
        self._assert_changed_check_literal_refused('te xt')

    def test_canonical_reopen_migrate_and_append_after_validation(self):
        original = [point('alpha', '1.00'), point('beta', '2.000', True)]
        self.assertTrue(self.migrate(original))
        _, reopened = self.open_store()
        reopened.db.execute('BEGIN')
        self.assertFalse(reopened.migrate_legacy(original))
        self.assertTrue(reopened.append_snapshot(point('alpha', '3.00')))
        self.assertFalse(reopened.append_snapshot(point('alpha', '4.00')))
        reopened.db.execute('COMMIT')
        self.assertEqual(reopened.count(), 3)
        self.assertEqual(reopened.recent(), original + [point('alpha', '3.00')])


if __name__ == '__main__':
    unittest.main()
