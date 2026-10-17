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


# B2 inherited runtime integration, all data and roots synthetic.
import hashlib
from paper_runtime_v2 import PaperRuntime as BaseRuntime, canonical, stamp
from paper_runtime_v5 import PaperRuntime as V5Runtime


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lab = Path(__file__).resolve().parents[1]
        self.now = [1700000000000]

    def config(self, cls):
        return self.lab / ('paper_config_v2.json' if cls is BaseRuntime
                           else 'paper_config_f1h_btcusdt.json')

    def state(self, cls, original):
        return dict(config_hash=hashlib.sha256(self.config(cls).read_bytes()).hexdigest(),
                    fixture=True, forward_start_ms=self.now[0], cursor=123456,
                    errors=7, gaps=2, last_success_ms=None, markets=[],
                    latest_error=None, warmup=0, history=list(original),
                    diagnostic='warmup', extra={'receipt': ['synthetic'], 'account': 'preserve'})

    def seed(self, root, state):
        root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(root / 'runtime.sqlite3') as db:
            db.execute('CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.execute('CREATE TABLE audit(id INTEGER PRIMARY KEY, payload TEXT NOT NULL, '
                       'previous_hash TEXT NOT NULL, hash TEXT NOT NULL)')
            db.execute('INSERT INTO state VALUES(1,?)', (canonical(state),))

    def runtime(self, cls, root):
        obj = cls(root, self.config(cls), fixture=True, clock_ms=lambda: self.now[0])
        self.addCleanup(obj.close)
        return obj

    def payload(self, root):
        with sqlite3.connect(root / 'runtime.sqlite3') as db:
            return json.loads(db.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])

    def archive(self, root):
        with sqlite3.connect(root / 'runtime.sqlite3') as db:
            return [dict(ts=ts, equity_usdt=equity, valuation_stale=bool(stale))
                    for ts, equity, stale in db.execute(
                        'SELECT ts,equity_usdt,valuation_stale FROM runtime_equity_history ORDER BY seq')]

    def test_base_and_v5_real_save_snapshot_restart_no_adjacent_duplicates(self):
        for cls in (BaseRuntime, V5Runtime):
            with self.subTest(cls=cls.__module__):
                root = Path(self.temp.name) / cls.__module__
                original = [point('old-a', '01'), point('old-b', '2.000', True),
                            point('old-a', '3e0')]
                prior = self.state(cls, original)
                self.seed(root, prior)
                obj = self.runtime(cls, root)
                self.assertEqual(self.archive(root), original)
                result = obj.snapshot()
                new = point(stamp(self.now[0]), str(obj.config['initial_equity_usdt']))
                self.assertEqual(result['equity_history'], original + [new])
                obj.snapshot()
                obj.save()
                self.assertEqual(self.archive(root), original + [new])
                obj.close()
                resumed = self.runtime(cls, root)
                resumed.snapshot()
                self.assertEqual(self.archive(root), original + [new])
                self.now[0] += 1000
                resumed.snapshot()
                self.now[0] -= 1000
                resumed.snapshot()
                self.assertEqual([x['ts'] for x in self.archive(root)][-3:],
                                 [new['ts'], stamp(self.now[0] + 1000), new['ts']])
                for field in ('cursor', 'errors', 'gaps', 'extra'):
                    self.assertEqual(self.payload(root)[field], prior[field])

    def test_large_archives_recent_cap_and_constant_status_sizes(self):
        sizes = []
        raw_status_sizes = []
        for n in (8192, 16384):
            root = Path(self.temp.name) / ('large_' + str(n))
            points = [point('%08d' % i, '%014d.0000' % i, bool(i % 2))
                      for i in range(n)]
            prior = self.state(BaseRuntime, points)
            self.seed(root, prior)
            obj = self.runtime(BaseRuntime, root)
            status = obj.snapshot()
            expected = points + [point(stamp(self.now[0]), str(obj.config['initial_equity_usdt']))]
            self.assertEqual(self.archive(root), expected)
            self.assertEqual(obj.state['history'], expected[-RECENT_LIMIT:])
            self.assertEqual(status['equity_history'], expected[-RECENT_LIMIT:])
            self.assertEqual(status['equity_history_window']['archived_points'], n + 1)
            self.assertFalse(status['equity_history_window']['complete_lifetime'])
            persisted = self.payload(root)
            for key in ('cursor', 'errors', 'gaps', 'extra'):
                self.assertEqual(persisted[key], prior[key])
            size = (len(canonical(persisted).encode()),
                    len(canonical(persisted['history']).encode()),
                    len(canonical(status).encode()))
            # Only the archived_points digit width may scale in status metadata.
            normalized_status = json.loads(canonical(status))
            normalized_status['equity_history_window']['archived_points'] = 0
            normalized_status_bytes = len(canonical(normalized_status).encode())
            sizes.append((size[0], size[1], normalized_status_bytes))
            raw_status_sizes.append(size[2])
            print('B2 archive=%d singleton_bytes=%d history_bytes=%d status_bytes=%d normalized_status_bytes=%d' %
                  ((n + 1,) + size + (normalized_status_bytes,)))
            obj.close()
            resumed = self.runtime(BaseRuntime, root)
            self.assertEqual(self.archive(root), expected)
            self.assertEqual(resumed.state['history'], expected[-RECENT_LIMIT:])
        self.assertEqual(sizes[0], sizes[1])
        self.assertEqual(raw_status_sizes[1] - raw_status_sizes[0],
                         len(str(16385)) - len(str(8193)))

    def bare_legacy(self, label, points):
        root = Path(self.temp.name) / label
        prior = self.state(BaseRuntime, points)
        self.seed(root, prior)
        db = sqlite3.connect(root / 'runtime.sqlite3')
        db.execute('PRAGMA foreign_keys=ON')
        obj = BaseRuntime.__new__(BaseRuntime)
        obj.db, obj.state = db, json.loads(canonical(prior))
        self.addCleanup(db.close)
        return root, db, obj, prior

    def test_sql_migration_and_singleton_write_failures_atomic_retry(self):
        points = [point('first'), point('second', '2.000', True)]
        root, db, obj, original = self.bare_legacy('sql', points)
        denied = []
        def deny(action, table, col, database, trigger):
            if action == sqlite3.SQLITE_INSERT and table == 'runtime_equity_history':
                denied.append(table)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(deny)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                obj.save()
        finally:
            db.set_authorizer(None)
        self.assertTrue(denied)
        self.assertEqual(obj.state, original)
        self.assertEqual(self.payload(root), original)
        self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='runtime_equity_history'").fetchone()[0], 0)
        obj.save()
        self.assertEqual(self.archive(root), points)
        before = json.loads(canonical(obj.state))
        db.execute("CREATE TRIGGER fail_singleton BEFORE INSERT ON state "
                   "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            obj.save(equity_point=point('later', '4'))
        self.assertEqual(obj.state, before)
        self.assertEqual(self.archive(root), points)
        db.execute('DROP TRIGGER fail_singleton')
        obj.save(equity_point=point('later', '4'))
        self.assertEqual(self.archive(root), points + [point('later', '4')])

    def test_real_deferred_commit_failure_rollback_and_retry(self):
        points = [point('a'), point('b', '2', True)]
        root, db, obj, original = self.bare_legacy('commit', points)
        db.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE child(parent_id INTEGER REFERENCES parent(id) '
                   'DEFERRABLE INITIALLY DEFERRED)')
        db.execute('CREATE TRIGGER fail_commit AFTER INSERT ON state '
                   'BEGIN INSERT INTO child VALUES(999); END')
        with self.assertRaises(sqlite3.IntegrityError):
            obj.save()
        self.assertFalse(db.in_transaction)
        self.assertEqual(self.payload(root), original)
        self.assertEqual(obj.state, original)
        self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='runtime_equity_history'").fetchone()[0], 0)
        db.execute('DROP TRIGGER fail_commit')
        obj.save()
        self.assertEqual(self.archive(root), points)


# B3A independent exact receipt storage tests; no runtime integration.
from runtime_equity_history import ExactReceiptStore


class ExactReceiptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'receipts.sqlite3'
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE unrelated (value TEXT NOT NULL)')
        self.db.execute("INSERT INTO unrelated VALUES ('preserved')")
        self.db.execute('BEGIN')
        self.store = ExactReceiptStore(self.db)
        self.store.prepare()
        self.db.execute('COMMIT')

    def receipt(self, observed='2026-10-14T12:00:00Z'):
        return {'payload': {'exchangeInfo': {'symbols': [
            {'symbol': 'BTCUSDT', 'filters': [{'tickSize': '0.01000000'},
                                             {'quantity': 12, 'enabled': True}]},
            {'symbol': 'ETHUSDT', 'filters': []}]},
                            'precision': '1.00000000000000000001',
                            'small': 0.125, 'none': None},
                'observed_at': observed, 'provenance': {'source': 'synthetic',
                                                       'attempts': [1, 2, 3]},
                'extra': {'array': [False, None, '0042', {'nested': ['x', 'y']}]}}

    def put_committed(self, receipt):
        self.db.execute('BEGIN')
        digest = self.store.put(receipt)
        self.assertTrue(self.db.in_transaction)
        self.db.execute('COMMIT')
        return digest

    def rows(self):
        return self.db.execute(
            'SELECT digest,receipt_json FROM runtime_exact_receipts ORDER BY digest'
        ).fetchall()

    def test_nested_exact_roundtrip_and_returned_data_is_detached(self):
        source = self.receipt()
        digest = self.put_committed(source)
        self.assertEqual(self.store.get(digest), source)
        self.assertEqual(len(digest), 64)
        source['payload']['exchangeInfo']['symbols'].clear()
        returned = self.store.get(digest)
        returned['extra']['array'][3]['nested'].append('changed')
        self.assertEqual(len(self.store.get(digest)['payload']['exchangeInfo']['symbols']), 2)
        self.assertEqual(self.store.get(digest)['extra']['array'][3]['nested'], ['x', 'y'])

    def test_idempotent_duplicate_and_key_order(self):
        receipt = self.receipt()
        first = self.put_committed(receipt)
        second = self.put_committed(dict(reversed(list(receipt.items()))))
        self.assertEqual(first, second)
        self.assertEqual(len(self.rows()), 1)

    def test_timestamp_participates_in_identity(self):
        a = self.put_committed(self.receipt('2026-10-14T12:00:00Z'))
        b = self.put_committed(self.receipt('2026-10-14T12:00:01Z'))
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.rows()), 2)
        self.assertNotEqual(self.store.get(a)['observed_at'],
                            self.store.get(b)['observed_at'])

    def test_caller_rollback_and_reopen(self):
        self.db.execute('BEGIN')
        digest = self.store.put(self.receipt())
        self.db.execute('ROLLBACK')
        with self.assertRaises(KeyError):
            self.store.get(digest)
        self.assertEqual(len(self.rows()), 0)
        saved = self.put_committed(self.receipt())
        reopened = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(reopened.close)
        reopened.execute('BEGIN')
        other = ExactReceiptStore(reopened)
        other.prepare()
        reopened.execute('COMMIT')
        self.assertEqual(other.get(saved), self.receipt())
        self.assertEqual(len(self.rows()), 1)

    def test_real_deferred_commit_failure_then_retry(self):
        self.db.execute('CREATE TABLE parent(id INTEGER PRIMARY KEY)')
        self.db.execute('CREATE TABLE child(parent_id INTEGER REFERENCES parent(id) '
                        'DEFERRABLE INITIALLY DEFERRED)')
        self.db.execute('BEGIN')
        digest = self.store.put(self.receipt())
        self.db.execute('INSERT INTO child VALUES (999)')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('COMMIT')
        self.assertTrue(self.db.in_transaction)
        self.db.execute('ROLLBACK')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.put_committed(self.receipt()), digest)
        self.assertEqual(len(self.rows()), 1)

    def test_malformed_missing_and_unsupported_json_fail_closed(self):
        for bad in ('', 'g' * 64, 'A' * 64, 'a' * 63, None, 42):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.get(bad)
        with self.assertRaises(KeyError):
            self.store.get('0' * 64)
        invalid = [{'payload': float('nan')}, {'payload': float('inf')},
                   {'payload': {1: 'bad'}}, {'payload': (1, 2)},
                   {'payload': object()}]
        for item in invalid:
            with self.subTest(item=str(type(item['payload']))):
                self.db.execute('BEGIN')
                with self.assertRaises(ValueError):
                    self.store.put(item)
                self.db.execute('ROLLBACK')
        with self.assertRaises(RuntimeError):
            self.store.put(self.receipt())
        self.assertEqual(self.rows(), [])

    def test_corrupt_existing_digest_and_payload_refused(self):
        receipt = self.receipt()
        digest = self.put_committed(receipt)
        original = self.rows()
        self.db.execute('UPDATE runtime_exact_receipts SET receipt_json=? WHERE digest=?',
                        ('{"tampered":true}', digest))
        with self.assertRaises(ValueError):
            self.store.get(digest)
        self.db.execute('BEGIN')
        with self.assertRaises(ValueError):
            self.store.prepare()
        self.db.execute('ROLLBACK')
        self.assertNotEqual(self.rows(), original)
        self.db.execute('UPDATE runtime_exact_receipts SET receipt_json=? WHERE digest=?',
                        (original[0][1], digest))
        self.db.execute('PRAGMA ignore_check_constraints=ON')
        self.db.execute('UPDATE runtime_exact_receipts SET digest=? WHERE digest=?',
                        ('z' * 64, digest))
        self.db.execute('PRAGMA ignore_check_constraints=OFF')
        bad_rows = self.rows()
        self.db.execute('BEGIN')
        with self.assertRaises(ValueError):
            self.store.prepare()
        self.db.execute('ROLLBACK')
        self.assertEqual(self.rows(), bad_rows)
        self.assertEqual(self.db.execute('SELECT value FROM unrelated').fetchone(),
                         ('preserved',))

    def test_reject_incompatible_and_quoted_literal_variants_unchanged(self):
        canonical_sql = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='runtime_exact_receipts'"
        ).fetchone()[0]
        alternatives = [
            canonical_sql.replace("length(digest) = 64", "length(digest) > 0"),
            canonical_sql.replace("'text'", "'TEXT'", 1),
            canonical_sql.replace("'text'", "'te xt'", 1),
            canonical_sql.replace("digest NOT GLOB '*[^0-9a-f]*'", "1 = 1"),
        ]
        for i, sql in enumerate(alternatives):
            with self.subTest(variant=i):
                path = Path(self.temp.name) / ('different_' + str(i) + '.sqlite3')
                db = sqlite3.connect(path, isolation_level=None)
                try:
                    db.execute(sql)
                    db.execute('CREATE TABLE unrelated(value TEXT)')
                    db.execute("INSERT INTO unrelated VALUES ('keep')")
                    before = db.execute(
                        "SELECT name,type,sql FROM sqlite_master ORDER BY name").fetchall()
                    db.execute('BEGIN')
                    with self.assertRaisesRegex(ValueError, 'incompatible exact receipt schema'):
                        ExactReceiptStore(db).prepare()
                    self.assertTrue(db.in_transaction)
                    self.assertEqual(db.execute(
                        "SELECT name,type,sql FROM sqlite_master ORDER BY name").fetchall(),
                        before)
                    db.execute('ROLLBACK')
                    self.assertEqual(db.execute('SELECT value FROM unrelated').fetchone(),
                                     ('keep',))
                finally:
                    db.close()

    def test_existing_equity_and_unrelated_tables_survive(self):
        self.db.execute('BEGIN')
        history = EquityHistoryStore(self.db)
        history.prepare()
        history.migrate_legacy([point('one', '1.00')])
        self.store.put(self.receipt())
        self.db.execute('COMMIT')
        self.assertEqual(history.recent(), [point('one', '1.00')])
        self.assertEqual(history.count(), 1)
        self.assertEqual(self.db.execute('SELECT value FROM unrelated').fetchone(),
                         ('preserved',))


