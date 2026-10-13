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


if __name__ == '__main__':
    unittest.main()
