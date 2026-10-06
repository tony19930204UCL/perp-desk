"""Shared causal public-input pipeline for the staged Issue #23 discovery lab.

Synthetic/public engineering only. It never connects to an exchange and never mutates the
existing default PAPER account. Callers provide already-fetched public payloads once; this
module normalizes, hashes, deduplicates, bounds retention, and fans out identical events.
"""
from __future__ import annotations
import hashlib, json, sqlite3
from collections import deque
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_RAW_EVENTS=20000

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),default=str)

def _decimal(value, *, positive=False):
    if isinstance(value,(float,bool)) or not isinstance(value,(str,int,Decimal)):
        raise ValueError('exact decimal required')
    try:n=Decimal(value)
    except (InvalidOperation,ValueError):
        raise ValueError('invalid decimal') from None
    if not n.is_finite() or (positive and n<=0):
        raise ValueError('invalid decimal')
    return n

def binance_aggtrade_input(raw):
    """Map one actual Binance USD-M aggTrade payload shape into the shared contract."""
    if not isinstance(raw,dict):
        raise ValueError('aggTrade object required')
    trade_id=raw.get('a');first=raw.get('f');last=raw.get('l');source=raw.get('T');maker=raw.get('m')
    if (type(trade_id) is not int or trade_id<0 or type(first) is not int or type(last) is not int
            or first<0 or last<first or type(source) is not int or type(maker) is not bool):
        raise ValueError('invalid Binance aggTrade identity')
    price=str(_decimal(raw.get('p'),positive=True));qty=str(_decimal(raw.get('q'),positive=True))
    return dict(trade_id=trade_id,source_ts=source,price=price,qty=qty,
                aggressor='SELL' if maker else 'BUY',
                first_trade_id=first,last_trade_id=last)