# B3B: full exchangeInfo reference integration through inherited save.
class ReferenceRuntimeTests(RuntimeIntegrationTests):
    def reference_receipt(self, observed=1700000000000, count=1):
        from datetime import datetime, timezone
        received_at = datetime.fromtimestamp(observed / 1000, timezone.utc).isoformat()
        return {'payload': {'symbols': [
            {'symbol': 'ETHUSDT', 'filters': [{'tickSize': '0.01000000'},
                                             {'minNotional': '20.000000000000001'}],
             'extra': {'levels': ['0.100', '0.200']}} for _ in range(count)]},
                'received_at': received_at,
                'received_at_ms': observed, 'source_timestamp_ms': observed,
                'provenance': {'fixture': True, 'ordering': ['z', 'a']}}

    def seeded(self, cls, name, receipt_marker=None, history=None):
        root = Path(self.temp.name) / name
        state = self.state(cls, history or [])
        if receipt_marker is not None:
            state['reference'] = receipt_marker
        self.seed(root, state)
        return root, state

    def stored(self, root):
        with sqlite3.connect(root / 'runtime.sqlite3') as db:
            return db.execute('SELECT digest,receipt_json FROM runtime_exact_receipts').fetchall()

    def test_base_v5_legacy_migrate_reopen_full_exact_and_equity(self):
        for cls in (BaseRuntime, V5Runtime):
            with self.subTest(cls=cls.__module__):
                receipt = self.reference_receipt()
                historical = [point('x', '1.000'), point('y', '2.00')]
                root, prior = self.seeded(cls, cls.__module__ + '_reference', receipt, historical)
                obj = self.runtime(cls, root)
                self.assertEqual(obj.state['reference'], receipt)
                payload = self.payload(root)
                self.assertNotIn('reference', payload)
                self.assertEqual(len(payload['reference_digest']), 64)
                self.assertEqual(self.stored(root)[0][0], payload['reference_digest'])
                self.assertEqual(self.archive(root), historical)
                self.assertEqual(len(self.stored(root)), 1)
                for k in ('cursor', 'errors', 'gaps', 'extra'):
                    self.assertEqual(payload[k], prior[k])
                obj.save()
                obj.close()
                resumed = self.runtime(cls, root)
                self.assertEqual(resumed.state['reference'], receipt)
                self.assertEqual(self.archive(root), historical)
                self.assertEqual(len(self.stored(root)), 1)

    def test_absent_reference_remains_absent(self):
        root, _ = self.seeded(BaseRuntime, 'no_reference')
        obj = self.runtime(BaseRuntime, root)
        self.assertNotIn('reference', obj.state)
        self.assertNotIn('reference_digest', self.payload(root))
        self.assertEqual(self.stored(root), [])

    def test_timestamp_changes_identity_and_unchanged_save_idempotent(self):
        receipt = self.reference_receipt()
        root, _ = self.seeded(BaseRuntime, 'identity', receipt)
        obj = self.runtime(BaseRuntime, root)
        original_digest = self.payload(root)['reference_digest']
        for _ in range(3):
            obj.save()
        self.assertEqual(len(self.stored(root)), 1)
        self.assertEqual(self.payload(root)['reference_digest'], original_digest)
        changed = self.reference_receipt(observed=receipt['received_at_ms'] + 1)
        obj.state['reference'] = changed
        obj.save()
        self.assertNotEqual(self.payload(root)['reference_digest'], original_digest)
        self.assertEqual(len(self.stored(root)), 2)
        self.assertEqual(obj.state['reference'], changed)

    def test_missing_and_corrupt_pointer_refused_without_mutation(self):
        for label, corrupt in (('missing', False), ('corrupt', True)):
            with self.subTest(label=label):
                root, _ = self.seeded(BaseRuntime, label, self.reference_receipt())
                obj = self.runtime(BaseRuntime, root)
                obj.close()
                with sqlite3.connect(root / 'runtime.sqlite3') as db:
                    digest = self.payload(root)['reference_digest']
                    if corrupt:
                        db.execute('UPDATE runtime_exact_receipts SET receipt_json=? WHERE digest=?',
                                   ('{"damaged":true}', digest))
                    else:
                        db.execute('DELETE FROM runtime_exact_receipts WHERE digest=?', (digest,))
                before = self.payload(root)
                archive_before = self.archive(root)
                with self.assertRaises((KeyError, ValueError)):
                    self.runtime(BaseRuntime, root)
                self.assertEqual(self.payload(root), before)
                self.assertEqual(self.archive(root), archive_before)

    def test_sql_failure_rolls_back_legacy_reference_and_equity(self):
        receipt = self.reference_receipt()
        root, db, obj, original = self.bare_legacy('ref_sql', [point('a')])
        obj.state['reference'] = receipt
        original['reference'] = receipt
        db.execute('CREATE TRIGGER no_singleton BEFORE INSERT ON state '
                   "BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            obj.save()
        self.assertEqual(obj.state, original)
        self.assertEqual(self.payload(root)['history'], original['history'])
        self.assertNotIn('reference', self.payload(root))
        self.assertEqual(db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='runtime_exact_receipts'").fetchone()[0], 0)
        db.execute('DROP TRIGGER no_singleton')
        obj.save()
        self.assertEqual(self.archive(root), [point('a')])
        self.assertEqual(len(self.stored(root)), 1)

    def test_deferred_commit_failure_then_retry_both_stores(self):
        receipt = self.reference_receipt()
        root, db, obj, _ = self.bare_legacy('ref_commit', [point('a')])
        obj.state['reference'] = receipt
        db.execute('CREATE TABLE ref_parent(id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE ref_child(parent_id INTEGER REFERENCES ref_parent(id) '
                   'DEFERRABLE INITIALLY DEFERRED)')
        db.execute('CREATE TRIGGER ref_commit_failure AFTER INSERT ON state '
                   'BEGIN INSERT INTO ref_child VALUES(999); END')
        original_memory = json.loads(canonical(obj.state))
        with self.assertRaises(sqlite3.IntegrityError):
            obj.save()
        self.assertEqual(obj.state, original_memory)
        self.assertFalse(db.in_transaction)
        self.assertEqual(db.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='runtime_exact_receipts'").fetchone()[0], 0)
        db.execute('DROP TRIGGER ref_commit_failure')
        obj.save()
        self.assertEqual(self.archive(root), [point('a')])
        self.assertEqual(len(self.stored(root)), 1)

    def test_singleton_does_not_embed_large_receipt(self):
        sizes = []
        for n in (1, 800):
            receipt = self.reference_receipt(count=n)
            root, _ = self.seeded(BaseRuntime, 'reference_size_' + str(n), receipt)
            obj = self.runtime(BaseRuntime, root)
            saved = self.payload(root)
            encoded = canonical(saved).encode()
            self.assertNotIn('reference', saved)
            self.assertNotIn('minNotional', encoded.decode())
            sizes.append(len(encoded))
            self.assertEqual(len(self.stored(root)), 1)
            print('B3B symbols=%d singleton_bytes=%d receipt_bytes=%d' %
                  (n, len(encoded), len(canonical(receipt).encode())))
            obj.close()
        self.assertEqual(sizes[0], sizes[1])

    def test_reference_freshness_boundaries_and_fetch_behavior(self):
        # Exercise unmodified reference() logic without network through a stub.
        from unittest.mock import Mock
        from paper_market import receipt_ms
        for delta, should_fetch in ((86400000 - 1, False), (86400000, True),
                                    (-1, True), (0, False)):
            with self.subTest(delta=delta):
                root, _ = self.seeded(BaseRuntime, 'fresh_' + str(delta),
                                      self.reference_receipt())
                obj = self.runtime(BaseRuntime, root)
                observed = receipt_ms(obj.state['reference'])
                self.now[0] = observed + delta
                replacement = self.reference_receipt(observed + delta)
                obj.fetch = Mock(return_value=replacement)
                got = obj.reference()
                self.assertEqual(got, replacement if should_fetch else self.reference_receipt())
                self.assertEqual(obj.fetch.call_count, int(should_fetch))
                obj.close()
                self.now[0] = 1700000000000



    def test_recent_window_never_deletes_append_only_archive(self):
        root, _ = self.seeded(BaseRuntime, 'append-only-window', history=[])
        obj = self.open_runtime(BaseRuntime, root)
        history = obj._equity_history_store
        obj.db.execute('BEGIN')
        for n in range(RECENT_LIMIT + 17):
            history.append(point('t' + str(n), str(n)))
        obj.db.execute('COMMIT')
        self.assertEqual(history.count(), RECENT_LIMIT + 17)
        recent = history.recent()
        self.assertEqual(len(recent), RECENT_LIMIT)
        self.assertEqual(recent[0]['ts'], 't17')
        self.assertEqual(recent[-1]['ts'], 't' + str(RECENT_LIMIT + 16))
        obj.close()


if __name__ == '__main__':
    unittest.main()
