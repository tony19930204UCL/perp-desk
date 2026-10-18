"""Real temporary SQLite fixtures and read-only source proofs."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dashboard_multiarm_reader as r
import dashboard_multiarm_model as m

DATA=json.loads((Path(__file__).parent/'fixtures/dashboard_multiarm_browser.json').read_text())
NOW=DATA['last_ts']+120000


def make_db(root, dirname='paper-w1-xrpusdt', payload=None):
    folder=root/dirname;folder.mkdir(parents=True,exist_ok=True)
    path=folder/'broker.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sim_broker_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),payload TEXT NOT NULL)')
        db.execute('INSERT INTO sim_broker_state VALUES(1,?)',(json.dumps(DATA if payload is None else payload),))
    return path


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=make_db(self.root)
    def test_read(self):
        payload,reason=r.read_one(self.root,'paper-w1-xrpusdt')
        self.assertIsNone(reason)
        self.assertEqual(payload['cash'],DATA['cash'])
    def test_neighbor_unknown(self):
        result=r.read_all(self.root,now_ms=NOW)
        self.assertEqual(len(result['arms']),12)
        self.assertEqual(result['arms'][7]['state'],'known')
        self.assertEqual(result['arms'][0]['state'],'unknown')
    def test_no_mutations(self):
        before=(self.path.read_bytes(),self.path.stat().st_mtime_ns)
        for _ in range(50):
            self.assertEqual(r.read_one(self.root,'paper-w1-xrpusdt')[1],None)
        self.assertEqual(before,(self.path.read_bytes(),self.path.stat().st_mtime_ns))
        self.assertFalse(any(Path(str(self.path)+name).exists() for name in ('-wal','-shm','-journal')))
    def test_readonly_handle(self):
        from urllib.parse import quote
        uri='file:'+quote(str(self.path),safe='/')+'?mode=ro'
        with sqlite3.connect(uri,uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            with self.assertRaises(sqlite3.OperationalError):
                db.execute('INSERT INTO sim_broker_state VALUES(2,?)',('{}',))
    def test_no_row(self):
        with sqlite3.connect(self.path) as db:
            db.execute('DELETE FROM sim_broker_state')
        self.assertEqual(r.read_one(self.root,'paper-w1-xrpusdt')[1],'MISSING_ROW')
    def test_bad_json(self):
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE sim_broker_state SET payload=?',('{bad',))
        self.assertEqual(r.read_one(self.root,'paper-w1-xrpusdt')[1],'BAD_JSON')
    def test_corrupt(self):
        self.path.write_bytes(b'not a database')
        self.assertEqual(r.read_one(self.root,'paper-w1-xrpusdt')[1],'DB_UNAVAILABLE')
    def test_oversize(self):
        self.assertEqual(r.read_one(self.root,'paper-w1-xrpusdt',max_bytes=5)[1],'TOO_LARGE')
    def test_missing_file(self):
        self.path.unlink()
        self.assertEqual(r.read_one(self.root,'paper-w1-xrpusdt')[1],'MISSING_FILE')
    def test_missing_table(self):
        self.path.unlink()
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE other(id INTEGER)')
        self.assertEqual(r.read_one(self.root,'paper-w1-xrpusdt')[1],'DB_UNAVAILABLE')
    def test_directory_at_db(self):
        self.path.unlink();self.path.mkdir()
        self.assertEqual(r.read_one(self.root,'paper-w1-xrpusdt')[1],'NOT_REGULAR')
    def test_sidecar_rejected(self):
        side=Path(str(self.path)+'-wal');side.write_bytes(b'opaque')
        self.assertEqual(r.read_one(self.root,'paper-w1-xrpusdt')[1],'ACTIVE_SIDECAR')
    def test_locked_db(self):
        lock=sqlite3.connect(self.path)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            result=r.read_one(self.root,'paper-w1-xrpusdt')
            self.assertIsNotNone(result[1])
        finally:
            lock.rollback();lock.close()
    def test_flat_arms(self):
        flat=dict(DATA,positions={},marks={},fills=[],orders={},ledger=[],cash='100')
        make_db(self.root,'paper-t1-btcusdt',flat)
        result=r.read_all(self.root,now_ms=NOW)
        self.assertEqual(result['arms'][8]['orders']['total'],0)


if __name__=='__main__':unittest.main()
