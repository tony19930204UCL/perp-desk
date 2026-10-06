"""Compact causal evidence for the staged discovery lab.

Decision/order/fill/fee/funding/stop facts remain append-only hash-chained
records. Repeated source-unknown observations that did not themselves change a
trading/risk decision may be represented by bounded rolling-integrity ranges.
Those ranges deliberately do not claim per-event market reconstruction.
"""
from __future__ import annotations
import hashlib, json, sqlite3
from collections import Counter
from pathlib import Path

UNKNOWN_RANGE_BUCKET_MS=300_000

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),default=str)

def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()

class CausalEvidence:
    def __init__(self,path):
        self.path=Path(path)
        with sqlite3.connect(self.path) as db:
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names-{'evidence','unknown_ranges','sqlite_sequence'}:
                raise ValueError('dedicated causal evidence store required')
            db.execute("""CREATE TABLE IF NOT EXISTS evidence(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                evidence_key TEXT UNIQUE NOT NULL,
                kind TEXT NOT NULL,
                ts INTEGER NOT NULL,
                payload TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                hash TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS unknown_ranges(
                range_key TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                bucket_start_ms INTEGER NOT NULL,
                first_ts INTEGER NOT NULL,
                last_ts INTEGER NOT NULL,
                first_source_ts INTEGER,
                last_source_ts INTEGER,
                first_receipt_ts INTEGER,
                last_receipt_ts INTEGER,
                first_event_id TEXT,
                last_event_id TEXT,
                first_trade_id INTEGER,
                last_trade_id INTEGER,
                count INTEGER NOT NULL,
                rolling_hash TEXT NOT NULL,
                first_reason TEXT,
                last_reason TEXT,
                reconstructible_events INTEGER NOT NULL,
                row_hash TEXT NOT NULL)""")

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
            digest=_hash(previous+'|'+evidence_key+'|'+kind+'|'+str(ts)+'|'+raw)
            db.execute('INSERT INTO evidence(evidence_key,kind,ts,payload,previous_hash,hash) VALUES(?,?,?,?,?,?)',
                       (evidence_key,kind,ts,raw,previous,digest))
            return digest

    @staticmethod
    def _range_values(scope,reason_code,bucket,first_ts,last_ts,first_source,last_source,
                      first_receipt,last_receipt,first_event,last_event,first_trade,last_trade,
                      count,rolling,first_reason,last_reason):
        return dict(scope=scope,reason_code=reason_code,bucket_start_ms=bucket,
                    first_ts=first_ts,last_ts=last_ts,
                    first_source_ts=first_source,last_source_ts=last_source,
                    first_receipt_ts=first_receipt,last_receipt_ts=last_receipt,
                    first_event_id=first_event,last_event_id=last_event,
                    first_trade_id=first_trade,last_trade_id=last_trade,
                    count=count,rolling_hash=rolling,first_reason=first_reason,last_reason=last_reason,
                    reconstructible_events=0)

    @staticmethod
    def _row_hash(values):
        return _hash(canonical(values))

    def record_unknown_range(self,scope,reason_code,ts,payload):
        """Durably aggregate a non-decision-changing unknown observation.

        The record preserves count, first/last source/receipt/dispatch/event/trade
        boundaries and a rolling digest of every observation. It intentionally
        cannot reconstruct each rolled raw market event.
        """
        if (not isinstance(scope,str) or not scope or len(scope)>80 or
                not isinstance(reason_code,str) or not reason_code or len(reason_code)>80):
            raise ValueError('bounded unknown scope/reason required')
        if type(ts) is not int or ts<0 or not isinstance(payload,dict):
            raise ValueError('invalid unknown range observation')
        source=payload.get('source_ts');receipt=payload.get('receipt_ts')
        event_id=payload.get('event_id');trade_id=payload.get('trade_id');reason=payload.get('reason')
        if source is not None and type(source) is not int:raise ValueError('invalid unknown source boundary')
        if receipt is not None and type(receipt) is not int:raise ValueError('invalid unknown receipt boundary')
        if event_id is not None and not isinstance(event_id,str):raise ValueError('invalid unknown event boundary')
        if trade_id is not None and type(trade_id) is not int:raise ValueError('invalid unknown trade boundary')
        if reason is not None and not isinstance(reason,str):raise ValueError('invalid unknown reason')
        bucket=(ts//UNKNOWN_RANGE_BUCKET_MS)*UNKNOWN_RANGE_BUCKET_MS
        key=scope+'|'+reason_code+'|'+str(bucket)
        observed=dict(scope=scope,reason_code=reason_code,dispatch_ts=ts,
                      source_ts=source,receipt_ts=receipt,event_id=event_id,trade_id=trade_id,
                      event_type=payload.get('type'),reason=reason)
        event_digest=_hash(canonical(observed))
        with sqlite3.connect(self.path) as db:
            row=db.execute("""SELECT scope,reason_code,bucket_start_ms,first_ts,last_ts,
                first_source_ts,last_source_ts,first_receipt_ts,last_receipt_ts,
                first_event_id,last_event_id,first_trade_id,last_trade_id,count,rolling_hash,
                first_reason,last_reason,reconstructible_events,row_hash
                FROM unknown_ranges WHERE range_key=?""",(key,)).fetchone()
            if row is None:
                values=self._range_values(scope,reason_code,bucket,ts,ts,source,source,receipt,receipt,
                                          event_id,event_id,trade_id,trade_id,1,event_digest,reason,reason)
                db.execute("""INSERT INTO unknown_ranges(range_key,scope,reason_code,bucket_start_ms,
                    first_ts,last_ts,first_source_ts,last_source_ts,first_receipt_ts,last_receipt_ts,
                    first_event_id,last_event_id,first_trade_id,last_trade_id,count,rolling_hash,
                    first_reason,last_reason,reconstructible_events,row_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key,values['scope'],values['reason_code'],values['bucket_start_ms'],
                     values['first_ts'],values['last_ts'],values['first_source_ts'],values['last_source_ts'],
                     values['first_receipt_ts'],values['last_receipt_ts'],values['first_event_id'],values['last_event_id'],
                     values['first_trade_id'],values['last_trade_id'],values['count'],values['rolling_hash'],
                     values['first_reason'],values['last_reason'],0,self._row_hash(values)))
                return key
            prior=self._range_values(*row[:17])
            if row[17]!=0 or row[18]!=self._row_hash(prior):
                raise ValueError('unknown range integrity mismatch')
            if ts<prior['last_ts']:
                raise ValueError('unknown range chronology regression')
            rolling=_hash(prior['rolling_hash']+'|'+event_digest)
            values=self._range_values(scope,reason_code,bucket,prior['first_ts'],ts,
                                      prior['first_source_ts'],source,
                                      prior['first_receipt_ts'],receipt,
                                      prior['first_event_id'],event_id,
                                      prior['first_trade_id'],trade_id,
                                      prior['count']+1,rolling,prior['first_reason'],reason)
            db.execute("""UPDATE unknown_ranges SET last_ts=?,last_source_ts=?,last_receipt_ts=?,
                last_event_id=?,last_trade_id=?,count=?,rolling_hash=?,last_reason=?,row_hash=?
                WHERE range_key=?""",
                (ts,source,receipt,event_id,trade_id,values['count'],rolling,reason,
                 self._row_hash(values),key))
            return key

    def records(self,kind=None):
        with sqlite3.connect(self.path) as db:
            if kind is None:
                rows=db.execute('SELECT evidence_key,kind,ts,payload,previous_hash,hash FROM evidence ORDER BY seq').fetchall()
            else:
                rows=db.execute('SELECT evidence_key,kind,ts,payload,previous_hash,hash FROM evidence WHERE kind=? ORDER BY seq',(kind,)).fetchall()
        return [dict(evidence_key=k,kind=t,ts=ts,payload=json.loads(p),previous_hash=prev,hash=h)
                for k,t,ts,p,prev,h in rows]

    def unknown_ranges(self):
        with sqlite3.connect(self.path) as db:
            rows=db.execute("""SELECT range_key,scope,reason_code,bucket_start_ms,first_ts,last_ts,
                first_source_ts,last_source_ts,first_receipt_ts,last_receipt_ts,
                first_event_id,last_event_id,first_trade_id,last_trade_id,count,rolling_hash,
                first_reason,last_reason,reconstructible_events,row_hash
                FROM unknown_ranges ORDER BY bucket_start_ms,range_key""").fetchall()
        result=[]
        for row in rows:
            key=row[0];values=self._range_values(*row[1:18])
            if row[18]!=0 or row[19]!=self._row_hash(values):
                raise ValueError('unknown range integrity mismatch')
            result.append(dict(range_key=key,**values,row_hash=row[19]))
        return result

    def summary(self):
        rows=self.records();ranges=self.unknown_ranges()
        counts=Counter(r['kind'] for r in rows)
        return dict(records=len(rows),kinds=dict(sorted(counts.items())),
                    first_hash=rows[0]['hash'] if rows else None,
                    head_hash=rows[-1]['hash'] if rows else None,
                    unknown_range_rows=len(ranges),
                    unknown_events_aggregated=sum(r['count'] for r in ranges),
                    unknown_range_bucket_ms=UNKNOWN_RANGE_BUCKET_MS,
                    unknown_range_reconstructible_events=0,
                    automatic_pruning=False,
                    reconstruction_policy=('decision-changing causal facts remain append-only; repeated non-decision '
                        'unknowns retain count/bounds/rolling integrity only; rolled raw events remain unknown'))
