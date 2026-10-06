#!/usr/bin/env python3
"""Staged operator CLI for Issue #23 three-arm ETH discovery.

Public market-data GETs only. No private API, exchange order endpoint, scheduler,
service installation, default PAPER mutation, or automatic research activation.
"""
from __future__ import annotations
import argparse, fcntl, json, os, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

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
        receipt={k:v for k,v in selected.items() if k!='fixture_match'}
        receipt['endpoint']=endpoint;receipt['params']=dict(params)
        self.calls.append(dict(endpoint=endpoint,params=dict(params)))
        return receipt

class DiscoveryRunner:
    def __init__(self,root,*,client=None,clock_ms=None,config_path=CONFIG):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.client=client or BinanceDiscoveryClient()
        self.clock=clock_ms or (lambda:int(time.time()*1000))
        self.config_path=Path(config_path)
        self.config=json.loads(self.config_path.read_text())
        self.runner_path=self.root/'runner_state.json'
        self.state=self._load_runner_state()

    def _load_runner_state(self):
        if self.runner_path.exists():
            state=json.loads(self.runner_path.read_text())
            if state.get('schema_version')!=RUNNER_SCHEMA:
                raise ValueError('runner state schema mismatch')
            return state
        state=dict(schema_version=RUNNER_SCHEMA,prepared=False,stop_requested=False,
                   instrument=None,last_bar_cursor=None,funding_cursor=None,
                   polls=0,last_poll_ms=None,last_error=None,poll_failures=0,last_failure_ms=None)
        atomic_json(self.runner_path,state);return state

    def _save(self):
        atomic_json(self.runner_path,self.state)

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
        self.state['stop_requested']=True;self.state['stop_requested_ms']=self.clock();self._save()
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
        matched=[row for row in adjustments.get('payload',[]) if row.get('symbol')=='ETHUSDT']
        if len(matched)>1 or (matched and matched[0].get('fundingIntervalHours')!=8):
            raise ValueError('unsupported funding interval requires operator review')
        receipt=self.client.get('/fapi/v1/fundingRate',dict(symbol='ETHUSDT',startTime=start,limit=1000))
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
        rows=receipt.get('payload')
        if not isinstance(rows,list) or len(rows)>1000:raise ValueError('invalid aggTrade batch')
        if not rows:return
        observed=receipt_ms(receipt)
        first=binance_aggtrade_input(rows[0])
        expected=first['trade_id'] if last is None else last+1
        if feed.state.get('aggtrade_valid') is not True and first['trade_id']==expected:
            feed.mark_reconnected(next_trade_id=expected,observed_ms=observed)
        for raw in rows:
            mapped=binance_aggtrade_input(raw)
            if mapped['source_ts']<lab.state['start_ms']:continue
            feed.ingest('aggTrade',mapped,received_ms=observed)

    def _closed_bars(self,lab,feed):
        start=max(lab.state['start_ms'],self.state.get('last_bar_cursor') or lab.state['start_ms'])
        receipt=self.client.get('/fapi/v1/klines',dict(symbol='ETHUSDT',interval='1m',startTime=start,limit=1000))
        observed=receipt_ms(receipt)
        bars=self._bars(receipt,observed)
        for bar in bars:
            if bar['close_time_ms']<=start:continue
            feed.ingest('closed_bar',bar,received_ms=observed)
            self.state['last_bar_cursor']=bar['close_time_ms']

    def poll_once(self):
        self.state=self._load_runner_state()
        lab,feed=self._open()
        stage='shared'
        try:
            if self.state.get('stop_requested'):lab.request_operator_stop(self.clock())
            # Exact filter verification is performed before every process run/poll batch.
            self._spec()
            feed.begin_poll()
            depth=self.client.get('/fapi/v1/depth',dict(symbol='ETHUSDT',limit=20))
            b=book_event(depth,'ETHUSDT',self.config['max_source_age_ms'])
            feed.ingest('book',dict(source_ts=b['ts_ms'],bids=b['bids'],asks=b['asks']),received_ms=b['observed_ms'])
            mark=self.client.get('/fapi/v1/premiumIndex',dict(symbol='ETHUSDT'))
            m=mark_event(mark,'ETHUSDT',self.config['max_source_age_ms'])
            feed.ingest('mark',dict(source_ts=m['ts_ms'],price=m['mark_price']),received_ms=m['observed_ms'])
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
            report['runner']=dict(polls=self.state['polls']+1,last_poll_ms=now,
                                  last_error=None,poll_failures=self.state.get('poll_failures',0),
                                  last_failure_ms=self.state.get('last_failure_ms'),source_failure=False,
                                  public_source='Binance USD-M public REST or injected public-shaped fixture',
                                  one_shared_poll_per_cycle=True,stop_requested=bool(self.state.get('stop_requested')),
                                  historical_warmup_only=True,feed=feed.snapshot())
            self.state.update(polls=self.state['polls']+1,last_poll_ms=now,last_error=None)
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
            self.state['last_error']=gap_reason
            self.state['poll_failures']=self.state.get('poll_failures',0)+1
            self.state['last_failure_ms']=now
            self._save()
            # Persist the fail-closed state before propagating the failed poll. This is
            # evidence of a source failure, never a successful poll or synthetic fill.
            try:
                failed=lab.report(now)
                failed['runner']=dict(polls=self.state['polls'],last_poll_ms=self.state.get('last_poll_ms'),
                                      last_error=gap_reason,poll_failures=self.state['poll_failures'],
                                      last_failure_ms=now,source_failure=True,
                                      public_source='Binance USD-M public REST or injected public-shaped fixture',
                                      one_shared_poll_per_cycle=True,stop_requested=bool(self.state.get('stop_requested')),
                                      historical_warmup_only=True,feed=feed.snapshot())
                self._persist_report(failed,lab,now)
            except Exception:
                # runner_state + lab source-gap state remain authoritative if report
                # rendering itself is unavailable; never mask the original source error.
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
        lab,feed=self._open()
        try:
            now=self.clock();report=lab.report(now)
            report['runner']=dict(polls=self.state['polls'],last_poll_ms=self.state.get('last_poll_ms'),
                                  last_error=self.state.get('last_error'),stop_requested=bool(self.state.get('stop_requested')),
                                  feed=feed.snapshot())
            self._persist_report(report,lab,now);return report
        finally:lab.close()

    def run(self,*,once=False,poll_seconds=1.0,max_cycles=None):
        attempts=0
        while True:
            try:
                report=self.poll_once();attempts+=1
            except Exception:
                attempts+=1
                # A one-shot probe remains strict: its caller gets the source failure.
                # The documented continuous runner stays fail-closed but alive so a
                # later causal public receipt can recover without replacing/resetting
                # the activated root or extending its checkpoint/deadline.
                if once or (max_cycles is not None and attempts>=max_cycles):
                    raise
                time.sleep(poll_seconds)
                continue
            flat=all(x['positions']==0 for x in report['arms'].values())
            if once or (self.state.get('stop_requested') and flat):
                return report
            if max_cycles is not None and attempts>=max_cycles:return report
            time.sleep(poll_seconds)

def build_runner(args):
    if args.engineering_fixture:
        if not args.transport_fixture or args.now_ms is None:
            raise ValueError('fixture mode requires --transport-fixture and --now-ms')
        return DiscoveryRunner(args.root,client=FixtureClient(args.transport_fixture),clock_ms=lambda:args.now_ms)
    if args.transport_fixture or args.now_ms is not None:
        raise ValueError('fixture-only options require --engineering-fixture')
    return DiscoveryRunner(args.root)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',required=True)
    p.add_argument('--engineering-fixture',action='store_true')
    p.add_argument('--transport-fixture')
    p.add_argument('--now-ms',type=int)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('prepare')
    a=sub.add_parser('activate');a.add_argument('--operator-accepted',action='store_true')
    run=sub.add_parser('run');run.add_argument('--once',action='store_true');run.add_argument('--poll-seconds',type=float,default=1.0);run.add_argument('--max-cycles',type=int)
    sub.add_parser('report');sub.add_parser('stop')
    args=p.parse_args(argv)
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