class SharedFeed:
    """One durable source cursor/retention store for all three research arms."""

    def __init__(self,path,*,forward_start_ms,max_source_age_ms=15000,retention=MAX_RAW_EVENTS):
        self.path=Path(path)
        self.forward_start_ms=forward_start_ms
        self.max_source_age_ms=max_source_age_ms
        self.retention=retention
        if type(forward_start_ms) is not int or forward_start_ms<0:
            raise ValueError('invalid forward start')
        if type(retention) is not int or not 1<=retention<=MAX_RAW_EVENTS:
            raise ValueError('invalid bounded retention')
        self._subscribers=[]
        self.fetch_calls=0
        with sqlite3.connect(self.path) as db:
            names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names-{'feed_state','feed_events','sqlite_sequence'}:
                raise ValueError('dedicated shared feed store required')
            db.execute('CREATE TABLE IF NOT EXISTS feed_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS feed_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, sha256 TEXT NOT NULL)')
            row=db.execute('SELECT payload FROM feed_state WHERE id=1').fetchone()
            if row:
                self.state=json.loads(row[0])
                if self.state['forward_start_ms']!=forward_start_ms:
                    raise ValueError('shared feed identity mismatch')
                self.state.setdefault('events_evicted',0)
            else:
                self.state=dict(forward_start_ms=forward_start_ms,last_agg_trade_id=None,
                                aggtrade_valid=False,gaps=[],duplicates=0,out_of_order=0,
                                source_invalid=0,events_persisted=0,events_evicted=0,reconnects=0)
                self._save(db)

    def subscribe(self,callback):
        self._subscribers.append(callback)

    def _save(self,db):
        db.execute('INSERT OR REPLACE INTO feed_state VALUES(1,?)',(canonical(self.state),))

    def begin_poll(self):
        """Evidence counter: one upstream poll may fan out to all arms."""
        self.fetch_calls+=1

    def normalize(self,kind,payload,*,received_ms):
        if type(received_ms) is not int or received_ms<self.forward_start_ms:
            raise ValueError('invalid receipt time')
        if not isinstance(payload,dict):
            raise ValueError('payload required')
        if kind=='aggTrade':
            return self._aggtrade(payload,received_ms)
        if kind=='closed_bar':
            source=payload.get('close_time_ms')
            if (type(payload.get('open_time_ms')) is not int or type(source) is not int
                    or source!=payload['open_time_ms']+60000 or source>received_ms):
                raise ValueError('invalid closed bar time')
            values={}
            for name in ('open','high','low','close'):
                values[name]=str(_decimal(payload.get(name),positive=True))
            values['volume']=str(_decimal(payload.get('volume')))
            if Decimal(values['volume'])<0:
                raise ValueError('negative volume')
            if Decimal(values['low'])>min(Decimal(values['open']),Decimal(values['close'])) or Decimal(values['high'])<max(Decimal(values['open']),Decimal(values['close'])):
                raise ValueError('invalid OHLC')
            return dict(type='closed_bar',event_id='closed-bar:'+str(payload['open_time_ms']),
                        symbol='ETHUSDT',ts=received_ms,source_ts=source,
                        open_time_ms=payload['open_time_ms'],close_time_ms=source,closed=True,
                        source_valid=(0<=received_ms-source<=self.max_source_age_ms),**values)
        if kind=='book':
            source=payload.get('source_ts');receipt=payload.get('receipt_ts',received_ms)
            if (type(source) is not int or type(receipt) is not int or receipt>received_ms
                    or not self.forward_start_ms<=source<=received_ms):
                raise ValueError('invalid book source time')
            bids=payload.get('bids');asks=payload.get('asks')
            if not isinstance(bids,list) or not isinstance(asks,list):
                raise ValueError('book levels required')
            for side in (bids,asks):
                for level in side:
                    if not isinstance(level,(list,tuple)) or len(level)!=2:
                        raise ValueError('invalid level')
                    _decimal(level[0],positive=True);_decimal(level[1],positive=True)
            return dict(type='book',event_id='book:'+str(source)+':'+hashlib.sha256(canonical(payload).encode()).hexdigest()[:16],
                        symbol='ETHUSDT',ts=received_ms,source_ts=source,receipt_ts=receipt,bids=bids,asks=asks,
                        source_valid=(0<=received_ms-source<=self.max_source_age_ms))
        if kind=='mark':
            source=payload.get('source_ts');receipt=payload.get('receipt_ts',received_ms);price=payload.get('price')
            if (type(source) is not int or type(receipt) is not int or receipt>received_ms
                    or not self.forward_start_ms<=source<=received_ms):
                raise ValueError('invalid mark source time')
            _decimal(price,positive=True)
            return dict(type='mark',event_id='mark:'+str(source),symbol='ETHUSDT',ts=received_ms,
                        source_ts=source,receipt_ts=receipt,price=str(price),
                        source_valid=(0<=received_ms-source<=self.max_source_age_ms))
        if kind=='funding_status':
            source=payload.get('source_ts');valid=payload.get('valid_until_ts');complete=payload.get('complete')
            if type(source) is not int or type(valid) is not int or type(complete) is not bool:
                raise ValueError('invalid funding status')
            return dict(type='funding_status',event_id='funding-status:'+str(source),symbol='ETHUSDT',
                        ts=received_ms,source_ts=source,complete=complete,valid_until_ts=valid,
                        source_valid=(self.forward_start_ms<=source<=received_ms and 0<=received_ms-source<=self.max_source_age_ms))
        if kind=='funding':
            source=payload.get('source_ts');settle=payload.get('settlement_ts')
            if type(source) is not int or type(settle) is not int:
                raise ValueError('invalid funding timestamp')
            _decimal(payload.get('rate'));_decimal(payload.get('mark'),positive=True)
            return dict(type='funding',event_id='funding:'+str(settle)+':'+str(payload.get('rate_type','Regular')),
                        symbol='ETHUSDT',ts=received_ms,source_ts=source,settlement_ts=settle,
                        rate=str(payload['rate']),mark=str(payload['mark']),rate_type=payload.get('rate_type','Regular'),
                        finalized=payload.get('finalized') is True,
                        source_valid=(self.forward_start_ms<=source<=received_ms and 0<=received_ms-source<=self.max_source_age_ms))
        raise ValueError('unsupported shared source kind')

    def _aggtrade(self,payload,received_ms):
        tid=payload.get('trade_id');source=payload.get('source_ts')
        if type(tid) is not int or tid<0 or type(source) is not int or not self.forward_start_ms<=source<=received_ms:
            raise ValueError('invalid aggTrade identity/timestamp')
        receipt=payload.get('receipt_ts',received_ms)
        if type(receipt) is not int or receipt>received_ms:
            raise ValueError('invalid aggTrade receipt timestamp')
        price=_decimal(payload.get('price'),positive=True);qty=_decimal(payload.get('qty'),positive=True)
        aggressor=payload.get('aggressor')
        if aggressor not in ('BUY','SELL'):
            raise ValueError('explicit aggressor required')
        last=self.state.get('last_agg_trade_id')
        if last is not None and tid<last:
            self.state['out_of_order']+=1
            raise ValueError('out-of-order aggTrade id')
        if last is not None and tid>last+1:
            self.state['gaps'].append(dict(after_id=last,next_id=tid,observed_ms=received_ms))
            self.state['gaps']=self.state['gaps'][-128:]
            self.state['aggtrade_valid']=False
        duplicate=(last is not None and tid==last)
        if not duplicate:
            self.state['last_agg_trade_id']=tid
        timely=0<=received_ms-source<=self.max_source_age_ms
        valid=bool(timely and self.state.get('aggtrade_valid'))
        return dict(type='aggTrade',event_id='aggTrade:'+str(tid),trade_id=tid,symbol='ETHUSDT',
                    ts=received_ms,source_ts=source,receipt_ts=receipt,price=str(price),qty=str(qty),aggressor=aggressor,
                    source_valid=valid,duplicate=duplicate)

    def mark_reconnected(self,*,next_trade_id,observed_ms):
        """Explicitly establishes a new forward cursor. It never backfills maker fills."""
        if type(next_trade_id) is not int or next_trade_id<0 or type(observed_ms) is not int:
            raise ValueError('invalid reconnect')
        self.state['last_agg_trade_id']=next_trade_id-1
        self.state['aggtrade_valid']=True
        self.state['reconnects']+=1
        with sqlite3.connect(self.path) as db:
            self._save(db)

    def ingest(self,kind,payload,*,received_ms):
        event=self.normalize(kind,payload,received_ms=received_ms)
        if event.get('duplicate'):
            with sqlite3.connect(self.path) as db:
                row=db.execute('SELECT payload FROM feed_events WHERE event_id=?',(event['event_id'],)).fetchone()
                if row:
                    prior=json.loads(row[0])
                    for key in ('trade_id','source_ts','price','qty','aggressor'):
                        if prior.get(key)!=event.get(key):
                            raise ValueError('conflicting duplicate aggTrade id')
                self.state['duplicates']+=1
                self._save(db)
            return dict(event=event,persisted=False,dispatched=0)
        if event.get('source_valid') is False:
            self.state['source_invalid']+=1
        raw=canonical(event);digest=hashlib.sha256(raw.encode()).hexdigest()
        with sqlite3.connect(self.path) as db:
            row=db.execute('SELECT payload,sha256 FROM feed_events WHERE event_id=?',(event['event_id'],)).fetchone()
            if row:
                if row!=(raw,digest):
                    raise ValueError('conflicting shared event id')
                self.state['duplicates']+=1;self._save(db)
                return dict(event=event,persisted=False,dispatched=0)
            db.execute('INSERT INTO feed_events(event_id,payload,sha256) VALUES(?,?,?)',(event['event_id'],raw,digest))
            overflow=db.execute('SELECT count(*) FROM feed_events').fetchone()[0]-self.retention
            if overflow>0:
                db.execute('DELETE FROM feed_events WHERE seq IN (SELECT seq FROM feed_events ORDER BY seq LIMIT ?)',(overflow,))
                self.state['events_evicted']+=overflow
            self.state['events_persisted']+=1;self._save(db)
        dispatched=0
        for callback in tuple(self._subscribers):
            callback(dict(event));dispatched+=1
        return dict(event=event,persisted=True,sha256=digest,dispatched=dispatched)

    def snapshot(self):
        with sqlite3.connect(self.path) as db:
            rows=db.execute('SELECT count(*),coalesce(sum(length(payload)),0) FROM feed_events').fetchone()
            first=db.execute('SELECT event_id,sha256 FROM feed_events ORDER BY seq LIMIT 1').fetchone()
            last=db.execute('SELECT event_id,sha256 FROM feed_events ORDER BY seq DESC LIMIT 1').fetchone()
        return dict(self.state,retained_events=rows[0],retained_payload_bytes=rows[1],
                    retained_first_event_id=first[0] if first else None,
                    retained_first_sha256=first[1] if first else None,
                    retained_last_event_id=last[0] if last else None,
                    retained_last_sha256=last[1] if last else None,
                    retention_limit=self.retention,fetch_calls=self.fetch_calls)
