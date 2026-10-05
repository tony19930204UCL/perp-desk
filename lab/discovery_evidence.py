"""Append-only compact causal evidence for Issue #23 discovery lab.

This is not a raw market archive. It preserves only decision/order/fill/fee/gap
facts required to explain outcomes after the bounded shared raw buffer rolls.
Records are hash chained and never pruned automatically; the lab's existing
32 MiB storage gate stops new entries before evidence is deleted.
"""
from __future__ import annotations
import hashlib, json, sqlite3
from collections import Counter
from pathlib import Path

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),default=str)

class CausalEvidence:
    def __init__(self,path):
        self.path=Path(path)
        with sqlite3.connect(self.path) as db:
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names-{'evidence','sqlite_sequence'}:
                raise ValueError('dedicated causal evidence store required')
            db.execute("""CREATE TABLE IF NOT EXISTS evidence(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_key TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL,
                ts INTEGER NOT NULL,
                payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                hash TEXT NOT NULL)""")

    def append(self,evidence_key,kind,ts,payload):
        if not isinstance(evidence_key,str) or not evidence_key or not isinstance(kind,str) or not kind:
            raise ValueError('evidence identity required')
        if type(ts) is not int or ts<0 or not isinstance(payload,dict):
            raise ValueError('invalid evidence record')
        raw=canonical(payload)
        with sqlite3.connect(self.path) as db:
            row=db.execute('SELECT kind,ts,payload,hash FROM evidence WHERE evidence_key=?',(evidence_key,)).fetchone()
            if row:
                if row[0]!=kind or row[2]!=raw:
                    raise ValueError('conflicting causal evidence key')
                return row[3]
            prior=db.execute('SELECT hash FROM evidence ORDER BY seq DESC LIMIT 1').fetchone()
            previous=prior[0] if prior else '0'*64
            digest=hashlib.sha256((previous+'|'+evidence_key+'|'+kind+'|'+str(ts)+'|'+raw).encode()).hexdigest()
            db.execute('INSERT INTO evidence(evidence_key,kind,ts,payload,previous_hash,hash) VALUES(?,?,?,?,?,?)',
                       (evidence_key,kind,ts,raw,previous,digest))
            return digest

    def records(self,kind=None):
        with sqlite3.connect(self.path) as db:
            if kind is None:
                rows=db.execute('SELECT evidence_key,kind,ts,payload,previous_hash,hash FROM evidence ORDER BY seq').fetchall()
            else:
                rows=db.execute('SELECT evidence_key,kind,ts,payload,previous_hash,hash FROM evidence WHERE kind=? ORDER BY seq',(kind,)).fetchall()
        return [dict(evidence_key=k,kind=t,ts=ts,payload=json.loads(p),previous_hash=prev,hash=h)
                for k,t,ts,p,prev,h in rows]

    def summary(self):
        rows=self.records()
        counts=Counter(r['kind'] for r in rows)
        return dict(records=len(rows),kinds=dict(sorted(counts.items())),
                    first_hash=rows[0]['hash'] if rows else None,
                    head_hash=rows[-1]['hash'] if rows else None,
                    automatic_pruning=False,
                    reconstruction_policy='compact causal facts retained; unavailable raw context is unknown')
