"""Synthetic SQLite persistence regressions; no live execution or network."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from sim_broker import SimBroker, InstrumentSettings, ExecutionModel, Intent


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sim-persistence-')
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'offline.sqlite'
        self.opened = []

    def broker(self, compact=False, path=None, version='v1'):
        b = SimBroker(path or self.path, initial_cash=D('100'),
                      instruments=[InstrumentSettings('TEST', D('0.0002'), D('0.0005'),
                                                      D('0.1'), D('0.1'), D('5'), D('100'))],
                      execution=ExecutionModel(10, 2, None),
                      risk=None, version_id=version, forward_start=100,
                      compact_seen_events=compact)
        self.opened.append(b)
        return b

    def tearDown(self):
        for b in self.opened:
            b.close()

    def mark(self, eid='m1', price='100', ts=100):
        return dict(type='mark', event_id=eid, symbol='TEST', ts=ts, price=price)

    def digest(self, event):
        event = json.loads(json.dumps(event, default=str, sort_keys=True))
        return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def saved(self):
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()
            history = db.execute('SELECT event_id, digest FROM sim_seen_events ORDER BY event_id').fetchall()
        return json.loads(row[0]), history

    def reopen(self, compact=False):
        self.opened[-1].close()
        return self.broker(compact=compact)

    def legacy(self, full=True, count=2):
        b = self.broker()
        b.on_event(self.mark())
        b.on_event(self.mark('m2', '101', 101))
        events = {key: self.mark(key, '100' if key == 'm1' else '101', 100 if key == 'm1' else 101)
                  for key in ('m1', 'm2')}
        b.close()
        with sqlite3.connect(self.path) as db:
            state = json.loads(db.execute('SELECT payload FROM sim_broker_state').fetchone()[0])
            state['seen_events'] = events if full else {k: self.digest(v) for k, v in events.items()}
            db.execute('UPDATE sim_broker_state SET payload=?', (json.dumps(state),))
            db.execute('DELETE FROM sim_seen_events')
        return events

    def test_new_database_creates_constraint_table_and_small_payload(self):
        b = self.broker()
        b.on_event(self.mark())
        state, history = self.saved()
        self.assertNotIn('seen_events', state)
        self.assertEqual(history, [('m1', self.digest(self.mark()))])
        self.assertEqual(b.seen_events, {'m1': self.mark()})
        with sqlite3.connect(self.path) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO sim_seen_events VALUES (?,NULL)', ('bad',))
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('INSERT INTO sim_seen_events VALUES (?,?)', ('m1', 'anything'))

    def test_legacy_full_history_migrates_without_changing_other_state(self):
        events = self.legacy(full=True)
        with sqlite3.connect(self.path) as db:
            before = json.loads(db.execute('SELECT payload FROM sim_broker_state').fetchone()[0])
        b = self.broker()
        after, rows = self.saved()
        self.assertNotIn('seen_events', after)
        self.assertEqual(after, {k: v for k, v in before.items() if k != 'seen_events'})
        self.assertEqual(dict(rows), {k: self.digest(v) for k, v in events.items()})
        self.assertEqual(b.marks, {'TEST': '101'})

    def test_legacy_digest_history_migrates(self):
        events = self.legacy(full=False)
        b = self.broker(compact=False)
        self.assertEqual(b.seen_events, {k: self.digest(v) for k, v in events.items()})
        self.assertNotIn('seen_events', self.saved()[0])

    def test_migration_runs_only_once_reopen(self):
        self.legacy()
        b = self.broker()
        first = self.saved()
        b.close()
        self.broker()
        self.assertEqual(self.saved(), first)

    def test_duplicate_same_and_conflicting_after_migration(self):
        self.legacy()
        b = self.broker()
        before = self.saved()
        b.on_event(self.mark())
        self.assertEqual(self.saved(), before)
        with self.assertRaisesRegex(ValueError, 'conflicting event ID'):
            b.on_event(self.mark(price='102'))
        self.assertEqual(self.saved(), before)

    def test_duplicate_after_restart_and_new_digest(self):
        b = self.broker()
        b.on_event(self.mark())
        b = self.reopen(compact=True)
        b.on_event(self.mark())
        with self.assertRaisesRegex(ValueError, 'conflicting event ID'):
            b.on_event(self.mark(price='102'))
        b.on_event(self.mark('m2', '101', 101))
        self.assertEqual(len(self.saved()[1]), 2)

    def test_midway_migration_error_is_atomic_and_retryable(self):
        self.legacy()
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON sim_seen_events
                          WHEN NEW.event_id='m2'
                          BEGIN SELECT RAISE(FAIL, 'synthetic migration failure'); END""")
            payload_before = db.execute('SELECT payload FROM sim_broker_state').fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            self.broker()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT payload FROM sim_broker_state').fetchone()[0],
                             payload_before)
            self.assertEqual(db.execute('SELECT count(*) FROM sim_seen_events').fetchone()[0], 0)
            db.execute('DROP TRIGGER reject_second')
        b = self.broker()
        self.assertEqual(len(b.seen_events), 2)

    def test_event_insert_failure_rollback_and_retry(self):
        b = self.broker()
        baseline = self.saved()
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TRIGGER reject_once BEFORE INSERT ON sim_seen_events
                          BEGIN SELECT RAISE(FAIL, 'synthetic insert failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            b.on_event(self.mark())
        self.assertEqual(b.seen_events, {})
        self.assertEqual(b.marks, {})
        self.assertEqual(self.saved(), baseline)
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER reject_once')
        b.on_event(self.mark())
        self.assertEqual(len(self.saved()[1]), 1)

    def test_processing_failure_rollback_and_retry(self):
        b = self.broker()
        bad = dict(self.mark(), type='funding', finalized=True,
                   settlement_ts=100, rate='.01', mark='100', rate_type='Invalid')
        with self.assertRaises(ValueError):
            b.on_event(bad)
        self.assertEqual(self.saved()[1], [])
        b.on_event(self.mark())
        self.assertEqual(len(self.saved()[1]), 1)

    def test_submit_cancel_and_reopen_still_persist(self):
        b = self.broker()
        intent = Intent('a', 'TEST', 'BUY', D('1'), D('90'), 100,
                        D('.1'), D('.1'), D('5'), D('100'), 'TAKER', False)
        result = b.submit(intent)
        self.assertEqual(result['status'], 'REJECTED')
        b = self.reopen()
        self.assertEqual(json.loads(json.dumps(b.submit(intent), default=str, sort_keys=True)),
                         json.loads(json.dumps(result, default=str, sort_keys=True)))
        self.assertEqual(b.orders['order:a']['status'], 'REJECTED')
        # A pending synthetic reduce-only order is not needed to test the
        # cancellation persistence path: seed a pending order in memory.
        b.orders['order:pending'] = dict(order_id='order:pending', status='PENDING')
        b._save()
        b.cancel('order:pending', ts=100, reason='unit')
        b = self.reopen()
        self.assertEqual(b.orders['order:pending']['status'], 'CANCELED')
        self.assertEqual(self.saved()[1], [])

    def test_both_compact_flags_store_digests(self):
        for compact in (False, True):
            with self.subTest(compact=compact):
                path = self.path.with_name('case-' + str(compact) + '.sqlite')
                b = self.broker(compact=compact, path=path)
                b.on_event(self.mark())
                self.assertEqual(b.seen_events['m1'],
                                 self.digest(self.mark()) if compact else self.mark())
                b.close()
                r = self.broker(compact=not compact, path=path)
                self.assertEqual(r.seen_events['m1'], self.digest(self.mark()))

    def test_foreign_table_refusal(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE foreign_owner (value INTEGER)')
        with self.assertRaisesRegex(ValueError, 'refuse shared or foreign database'):
            self.broker()

    def test_incompatible_primary_key_refusal(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE sim_seen_events (event_id TEXT, digest TEXT NOT NULL)')
        with self.assertRaisesRegex(ValueError, 'incompatible sim_seen_events'):
            self.broker()

    def test_wrong_table_constraints_refusal(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE sim_seen_events (event_id TEXT PRIMARY KEY, digest TEXT)')
        with self.assertRaisesRegex(ValueError, 'incompatible sim_seen_events'):
            self.broker()

    def test_metadata_mismatch_before_migration(self):
        self.legacy()
        with sqlite3.connect(self.path) as db:
            before = db.execute('SELECT payload FROM sim_broker_state').fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'metadata frozen'):
            self.broker(version='v2')
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT payload FROM sim_broker_state').fetchone()[0], before)
            self.assertEqual(db.execute('SELECT count(*) FROM sim_seen_events').fetchone()[0], 0)

# P3-A bounded retention tests, isolated SQLite and injected observation times.
class P3RetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='p3-retention-')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'broker.sqlite'
        self.brokers = []

    def broker(self, retention=10, compact=True):
        b = SimBroker(self.path, initial_cash=D('100'),
                      instruments=[InstrumentSettings('TEST', D('.0002'), D('.0005'),
                                                      D('.1'), D('.1'), D('5'), D('100'))],
                      execution=ExecutionModel(10, 2, None), risk=None,
                      version_id='p3-synthetic', forward_start=100,
                      compact_seen_events=compact,
                      seen_event_retention_ms=retention)
        self.brokers.append(b)
        return b

    def tearDown(self):
        for broker in self.brokers:
            broker.close()

    @staticmethod
    def mark(eid, ts):
        return dict(type='mark', event_id=eid, symbol='TEST', ts=ts, price='100')

    def rows(self):
        with sqlite3.connect(self.path) as db:
            return (db.execute('SELECT event_id FROM sim_seen_events ORDER BY event_id').fetchall(),
                    db.execute('SELECT event_id,event_type,observed_ts '
                               'FROM sim_seen_event_meta ORDER BY event_id').fetchall(),
                    db.execute('SELECT payload FROM sim_broker_state').fetchone()[0])

    def test_exact_cutoff_retained_one_millisecond_older_pruned(self):
        b = self.broker()
        for eid, ts in [('older', 100), ('cutoff', 101), ('now', 111)]:
            b.on_event(self.mark(eid, ts))
        self.assertEqual([r[0] for r in self.rows()[0]], ['cutoff', 'now'])
        self.assertEqual(set(b.seen_events), {'cutoff', 'now'})
        self.assertEqual(b._seen_event_meta['cutoff'], ('mark', 101))

    def test_retained_same_and_conflicting_duplicates_before_chronology(self):
        b = self.broker()
        event = self.mark('retained', 100)
        b.on_event(event)
        b.on_event(self.mark('advance', 109))
        previous = self.rows()
        b.on_event(event)
        with self.assertRaisesRegex(ValueError, 'conflicting event ID'):
            b.on_event(dict(event, price='102'))
        self.assertEqual(self.rows(), previous)

    def test_pruned_replay_rejected_without_any_state_change(self):
        b = self.broker()
        b.on_event(self.mark('stale', 100))
        b.on_event(self.mark('current', 111))
        previous = self.rows()
        memory = (dict(b.seen_events), dict(b._seen_event_meta), b.last_ts, dict(b.marks))
        with self.assertRaisesRegex(ValueError, 'out-of-order'):
            b.on_event(self.mark('stale', 100))
        self.assertEqual(self.rows(), previous)
        self.assertEqual((b.seen_events, b._seen_event_meta, b.last_ts, b.marks), memory)

    def test_funding_and_old_funding_identity_never_pruned(self):
        b = self.broker(retention=1)
        funding = dict(type='funding', event_id='fund', symbol='TEST', ts=100,
                       settlement_ts=100, rate='.01', mark='100', finalized=True)
        b.on_event(funding)
        b.on_event(self.mark('later', 200))
        self.assertIn('fund', b.seen_events)
        self.assertEqual(b._seen_event_meta['fund'], ('funding', 100))
        before = self.rows()
        b.on_event(funding)
        with self.assertRaisesRegex(ValueError, 'conflicting event ID'):
            b.on_event(dict(funding, rate='.02'))
        self.assertEqual(self.rows(), before)

    def test_digest_only_unknown_migration_stays_unprunable(self):
        b = self.broker()
        b.on_event(self.mark('legacy', 100))
        b.close()
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM sim_seen_event_meta')
        b = self.broker()
        self.assertNotIn('legacy', b._seen_event_meta)
        b.on_event(self.mark('future', 200))
        self.assertEqual([r[0] for r in self.rows()[0]], ['future', 'legacy'])
        self.assertEqual([r[0] for r in self.rows()[1]], ['future'])
        with self.assertRaisesRegex(ValueError, 'conflicting event ID'):
            b.on_event(dict(self.mark('legacy', 100), price='102'))

    def test_restart_retains_known_metadata_and_cutoff(self):
        b = self.broker()
        b.on_event(self.mark('cutoff', 100))
        b.on_event(self.mark('latest', 110))
        b.close()
        b = self.broker()
        self.assertEqual(b._seen_event_meta['cutoff'], ('mark', 100))
        b.on_event(self.mark('next', 111))
        self.assertEqual([r[0] for r in self.rows()[0]], ['latest', 'next'])
        self.assertNotIn('cutoff', b.seen_events)

    def test_prune_failure_rolls_back_tables_memory_singleton_then_retry(self):
        b = self.broker()
        b.on_event(self.mark('old', 100))
        before = self.rows()
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TRIGGER reject_prune BEFORE DELETE ON sim_seen_events
                          BEGIN SELECT RAISE(FAIL, 'synthetic prune failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            b.on_event(self.mark('new', 111))
        self.assertEqual(self.rows(), before)
        self.assertEqual(set(b.seen_events), {'old'})
        self.assertEqual(set(b._seen_event_meta), {'old'})
        self.assertEqual(b.last_ts, 100)
        with sqlite3.connect(self.path) as db:
            db.execute('DROP TRIGGER reject_prune')
        b.on_event(self.mark('new', 111))
        self.assertEqual(set(b.seen_events), {'new'})

    def test_commit_failure_rolls_back_then_retry(self):
        b = self.broker()
        b.on_event(self.mark('old', 100))
        previous = self.rows()
        b._db.execute('PRAGMA foreign_keys=ON')
        b._db.execute('CREATE TABLE synthetic_guard (id INTEGER REFERENCES synthetic_parent(id) '
                      'DEFERRABLE INITIALLY DEFERRED)')
        b._db.execute('CREATE TABLE synthetic_parent (id INTEGER PRIMARY KEY)')
        b._db.execute("""CREATE TRIGGER synthetic_commit_failure
                         AFTER INSERT ON sim_seen_event_meta
                         BEGIN INSERT INTO synthetic_guard VALUES (999); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            b.on_event(self.mark('new', 111))
        self.assertEqual(self.rows(), previous)
        self.assertEqual(set(b.seen_events), {'old'})
        self.assertEqual(b.last_ts, 100)
        b._db.execute('DROP TRIGGER synthetic_commit_failure')
        b.on_event(self.mark('new', 111))
        self.assertEqual(set(b.seen_events), {'new'})

    def test_option_validation_and_disabled_pruning(self):
        for invalid in (0, -1, True, False, 1.5, '10'):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, 'seen_event_retention_ms'):
                    self.broker(retention=invalid)
        b = self.broker(retention=None)
        b.on_event(self.mark('very-old', 100))
        b.on_event(self.mark('far-ahead', 500000))
        self.assertEqual(len(self.rows()[0]), 2)
        self.assertEqual(len(self.rows()[1]), 2)

    def test_foreign_and_malformed_side_table_refused_atomically(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE sim_seen_event_meta (event_id TEXT, event_type TEXT, observed_ts INTEGER)')
        with self.assertRaisesRegex(ValueError, 'incompatible sim_seen_event_meta'):
            self.broker()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master "
                                        "WHERE name='sim_broker_state'").fetchone()[0], 0)

    def test_structural_history_bounds_at_two_sizes(self):
        for size in (128, 2048):
            with self.subTest(size=size):
                path = self.path.with_name('history-' + str(size) + '.sqlite')
                b = SimBroker(path, initial_cash=D('100'),
                              instruments=[InstrumentSettings('TEST', D('.0002'), D('.0005'),
                                                              D('.1'), D('.1'), D('5'), D('100'))],
                              execution=ExecutionModel(10, 2, None), risk=None,
                              version_id='p3-synthetic', forward_start=100,
                              compact_seen_events=True, seen_event_retention_ms=10)
                try:
                    for n in range(size):
                        b.on_event(self.mark('sample:' + str(n), 100 + n))
                    with sqlite3.connect(path) as db:
                        seen = db.execute('SELECT count(*) FROM sim_seen_events').fetchone()[0]
                        known = db.execute('SELECT count(*) FROM sim_seen_event_meta').fetchone()[0]
                    self.assertEqual((seen, known), (11, 11))
                    self.assertEqual(len(b.seen_events), 11)
                    print('P3-A rows history=' + str(size) + ' seen=' + str(seen) +
                          ' typed=' + str(known), flush=True)
                finally:
                    b.close()


    def _legacy_with_companion(self, *, mode='full'):
        """Produce the frozen P1 fixture's transient digest gap."""
        b = self.broker(retention=None)
        a, z = self.mark('m1', 100), self.mark('m2', 101)
        b.on_event(a)
        b.on_event(z)
        b.close()
        digest = lambda event: hashlib.sha256(
            json.dumps(event, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()
        with sqlite3.connect(self.path) as db:
            state = json.loads(db.execute(
                'SELECT payload FROM sim_broker_state WHERE singleton=1'
            ).fetchone()[0])
            state['seen_events'] = ({'m1': a, 'm2': z} if mode == 'full'
                                    else {'m1': digest(a), 'm2': digest(z)})
            db.execute('UPDATE sim_broker_state SET payload=?',
                       (json.dumps(state, sort_keys=True),))
            db.execute('DELETE FROM sim_seen_events')
        return a, z

    def test_legacy_full_with_existing_companion_migrates_atomically(self):
        self._legacy_with_companion()
        b = self.broker(retention=None)
        rows, meta, state = self.rows()
        self.assertEqual(rows, [('m1',), ('m2',)])
        self.assertEqual(meta, [('m1', 'mark', 100), ('m2', 'mark', 101)])
        self.assertNotIn('seen_events', json.loads(state))
        prior = self.rows()
        b.on_event(self.mark('m1', 100))
        with self.assertRaisesRegex(ValueError, 'conflicting event ID'):
            b.on_event(dict(self.mark('m1', 100), price='102'))
        self.assertEqual(self.rows(), prior)

    def test_digest_legacy_with_existing_companion_preserves_identity(self):
        self._legacy_with_companion(mode='digest')
        b = self.broker(retention=None)
        self.assertEqual(self.rows()[1],
                         [('m1', 'mark', 100), ('m2', 'mark', 101)])
        self.assertEqual(set(b.seen_events), {'m1', 'm2'})
        self.assertEqual(set(b._seen_event_meta), {'m1', 'm2'})

    def test_true_orphan_companion_refused_without_mutation(self):
        self._legacy_with_companion()
        with sqlite3.connect(self.path) as db:
            db.execute("DELETE FROM sim_seen_event_meta WHERE event_id='m2'")
            db.execute("INSERT INTO sim_seen_event_meta VALUES ('orphan','mark',100)")
            before = (db.execute('SELECT payload FROM sim_broker_state').fetchone()[0],
                      db.execute('SELECT * FROM sim_seen_events').fetchall(),
                      db.execute('SELECT * FROM sim_seen_event_meta ORDER BY event_id').fetchall())
        with self.assertRaisesRegex(ValueError, 'invalid persisted seen-event metadata'):
            self.broker(retention=None)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(
                (db.execute('SELECT payload FROM sim_broker_state').fetchone()[0],
                 db.execute('SELECT * FROM sim_seen_events').fetchall(),
                 db.execute('SELECT * FROM sim_seen_event_meta ORDER BY event_id').fetchall()),
                before)

    def test_companion_identity_conflict_refused_and_retryable(self):
        self._legacy_with_companion()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE sim_seen_event_meta SET observed_ts=102 WHERE event_id='m2'")
            original = db.execute('SELECT payload FROM sim_broker_state').fetchone()[0]
        with self.assertRaisesRegex(ValueError, 'conflicting migrated seen-event metadata'):
            self.broker(retention=None)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT payload FROM sim_broker_state').fetchone()[0],
                             original)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_seen_events').fetchone()[0], 0)
            db.execute("UPDATE sim_seen_event_meta SET observed_ts=101 WHERE event_id='m2'")
        b = self.broker(retention=None)
        self.assertEqual(set(b.seen_events), {'m1', 'm2'})

    def test_companion_migration_insert_failure_rolls_back_and_retries(self):
        self._legacy_with_companion()
        with sqlite3.connect(self.path) as db:
            original = db.execute('SELECT payload FROM sim_broker_state').fetchone()[0]
            db.execute("""CREATE TRIGGER reject_legacy_m2 BEFORE INSERT ON sim_seen_events
                          WHEN NEW.event_id='m2'
                          BEGIN SELECT RAISE(FAIL, 'synthetic insert failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.broker(retention=None)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT payload FROM sim_broker_state').fetchone()[0],
                             original)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sim_seen_events').fetchone()[0], 0)
            db.execute('DROP TRIGGER reject_legacy_m2')
        b = self.broker(retention=None)
        self.assertEqual(set(b._seen_event_meta), {'m1', 'm2'})


    def test_context_only_wrapper_restores_original_write_error_and_all_state(self):
        b = self.broker()
        b.on_event(self.mark('baseline', 100))
        before_rows = self.rows()
        before = (dict(b.seen_events), dict(b._seen_event_meta),
                  dict(b.marks), dict(b.day_baselines), b.cash, b.last_ts)
        raw = b._db

        class InterruptedConnection:
            def __init__(self, inner):
                self.inner = inner
            def __enter__(self):
                self.inner.__enter__()
                return self
            def __exit__(self, *args):
                return self.inner.__exit__(*args)
            def execute(self, sql, args=()):
                cursor = self.inner.execute(sql, args)
                if sql.startswith('INSERT OR REPLACE INTO sim_broker_state'):
                    raise OSError('synthetic context-only write interruption')
                return cursor

        b._db = InterruptedConnection(raw)
        try:
            with self.assertRaisesRegex(OSError, 'context-only write interruption'):
                b.on_event(self.mark('new', 111))
            self.assertEqual((b.seen_events, b._seen_event_meta,
                              b.marks, b.day_baselines, b.cash, b.last_ts), before)
            self.assertEqual(self.rows(), before_rows)
            self.assertFalse(raw.in_transaction)
        finally:
            b._db = raw
        b.on_event(self.mark('new', 111))
        self.assertEqual(set(b.seen_events), {'new'})
        self.assertEqual(set(b._seen_event_meta), {'new'})

    def test_context_only_wrapper_prune_failure_restores_original_error(self):
        b = self.broker()
        b.on_event(self.mark('old', 100))
        before_rows = self.rows()
        before = (dict(b.seen_events), dict(b._seen_event_meta),
                  dict(b.marks), dict(b.day_baselines), b.cash, b.last_ts)
        raw = b._db

        class PruneInterruptedConnection:
            def __init__(self, inner):
                self.inner = inner
            def __enter__(self):
                self.inner.__enter__()
                return self
            def __exit__(self, *args):
                return self.inner.__exit__(*args)
            def execute(self, sql, args=()):
                cursor = self.inner.execute(sql, args)
                if sql.startswith('DELETE FROM sim_seen_events'):
                    raise OSError('synthetic context-only prune interruption')
                return cursor

        b._db = PruneInterruptedConnection(raw)
        try:
            with self.assertRaisesRegex(OSError, 'context-only prune interruption'):
                b.on_event(self.mark('new', 111))
            self.assertEqual((b.seen_events, b._seen_event_meta,
                              b.marks, b.day_baselines, b.cash, b.last_ts), before)
            self.assertEqual(self.rows(), before_rows)
            self.assertFalse(raw.in_transaction)
        finally:
            b._db = raw
        b.on_event(self.mark('new', 111))
        self.assertEqual(set(b.seen_events), {'new'})
        self.assertEqual(set(b._seen_event_meta), {'new'})


    def test_deployed_offtick_diagnostic_preserved(self):
        b = self.broker()
        event = dict(type='book', event_id='bad-tick', symbol='TEST', ts=100,
                     bids=[['99.05', '1']], asks=[['100.0', '1']])
        with self.assertRaisesRegex(
                ValueError,
                r'off-tick quote: validator=SimBroker\._validate_event side=bids level=0 '
                r'price=99\.05 tick=0\.1 remainder=0\.05'):
            b.on_event(event)
        self.assertNotIn('bad-tick', b.seen_events)
        events, metadata, _ = self.rows()
        self.assertEqual(events, [])
        self.assertEqual(metadata, [])

    def test_funding_id_survives_nonfunding_retention_forever(self):
        b = self.broker()
        funding = dict(type='funding', event_id='funding-permanent', symbol='TEST',
                       ts=100, settlement_ts=100, rate='.001', mark='100',
                       finalized=True)
        b.on_event(funding)
        for n in range(1, 40):
            b.on_event(self.mark('ordinary:' + str(n), 100 + n))
        b.close()
        b = self.broker()
        before = (str(b.cash), list(b.ledger), dict(b.funding_events))
        b.on_event(funding)
        self.assertEqual((str(b.cash), b.ledger, b.funding_events), before)
        self.assertIn('funding-permanent', b.seen_events)
        self.assertEqual(b._seen_event_meta['funding-permanent'][0], 'funding')



# P2 differential / write-volume evidence. Kept separate from P1 unit tests.
# Commands from repo root:
# cd lab && SIM_BROKER_P2_DIFFERENTIAL=1 python3 -m unittest -v tests.test_sim_broker_persistence.P2EvidenceTests.test_parent_differential_2048_events
# cd lab && SIM_BROKER_P2_WRITE_PROBE=1 python3 -m unittest -v tests.test_sim_broker_persistence.P2EvidenceTests.test_linux_write_bytes_40000_history
# Parent code loads solely from local git show at pinned PR51 SHA, or from
# SIM_BROKER_P2_PARENT_PATH (untracked scratch file, pinned blob checked).
# No network calls or new source/third-party dependencies.
import os
import subprocess
import sys
import types
import sim_broker as p2_current

_P2_PARENT_COMMIT = 'b1c5d9279506123bfb2939f795e71f3455368d78'
_P2_PARENT_BLOB = 'a8f96f9237d0161308e875a19347ba54000a943c'
_P2_FIELDS = ('cash', 'orders', 'positions', 'fills', 'ledger', 'audit', 'marks',
              'funding_events', 'funding_status', 'day_baselines', 'last_ts')


def _p2_parent_module():
    path = os.environ.get('SIM_BROKER_P2_PARENT_PATH')
    if path:
        source = Path(path).read_bytes()
    else:
        root = Path(__file__).resolve().parents[2]
        cmd = ['git', '-C', str(root), 'show',
               _P2_PARENT_COMMIT + ':lab/sim_broker.py']
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, check=False)
        except (OSError, ValueError) as exc:
            raise unittest.SkipTest('PR51 source unavailable locally: ' + str(exc)) from exc
        if proc.returncode:
            raise unittest.SkipTest(
                'PR51 git object unavailable locally (no network fetch): '
                + proc.stderr.decode('utf-8', 'replace')[:240])
        source = proc.stdout
    data = b'blob ' + str(len(source)).encode() + b'\0' + source
    blob = hashlib.sha1(data).hexdigest()
    if blob != _P2_PARENT_BLOB:
        raise AssertionError('PR51 original source blob mismatch: ' + blob)
    name = '_p2_original_sim_broker_pr51'
    module = types.ModuleType(name)
    module.__file__ = str(Path(__file__).resolve().parents[1] / 'sim_broker.py')
    sys.modules[name] = module
    try:
        exec(compile(source, module.__file__, 'exec'), module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _p2_broker(module, path, *, compact=False):
    settings = module.InstrumentSettings('TEST', D('.0002'), D('.0005'),
                                         D('.1'), D('.1'), D('5'), D('100'))
    risk = module.RiskContract(
        approved=True, version='P2-SYNTHETIC',
        max_loss_per_trade_usdt=D('1000'), max_daily_loss_usdt=D('1000'),
        max_effective_exposure_x=D('100'), max_positions=2,
        total_loss_limit_usdt=D('1000'), require_stop=True)
    return module.SimBroker(path, initial_cash=D('100'), instruments=[settings],
                            execution=module.ExecutionModel(10, 2, None), risk=risk,
                            version_id='p2-synthetic', forward_start=100,
                            compact_seen_events=compact)


def _p2_canonical(value):
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _p2_snapshot(b):
    return {name: _p2_canonical(getattr(b, name)) for name in _P2_FIELDS}


def _p2_event(kind, ts, eid, **extras):
    return dict(type=kind, event_id=eid, symbol='TEST', ts=ts, **extras)


def _p2_book(ts, eid, bid='99', ask='100'):
    return _p2_event('book', ts, eid, bids=[[bid, '10']], asks=[[ask, '10']])


def _p2_intent(module, key, ts, side='BUY', *, reduce=False, stop='90'):
    return module.Intent(key, 'TEST', side, D('1'), None if reduce else D(stop),
                         ts, D('.1'), D('.1'), D('5'), D('100'), 'TAKER', reduce)


def _p2_workload():
    """Fixed fill, stop, manual exit, late settlement, short and varied feed."""
    actions = [
        ('event', _p2_event('mark', 100, 'initial-mark', price='100')),
        ('event', _p2_event('funding_status', 100, 'funding-ready', complete=True,
                            valid_until_ts=1_000_000_000)),
        ('intent', ('long-one', 100, 'BUY', False, '90')),
        ('event', _p2_book(111, 'entry-one-book')),
        ('event', _p2_event('funding', 112, 'long-funding', settlement_ts=112,
                            rate='.001', mark='100', finalized=True)),
        ('event', _p2_event('mark', 113, 'trigger-long-stop', price='80')),
        ('event', _p2_book(124, 'fill-long-stop', bid='80', ask='81')),
        ('intent', ('long-two', 125, 'BUY', False, '90')),
        ('event', _p2_book(136, 'entry-two-book')),
        ('intent', ('manual-exit', 137, 'SELL', True, '90')),
        ('event', _p2_book(148, 'fill-manual-exit', bid='105', ask='106')),
        ('event', _p2_event('funding', 150, 'late-funding', settlement_ts=140,
                            rate='.002', mark='100', finalized=True)),
        ('intent', ('short-one', 151, 'SELL', False, '110')),
        ('event', _p2_book(162, 'enter-short')),
        ('event', _p2_event('mark', 163, 'trigger-short-stop', price='111')),
        ('event', _p2_book(174, 'fill-short-stop', bid='111', ask='112')),
        ('event', _p2_event('funding', 175, 'short-funding', settlement_ts=165,
                            rate='.001', mark='100', finalized=True)),
    ]
    for n in range(2048):
        ts = 200 + n
        if n % 37 == 0:
            event = _p2_event('funding', ts, 'batch-funding:' + str(n),
                              settlement_ts=ts, rate='.0001', mark='100',
                              finalized=True)
        elif n % 11 == 0:
            event = _p2_event('funding_status', ts, 'batch-status:' + str(n),
                              complete=True, valid_until_ts=1_000_000_000)
        elif n % 5 == 0:
            event = _p2_event('aggTrade', ts, 'batch-trade:' + str(n),
                              price='99', qty='1', aggressor='SELL')
        elif n % 3 == 0:
            event = _p2_book(ts, 'batch-book:' + str(n))
        else:
            event = _p2_event('mark', ts, 'batch-mark:' + str(n), price='100')
        actions.append(('event', event))
    return actions


def _p2_coverage(b):
    entries = [f for f in b.fills
               if f['intent_id'] in ('long-one', 'long-two', 'short-one')]
    exits = [f for f in b.fills if f['intent_id'] == 'manual-exit']
    stops = [f for f in b.fills if f['intent_id'].startswith('stop:')]
    funding = [x for x in b.ledger
               if x['type'] == 'funding' and D(x['amount']) != 0]
    return dict(entry_fills=len(entries), explicit_exit_fills=len(exits),
                stop_fills=len(stops), nonzero_funding_settlements=len(funding),
                events=len(b.seen_events))


def _p2_digest(event):
    value = _p2_canonical(event)
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _p2_warm_event(n):
    return _p2_event('mark', 100 + n, 'warm:' + str(n), price='100')


def _p2_seed_warm(module, path, count, *, legacy):
    """Seed equivalent fully formed histories, not O(N^2) prior replay.

    Each mark is a valid public event with monotonically increasing time.
    Both snapshots reflect the same final mark, baseline and timestamp.
    """
    b = _p2_broker(module, path, compact=True)
    try:
        b.close()
        with sqlite3.connect(path) as db:
            state = json.loads(db.execute(
                'SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()[0])
            state.update(last_ts=99 + count, marks={'TEST': '100'},
                         day_baselines={'0': '100'})
            if legacy:
                state['seen_events'] = {e['event_id']: e
                                        for e in (_p2_warm_event(i)
                                                  for i in range(count))}
            db.execute('UPDATE sim_broker_state SET payload=? WHERE singleton=1',
                       (json.dumps(state, sort_keys=True),))
            if not legacy:
                db.executemany(
                    'INSERT INTO sim_seen_events(event_id,digest) VALUES (?,?)',
                    ((e['event_id'], _p2_digest(e))
                     for e in (_p2_warm_event(i) for i in range(count))))
        b = _p2_broker(module, path, compact=True)
        assert len(b.seen_events) == count, 'wrong warm history count'
        assert b.last_ts == 99 + count, 'wrong warm timestamp'
        return b
    except BaseException:
        b.close()
        raise


def _p2_proc_write_bytes():
    with open('/proc/self/io', encoding='ascii') as stream:
        for line in stream:
            if line.startswith('write_bytes:'):
                return int(line.split(':', 1)[1])
    raise unittest.SkipTest('Linux /proc/self/io missing write_bytes')


def _p2_sample_bytes(broker, count):
    os.sync()
    before = _p2_proc_write_bytes()
    for n in range(20):
        broker.on_event(_p2_event('mark', 100 + count + n,
                                  'measured:' + str(n), price='100'))
        os.sync()  # Actual sync after EACH submission.
    total = _p2_proc_write_bytes() - before
    if total <= 0:
        raise AssertionError('Linux write_bytes recorded no committed writes')
    return total


class P2EvidenceTests(unittest.TestCase):
    def test_deterministic_trace_shape(self):
        a, b = _p2_workload(), _p2_workload()
        self.assertEqual(a, b)
        self.assertGreaterEqual(sum(k == 'event' for k, _ in a), 2000)
        self.assertGreaterEqual(sum(k == 'intent' for k, _ in a), 4)

    @unittest.skipUnless(os.environ.get('SIM_BROKER_P2_DIFFERENTIAL') == '1',
                         'opt-in SIM_BROKER_P2_DIFFERENTIAL=1; needs original PR51')
    def test_parent_differential_2048_events(self):
        parent = _p2_parent_module()
        with tempfile.TemporaryDirectory(prefix='p2-differential-') as directory:
            root = Path(directory)
            (root / 'parent').mkdir()
            (root / 'current').mkdir()
            old_path = root / 'parent' / 'state.sqlite'
            new_path = root / 'current' / 'state.sqlite'
            old = _p2_broker(parent, old_path)
            new = _p2_broker(p2_current, new_path)
            events = 0
            try:
                for action, value in _p2_workload():
                    if action == 'intent':
                        key, ts, side, reduce, stop = value
                        original = old.submit(_p2_intent(
                            parent, key, ts, side, reduce=reduce, stop=stop))
                        revised = new.submit(_p2_intent(
                            p2_current, key, ts, side, reduce=reduce, stop=stop))
                        self.assertEqual(_p2_canonical(original),
                                         _p2_canonical(revised), key)
                        self.assertEqual(original['status'], 'PENDING', key)
                    else:
                        old.on_event(value)
                        new.on_event(value)
                        events += 1
                    if action == 'intent' or (
                            events and events % 256 == 0 and action == 'event'):
                        self.assertEqual(_p2_snapshot(old), _p2_snapshot(new),
                                         'checkpoint event ' + str(events))
                    if events == 1024 and action == 'event':
                        old.close()
                        new.close()
                        old = _p2_broker(parent, old_path)
                        new = _p2_broker(p2_current, new_path)
                        self.assertEqual(_p2_snapshot(old), _p2_snapshot(new),
                                         'restarted at event 1024')
                self.assertGreaterEqual(events, 2000)
                self.assertEqual(_p2_snapshot(old), _p2_snapshot(new))
                # Idempotent replay across the restart; the event is persisted.
                last = _p2_workload()[-1][1]
                old.on_event(last)
                new.on_event(last)
                self.assertEqual(_p2_snapshot(old), _p2_snapshot(new))
                proof = _p2_coverage(new)
                for field in ('entry_fills', 'explicit_exit_fills',
                              'stop_fills', 'nonzero_funding_settlements'):
                    self.assertGreater(proof[field], 0, proof)
                self.assertEqual(proof['events'], events)
                print('P2 DIFFERENTIAL parent=' + _P2_PARENT_COMMIT +
                      ' events=' + str(events) + ' exact_fields=' +
                      str(_P2_FIELDS) + ' coverage=' +
                      json.dumps(proof, sort_keys=True), flush=True)
            finally:
                old.close()
                new.close()

    @unittest.skipUnless(os.environ.get('SIM_BROKER_P2_WRITE_PROBE') == '1',
                         'opt-in SIM_BROKER_P2_WRITE_PROBE=1; Linux only')
    def test_linux_write_bytes_40000_history(self):
        if (not sys.platform.startswith('linux') or
                not hasattr(os, 'sync') or
                not Path('/proc/self/io').is_file()):
            self.skipTest('Linux /proc/self/io write_bytes and os.sync required')
        _p2_proc_write_bytes()
        parent = _p2_parent_module()
        observations = {}
        with tempfile.TemporaryDirectory(prefix='p2-write-bytes-') as directory:
            for size in (512, 40000):
                for label, module, legacy in (
                        ('legacy', parent, True), ('new', p2_current, False)):
                    path = Path(directory) / (label + '-' + str(size))
                    path.mkdir()
                    b = _p2_seed_warm(module, path / 'broker.sqlite', size,
                                      legacy=legacy)
                    try:
                        measured = _p2_sample_bytes(b, size)
                        self.assertEqual(b.last_ts, 119 + size)
                        self.assertEqual(len(b.seen_events), size + 20)
                        observations[(size, label)] = measured / 20
                        print('P2 WRITE history=' + str(size) +
                              ' implementation=' + label + ' write_bytes=' +
                              str(measured) + ' events=20 mean_bytes=' +
                              format(measured / 20, '.1f'), flush=True)
                    finally:
                        b.close()
        old_large = observations[(40000, 'legacy')]
        new_large = observations[(40000, 'new')]
        new_small = observations[(512, 'new')]
        self.assertGreater(old_large, 0)
        self.assertLess(new_large, 1_000_000, observations)
        self.assertLess(new_large, .05 * old_large, observations)
        self.assertLess(new_large, 2 * new_small + 262144, observations)
        print('P2 WRITE ratio_new_to_legacy_40000=' +
              format(new_large / old_large, '.6f') +
              ' new_bytes_per_event_small_large=' + format(new_small, '.1f') +
              '/' + format(new_large, '.1f'), flush=True)


_DEPLOYED_BROKER_BLOB = 'b34ef7e7a30e35c51019420937f505513679c29c'


def _sr1_deployed_module():
    source = (Path(__file__).resolve().parents[2] /
              'operator-input/deployed/sim_broker.py').read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(source)).encode() + b'\0' + source).hexdigest()
    if blob != _DEPLOYED_BROKER_BLOB:
        raise AssertionError('deployed broker source blob mismatch: ' + blob)
    name = '_sr1_deployed_sim_broker'
    module = types.ModuleType(name)
    module.__file__ = 'operator-input/deployed/sim_broker.py'
    sys.modules[name] = module
    exec(compile(source, module.__file__, 'exec'), module.__dict__)
    return module


@unittest.skipUnless(os.environ.get('STORAGE_SR1_DEPLOYED_DIFFERENTIAL') == '1',
                     'opt-in STORAGE_SR1_DEPLOYED_DIFFERENTIAL=1')
class SR1DeployedDifferentialTests(unittest.TestCase):
    """Current storage versus pinned deployed broker semantics, no network."""

    def test_2048_events_restart_and_rejection_matrix(self):
        deployed = _sr1_deployed_module()
        with tempfile.TemporaryDirectory(prefix='sr1-deployed-diff-') as directory:
            root = Path(directory)
            old = _p2_broker(deployed, root / 'old.sqlite')
            new = _p2_broker(p2_current, root / 'new.sqlite', compact=True)
            try:
                # Funding readiness plus actual entries/exits/stops/finalized funding.
                for action, value in _p2_workload():
                    if action == 'intent':
                        key, ts, side, reduce, stop = value
                        a = old.submit(_p2_intent(deployed, key, ts, side,
                                                  reduce=reduce, stop=stop))
                        b = new.submit(_p2_intent(p2_current, key, ts, side,
                                                 reduce=reduce, stop=stop))
                        self.assertEqual(_p2_canonical(a), _p2_canonical(b), key)
                    else:
                        old.on_event(value); new.on_event(value)
                self.assertEqual(_p2_snapshot(old), _p2_snapshot(new))
                coverage = _p2_coverage(new)
                self.assertGreaterEqual(coverage['events'], 2000)
                for key in ('entry_fills','explicit_exit_fills','stop_fills',
                            'nonzero_funding_settlements'):
                    self.assertGreater(coverage[key], 0, coverage)

                # Exact duplicate remains idempotent across both implementations.
                duplicate = _p2_event('mark', new.last_ts + 1, 'duplicate', price='100')
                old.on_event(duplicate); new.on_event(duplicate)
                old_before, new_before = _p2_snapshot(old), _p2_snapshot(new)
                old.on_event(duplicate); new.on_event(duplicate)
                self.assertEqual(_p2_snapshot(old), old_before)
                self.assertEqual(_p2_snapshot(new), new_before)

                # Same ID/different content, out-of-order and off-tick reject in
                # both. Off-tick text is intentionally later-deployed diagnostic.
                conflict = dict(duplicate, price='101')
                for broker in (old, new):
                    with self.assertRaisesRegex(ValueError, 'conflicting event ID'):
                        broker.on_event(conflict)
                    with self.assertRaisesRegex(ValueError, 'event timestamp older'):
                        broker.on_event(_p2_event('mark', 100, 'old-ts', price='100'))
                off = _p2_book(new.last_ts + 2, 'offtick', bid='99.05', ask='100')
                with self.assertRaisesRegex(ValueError, 'off-tick quote'):
                    old.on_event(off)
                with self.assertRaisesRegex(
                        ValueError, 'validator=SimBroker._validate_event'):
                    new.on_event(off)

                # Risk rejection contract remains unchanged by storage.
                bad_old = deployed.RiskContract()
                bad_new = p2_current.RiskContract()
                old.risk, new.risk = bad_old, bad_new
                ts = max(old.last_ts, new.last_ts) + 1
                oa = old.submit(_p2_intent(deployed, 'risk-reject', ts))
                nb = new.submit(_p2_intent(p2_current, 'risk-reject', ts))
                self.assertEqual(oa['reason'], 'risk_missing_or_unapproved')
                self.assertEqual(nb['reason'], oa['reason'])

                # Restart current storage and permanently replay finalized funding.
                permanent = _p2_event('funding', new.last_ts + 3, 'permanent-funding',
                                      settlement_ts=new.last_ts + 3, rate='.0001',
                                      mark='100', finalized=True)
                old.on_event(permanent); new.on_event(permanent)
                expected = _p2_snapshot(new)
                old.close(); new.close()
                old = _p2_broker(deployed, root / 'old.sqlite')
                new = _p2_broker(p2_current, root / 'new.sqlite', compact=True)
                self.assertEqual(_p2_snapshot(old), _p2_snapshot(new))
                new.on_event(permanent)
                self.assertEqual(_p2_snapshot(new), expected)
                print('SR1 DEPLOYED DIFFERENTIAL blob=' + _DEPLOYED_BROKER_BLOB +
                      ' coverage=' + json.dumps(coverage, sort_keys=True), flush=True)
            finally:
                old.close(); new.close()

if __name__ == '__main__':
    unittest.main()
