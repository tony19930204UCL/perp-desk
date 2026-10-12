"""SQLite checkpoint storage; separate from the SimBroker ledger."""
import json
import os
import sqlite3
from dataclasses import fields
from decimal import Decimal
from execution_interface import Intent, Event, OrderState

DEC = {'qty', 'stop_price', 'limit_price', 'queue_ahead_qty', 'price', 'fee'}
OPT = {'stop_price', 'limit_price', 'queue_ahead_qty', 'expires_ts', 'price', 'reason', 'fill_id'}
BOOL = {'reduce_only', 'protective'}
INT = {'created_ts', 'expires_ts'}

def pack(obj):
    if type(obj) not in (Intent, Event):
        raise ValueError('record type')
    data = {}
    for f in fields(obj):
        k, v = f.name, getattr(obj, f.name)
        if v is None and k in OPT:
            pass
        elif k in DEC:
            if type(v) is not Decimal or not v.is_finite():
                raise ValueError('decimal')
            v = str(v)
        elif k in BOOL:
            if type(v) is not bool:
                raise ValueError('bool')
        elif k in INT:
            if type(v) is not int:
                raise ValueError('integer')
        elif type(v) is not str:
            raise ValueError('string')
        data[k] = v
    if not data['intent_id'] or (type(obj) is Event and not data['event_id']):
        raise ValueError('identity')
    if type(obj) is Intent and (not data['symbol'] or obj.qty <= 0):
        raise ValueError('intent')
    return data

def unpack(data, cls):
    if type(data) is not dict or set(data) != {f.name for f in fields(cls)}:
        raise ValueError('record fields')
    vals = dict(data)
    try:
        for k in DEC & vals.keys():
            if vals[k] is not None:
                if type(vals[k]) is not str:
                    raise ValueError('decimal string')
                vals[k] = Decimal(vals[k])
        result = cls(**vals)
        pack(result)
        return result
    except (TypeError, ArithmeticError) as exc:
        raise ValueError('corrupt record') from exc

def replay(intent, raw):
    data = json.loads(raw) if type(raw) is str else raw
    if type(data) is not list:
        raise ValueError('checkpoint format')
    state = OrderState(intent.intent_id, intent.qty)
    for entry in data:
        state.apply(unpack(entry, Event))
    if len(data) != len(state.events):
        raise ValueError('duplicate events')
    return state

def encode(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'))

class ExecutionStateStore:
    def __init__(self, path, *, namespace):
        if type(namespace) is not str or not namespace or '\x00' in namespace:
            raise ValueError('namespace')
        path = os.fspath(path)
        if not path or path == ':memory:':
            raise ValueError('durable path required')
        new = not os.path.exists(path)
        self.db = sqlite3.connect(path, isolation_level=None)
        try:
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA synchronous=FULL')
            if new:
                with self.db:
                    self.db.execute('PRAGMA application_id=1160926513')
                    self.db.execute('PRAGMA user_version=1')
                    self.db.execute('CREATE TABLE meta(namespace TEXT NOT NULL)')
                    self.db.execute('CREATE TABLE intents(id TEXT PRIMARY KEY, data TEXT NOT NULL)')
                    self.db.execute('CREATE TABLE states(id TEXT PRIMARY KEY REFERENCES intents(id), data TEXT NOT NULL)')
                    self.db.execute('INSERT INTO meta VALUES (?)', (namespace,))
            self.check(namespace)
        except Exception:
            self.close()
            raise

    def check(self, namespace):
        db = self.db
        if db.execute('PRAGMA application_id').fetchone()[0] != 1160926513 or db.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('foreign database/version')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {'meta', 'intents', 'states'}:
            raise ValueError('foreign schema')
        if db.execute('SELECT namespace FROM meta').fetchall() != [(namespace,)]:
            raise ValueError('namespace mismatch')
        for table, expected in (('meta', ['namespace']), ('intents', ['id', 'data']), ('states', ['id', 'data'])):
            if [r[1] for r in db.execute('PRAGMA table_info(' + table + ')')] != expected:
                raise ValueError('schema mismatch')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('database corruption')
        for key, raw in db.execute('SELECT id,data FROM intents'):
            if unpack(json.loads(raw), Intent).intent_id != key:
                raise ValueError('corrupt intent')
        for key, raw in db.execute('SELECT id,data FROM states'):
            intent = self.load_intent(key)
            if intent is None:
                raise ValueError('orphan state')
            replay(intent, raw)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def load_intent(self, intent_id):
        row = self.db.execute('SELECT data FROM intents WHERE id=?', (intent_id,)).fetchone()
        if row is None:
            return None
        obj = unpack(json.loads(row[0]), Intent)
        if obj.intent_id != intent_id:
            raise ValueError('intent identity')
        return obj

    def reserve(self, intent):
        raw = encode(pack(intent))
        db = self.db
        db.execute('BEGIN IMMEDIATE')
        try:
            old = db.execute('SELECT data FROM intents WHERE id=?', (intent.intent_id,)).fetchone()
            if old:
                if old[0] != raw:
                    raise ValueError('conflicting intent')
                db.execute('COMMIT')
                return False
            db.execute('INSERT INTO intents VALUES (?,?)', (intent.intent_id, raw))
            db.execute('COMMIT')
            return True
        except Exception:
            db.execute('ROLLBACK')
            raise

    def load_state(self, intent_id):
        row = self.db.execute('SELECT data FROM states WHERE id=?', (intent_id,)).fetchone()
        if row is None:
            return None
        intent = self.load_intent(intent_id)
        if intent is None:
            raise ValueError('orphan state')
        return replay(intent, row[0])

    def save_state(self, state):
        if type(state) is not OrderState:
            raise ValueError('state type')
        db = self.db
        db.execute('BEGIN IMMEDIATE')
        try:
            intent = self.load_intent(state.intent_id)
            if intent is None or state.qty != intent.qty:
                raise ValueError('unreserved or mismatched state')
            events = [pack(e) for e in state.events.values()]
            valid = replay(intent, events)
            if (valid.state, valid.filled_qty, valid.fees, list(valid.events)) != (state.state, state.filled_qty, state.fees, list(state.events)):
                raise ValueError('state mismatch')
            old = db.execute('SELECT data FROM states WHERE id=?', (state.intent_id,)).fetchone()
            if old:
                previous = json.loads(old[0])
                replay(intent, previous)
                if events[:len(previous)] != previous or len(events) < len(previous):
                    raise ValueError('checkpoint rollback/conflict')
            db.execute('INSERT OR REPLACE INTO states VALUES (?,?)', (state.intent_id, encode(events)))
            db.execute('COMMIT')
        except Exception:
            db.execute('ROLLBACK')
            raise
