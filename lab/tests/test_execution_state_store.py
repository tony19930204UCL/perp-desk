"""Deterministic synthetic execution checkpoint tests."""
import sys
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from decimal import Decimal as D
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from execution_interface import Intent, Event, OrderState
from execution_state_store import ExecutionStateStore

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'storage.db'
        self.intent = Intent('synthetic', 'TEST', 'BUY', 'market', D('3.000'),
                             False, D('90.00'), False, 100,
                             D('101.00100'), D('0.00000001'), 110)

    def db(self, path=None, namespace='arm'):
        return ExecutionStateStore(path or self.path, namespace=namespace)

    def event(self, key, kind, qty='0', price=None, fee='0', **extra):
        return Event(key, 'synthetic', kind, D(qty), None if price is None else D(price),
                     D(fee), **extra)

    def state(self, *events):
        s = OrderState('synthetic', self.intent.qty)
        for event in events:
            s.apply(event)
        return s

    def test_reserve_reopen_duplicate(self):
        with self.db() as d:
            self.assertTrue(d.reserve(self.intent))
        with self.db() as d:
            self.assertFalse(d.reserve(self.intent))
            self.assertEqual(d.load_intent('synthetic'), self.intent)

    def test_conflicting_intent(self):
        with self.db() as d:
            d.reserve(self.intent)
            with self.assertRaises(ValueError):
                d.reserve(replace(self.intent, side='SELL'))

    def test_prepared_no_state(self):
        with self.db() as d:
            d.reserve(self.intent)
        with self.db() as d:
            self.assertIsNone(d.load_state('synthetic'))
            self.assertIsNotNone(d.load_intent('synthetic'))

    def test_accepted_reopen(self):
        with self.db() as d:
            d.reserve(self.intent)
            d.save_state(self.state(self.event('a', 'ACCEPTED')))
        with self.db() as d:
            self.assertEqual(d.load_state('synthetic').state, 'ACCEPTED')

    def test_unknown_reopen(self):
        with self.db() as d:
            d.reserve(self.intent)
            d.save_state(self.state(self.event('u', 'UNKNOWN_OUTCOME')))
        with self.db() as d:
            self.assertEqual(d.load_state('synthetic').state, 'UNKNOWN_OUTCOME')

    def test_partial_final_exact(self):
        a = self.event('a', 'ACCEPTED')
        p = self.event('p', 'PARTIALLY_FILLED', '1.000', '101.001234567890123',
                       '0.00000000000009', fill_id='fp')
        f = self.event('f', 'FILLED', '2.000', '102.0000001', '0.00000000000011',
                       fill_id='ff')
        with self.db() as d:
            d.reserve(self.intent)
            d.save_state(self.state(a, p))
        with self.db() as d:
            self.assertEqual(d.load_state('synthetic').filled_qty, D('1.000'))
            d.save_state(self.state(a, p, f))
        with self.db() as d:
            s = d.load_state('synthetic')
            self.assertEqual(s.state, 'FILLED')
            self.assertEqual(s.filled_qty, D('3.000'))
            self.assertEqual(s.fees, D('0.00000000000020'))
            self.assertEqual(list(s.events.values()), [a, p, f])

    def test_unknown_to_accepted(self):
        u = self.event('u', 'UNKNOWN_OUTCOME')
        a = self.event('a', 'ACCEPTED')
        with self.db() as d:
            d.reserve(self.intent)
            d.save_state(self.state(u))
            d.save_state(self.state(u, a))
        with self.db() as d:
            self.assertEqual(list(d.load_state('synthetic').events), ['u', 'a'])

    def test_terminal_checkpoint_duplicate(self):
        r = self.state(self.event('r', 'REJECTED', reason='synthetic'))
        with self.db() as d:
            d.reserve(self.intent)
            d.save_state(r)
            d.save_state(r)
        with self.db() as d:
            self.assertEqual(d.load_state('synthetic').state, 'REJECTED')

    def test_terminal_regression(self):
        a = self.event('a', 'ACCEPTED')
        x = self.event('x', 'CANCELED')
        with self.db() as d:
            d.reserve(self.intent)
            d.save_state(self.state(a, x))
            with self.assertRaises(ValueError):
                d.save_state(self.state(a))
            altered = self.state(a, x)
            altered.events['y'] = self.event('y', 'ACCEPTED')
            with self.assertRaises(ValueError):
                d.save_state(altered)

    def test_conflicting_event_and_reorder(self):
        a = self.event('a', 'ACCEPTED')
        p = self.event('p', 'PARTIALLY_FILLED', '1', '100', '0.1')
        with self.db() as d:
            d.reserve(self.intent)
            d.save_state(self.state(a, p))
            with self.assertRaises(ValueError):
                d.save_state(self.state(a, replace(p, fee=D('0.2'))))
            with self.assertRaises(ValueError):
                d.save_state(self.state(self.event('u', 'UNKNOWN_OUTCOME'), a, p))

    def test_unreserved_qty_mismatch(self):
        with self.db() as d:
            with self.assertRaises(ValueError):
                d.save_state(self.state())
            d.reserve(self.intent)
            with self.assertRaises(ValueError):
                d.save_state(OrderState('synthetic', D('4')))

    def test_isolated_paths(self):
        other = Path(self.tmp.name) / 'other.db'
        with self.db() as first, self.db(other) as second:
            first.reserve(self.intent)
            self.assertIsNone(second.load_intent('synthetic'))
            second.reserve(replace(self.intent, qty=D('4')))
            self.assertEqual(first.load_intent('synthetic').qty, D('3.000'))

    def test_foreign_and_empty_database(self):
        with sqlite3.connect(self.path) as raw:
            raw.execute('CREATE TABLE foreign_data(value TEXT)')
        with self.assertRaises(ValueError):
            self.db()
        self.path.unlink()
        self.path.touch()
        with self.assertRaises(ValueError):
            self.db()

    def test_version_mismatch(self):
        with self.db():
            pass
        with sqlite3.connect(self.path) as raw:
            raw.execute('PRAGMA user_version=99')
        with self.assertRaises(ValueError):
            self.db()

    def test_namespace_mismatch(self):
        with self.db(namespace='alpha') as d:
            d.reserve(self.intent)
        with self.assertRaises(ValueError):
            self.db(namespace='beta')

    def test_corrupt_intent_and_events(self):
        with self.db() as d:
            d.reserve(self.intent)
            d.save_state(self.state(self.event('a', 'ACCEPTED')))
        with sqlite3.connect(self.path) as raw:
            raw.execute('UPDATE states SET data=?', (json.dumps([{'bad': 1}]),))
        with self.assertRaises(ValueError):
            self.db()
        with sqlite3.connect(self.path) as raw:
            raw.execute('DELETE FROM states')
            raw.execute('UPDATE intents SET data=?', ('{"bad":1}',))
        with self.assertRaises(ValueError):
            self.db()

    def test_missing_primary_key_rejects_conflicting_identity(self):
        with self.db() as store:
            store.reserve(self.intent)
        with sqlite3.connect(self.path) as raw:
            raw.execute('PRAGMA foreign_keys=OFF')
            raw.execute('DROP TABLE states')
            raw.execute('ALTER TABLE intents RENAME TO old_intents')
            raw.execute('CREATE TABLE intents(id TEXT, data TEXT)')
            raw.execute('INSERT INTO intents SELECT id, data FROM old_intents')
            raw.execute('DROP TABLE old_intents')
            raw.execute('CREATE TABLE states(id TEXT, data TEXT)')
            from execution_state_store import encode, pack
            raw.execute('INSERT INTO intents VALUES (?, ?)',
                        (self.intent.intent_id,
                         encode(pack(replace(self.intent, side='SELL')))))
            self.assertEqual(raw.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(raw.execute('PRAGMA foreign_key_check').fetchall(), [])
        with self.assertRaises(ValueError):
            self.db()
        with sqlite3.connect(self.path) as raw:
            self.assertEqual(raw.execute('SELECT COUNT(*) FROM intents').fetchone()[0], 2)

    def test_missing_not_null_constraint_rejected(self):
        with self.db():
            pass
        with sqlite3.connect(self.path) as raw:
            raw.execute('DROP TABLE states')
            raw.execute('DROP TABLE intents')
            raw.execute('CREATE TABLE intents(id TEXT PRIMARY KEY, data TEXT)')
            raw.execute('CREATE TABLE states(id TEXT PRIMARY KEY REFERENCES intents(id), data TEXT NOT NULL)')
        with self.assertRaises(ValueError):
            self.db()

    def test_missing_states_foreign_key_rejected(self):
        with self.db():
            pass
        with sqlite3.connect(self.path) as raw:
            raw.execute('DROP TABLE states')
            raw.execute('CREATE TABLE states(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
        with self.assertRaises(ValueError):
            self.db()

    def test_missing_meta_not_null_rejected(self):
        with self.db():
            pass
        with sqlite3.connect(self.path) as raw:
            raw.execute('ALTER TABLE meta RENAME TO prior_meta')
            raw.execute('CREATE TABLE meta(namespace TEXT)')
            raw.execute('INSERT INTO meta SELECT namespace FROM prior_meta')
            raw.execute('DROP TABLE prior_meta')
        with self.assertRaises(ValueError):
            self.db()

    def test_precision_optional_fields_and_nonmutation(self):
        intent = replace(self.intent, qty=D('3.00000000000001'), stop_price=None,
                         limit_price=None, queue_ahead_qty=None, expires_ts=None)
        s = OrderState(intent.intent_id, intent.qty)
        e = self.event('u', 'UNKNOWN_OUTCOME', reason=None, fill_id=None)
        s.apply(e)
        before = (repr(intent), list(s.events.items()))
        with self.db() as d:
            d.reserve(intent)
            d.save_state(s)
        with self.db() as d:
            self.assertEqual(d.load_intent('synthetic'), intent)
            self.assertEqual(str(d.load_intent('synthetic').qty), str(intent.qty))
            self.assertEqual(d.load_state('synthetic').events['u'], e)
        self.assertEqual((repr(intent), list(s.events.items())), before)

    def test_reject_forged_state(self):
        with self.db() as d:
            d.reserve(self.intent)
            s = self.state(self.event('a', 'ACCEPTED'))
            s.fees = D('1')
            with self.assertRaises(ValueError):
                d.save_state(s)

if __name__ == '__main__':
    unittest.main()
