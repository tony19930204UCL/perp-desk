#!/usr/bin/env python3
"""Staged operator CLI for Issue #23 three-arm ETH discovery.

Public market-data GETs only. No private API, exchange order endpoint, scheduler,
service installation, default PAPER mutation, or automatic research activation.
"""
from __future__ import annotations
import argparse, fcntl, json, os, tempfile, time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import URLError

from discovery_feed import SharedFeed, binance_aggtrade_input
from discovery_lab import DiscoveryLab
from paper_market import PublicMarketClient, instrument, book_event, mark_event, receipt_ms, decimal_string

CONFIG=Path(__file__).with_name('discovery_config_v1.json')
RUNNER_SCHEMA=1

def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as s:
            json.dump(data,s,sort_keys=True,separators=(',',':'))
            s.flush();os.fsync(s.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def iso(ms):
    return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat().replace('+00:00','Z')

class BinanceDiscoveryClient(PublicMarketClient):
    ALLOWED={**PublicMarketClient.ALLOWED,
             '/fapi/v1/aggTrades':{'symbol','fromId','startTime','endTime','limit'},
             '/fapi/v1/fundingInfo':set()}

class FixtureClient:
    """Static public-shaped receipt injector for subprocess engineering tests."""
    def __init__(self,path):
        data=json.loads(Path(path).read_text())
        if data.get('label')!='PUBLIC-SHAPED SYNTHETIC TRANSPORT - NOT MARKET PERFORMANCE':
            raise ValueError('explicit synthetic transport label required')
        self.responses=data.get('responses',{})
        self.calls=[];self.indices={}
    def get(self,endpoint,params=None):
        params=params or {}
        key=endpoint
        choices=self.responses.get(key)
        if not isinstance(choices,list) or not choices:
            raise ValueError('fixture missing endpoint '+endpoint)
        # Select the first receipt whose optional fixture_match is a subset of params.
        candidates=[item for item in choices
                    if all(params.get(k)==v for k,v in item.get('fixture_match',{}).items())]
        if not candidates:candidates=[choices[-1]]
        index=self.indices.get(endpoint,0);selected=candidates[min(index,len(candidates)-1)]
        self.indices[endpoint]=index+1
        delay=selected.get('fixture_delay_ms',0)
        if type(delay) is not int or delay<0 or delay>5000:
            raise ValueError('invalid fixture delay')
        if delay:time.sleep(delay/1000)
        receipt={k:v for k,v in selected.items() if k not in ('fixture_match','fixture_delay_ms')}
        receipt['endpoint']=endpoint;receipt['params']=dict(params)
        self.calls.append(dict(endpoint=endpoint,params=dict(params)))
        return receipt

def public_source_preflight(*,client=None,config_path=CONFIG,clock_ms=None):
    """Rootless bounded validation of every public source required by one runner cycle.

    No DiscoveryRunner/DiscoveryLab/SharedFeed object is constructed, so no root,
    research state, window, order, account or history can be created or changed.
    """
    config=json.loads(Path(config_path).read_text())
    client=client or BinanceDiscoveryClient()
    now_fn=clock_ms or (lambda:int(time.time()*1000))
    fees={'maker':config['fees']['maker'],'taker':config['fees']['taker']}
    reference=client.get('/fapi/v1/exchangeInfo')
    spec=instrument(reference,'ETHUSDT','crypto',fees)

    depth=client.get('/fapi/v1/depth',dict(symbol='ETHUSDT',limit=20))
    book=book_event(depth,'ETHUSDT',config['max_source_age_ms'])
    mark_receipt=client.get('/fapi/v1/premiumIndex',dict(symbol='ETHUSDT'))
    mark=mark_event(mark_receipt,'ETHUSDT',config['max_source_age_ms'])
    observed_mark=receipt_ms(mark_receipt)
    next_funding=mark_receipt['payload'].get('nextFundingTime')
    if type(next_funding) is not int or next_funding<=observed_mark:
        raise ValueError('future nextFundingTime required')

    funding_info=client.get('/fapi/v1/fundingInfo')
    rows=funding_info.get('payload')
    if not isinstance(rows,list):raise ValueError('fundingInfo array required')
    matched=[row for row in rows if row.get('symbol')=='ETHUSDT']
    if len(matched)>1 or (matched and matched[0].get('fundingIntervalHours')!=8):
        raise ValueError('unsupported funding interval requires operator review')

    funding=client.get('/fapi/v1/fundingRate',dict(symbol='ETHUSDT',limit=1000))
    frows=funding.get('payload');fobs=receipt_ms(funding)
    if not isinstance(frows,list) or len(frows)>=1000:raise ValueError('missing/truncated funding coverage')
    previous=None
    for row in frows:
        ts=row.get('fundingTime')
        if row.get('symbol')!='ETHUSDT' or type(ts) is not int or ts>fobs or (previous is not None and ts<=previous):
            raise ValueError('invalid finalized funding sequence')
        decimal_string(row.get('fundingRate'));decimal_string(row.get('markPrice'),True)
        previous=ts
    regular=[row.get('fundingTime') for row in frows if row.get('rateType','Regular')=='Regular']
    if any(b-a!=28_800_000 for a,b in zip(regular,regular[1:])):
        raise ValueError('incomplete regular finalized funding sequence')

    agg=client.get('/fapi/v1/aggTrades',dict(symbol='ETHUSDT',limit=1000))
    arows=agg.get('payload');aobs=receipt_ms(agg)
    if not isinstance(arows,list) or not arows or len(arows)>1000:raise ValueError('nonempty bounded aggTrade batch required')
    mapped=[binance_aggtrade_input(row) for row in arows]
    if any(b['trade_id']!=a['trade_id']+1 for a,b in zip(mapped,mapped[1:])):
        raise ValueError('aggTrade IDs not contiguous')
    if any(b['source_ts']<a['source_ts'] for a,b in zip(mapped,mapped[1:])):
        raise ValueError('aggTrade source chronology regression')
    if any(not 0<=aobs-row['source_ts']<=config['max_source_age_ms'] for row in mapped):
        raise ValueError('stale or future aggTrade source timestamp')

    klines=client.get('/fapi/v1/klines',dict(symbol='ETHUSDT',interval='1m',limit=100))
    kobs=receipt_ms(klines);krows=klines.get('payload')
    if not isinstance(krows,list):raise ValueError('kline array required')
    bars=[]
    for row in krows:
        if not isinstance(row,list) or len(row)<7 or type(row[0]) is not int or type(row[6]) is not int or row[6]!=row[0]+59_999:
            raise ValueError('invalid 1m kline')
        close_ms=row[6]+1
        if close_ms>kobs:continue
        for value in row[1:6]: decimal_string(value, value is not row[5])
        bars.append((row[0],close_ms))
    if len(bars)<61:raise ValueError('61 contiguous historical warmup bars unavailable')
    bars=bars[-61:]
    if any(b[0]!=a[0]+60_000 for a,b in zip(bars,bars[1:])):
        raise ValueError('historical warmup gap')

    checked=['/fapi/v1/exchangeInfo','/fapi/v1/depth','/fapi/v1/premiumIndex','/fapi/v1/fundingInfo',
             '/fapi/v1/fundingRate','/fapi/v1/aggTrades','/fapi/v1/klines']
    return dict(action='preflight',pass=True,classification='PUBLIC_SOURCE_PREACTIVATION_NO_TRADING_STATE',
                checked_endpoints=checked,instrument=spec,warmup_bars=len(bars),
                aggtrade_contiguous=True,source_age_valid=True,
                book_source_age_ms=book['observed_ms']-book['ts_ms'],
                mark_source_age_ms=mark['observed_ms']-mark['ts_ms'],
                observed_at_ms=now_fn(),root_touched=False,window_started=False,activated=False,
                private_endpoint_used=False,orders_created=False)

class DiscoveryRunner:
    def __init__(self,root,*,client=None,clock_ms=None,config_path=CONFIG):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.client=client or BinanceDiscoveryClient()
        self.clock=clock_ms or (lambda:int(time.time()*1000))
        self.config_path=Path(config_path)
        self.config=json.loads(self.config_path.read_text())
        self.runner_path=self.root/'runner_state.json'
        self.runner_state_lock=self.root/'runner-state.lock'
        self.state=self._load_runner_state()

    def _load_runner_state(self):
        if self.runner_path.exists():
            state=json.loads(self.runner_path.read_text())
            if state.get('schema_version')!=RUNNER_SCHEMA:
                raise ValueError('runner state schema mismatch')
            return state
        state=dict(schema_version=RUNNER_SCHEMA,prepared=False,stop_requested=False,
                   instrument=None,last_bar_cursor=None,funding_cursor=None,
                   polls=0,last_poll_ms=None,last_error=None,poll_failures=0,
                   consecutive_failures=0,last_failure_ms=None,last_failure_retryable=False,
                   lifecycle='idle',terminal_blocked=False)
        atomic_json(self.runner_path,state);return state

    def _save(self):
        # runner_state has two writers: the owned pump and the documented stop CLI.
        # Stop is monotonic for an activated root: there is no resume/clear command,
        # so a stale in-flight pump must never overwrite a concurrent true with false.
        fd=os.open(self.runner_state_lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            if self.runner_path.exists():
                current=json.loads(self.runner_path.read_text())
                if current.get('schema_version')!=RUNNER_SCHEMA:
                    raise ValueError('runner state schema mismatch')
                if current.get('stop_requested'):
                    self.state['stop_requested']=True
                    if current.get('stop_requested_ms') is not None:
                        prior=self.state.get('stop_requested_ms')
                        self.state['stop_requested_ms']=current['stop_requested_ms'] if prior is None else min(prior,current['stop_requested_ms'])
            atomic_json(self.runner_path,self.state)
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

    def _observe_stop(self,lab):
        durable=self._load_runner_state()
        if durable.get('stop_requested'):
            self.state['stop_requested']=True
            self.state['stop_requested_ms']=durable.get('stop_requested_ms')
            lab.request_operator_stop(self.clock())
            return True
        return False

    def _causal_dispatch_ms(self,source_ms,receipt_ms,label):
        if type(source_ms) is not int or type(receipt_ms) is not int:
            raise ValueError('invalid '+label+' timing')
        if source_ms<=receipt_ms:
            return receipt_ms
        future=source_ms-receipt_ms
        if future>5000:
            raise ValueError(label+' source timestamp too far in future')
        # Quarantine, do not dispatch, until the unchanged local wall clock reaches
        # the raw source timestamp. Raw source and receipt remain separately stored.
        deadline=time.monotonic()+5.0
        while self.clock()<source_ms:
            if time.monotonic()>=deadline:
                raise ValueError(label+' source remained future after bounded wait')
            wait_ms=max(1,min(250,source_ms-self.clock()))
            time.sleep(wait_ms/1000)
        dispatch=self.clock()
        if dispatch-source_ms>self.config['max_source_age_ms']:
            raise ValueError(label+' source stale after bounded wait')
        return dispatch

    def _spec(self):
        receipt=self.client.get('/fapi/v1/exchangeInfo')
        spec=instrument(receipt,'ETHUSDT','crypto',
                        {'maker':self.config['fees']['maker'],'taker':self.config['fees']['taker']})
        expected=self.state.get('instrument')
        if expected is not None and spec!=expected:
            raise ValueError('ETH reference filters changed; operator review required')
        return spec

    @staticmethod
    def _bars(receipt,cutoff_ms):
        if receipt.get('endpoint')!='/fapi/v1/klines' or receipt.get('params',{}).get('symbol')!='ETHUSDT':
            raise ValueError('ETH 1m kline receipt required')
        rows=receipt.get('payload')
        if not isinstance(rows,list):raise ValueError('kline array required')
        bars=[]
        for row in rows:
            if not isinstance(row,list) or len(row)<7 or type(row[0]) is not int or type(row[6]) is not int or row[6]!=row[0]+59999:
                raise ValueError('invalid 1m kline')
            close_ms=row[6]+1
            if close_ms>cutoff_ms:continue
            bars.append(dict(open_time_ms=row[0],close_time_ms=close_ms,
                             open=decimal_string(row[1],True),high=decimal_string(row[2],True),
                             low=decimal_string(row[3],True),close=decimal_string(row[4],True),
                             volume=decimal_string(row[5])))
        return bars

    def prepare(self):
        spec=self._spec()
        self.state.update(prepared=True,instrument=spec,prepared_at_ms=self.clock(),last_error=None)
        self._save()
        return dict(action='prepare',activated=False,window_started=False,instrument=spec,
                    note='reference filters verified; no research window started')

    def activate(self,*,operator_accepted):
        if operator_accepted is not True:raise ValueError('explicit --operator-accepted required')
        spec=self._spec()
        now=self.clock()
        remainder=now%60000
        if remainder:
            if isinstance(self.client,FixtureClient):
                raise ValueError('synthetic activation time must be an exact minute boundary')
            target=now+(60000-remainder)
            time.sleep((target-now)/1000)
            now=self.clock()
            if now<target or now-target>5000:
                raise ValueError('activation boundary timing invalid')
            now=target
        warm=self.client.get('/fapi/v1/klines',dict(symbol='ETHUSDT',interval='1m',limit=100))
        bars=self._bars(warm,now)
        if len(bars)<61:raise ValueError('61 contiguous historical warmup bars unavailable')
        bars=bars[-61:]
        for a,b in zip(bars,bars[1:]):
            if b['open_time_ms']!=a['open_time_ms']+60000:raise ValueError('historical warmup gap')
        lab=DiscoveryLab(self.root,self.config_path,spec)
        try:
            if lab.state.get('activated'):
                raise ValueError('research window already activated; use run/report')
            lab.seed_warmup(bars,cutoff_ms=now)
            lab.activate(at_ms=now,operator_accepted=True)
            self.state.update(prepared=True,instrument=spec,activated_at_ms=now,
                              last_bar_cursor=bars[-1]['close_time_ms'],stop_requested=False,last_error=None)
            self._save()
            return dict(action='activate',activated=True,window_started=True,start_ms=now,
                        checkpoint_ms=lab.state['checkpoint_ms'],deadline_ms=lab.state['deadline_ms'],
                        warmup_bars=61,warmup_last_close_ms=bars[-1]['close_time_ms'],
                        historical_warmup_only=True)
        finally:lab.close()

    def request_stop(self):
        fd=os.open(self.runner_state_lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            state=self._load_runner_state()
            state['stop_requested']=True
            state.setdefault('stop_requested_ms',self.clock())
            if state.get('stop_requested_ms') is None:state['stop_requested_ms']=self.clock()
            atomic_json(self.runner_path,state);self.state=state
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)
        return dict(action='stop_requested',kills_process=False,new_entries_will_stop=True,
                    exits_continue_until_flat=True)

    def _open(self):
        if not self.state.get('instrument'):raise ValueError('run prepare/activate first')
        lab=DiscoveryLab(self.root,self.config_path,self.state['instrument'])
        if not lab.state.get('activated'):raise ValueError('research window not activated')
        feed=SharedFeed(self.root/'shared-feed.sqlite3',forward_start_ms=lab.state['start_ms'],
                        max_source_age_ms=self.config['max_source_age_ms'])
        lab.bind_feed(feed)
        return lab,feed

    def _funding(self,lab,feed,mark_receipt):
        now=receipt_ms(mark_receipt);payload=mark_receipt['payload']
        next_funding=payload.get('nextFundingTime')
        if type(next_funding) is not int or next_funding<=now:
            raise ValueError('future nextFundingTime required')
        start=max(0,self.state.get('funding_cursor') or lab.state['start_ms'])
        adjustments=self.client.get('/fapi/v1/fundingInfo')
        self._observe_stop(lab)
        matched=[row for row in adjustments.get('payload',[]) if row.get('symbol')=='ETHUSDT']
        if len(matched)>1 or (matched and matched[0].get('fundingIntervalHours')!=8):
            raise ValueError('unsupported funding interval requires operator review')
        receipt=self.client.get('/fapi/v1/fundingRate',dict(symbol='ETHUSDT',startTime=start,limit=1000))
        self._observe_stop(lab)
        rows=receipt.get('payload')
        if not isinstance(rows,list) or len(rows)>=1000:raise ValueError('missing/truncated funding coverage')
        observed=receipt_ms(receipt)
        regular=[row for row in rows if row.get('rateType','Regular')=='Regular']
        slots=[row.get('fundingTime') for row in regular]
        if any(type(x) is not int for x in slots) or any(b-a!=28800000 for a,b in zip(slots,slots[1:])):
            raise ValueError('incomplete regular finalized funding sequence')
        expected_previous=next_funding-28800000
        if start<=expected_previous<=observed and (not slots or slots[-1]<expected_previous):
            raise ValueError('missing finalized funding settlement')
        previous=None
        for row in rows:
            ts=row.get('fundingTime')
            if row.get('symbol')!='ETHUSDT' or type(ts) is not int or (previous is not None and ts<=previous):
                raise ValueError('invalid finalized funding sequence')
            previous=ts
            if ts<lab.state['start_ms'] or ts<start:continue
            feed.ingest('funding',dict(source_ts=observed,settlement_ts=ts,
                        rate=decimal_string(row['fundingRate']),mark=decimal_string(row['markPrice'],True),
                        rate_type=row.get('rateType','Regular'),finalized=True),received_ms=observed)
            self.state['funding_cursor']=ts+1
        status_ts=max(now,observed)
        feed.ingest('funding_status',dict(source_ts=status_ts,complete=True,valid_until_ts=next_funding),received_ms=status_ts)

    def _aggtrades(self,lab,feed):
        last=feed.state.get('last_agg_trade_id')
        params=dict(symbol='ETHUSDT',limit=1000)
        if last is None:params['startTime']=lab.state['start_ms']
        else:params['fromId']=last+1
        receipt=self.client.get('/fapi/v1/aggTrades',params)
        self._observe_stop(lab)
        rows=receipt.get('payload')
        if not isinstance(rows,list) or len(rows)>1000:raise ValueError('invalid aggTrade batch')
        if not rows:return
        observed=receipt_ms(receipt)
        first=binance_aggtrade_input(rows[0])
        max_source=max(binance_aggtrade_input(row)['source_ts'] for row in rows)
        dispatch=self._causal_dispatch_ms(max_source,observed,'aggTrade')
        expected=first['trade_id'] if last is None else last+1
        if feed.state.get('aggtrade_valid') is not True and first['trade_id']==expected:
            feed.mark_reconnected(next_trade_id=expected,observed_ms=observed)
        for raw in rows:
            mapped=binance_aggtrade_input(raw)
            if mapped['source_ts']<lab.state['start_ms']:continue
            feed.ingest('aggTrade',dict(mapped,receipt_ts=observed),received_ms=dispatch)

    def _closed_bars(self,lab,feed):
        start=max(lab.state['start_ms'],self.state.get('last_bar_cursor') or lab.state['start_ms'])
        receipt=self.client.get('/fapi/v1/klines',dict(symbol='ETHUSDT',interval='1m',startTime=start,limit=1000))
        self._observe_stop(lab)
        observed=receipt_ms(receipt)
        bars=self._bars(receipt,observed)
        for bar in bars:
            if bar['close_time_ms']<=start:continue
            feed.ingest('closed_bar',bar,received_ms=observed)
            self.state['last_bar_cursor']=bar['close_time_ms']

    @staticmethod
    def _retryable_failure(stage,exc):
        if isinstance(exc,(URLError,TimeoutError,ConnectionError,OSError)):
            return True
        return stage in ('shared','aggTrade') and isinstance(exc,ValueError)

    def poll_once(self):
        self.state=self._load_runner_state()
        lab,feed=self._open()
        stage='reference'
        try:
            if self.state.get('stop_requested'):lab.request_operator_stop(self.clock())
            # Exact filter verification is performed before every process run/poll batch.
            self._spec()
            self._observe_stop(lab)
            stage='shared';feed.begin_poll()
            depth=self.client.get('/fapi/v1/depth',dict(symbol='ETHUSDT',limit=20))
            self._observe_stop(lab)
            b=book_event(depth,'ETHUSDT',self.config['max_source_age_ms'])
            book_dispatch=self._causal_dispatch_ms(b['ts_ms'],b['observed_ms'],'book')
            self._observe_stop(lab)
            feed.ingest('book',dict(source_ts=b['ts_ms'],receipt_ts=b['observed_ms'],
                                    bids=b['bids'],asks=b['asks']),received_ms=book_dispatch)
            mark=self.client.get('/fapi/v1/premiumIndex',dict(symbol='ETHUSDT'))
            self._observe_stop(lab)
            m=mark_event(mark,'ETHUSDT',self.config['max_source_age_ms'])
            mark_dispatch=self._causal_dispatch_ms(m['ts_ms'],m['observed_ms'],'mark')
            self._observe_stop(lab)
            feed.ingest('mark',dict(source_ts=m['ts_ms'],receipt_ts=m['observed_ms'],
                                    price=m['mark_price']),received_ms=mark_dispatch)
            self._funding(lab,feed,mark)
            stage='aggTrade'
            self._aggtrades(lab,feed)
            if feed.state.get('aggtrade_valid') is not True:
                raise ValueError('aggTrade forward coverage unavailable')
            lab.clear_maker_source_gap(self.clock())
            stage='shared'
            self._closed_bars(lab,feed)
            now=self.clock()
            lab.clear_source_gap(now)
            report=lab.report(now)
            self._observe_stop(lab)
            report['runner']=dict(polls=self.state['polls']+1,last_poll_ms=now,
                                  last_error=None,poll_failures=self.state.get('poll_failures',0),
                                  last_failure_ms=self.state.get('last_failure_ms'),source_failure=False,
                                  public_source='Binance USD-M public REST or injected public-shaped fixture',
                                  one_shared_poll_per_cycle=True,stop_requested=bool(self.state.get('stop_requested')),
                                  historical_warmup_only=True,feed=feed.snapshot())
            self.state.update(polls=self.state['polls']+1,last_poll_ms=now,last_error=None,
                              consecutive_failures=0,last_failure_retryable=False,
                              lifecycle='running',terminal_blocked=False)
            self._save();self._persist_report(report,lab,now)
            return report
        except Exception as exc:
            now=self.clock()
            gap_reason=type(exc).__name__+': '+str(exc)
            try:
                if stage=='aggTrade':
                    lab.request_maker_source_gap(now,gap_reason)
                else:
                    lab.request_source_gap(now,gap_reason)
            except Exception as gap_exc:
                gap_reason += '; gap_record_error='+type(gap_exc).__name__+': '+str(gap_exc)
            transport_failure=isinstance(exc,(URLError,TimeoutError,ConnectionError,OSError))
            retryable=self._retryable_failure(stage,exc)
            try:self._observe_stop(lab)
            except Exception:pass
            self.state['last_error']=gap_reason
            self.state['last_failure_retryable']=retryable
            self.state['poll_failures']=self.state.get('poll_failures',0)+1
            self.state['consecutive_failures']=self.state.get('consecutive_failures',0)+1
            self.state['last_failure_ms']=now
            self.state['failure_kind']='transport' if transport_failure else 'validation'
            self.state['lifecycle']='retryable_failure' if retryable else 'terminal_failure'
            self.state['terminal_blocked']=not retryable
            self._save()
            try:
                failed=lab.report(now)
                failed['runner']=dict(polls=self.state['polls'],last_poll_ms=self.state.get('last_poll_ms'),
                                      last_error=gap_reason,poll_failures=self.state['poll_failures'],
                                      last_failure_ms=now,source_failure=True,retryable_source_failure=retryable,
                                      stop_requested=bool(self.state.get('stop_requested')),feed=feed.snapshot())
                self._persist_report(failed,lab,now)
            except Exception:
                pass
            raise
        finally:lab.close()

    def _persist_report(self,report,lab,now):
        reports=self.root/'reports';reports.mkdir(exist_ok=True)
        atomic_json(reports/'latest.json',report)
        checkpoint=reports/'checkpoint-8h.json'
        if now>=lab.state['checkpoint_ms'] and not checkpoint.exists():atomic_json(checkpoint,report)
        if now>=lab.state['deadline_ms']:atomic_json(reports/'final-48h.json',report)

    def report(self):
        self.state=self._load_runner_state()
        lab,feed=self._open()
        try:
            self._observe_stop(lab)
            now=self.clock();report=lab.report(now)
            report['runner']=dict(polls=self.state['polls'],last_poll_ms=self.state.get('last_poll_ms'),
                                  last_error=self.state.get('last_error'),stop_requested=bool(self.state.get('stop_requested')),
                                  feed=feed.snapshot())
            self._persist_report(report,lab,now);return report
        finally:lab.close()

    def _flat_stop_report(self):
        self.state=self._load_runner_state()
        if not self.state.get('stop_requested'):return None
        lab,feed=self._open()
        try:
            lab.request_operator_stop(self.clock())
            report=lab.report(self.clock())
            flat=all(x['positions']==0 for x in report['arms'].values())
            report['runner']=dict(polls=self.state.get('polls',0),last_poll_ms=self.state.get('last_poll_ms'),
                                  last_error=self.state.get('last_error'),poll_failures=self.state.get('poll_failures',0),
                                  stop_requested=True,feed=feed.snapshot())
            if flat:self._persist_report(report,lab,self.clock())
            return report if flat else None
        finally:lab.close()

    def _storage_capacity_halt_report(self):
        lab,feed=self._open()
        try:
            now=self.clock();report=lab.report(now)
            limit=int(report['storage_budget_bytes'])
            threshold=int(Decimal(str(self.config['storage']['entry_stop_fraction']))*Decimal(limit))
            flat=all(x['positions']==0 for x in report['arms'].values())
            if not flat or int(report['storage_used_bytes'])<threshold:
                return None
            self.state=self._load_runner_state()
            self.state['lifecycle']='terminal_storage_capacity'
            self.state['terminal_blocked']=True
            self.state['storage_halt_used_bytes']=int(report['storage_used_bytes'])
            self.state['storage_halt_budget_bytes']=limit
            self._save()
            report['runner']=dict(
                polls=self.state.get('polls',0),last_poll_ms=self.state.get('last_poll_ms'),
                last_error=self.state.get('last_error'),poll_failures=self.state.get('poll_failures',0),
                stop_requested=bool(self.state.get('stop_requested')),feed=feed.snapshot(),
                lifecycle='terminal_storage_capacity',terminal_blocked=True,
                storage_entry_stop_bytes=threshold,
                note='flat root halted before another public poll; history and fixed budget preserved')
            self._persist_report(report,lab,now)
            return report
        finally:lab.close()

    def _mark_terminal_failure(self,lifecycle):
        self.state=self._load_runner_state()
        self.state['lifecycle']=lifecycle;self.state['terminal_blocked']=True
        self._save()
        latest=self.root/'reports'/'latest.json'
        if latest.exists():
            report=json.loads(latest.read_text())
            report.setdefault('runner',{}).update(
                lifecycle=lifecycle,terminal_blocked=True,
                retryable_source_failure=bool(self.state.get('last_failure_retryable')))
            atomic_json(latest,report)

    def run(self,*,once=False,poll_seconds=1.0,max_cycles=None):
        attempts=0
        stopped=self._flat_stop_report()
        if stopped is not None:return stopped
        capacity_halt=self._storage_capacity_halt_report()
        if capacity_halt is not None:return capacity_halt
        while True:
            try:
                report=self.poll_once();attempts+=1
            except Exception:
                attempts+=1
                stopped=self._flat_stop_report()
                if stopped is not None:return stopped
                self.state=self._load_runner_state()
                bounded=max_cycles is not None and attempts>=max_cycles
                if once or self.state.get('last_failure_retryable') is not True or bounded:
                    lifecycle=('terminal_one_shot_failure' if once else
                               'bounded_cycle_limit' if bounded else 'terminal_non_retryable_failure')
                    self._mark_terminal_failure(lifecycle)
                    raise
                # Continuous mode remains fail-closed while retrying. Exponential
                # backoff is capped so an outage cannot create one durable gap per
                # second indefinitely; the original deadline/history are unchanged.
                delay=min(60.0,poll_seconds*(2**min(6,max(0,self.state.get('consecutive_failures',1)-1))))
                self.state['lifecycle']='retry_wait';self.state['terminal_blocked']=False
                self.state['next_retry_delay_seconds']=delay;self._save()
                time.sleep(delay);continue
            flat=all(x['positions']==0 for x in report['arms'].values())
            capacity_halt=self._storage_capacity_halt_report()
            if capacity_halt is not None:return capacity_halt
            self.state=self._load_runner_state()
            if once or (self.state.get('stop_requested') and flat):
                return report
            if max_cycles is not None and attempts>=max_cycles:return report
            time.sleep(poll_seconds)

def build_runner(args):
    if args.engineering_fixture:
        if not args.transport_fixture or args.now_ms is None:
            raise ValueError('fixture mode requires --transport-fixture and --now-ms')
        return DiscoveryRunner(args.root,client=FixtureClient(args.transport_fixture),clock_ms=lambda:args.now_ms,config_path=args.config)
    if args.transport_fixture or args.now_ms is not None:
        raise ValueError('fixture-only options require --engineering-fixture')
    return DiscoveryRunner(args.root,config_path=args.config)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root')
    p.add_argument('--config',type=Path,default=CONFIG,help='immutable discovery config for this root; default remains LAB001')
    p.add_argument('--engineering-fixture',action='store_true')
    p.add_argument('--transport-fixture')
    p.add_argument('--now-ms',type=int)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('preflight')
    sub.add_parser('prepare')
    a=sub.add_parser('activate');a.add_argument('--operator-accepted',action='store_true')
    run=sub.add_parser('run');run.add_argument('--once',action='store_true');run.add_argument('--poll-seconds',type=float,default=1.0);run.add_argument('--max-cycles',type=int)
    sub.add_parser('report');sub.add_parser('stop')
    args=p.parse_args(argv)
    if args.command=='preflight':
        if args.root is not None:
            print(json.dumps(dict(action='preflight',pass=False,error_type='ValueError',
                error='preflight is rootless; omit --root',root_touched=False,window_started=False,activated=False),
                sort_keys=True,separators=(',',':')))
            return 2
        try:
            result=public_source_preflight(config_path=args.config)
            print(json.dumps(result,sort_keys=True,separators=(',',':'),default=str));return 0
        except Exception as exc:
            print(json.dumps(dict(action='preflight',pass=False,error_type=type(exc).__name__,error=str(exc),
                root_touched=False,window_started=False,activated=False),sort_keys=True,separators=(',',':')))
            return 2
    if not args.root:
        p.error('--root is required except for preflight')
    runner=build_runner(args)
    lock_fd=None
    if args.command=='run':
        lock_fd=os.open(runner.root/'runner.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd);raise RuntimeError('discovery runner already active') from None
    try:
        if args.command=='prepare':result=runner.prepare()
        elif args.command=='activate':result=runner.activate(operator_accepted=args.operator_accepted)
        elif args.command=='run':result=runner.run(once=args.once,poll_seconds=args.poll_seconds,max_cycles=args.max_cycles)
        elif args.command=='report':result=runner.report()
        else:result=runner.request_stop()
        print(json.dumps(result,sort_keys=True,separators=(',',':'),default=str))
        return 0
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd,fcntl.LOCK_UN);os.close(lock_fd)

if __name__=='__main__':
    raise SystemExit(main())
