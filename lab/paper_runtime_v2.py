"""Durable public REST PAPER runtime. No private endpoints or exchange orders."""
import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from contextlib import closing

from paper_market import PublicMarketClient, instrument, book_event, mark_event, finalized_funding, fresh_source, receipt_ms, decimal_string
from paper_sizing import size_long
from signals_v2 import Detector
from sim_broker import SimBroker, InstrumentSettings, ExecutionModel, RiskContract, Intent
from dashboard import validate_snapshot

D = Decimal

def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

class RuntimeClient(PublicMarketClient):
    ALLOWED = {**PublicMarketClient.ALLOWED, '/fapi/v1/fundingInfo': set()}

class PaperRuntime:
    def __init__(self, root, config_path, *, client=None, clock_ms=None, fixture=False):
        self.root = Path(root).resolve()
        live=Path(__file__).resolve().parent/'data'/'paper'
        if self.root==live or live in self.root.parents:
            raise ValueError('reserved live namespace')
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_bytes = Path(config_path).read_bytes()
        self.config = json.loads(self.config_bytes)
        self.config_hash = hashlib.sha256(self.config_bytes).hexdigest()
        if self.config_hash != 'dd1aa331f86df0892190dc4e06bb96a8b2086a605352e96702063dbc91879c96':
            raise ValueError('only the immutable H1-PAPER-002 configuration is implemented')
        lab=Path(__file__).resolve().parent
        self.source_hashes={name:hashlib.sha256((lab/name).read_bytes()).hexdigest() for name in ('paper_runtime_v2.py','signals_v2.py','paper_runtime.py','signals.py','paper_market.py','paper_sizing.py','sim_broker.py','perp_collector.py')}
        c = self.config
        if c.get('approved') is not True or c.get('mode') != 'paper' or c['strategy']['symbol'] != 'ETHUSDT':
            raise ValueError('explicit approved ETH PAPER configuration required')
        self.clock = clock_ms or (lambda:int(time.time()*1000))
        self.client = client or RuntimeClient()
        self.fixture = fixture
        self.broker = None
        self.db = None
        self.fd = os.open(self.root/'runtime.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError('PAPER runtime already locked') from None
        try:
            self.db = sqlite3.connect(self.root/'runtime.sqlite3')
            self.db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL)')
            row=self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
            self.state=json.loads(row[0]) if row else dict(config_hash=self.config_hash, fixture=fixture, forward_start_ms=self.clock(), cursor=None, errors=0, gaps=0, last_success_ms=None, markets=[], latest_error=None, warmup=0, history=[], diagnostic='warmup')
            if self.state['config_hash'] != self.config_hash or self.state['fixture'] != fixture:
                raise ValueError('immutable config/fixture namespace conflict')
            self.save()
            self.detector=Detector(self.root/'signals.sqlite3', c['version_id'], datetime.fromtimestamp(self.state['forward_start_ms']/1000, timezone.utc), {'ETHUSDT':'crypto'})
        except Exception:
            self.close()
            raise
    def save(self):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)', (canonical(self.state),))
    def audit(self, event):
        payload=canonical(event)
        row=self.db.execute('SELECT hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
        previous=row[0] if row else '0'*64
        with self.db:
            self.db.execute('INSERT INTO audit(payload,previous_hash,hash) VALUES(?,?,?)', (payload, previous, hashlib.sha256((previous+payload).encode()).hexdigest()))
    def fetch(self, endpoint, params=None):
        receipt=self.client.get(endpoint, params)
        self.audit(dict(type='public_receipt', receipt=receipt))
        age=self.clock()-receipt_ms(receipt)
        if not 0<=age<=15000:
            raise ValueError('stale/future receipt')
        return receipt
    def reference(self):
        cached=self.state.get('reference')
        if not cached or not 0<=self.clock()-receipt_ms(cached)<86400000:
            cached=self.fetch('/fapi/v1/exchangeInfo')
            self.state['reference']=cached
            self.save()
        return cached
    def ensure_broker(self, spec):
        c=self.config
        s=InstrumentSettings(spec['symbol'], D(spec['maker_fee']), D(spec['taker_fee']), D(spec['qty_step']), D(spec['tick_size']), D(spec['min_notional']), D(spec['max_qty']), D(spec['min_qty']), spec['category'])
        if self.broker:
            if self.broker.instruments['ETHUSDT'] != s:
                raise ValueError('public instrument filters changed; new review required')
            return
        e=c['execution_model']
        self.broker=SimBroker(self.root/'broker.sqlite3', initial_cash=D(c['initial_equity_usdt']), instruments=[s], execution=ExecutionModel(e['latency_ms'],e['exit_slippage_ticks'],None,e['entry_slippage_ticks'],e['exit_slippage_ticks']), risk=RiskContract(True,c['risk_version'],D(c['max_loss_per_trade_usdt']),D(c['max_daily_loss_usdt']),D(c['max_effective_exposure_x']),c['max_positions'],D(c['total_loss_limit_usdt']),True), version_id=c['version_id'], forward_start=self.state['forward_start_ms'])
        if self.state.get('gap_open'):
            self.cancel_entries_for_gap()
        with closing(sqlite3.connect(self.root/'signals.sqlite3')) as db:
            intents=[json.loads(row[0]) for row in db.execute('SELECT intent FROM h1_signals')]
        handled=self.state.setdefault('handled_signals',{})
        for signal in intents:
            sid=signal['signal_id']
            if sid not in handled and 'order:'+sid in self.broker.orders:
                handled[sid]=dict(status='submitted',target=signal['reversion_target'])
                self.audit(dict(type='recovered_committed_submission',signal_id=sid))
        self.save()
    def cancel_entries_for_gap(self):
        if not self.broker:
            return
        for order in list(self.broker.orders.values()):
            if not order['intent']['reduce_only'] and order['status'] in ('PENDING','RESTING'):
                self.broker.cancel(order['order_id'],ts=max(self.clock(),self.broker.last_ts))
    def emit(self, event):
        if event['type'] in ('book','mark'):
            delivery=self.clock()
            source=event['source_ts']
            if type(source) is not int or not 0<=delivery-source<=self.config['execution_model']['max_source_age_ms']:
                raise ValueError('stale/future source at actual broker delivery')
            event['received_ms']=event['ts']
            event['ts']=delivery
        event['event_id']=digest(event)
        self.broker.on_event(event)
    def funding(self, spec):
        self.ensure_broker(spec)
        now=self.clock()
        last=self.state.get('funding_checked_ms')
        if last is not None and 0<=now-last<30000:
            return
        adjustments=self.fetch('/fapi/v1/fundingInfo')
        matched=[row for row in adjustments['payload'] if row.get('symbol')=='ETHUSDT']
        if len(matched)>1 or (matched and matched[0].get('fundingIntervalHours')!=8):
            raise ValueError('unsupported funding interval requires explicit review')
        receipt=self.fetch('/fapi/v1/fundingRate',dict(symbol='ETHUSDT',limit=1000,startTime=max(0,self.state.get('funding_cursor',self.state['forward_start_ms']-28800000))))
        rows=receipt['payload']
        events=finalized_funding(receipt,'ETHUSDT',self.state['forward_start_ms'])
        observed=receipt_ms(receipt)
        if not rows or len(rows)>=1000:
            raise ValueError('missing/truncated finalized funding coverage')
        expected=observed//28800000*28800000
        slots=[row['fundingTime']//28800000*28800000 for row in rows if row.get('rateType','Regular')=='Regular']
        anchor=self.state.get('funding_cursor',self.state['forward_start_ms'])//28800000*28800000
        if (not slots or slots[-1]!=expected or slots[0]>anchor+28800000 or any(b-a!=28800000 for a,b in zip(slots,slots[1:]))
                or any(not 0<=row['fundingTime']%28800000<=5000 for row in rows if row.get('rateType','Regular')=='Regular')):
            raise ValueError('incomplete regular finalized funding coverage')
        for event in events:
            row=next(row for row in rows if row['fundingTime']==event['ts_ms'])
            self.emit(dict(type='funding',symbol='ETHUSDT',ts=observed,settlement_ts=event['ts_ms'],rate=event['rate'],mark=event['mark_price'],finalized=True,rate_type=row.get('rateType','Regular')))
        self.emit(dict(type='funding_status',symbol='ETHUSDT',ts=observed,complete=True,valid_until_ts=expected+28800000))
        self.state['funding_cursor']=rows[-1]['fundingTime']
        self.state['funding_checked_ms']=observed
        self.save()
    def risk_blockers(self):
        b=self.broker
        if not b or b.equity is None:
            return []
        now=self.clock()
        result=[]
        if D(b.initial_cash)-b.equity>=D(self.config['total_loss_limit_usdt']):
            self.state['total_halted']=True
        if self.state.get('total_halted'):
            result.append('risk_total_loss')
        day=str(now//86400000)
        baseline=b.day_baselines.get(day)
        if baseline is not None and D(baseline)-b.equity>=D(self.config['max_daily_loss_usdt']):
            self.state['daily_halted_day']=day
        if self.state.get('daily_halted_day')==day:
            result.append('risk_daily_loss')
        return result
    def route_signals(self, spec):
        with closing(sqlite3.connect(self.root/'signals.sqlite3')) as db:
            intents=[json.loads(row[0]) for row in db.execute('SELECT intent FROM h1_signals ORDER BY rowid')]
        handled=self.state.setdefault('handled_signals',{})
        market=next(m for m in self.state['markets'] if m['symbol']=='ETHUSDT')
        for signal in intents:
            sid=signal['signal_id']
            if sid in handled:
                continue
            if 'order:'+sid in self.broker.orders:
                handled[sid]=dict(status='submitted',target=signal['reversion_target'])
                self.save()
                continue
            now=self.clock()
            if self.risk_blockers():
                result=dict(status='rejected',reason=','.join(self.risk_blockers()))
            elif now-int(signal['features']['bar_close_ms'])>self.config['execution_model']['max_source_age_ms']:
                result=dict(status='rejected',reason='late_closed_bar_signal')
            elif self.broker.positions or any(o['status'] in ('PENDING','RESTING') for o in self.broker.orders.values()):
                result=dict(status='rejected',reason='single_position_or_pending')
            else:
                result=size_long(self.config,spec,equity=str(self.broker.equity),bid=market['bid'],ask=market['ask'],target=signal['reversion_target'])
                if result['status']=='accepted':
                    s=self.broker.instruments['ETHUSDT']
                    order=self.broker.submit(Intent(sid,'ETHUSDT','BUY',D(result['qty']),D(result['stop_price']),now,s.quantity_step,s.tick,s.min_notional,s.max_quantity,'TAKER',False,expires_ts=now+self.config['execution_model']['max_source_age_ms']))
                    result.update(status='submitted' if order['status']=='PENDING' else 'rejected',reason=order['reason'],target=signal['reversion_target'])
            handled[sid]=result
            self.audit(dict(type='signal_routing',signal_id=sid,result=result))
            self.save()
    def exit_policy(self, now, mark):
        p=self.broker.positions.get('ETHUSDT')
        if not p:
            return
        if any(o['intent']['reduce_only'] and o['status'] in ('PENDING','RESTING') for o in self.broker.orders.values()):
            return
        entry=next(f for f in reversed(self.broker.fills) if f['symbol']=='ETHUSDT' and f['side']=='BUY')
        signal=self.state.get('handled_signals',{}).get(entry['intent_id'])
        if not signal or 'target' not in signal:
            raise ValueError('position lacks durable target registration')
        reason='data_gap' if self.state.get('gap_open') else 'take_profit' if D(mark)>=D(signal['target']) else 'max_hold' if now-p['opened_ts']>=self.config['strategy']['max_holding_ms'] else None
        if reason:
            s=self.broker.instruments['ETHUSDT']
            order=self.broker.submit(Intent(f"exit:{entry['intent_id']}:{reason}",'ETHUSDT','SELL',D(p['qty']),None,now,s.quantity_step,s.tick,s.min_notional,s.max_quantity,'TAKER',True))
            self.audit(dict(type='exit_trigger',reason=reason,mark=mark,order=order))
    def collect_markets(self, ref):
        markets=[]
        events=[]
        spec_eth=None
        age=self.config['execution_model']['max_source_age_ms']
        for symbol, category in [('ETHUSDT','crypto'),('XAUUSDT','TradFi')]:
            spec=instrument(ref,symbol,category,self.config['fee_assumptions'][category])
            ticker=self.fetch('/fapi/v1/ticker/bookTicker', {'symbol':symbol})
            depth=self.fetch('/fapi/v1/depth', {'symbol':symbol,'limit':20})
            mark=self.fetch('/fapi/v1/premiumIndex', {'symbol':symbol})
            b=book_event(depth,symbol,age)
            m=mark_event(mark,symbol,age)
            ts, observed=fresh_source(ticker,symbol,age)
            if ticker['payload']['symbol'] != symbol:
                raise ValueError('ticker symbol mismatch')
            bid=decimal_string(ticker['payload']['bidPrice'],True)
            ask=decimal_string(ticker['payload']['askPrice'],True)
            if D(bid)>=D(ask):
                raise ValueError('crossed ticker')
            markets.append(dict(symbol=symbol,category=category,bid=bid,ask=ask,mark_price=m['mark_price'],min_notional=spec['min_notional'],last_received_at=mark['received_at'],source_timestamps_ms=dict(bookTicker=ts,depth5=b['ts_ms'],premiumIndex=m['ts_ms'])))
            if symbol=='ETHUSDT':
                deadline=mark['payload'].get('nextFundingTime')
                if type(deadline) is not int or deadline!=self.broker.funding_status['ETHUSDT']['valid_until_ts']:
                    raise ValueError('unverified or changed funding deadline')
                spec_eth=spec
                events=[dict(type='book',symbol=symbol,ts=b['observed_ms'],source_ts=b['ts_ms'],bids=b['bids'],asks=b['asks']),dict(type='mark',symbol=symbol,ts=m['observed_ms'],source_ts=m['ts_ms'],price=m['mark_price'])]
                # ETH execution/risk delivery precedes unrelated XAU observation.
                now=self.clock()
                if any(not 0<=now-ts<=age for ts in markets[-1]['source_timestamps_ms'].values()):
                    raise ValueError('batch source age exceeded')
                self.deliver_markets(markets, events, spec_eth)
        # Validate the entire batch again at decision time, not merely receipt time.
        now=self.clock()
        for market in markets:
            if any(not 0<=now-ts<=age for ts in market['source_timestamps_ms'].values()):
                raise ValueError('batch source age exceeded')
        return markets, spec_eth
    def deliver_markets(self, markets, events, spec_eth):
        self.ensure_broker(spec_eth)
        for event in sorted(events,key=lambda e:e['ts']):
            if event['source_ts']>=self.state['forward_start_ms']:
                self.emit(event)
                if event['type']=='mark':
                    self.exit_policy(event['ts'],event['price'])
        self.state['markets']=markets
    def collect(self):
        ref=self.reference()
        eth=instrument(ref,'ETHUSDT','crypto',self.config['fee_assumptions']['crypto'])
        self.funding(eth)
        markets, spec_eth=self.collect_markets(ref)
        self.state['markets']=markets
        age=self.config['execution_model']['max_source_age_ms']
        self.bootstrap()
        start=self.state['cursor'] if self.state['cursor'] is not None else self.state.get('decision_cutoff_ms',self.state['forward_start_ms'])//300000*300000
        # The durable cursor is the next bar open. Retry missing due bars, but
        # never ask for the same still-open candle on every quote cycle.
        rows=[]
        if start+300000<=self.clock():
            receipt=self.fetch('/fapi/v1/klines',dict(symbol='ETHUSDT',interval='5m',startTime=start,limit=1000))
            rows=receipt['payload']
        for row in rows:
            if type(row[0]) is not int or type(row[6]) is not int or row[6] != row[0]+299999:
                raise ValueError('invalid API closed-bar timestamps')
            if row[6]+1>self.clock():
                continue
            bar=dict(symbol='ETHUSDT',open_time_ms=row[0],close_time_ms=row[6]+1,closed=True,**{k:D(decimal_string(row[i],k!='volume')) for k,i in [('open',1),('high',2),('low',3),('close',4),('volume',5)]})
            result=self.detector.process(bar,now=datetime.fromtimestamp(self.clock()/1000,timezone.utc))
            self.audit(dict(type='detector',result=result,bar=bar))
            if result['diagnostic'].startswith('reject'):
                raise ValueError(result['diagnostic'])
            if result['diagnostic']=='gap_reset':
                self.state['gaps']+=1
                self.state['decision_cutoff_ms']=self.clock()
                self.state['gap_open']=True
                self.state['warmup']=0
                self.cancel_entries_for_gap()
                self.save()
                raise ValueError('bar gap requires context rebuild at new decision cutoff')
            self.state['diagnostic']=result['diagnostic']
            self.state['cursor']=row[0]+300000
            self.save()
        with closing(sqlite3.connect(self.root/'signals.sqlite3')) as db:
            row=db.execute('SELECT bars FROM h1_state WHERE version_id=? AND symbol=?',(self.config['version_id'],'ETHUSDT')).fetchone()
        self.state['warmup']=len(json.loads(row[0])) if row else 0
        if any(not 0<=self.clock()-ts<=age for market in markets for ts in market['source_timestamps_ms'].values()):
            # One bounded refresh of actual public inputs after slow candle work.
            # Never retimestamp old receipts or relax the decision-age gate.
            markets, spec_eth=self.collect_markets(ref)
            self.state['markets']=markets
        if any(not 0<=self.clock()-ts<=age for market in markets for ts in market['source_timestamps_ms'].values()):
            raise ValueError('decision source age exceeded after kline request')
        self.route_signals(spec_eth)
        self.state['last_success_ms']=self.clock()
        self.state['latest_error']=None
    def bootstrap(self):
        cutoff=self.state.get('decision_cutoff_ms',self.state['forward_start_ms'])
        if self.detector.seeded(cutoff):
            return
        end=(cutoff-1)//300000*300000
        receipt=self.fetch('/fapi/v1/klines',dict(symbol='ETHUSDT',interval='5m',startTime=end-61*300000,endTime=end-1,limit=61))
        manifest=self.detector.seed(receipt,cutoff_ms=cutoff,now_ms=self.clock())
        self.state['cursor']=end
        self.state['warmup']=61
        self.state['diagnostic']='context_ready_waiting_next_closed_bar'
        self.audit(dict(type='indicator_context_only',manifest=manifest))
        self.save()
    def handle_gap(self):
        now=self.clock()
        last=self.state['last_success_ms']
        if last is None or now-last<=self.config['execution_model']['gap_exit_after_ms'] or self.state.get('gap_open'):
            return
        self.state['gap_open']=True
        self.state['gaps']+=1
        self.state['warmup']=0
        self.state['decision_cutoff_ms']=now
        self.state['cursor']=now//300000*300000
        with closing(sqlite3.connect(self.root/'signals.sqlite3')) as db, db:
            db.execute('DELETE FROM h1_state WHERE version_id=?',(self.config['version_id'],))
        if self.broker:
            for order in list(self.broker.orders.values()):
                if not order['intent']['reduce_only'] and order['status'] in ('PENDING','RESTING'):
                    self.broker.cancel(order['order_id'],ts=max(now,self.broker.last_ts))
        self.audit(dict(type='heartbeat_gap',last_success_ms=last,resume_cursor=self.state['cursor'],at_ms=now))
        self.save()
    def poll(self):
        try:
            self.handle_gap()
            self.collect()
            self.state['gap_open']=False
        except Exception as exc:
            self.state['errors']+=1
            self.state['latest_error']=f'{type(exc).__name__}: {exc}'
            self.audit(dict(type='poll_error',at_ms=self.clock(),error=self.state['latest_error']))
        self.save()
        snapshot=self.snapshot()
        target=self.root/'paper_status.json'
        temporary=target.with_suffix('.json.tmp')
        with temporary.open('w') as stream:
            stream.write(canonical(snapshot)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,target)
        return snapshot
    def mark_deployed(self):
        if not self.state.get('deployment'):
            event=dict(type='parent_deployment',at_ms=self.clock(),version_id=self.config['version_id'],config_hash=self.config_hash)
            self.audit(event)
            self.state['deployment']=event
            self.save()

    def snapshot(self):
        with localcontext() as ctx:
            ctx.prec=60
            c=self.config
            b=self.broker
            initial=D(c['initial_equity_usdt'])
            cash=b.cash if b else initial
            if b:
                with localcontext() as replay_context:
                    replay_context.prec=40
                    replay_cash=initial
                    index=0
                    while index<len(b.ledger):
                        item=b.ledger[index]
                        if item['type']=='realized' and index+1<len(b.ledger) and b.ledger[index+1]['type']=='fee':
                            replay_cash+=D(b.ledger[index+1]['amount'])
                            replay_cash+=D(item['amount'])
                            index+=2
                        else:
                            replay_cash+=D(item['amount'])
                            index+=1
                if replay_cash!=cash:
                    raise ValueError('ledger cash invariant failed; refuse fictitious PNL snapshot')
            equity=b.equity if b else cash
            if equity is None:
                raise ValueError('unvalued PAPER position')
            net=cash-initial
            unreal=equity-cash
            positions=[]
            if b:
                for symbol,p in b.positions.items():
                    positions.append(dict(symbol=symbol,side='BUY',qty=p['qty'],entry_price=p['entry'],mark_price=b.marks[symbol],stop_price=p['stop'],unrealized_pnl_usdt=str(D(p['qty'])*(D(b.marks[symbol])-D(p['entry'])))))
            error=self.state['latest_error']
            risk_blocks=self.risk_blockers()
            self.save()
            warm=self.state['warmup']<61
            runtime_state='blocked' if error or risk_blocks else 'warming_up' if warm else 'observing'
            now=self.clock()
            point=dict(ts=stamp(now),equity_usdt=str(equity),valuation_stale=bool(error))
            if not self.state['history'] or self.state['history'][-1]['ts']!=point['ts']:
                self.state['history'].append(point)
            self.save()
            s=dict(schema_version=1,mode='paper',fixture=self.fixture,fixture_label='ARTIFICIAL ENGINEERING FIXTURE NOT PERFORMANCE' if self.fixture else None,updated_at=stamp(now),initial_equity_usdt=str(initial),cash_usdt=str(cash),equity_usdt=str(equity),realized_pnl_usdt=str(net),unrealized_pnl_usdt=str(unreal),total_pnl_usdt=str(net+unreal),gross_realized_pnl_usdt=str(sum((D(x['amount']) for x in b.ledger if x['type']=='realized'),D(0)) if b else D(0)),fees_usdt=str(-sum((D(x['amount']) for x in b.ledger if x['type']=='fee'),D(0)) if b else D(0)),funding_pnl_usdt=str(sum((D(x['amount']) for x in b.ledger if x['type']=='funding'),D(0)) if b else D(0)),paper_trading_enabled=not bool(error or risk_blocks) and not warm,live_trading_enabled=False,runtime_state=runtime_state,engine=dict(implementation='paper-engine-v1',version_id=c['version_id'],forward_start_at=stamp(self.state['forward_start_ms']),model_sha256=digest(dict(config_hash=self.config_hash,sources=self.source_hashes)),source_sha256=self.source_hashes,status='error' if error else 'halted' if risk_blocks else 'warmup' if warm else 'running',warmup_received=self.state['warmup'],warmup_required=61),risk=dict(approved=True,risk_version=c['risk_version'],risk_config_sha256=self.config_hash),positions=positions,fills_count=len(b.fills) if b else 0,fills=b.fills if b else [],signals_count=len(self.state.get('handled_signals',{})),blocked_signals_count=sum(x['status']=='rejected' for x in self.state.get('handled_signals',{}).values()),rejections=[dict(signal_id=k,**v) for k,v in self.state.get('handled_signals',{}).items() if v['status']=='rejected'],blockers=([error] if error else [])+risk_blocks+(['fresh_forward_warmup'] if warm else []),feed=dict(transport='REST polling',connected=not bool(error),last_success_at=stamp(self.state['last_success_ms']) if self.state['last_success_ms'] else None,errors_count=self.state['errors'],gaps_count=self.state['gaps']),markets=self.state['markets'],versions=[dict(version_id=c['version_id'],created_at=stamp(self.state['forward_start_ms']),status=runtime_state)],equity_history=self.state['history'],latest_error=error,cost_ledger=b.ledger if b else [])
            s['orders']=list(b.orders.values()) if b else []
            s['rejections'] += [dict(order_id=o['order_id'],signal_id=o['intent_id'],reason=o['reason'],status=o['status']) for o in s['orders'] if o['status']=='REJECTED']
            s['blocked_signals_count']=len({x['signal_id'] for x in s['rejections'] if x['signal_id'] in self.state.get('handled_signals',{})})
            s['candidate_not_deployed']=not bool(self.state.get('deployment'))
            s['engine']['candidate_implementation']='paper-engine-v2' if self.state.get('deployment') else 'paper-engine-v2-candidate'
            s['engine']['deployment']=self.state.get('deployment')
            s['engine']['decision_cutoff_at']=stamp(self.state.get('decision_cutoff_ms',self.state['forward_start_ms']))
            s['engine']['indicator_context']=self.detector.manifest()
            validate_snapshot(s)
            return s
    def close(self):
        if self.broker:
            self.broker.close()
            self.broker=None
        if self.db:
            self.db.close()
            self.db=None
        if getattr(self,'fd',None) is not None:
            fcntl.flock(self.fd,fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd=None


def publish(snapshot, target):
    target=Path(target).resolve()
    if target.name=='paper_status.json' and target.parent==Path(__file__).resolve().parent/'shared':
        raise ValueError('reserved live status target')
    if target.name=='status.json':
        raise ValueError('refuse existing shadow status.json target before parent acceptance')
    validate_snapshot(snapshot)
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix('.json.tmp')
    with temporary.open('w') as stream:
        stream.write(canonical(snapshot)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary,target)
    fd=os.open(target.parent,os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main(argv=None, *, client=None, clock_ms=None, fixture=False):
    root=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--state-dir',type=Path,default=root/'data'/'paper-v2')
    parser.add_argument('--config',type=Path,default=root/'paper_config_v2.json')
    parser.add_argument('--status',type=Path,default=root/'shared'/'paper_v2_candidate.json')
    args=parser.parse_args(argv)
    if args.status.name=='status.json':
        parser.error('refuse overwriting shadow status.json')
    runtime=PaperRuntime(args.state_dir,args.config,client=client,clock_ms=clock_ms,fixture=fixture)
    try:
        while True:
            snapshot=runtime.poll()
            publish(snapshot,args.status)
            print(canonical(snapshot),flush=True)
            if args.once:
                return 1 if snapshot['latest_error'] else 0
            time.sleep(runtime.config['execution_model']['poll_interval_seconds'])
    except KeyboardInterrupt:
        return 0
    finally:
        runtime.close()


if __name__=='__main__':
    raise SystemExit(main())
