"""Historical indicator context ONLY. Unchanged H1 comparison, no historical intents."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from signals import Detector as ForwardDetector
from paper_market import receipt_ms, decimal_string

STEP=300000

def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),default=str)

class Detector(ForwardDetector):
    interval_name = "5m"
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.context_path=self.path.with_name('indicator_context.sqlite3')
        with closing(sqlite3.connect(self.context_path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS context(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
    def manifest(self):
        with closing(sqlite3.connect(self.context_path)) as db:
            row=db.execute('SELECT payload FROM context WHERE id=1').fetchone()
        return json.loads(row[0]) if row else None
    def seeded(self,cutoff_ms):
        saved=self.manifest()
        return bool(saved and saved['decision_cutoff_ms']==cutoff_ms)
    def seed(self,receipt,*,cutoff_ms,now_ms):
        if self.seeded(cutoff_ms):
            return self.manifest()
        end=(cutoff_ms-1)//self.interval_ms*self.interval_ms
        if receipt.get('endpoint')!='/fapi/v1/klines' or receipt.get('params')!={'symbol':'ETHUSDT','interval':self.interval_name,'startTime':end-61*self.interval_ms,'endTime':end-1,'limit':61}:
            raise ValueError('invalid bootstrap source request')
        if not 0<=now_ms-receipt_ms(receipt)<=15000:
            raise ValueError('stale bootstrap receipt')
        rows=receipt['payload']
        if len(rows)!=61:
            raise ValueError('incomplete bootstrap history')
        bars=[]
        saved_registration=self._registration
        saved_bars=self._bars
        self._registration={**saved_registration,'forward_start':datetime.fromtimestamp(0,timezone.utc).isoformat()}
        self._bars={}
        try:
            for i,row in enumerate(rows):
                expected=end-61*self.interval_ms+i*self.interval_ms
                if type(row[0]) is not int or type(row[6]) is not int or row[0]!=expected or row[6]!=expected+self.interval_ms-1:
                    raise ValueError('discontinuous bootstrap history')
                if row[6]+1>=cutoff_ms or row[6]+1>now_ms:
                    raise ValueError('unclosed/preactivation boundary bootstrap history')
                bar=dict(symbol='ETHUSDT',open_time_ms=row[0],close_time_ms=row[6]+1,closed=True,**{k:Decimal(decimal_string(row[j],k!='volume')) for k,j in [('open',1),('high',2),('low',3),('close',4),('volume',5)]})
                result=super()._process(bar,now=datetime.fromtimestamp(now_ms/1000,timezone.utc))
                if result['diagnostic']!='warmup' or result['intent'] is not None:
                    raise ValueError('invalid bootstrap bar')
                bars.append(bar)
        finally:
            self._registration=saved_registration
            self._bars=saved_bars
        manifest=dict(decision_cutoff_ms=cutoff_ms,context_start_ms=bars[0]['open_time_ms'],context_end_ms=end,bars_count=61,receipt_sha256=hashlib.sha256(canonical(receipt).encode()).hexdigest(),bars_sha256=hashlib.sha256(canonical(bars).encode()).hexdigest(),received_at=receipt['received_at'],source_close_times_ms=[b['close_time_ms'] for b in bars],performance_sample=False)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('ATTACH DATABASE ? AS contextdb',(str(self.context_path),))
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO h1_state VALUES(?,?,?)',(self._registration['version_id'],'ETHUSDT',canonical(bars)))
            db.execute('INSERT OR REPLACE INTO contextdb.context VALUES(1,?)',(canonical(manifest),))
        return manifest
    def _process(self,bar,*,now,session=None):
        manifest=self.manifest()
        cutoff=manifest['decision_cutoff_ms'] if manifest else int(datetime.fromisoformat(self._registration['forward_start']).timestamp()*1000)
        if isinstance(bar,dict) and type(bar.get('close_time_ms')) is int and bar['close_time_ms']<cutoff:
            return dict(diagnostic='reject_before_decision_cutoff',intent=None)
        boundary=isinstance(bar,dict) and bar.get('close_time_ms')==cutoff
        if boundary and bar.get('symbol') in self._bars:
            # Observed boundary bar is forward context only, never an eligible decision.
            # Limit prior to 60 so unchanged detector cannot generate even an internal intent.
            self._bars[bar['symbol']]=self._bars[bar['symbol']][-60:]
        registration=self._registration
        self._registration={**registration,'forward_start':datetime.fromtimestamp(0,timezone.utc).isoformat()}
        try:
            result=super()._process(bar,now=now,session=session)
            if boundary and result['diagnostic']=='warmup':
                return dict(diagnostic='no_signal',intent=None,context_only=True,reason='activation_boundary_not_eligible')
            return result
        finally:
            self._registration=registration
