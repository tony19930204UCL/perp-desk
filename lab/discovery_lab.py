"""Staged three-arm ETH discovery lab for Issue #23.

No network, scheduler, service, live/default PAPER mutation, or automatic activation.
All three arms share one caller-supplied causal source but own independent broker DBs,
risk baselines, fills, funding ledgers, audit and research counters.
"""
from __future__ import annotations
import json, os, tempfile
from decimal import Decimal as D, Context, ROUND_FLOOR, ROUND_CEILING, localcontext
from pathlib import Path

from sim_broker import SimBroker, InstrumentSettings, ExecutionModel, RiskContract, Intent
from discovery_signal import evaluate
from discovery_evidence import CausalEvidence

ARMS=('A','B','C')
MAX_DIAGNOSTICS=3000

def _atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as s:
            json.dump(data,s,sort_keys=True,separators=(',',':'))
            s.flush();os.fsync(s.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def _ceil_tick(x,tick):
    return (x/tick).to_integral_value(rounding=ROUND_CEILING)*tick

def _floor_tick(x,tick):
    return (x/tick).to_integral_value(rounding=ROUND_FLOOR)*tick

class DiscoveryLab:
    def __init__(self,root,config_path,instrument,*,storage_probe=None):
        self.root=Path(root).resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.config=json.loads(Path(config_path).read_text())
        if self.config.get('active') is not False or self.config.get('version_id')!='ETH-DISCOVERY-LAB-001':
            raise ValueError('staged inactive discovery config required')
        if self.config.get('tradfi_enabled') is not False:
            raise ValueError('TradFi must remain disabled')
        self.storage_probe=storage_probe
        self.state_path=self.root/'lab_state.json'
        self.evidence=CausalEvidence(self.root/'causal_evidence.sqlite3')
        self.state=self._load_state()
        self.instrument=self._instrument(instrument)
        self.brokers={}
        if self.state.get('activated'):
            self._open_brokers()
        self.latest_book=self.state.get('latest_book')
        self.history=list(self.state.get('history',[]))
        self.feed=None

    def _load_state(self):
        if self.state_path.exists():
            state=json.loads(self.state_path.read_text())
            if state.get('schema_version')!=1 or state.get('version_id')!='ETH-DISCOVERY-LAB-001':
                raise ValueError('discovery state identity mismatch')
            return state
        state=dict(schema_version=1,version_id='ETH-DISCOVERY-LAB-001',activated=False,
                   start_ms=None,deadline_ms=None,checkpoint_ms=None,
                   coverage={},diagnostics=[],targets={},history=[],latest_book=None,
                   arm=dict(A={},B={},C={}),source_gaps=0,unknown_inputs=0,
                   storage_entry_inhibited=False,operator_stop_requested=False,
                   shared_source_gap_open=False,shared_source_gap_reason=None,
                   maker_source_gap_open=False,maker_source_gap_reason=None)
        _atomic_json(self.state_path,state)
        return state

    def _instrument(self,s):
        required=('symbol','maker_fee','taker_fee','qty_step','tick_size','min_notional','max_qty','min_qty','category')
        if not isinstance(s,dict) or any(k not in s for k in required) or s['symbol']!='ETHUSDT' or s['category']!='crypto':
            raise ValueError('explicit verified ETH instrument required')
        if D(str(s['maker_fee']))!=D(self.config['fees']['maker']) or D(str(s['taker_fee']))!=D(self.config['fees']['taker']):
            raise ValueError('fee contract mismatch')
        return InstrumentSettings('ETHUSDT',D(str(s['maker_fee'])),D(str(s['taker_fee'])),
                                  D(str(s['qty_step'])),D(str(s['tick_size'])),D(str(s['min_notional'])),
                                  D(str(s['max_qty'])),D(str(s['min_qty'])),'crypto')

    def activate(self,*,at_ms,operator_accepted):
        if operator_accepted is not True:
            raise ValueError('explicit operator acceptance required')
        if self.state['activated']:
            if at_ms!=self.state['start_ms']:
                raise ValueError('research window immutable after activation')
            return
        if type(at_ms) is not int or at_ms<0:
            raise ValueError('invalid activation time')
        self.state.update(activated=True,start_ms=at_ms,
                          deadline_ms=at_ms+self.config['window_ms'],
                          checkpoint_ms=at_ms+self.config['checkpoint_ms'])
        self._save();self._open_brokers()

    def _open_brokers(self):
        riskcfg=self.config['risk']
        risk=RiskContract(True,riskcfg['version'],D(riskcfg['max_loss_per_trade_usdt']),
                          D(riskcfg['max_daily_loss_usdt']),D(riskcfg['max_effective_exposure_x']),
                          riskcfg['max_positions'],D(riskcfg['total_loss_limit_usdt']),True)
        ex=ExecutionModel(self.config['latency_ms'],self.config['exit_slippage_ticks'],None,
                          self.config['entry_slippage_ticks'],self.config['exit_slippage_ticks'])
        for arm in ARMS:
            d=self.root/('arm-'+arm.lower());d.mkdir(exist_ok=True)
            self.brokers[arm]=SimBroker(d/'broker.sqlite3',initial_cash=D(self.config['initial_equity_usdt_per_arm']),
                instruments=[self.instrument],execution=ex,risk=risk,
                version_id='ETH-DISCOVERY-LAB-001-'+arm,forward_start=self.state['start_ms'],
                compact_seen_events=True)

    def close(self):
        for b in self.brokers.values():b.close()
        self.brokers={}

    def seed_warmup(self,bars,*,cutoff_ms):
        if self.state.get('activated'):
            raise ValueError('warmup cannot change after activation')
        if type(cutoff_ms) is not int or cutoff_ms<0 or not isinstance(bars,list) or len(bars)<61:
            raise ValueError('exact pre-activation warmup required')
        checked=[]
        for bar in bars[-61:]:
            required=('open_time_ms','close_time_ms','open','high','low','close','volume')
            if not isinstance(bar,dict) or any(k not in bar for k in required):
                raise ValueError('invalid warmup bar')
            if type(bar['open_time_ms']) is not int or type(bar['close_time_ms']) is not int or bar['close_time_ms']!=bar['open_time_ms']+60000:
                raise ValueError('invalid warmup timestamps')
            if bar['close_time_ms']>cutoff_ms:
                raise ValueError('warmup must be historical at cutoff')
            checked.append(dict(bar))
        for a,b in zip(checked,checked[1:]):
            if b['open_time_ms']!=a['open_time_ms']+60000:
                raise ValueError('warmup must be contiguous')
        self.history=checked
        self.state['history']=checked
        self.state['warmup_cutoff_ms']=cutoff_ms
        self._save()

    def request_operator_stop(self,now):
        if type(now) is not int or now<0:
            raise ValueError('invalid stop timestamp')
        already=bool(self.state.get('operator_stop_requested'))
        self.state['operator_stop_requested']=True
        if not already or self.state.get('operator_stop_requested_ms') is None:
            self.state['operator_stop_requested_ms']=now
        if self.brokers:
            for arm in ARMS:self._cancel_pending_entries(arm,now,'operator_stop_requested')
        self._save()

    def bind_feed(self,feed):
        self.feed=feed;feed.subscribe(self.on_shared_event)

    def request_source_gap(self,now,reason):
        """Durably fail closed after a runner-level public-source failure."""
        if type(now) is not int or now<0 or not isinstance(reason,str) or not reason:
            raise ValueError('valid source-gap timestamp/reason required')
        if not self.state.get('shared_source_gap_open'):
            self.state['source_gaps']+=1
        self.state['shared_source_gap_open']=True
        self.state['shared_source_gap_reason']=reason
        self.state['unknown_inputs']+=1
        self._evidence('runner-source-gap:'+str(now),'source_unknown',now,
                       dict(event_id='runner-source-gap:'+str(now),type='shared_source_gap',
                            source_ts=None,trade_id=None,price=None,qty=None,aggressor=None,
                            reason=reason,reconstructible_market_payload=False))
        for record in self._stress_orders().values():
            if record['status'] in ('WAITING','PARTIAL'):
                record['status']='UNKNOWN_SOURCE_GAP'
                record['transitions'].append(dict(ts=now,outcome='unknown_source_gap',reason=reason))
                record['transitions']=record['transitions'][-128:]
        for arm in ARMS:
            self._cancel_pending_entries(arm,now,'shared_source_gap')
            self._protective_exit(arm,'shared_source_gap',now)
        self._capture_broker_evidence(now)
        self._save()

    def clear_source_gap(self,now):
        if type(now) is not int or now<0:
            raise ValueError('valid recovery timestamp required')
        if self.state.get('shared_source_gap_open'):
            self._evidence('runner-source-recovered:'+str(now),'source_recovered',now,
                           dict(event_id='runner-source-recovered:'+str(now),
                                prior_reason=self.state.get('shared_source_gap_reason'),scope='all_arms'))
        self.state['shared_source_gap_open']=False
        self.state['shared_source_gap_reason']=None
        self._save()

    def request_maker_source_gap(self,now,reason):
        """Fail closed only Arm B when public aggTrade coverage is unavailable."""
        if type(now) is not int or now<0 or not isinstance(reason,str) or not reason:
            raise ValueError('valid maker source-gap timestamp/reason required')
        if not self.state.get('maker_source_gap_open'):
            self.state['source_gaps']+=1
        self.state['maker_source_gap_open']=True
        self.state['maker_source_gap_reason']=reason
        self.state['unknown_inputs']+=1
        self._evidence('runner-maker-gap:'+str(now),'source_unknown',now,
                       dict(event_id='runner-maker-gap:'+str(now),type='aggTrade_transport_gap',
                            source_ts=None,trade_id=None,price=None,qty=None,aggressor=None,
                            reason=reason,scope='B_only',reconstructible_market_payload=False))
        for record in self._stress_orders().values():
            if record['status'] in ('WAITING','PARTIAL'):
                record['status']='UNKNOWN_SOURCE_GAP'
                record['transitions'].append(dict(ts=now,outcome='unknown_source_gap',reason=reason))
                record['transitions']=record['transitions'][-128:]
        self._cancel_pending_entries('B',now,'maker_aggtrade_unknown')
        self._protective_exit('B','aggtrade_gap',now)
        self._capture_broker_evidence(now)
        self._save()

    def clear_maker_source_gap(self,now):
        if type(now) is not int or now<0:
            raise ValueError('valid maker recovery timestamp required')
        if self.state.get('maker_source_gap_open'):
            self._evidence('runner-maker-recovered:'+str(now),'source_recovered',now,
                           dict(event_id='runner-maker-recovered:'+str(now),
                                prior_reason=self.state.get('maker_source_gap_reason'),scope='B_only'))
        self.state['maker_source_gap_open']=False
        self.state['maker_source_gap_reason']=None
        self._save()

    def _save(self):
        if len(self.state['diagnostics'])>MAX_DIAGNOSTICS:
            raise ValueError('diagnostic bound exceeded')
        _atomic_json(self.state_path,self.state)

    def _storage_ok(self):
        used=self._storage_used()
        limit=self.config['storage']['budget_bytes']
        ok=used < int(D(str(self.config['storage']['entry_stop_fraction']))*D(limit))
        self.state['storage_entry_inhibited']=not ok
        return ok

    def _storage_used(self):
        if self.storage_probe is not None:return int(self.storage_probe())
        total=0
        for p in self.root.rglob('*'):
            if p.is_file() and not p.is_symlink():total+=p.stat().st_size
        return total

    def _evidence(self,key,kind,ts,payload):
        return self.evidence.append(key,kind,ts,payload)

    def _capture_broker_evidence(self,ts):
        for arm,b in self.brokers.items():
            for order in b.orders.values():
                snapshot=dict(order_id=order['order_id'],intent_id=order['intent_id'],status=order['status'],
                              reason=order.get('reason'),remaining=order.get('remaining'),
                              queue_remaining=order.get('queue_remaining'),
                              queue_observed_at_arrival=order.get('queue_observed_at_arrival'),
                              arrival_ts=order.get('arrival_ts'),resting_ts=order.get('resting_ts'),
                              intent={k:(str(v) if isinstance(v,D) else v) for k,v in order['intent'].items()})
                state_key='|'.join(str(snapshot.get(k)) for k in ('status','reason','remaining','queue_remaining','resting_ts'))
                self._evidence('order:'+arm+':'+order['order_id']+':'+state_key,'order_state',ts,
                               dict(arm=arm,**snapshot))
            for fill in b.fills:
                self._evidence('fill:'+arm+':'+fill['fill_id'],'fill',fill['ts'],dict(arm=arm,**fill))
            for row in b.ledger:
                self._evidence('ledger:'+arm+':'+row['ledger_id'],'ledger',row['ts'],dict(arm=arm,**row))

    def _stress_orders(self):
        return self.state['arm'].setdefault('B',{}).setdefault('queue_stress_2x_orders',{})

    def _init_b_stress(self,order,event):
        records=self._stress_orders()
        if order['order_id'] in records:return
        queue=D(order['queue_observed_at_arrival'])*D(self.config['maker']['stress_queue_multiplier'])
        record=dict(order_id=order['order_id'],intent_id=order['intent_id'],
                    role='profit_exit' if order['intent']['reduce_only'] else 'entry',
                    side=order['intent']['side'],limit_price=str(order['intent']['limit_price']),
                    qty=str(order['intent']['qty']),remaining=str(order['intent']['qty']),
                    queue_initial=str(queue),queue_remaining=str(queue),
                    resting_ts=order['resting_ts'],expires_ts=order['intent']['expires_ts'],
                    status='WAITING',fill_qty='0',transitions=[])
        records[order['order_id']]=record
        record['transitions'].append(dict(event_id=event['event_id'],ts=event['ts'],outcome='waiting',
                                          queue_remaining=str(queue),remaining=record['remaining']))
        self._evidence('stress-arrival:'+order['order_id'],'maker_queue_arrival',event['ts'],
                       dict(order_id=order['order_id'],event_id=event['event_id'],source_ts=event.get('source_ts'),
                            side=record['side'],limit_price=record['limit_price'],
                            queue_1x=order['queue_observed_at_arrival'],queue_2x=record['queue_initial'],
                            qty=record['qty'],role=record['role'],expires_ts=record['expires_ts']))

    def _update_b_stress(self,event):
        if event['type']!='aggTrade':return
        for record in self._stress_orders().values():
            if record['status'] not in ('WAITING','PARTIAL'):continue
            expiry=record.get('expires_ts')
            if expiry is not None and event['ts']>=expiry:
                record['status']='PARTIAL_EXPIRED' if D(record['fill_qty'])>0 else 'EXPIRED'
                record['transitions'].append(dict(event_id=event['event_id'],ts=event['ts'],outcome='expiry',
                                                  queue_remaining=record['queue_remaining'],remaining=record['remaining']))
                continue
            if event.get('source_valid') is False:
                record['status']='UNKNOWN_SOURCE_GAP'
                record['transitions'].append(dict(event_id=event['event_id'],ts=event['ts'],outcome='unknown_source_gap'))
                continue
            if type(event.get('source_ts')) is not int or event['source_ts']<=record['resting_ts']:
                record['transitions'].append(dict(event_id=event['event_id'],ts=event['ts'],outcome='ignored_pre_arrival_source'))
                continue
            price=D(str(event['price']));limit=D(record['limit_price'])
            through=(record['side']=='BUY' and event['aggressor']=='SELL' and price<limit) or (
                     record['side']=='SELL' and event['aggressor']=='BUY' and price>limit)
            if not through:
                record['transitions'].append(dict(event_id=event['event_id'],ts=event['ts'],outcome='waiting_no_trade_through',
                                                  price=str(price),aggressor=event['aggressor']))
                continue
            volume=D(str(event['qty']));before_queue=D(record['queue_remaining']);before_remaining=D(record['remaining'])
            ahead=min(before_queue,volume);after_queue=before_queue-ahead;volume-=ahead
            fill=min(before_remaining,volume);after_remaining=before_remaining-fill
            record['queue_remaining']=str(after_queue);record['remaining']=str(after_remaining)
            record['fill_qty']=str(D(record['fill_qty'])+fill)
            record['status']='FILLED' if after_remaining==0 else 'PARTIAL' if D(record['fill_qty'])>0 else 'WAITING'
            outcome='filled' if record['status']=='FILLED' else 'partial' if fill>0 else 'waiting_queue_depleted' if after_queue==0 else 'waiting_queue'
            transition=dict(event_id=event['event_id'],trade_id=event.get('trade_id'),ts=event['ts'],
                            source_ts=event.get('source_ts'),outcome=outcome,price=str(price),qty=str(event['qty']),
                            aggressor=event['aggressor'],queue_before=str(before_queue),queue_after=str(after_queue),
                            fill_delta=str(fill),remaining_after=str(after_remaining))
            record['transitions'].append(transition)
            record['transitions']=record['transitions'][-128:]
            self._evidence('stress-trade:'+record['order_id']+':'+event['event_id'],'maker_queue_stress_trade',
                           event['ts'],dict(order_id=record['order_id'],**transition))

    def _sync_b_stress_terminal(self,now):
        b=self.brokers.get('B')
        if not b:return
        for order_id,record in self._stress_orders().items():
            if record['status'] not in ('WAITING','PARTIAL'):continue
            order=b.orders.get(order_id)
            if not order:continue
            if order['status'] in ('CANCELED','EXPIRED','REJECTED') and order.get('reason')!='expiry':
                record['status']='PARTIAL_CANCELED' if D(record['fill_qty'])>0 else 'CANCELED'
                record['transitions'].append(dict(ts=now,outcome='policy_cancel',reason=order.get('reason')))
                record['transitions']=record['transitions'][-128:]
            elif order['status']=='EXPIRED' or (record.get('expires_ts') is not None and now>=record['expires_ts']):
                record['status']='PARTIAL_EXPIRED' if D(record['fill_qty'])>0 else 'EXPIRED'
                record['transitions'].append(dict(ts=now,outcome='expiry'))
                record['transitions']=record['transitions'][-128:]

    def _stress_report(self):
        b=self.brokers.get('B')
        result=[]
        if not b:return result
        for order_id,record in sorted(self._stress_orders().items()):
            fills=[f for f in b.fills if f['order_id']==order_id]
            primary_fill=sum((D(f['qty']) for f in fills),D(0))
            primary=b.orders.get(order_id,{})
            stress_fill=D(record['fill_qty'])
            if primary_fill>0 and stress_fill==0:comparison='primary_fill_vs_stress_no_fill'
            elif primary_fill>stress_fill and stress_fill>0:comparison='primary_more_fill_than_stress'
            elif primary_fill==stress_fill:comparison='same_fill_quantity'
            elif stress_fill>primary_fill:comparison='stress_more_fill_unexpected'
            else:comparison='waiting_or_unknown'
            result.append(dict(order_id=order_id,role=record['role'],
                primary_1x=dict(status=primary.get('status'),fill_qty=str(primary_fill),
                                queue_initial=primary.get('queue_observed_at_arrival')),
                stress_2x=dict(status=record['status'],fill_qty=record['fill_qty'],
                               queue_initial=record['queue_initial'],queue_remaining=record['queue_remaining'],
                               remaining=record['remaining']),
                comparison=comparison,transitions=list(record['transitions'])))
        return result

    def on_shared_event(self,event):
        if not self.state.get('activated'):
            raise ValueError('lab not operator-activated')
        if event.get('symbol')!='ETHUSDT':
            raise ValueError('unexpected shared symbol')
        self._enforce_entry_stops(event['ts'])
        if event.get('source_valid') is False:
            self.state['unknown_inputs']+=1
            self._evidence('unknown:'+event['event_id'],'source_unknown',event['ts'],
                           {k:event.get(k) for k in ('event_id','type','source_ts','trade_id','price','qty','aggressor')})
            if event.get('type')=='aggTrade':
                self._update_b_stress(event)
                self.state['source_gaps']+=1
                self._protective_exit('B','aggtrade_gap',event['ts'])
            self._sync_b_stress_terminal(event['ts']);self._capture_broker_evidence(event['ts'])
            self._save();return
        kind=event['type']
        if kind=='closed_bar':
            self._closed_bar(event);self._capture_broker_evidence(event['ts']);self._save();return
        if kind=='book':
            self.latest_book=event
            self.state['latest_book']=event
            for arm in ARMS:self.brokers[arm].on_event(self._broker_event(event))
            self._after_b_book(event)
        elif kind=='mark':
            self._pre_mark_b(event)
            for arm in ARMS:self.brokers[arm].on_event(self._broker_event(event))
            self._exit_policy_taker_arms(event)
        elif kind in ('aggTrade','funding','funding_status'):
            if kind=='aggTrade':
                active=any(o['intent']['kind']=='MAKER' and o['status']=='RESTING' for o in self.brokers['B'].orders.values())
                if active:
                    self._evidence('aggtrade:'+event['event_id'],'maker_trade_observation',event['ts'],
                                   {k:event.get(k) for k in ('event_id','trade_id','source_ts','price','qty','aggressor')})
                self._update_b_stress(event)
            for arm in ARMS:self.brokers[arm].on_event(self._broker_event(event))
        else:
            raise ValueError('unsupported lab event')
        self._after_b_fills(event)
        self._sync_b_stress_terminal(event['ts'])
        self._enforce_max_hold(event['ts'])
        self._capture_broker_evidence(event['ts'])
        self._update_episode_metrics(event['ts'], D(str(event['price'])) if kind=='mark' else None)
        self._save()

    @staticmethod
    def _broker_event(event):
        return {k:v for k,v in event.items() if k not in ('source_valid','duplicate','trade_id','closed',
                                                            'open_time_ms','close_time_ms','open','high','low','close','volume')}

    def _closed_bar(self,event):
        close_ms=event['close_time_ms'];start=self.state['start_ms']
        if start<close_ms<=self.state['checkpoint_ms']:
            self.state['coverage'][str(close_ms)]=dict(timely=True,source_valid=True)
        bar={k:event[k] for k in ('open_time_ms','close_time_ms','open','high','low','close','volume')}
        if self.history and bar['open_time_ms']!=self.history[-1]['open_time_ms']+60000:
            self.history=[bar];self.state['history']=self.history
            self.state['source_gaps']+=1
            for arm in ARMS:self._protective_exit(arm,'closed_bar_gap',event['ts'])
            return
        prior=list(self.history[-61:])
        self.history.append(bar);self.history=self.history[-62:]
        self.state['history']=self.history
        if len(prior)<61:return
        for signal in evaluate(prior,bar):
            self._route_signal(signal,event['ts'])

    def _market(self,now):
        b=self.latest_book
        if not b or b.get('source_valid') is False or not b['bids'] or not b['asks']:
            return None
        if not 0<=now-b['source_ts']<=self.config['max_source_age_ms']:
            return None
        bid=D(str(b['bids'][0][0]));ask=D(str(b['asks'][0][0]))
        if bid>=ask:return None
        return bid,ask

    def _economics(self,direction,mode,bid,ask,target):
        tick=self.instrument.tick;maker=self.instrument.maker_fee;taker=self.instrument.taker_fee
        target=D(str(target))
        with localcontext(Context(prec=50)):
            if mode=='taker':
                entry=ask+tick*self.config['entry_slippage_ticks'] if direction=='long' else bid-tick*self.config['entry_slippage_ticks']
                gross=target-entry if direction=='long' else entry-target
                total=(ask-bid)+tick*(self.config['entry_slippage_ticks']+self.config['exit_slippage_ticks'])+taker*(entry+target)
                entry_fee=taker;planned_exit_fee=taker
            else:
                if direction!='long':raise ValueError('maker arm is long only')
                entry=bid;gross=target-entry
                total=maker*(entry+target)
                entry_fee=maker;planned_exit_fee=maker
            ratio=(gross/total) if total>0 else None
            qualified=bool(total>0 and gross>total*D(self.config['minimum_gross_reward_to_estimated_cost']))
            stop_raw=entry*(D(1)-D(self.config['stop_distance_fraction']) if direction=='long'
                            else D(1)+D(self.config['stop_distance_fraction']))
            stop=_floor_tick(stop_raw,tick) if direction=='long' else _ceil_tick(stop_raw,tick)
            protective_exit=stop-tick*self.config['exit_slippage_ticks'] if direction=='long' else stop+tick*self.config['exit_slippage_ticks']
            # Existing PAPER broker reserves entry risk with taker fee even for
            # maker intents. Keep that conservative risk convention unchanged;
            # actual maker fills still book the configured maker fee.
            risk_entry_fee=taker
            planned_loss=abs(entry-protective_exit)+entry*risk_entry_fee+protective_exit*taker
            return dict(entry=entry,stop=stop,gross=gross,total_cost=total,ratio=ratio,
                        qualified=qualified,planned_loss=planned_loss,
                        planned_entry_liquidity=mode,planned_profit_exit_liquidity=('maker' if mode=='maker' else 'taker'),
                        protective_exit_liquidity='taker',planned_exit_fee=planned_exit_fee)

    def _qty(self,econ,equity):
        with localcontext(Context(prec=50)):
            if econ['planned_loss']<=0 or equity is None or D(equity)<=0:return D(0)
            raw=min(D(self.config['risk']['max_loss_per_trade_usdt'])/econ['planned_loss'],
                    D(equity)*D(self.config['risk']['max_effective_exposure_x'])/econ['entry'],
                    self.instrument.max_quantity)
            qty=_floor_tick(raw,self.instrument.quantity_step)
            if qty<self.instrument.min_quantity or qty*econ['entry']<self.instrument.min_notional:return D(0)
            return qty

    def _arm_block_reason(self,arm,now):
        b=self.brokers[arm]
        if self.state.get('operator_stop_requested'):return 'operator_stop_requested'
        if self.state.get('shared_source_gap_open'):return 'shared_source_unknown'
        if now>=self.state['deadline_ms']:return 'research_window_closed'
        if not self._storage_ok():return 'storage_entry_inhibited'
        if arm=='B' and (self.state.get('maker_source_gap_open')
                         or self.feed is None or self.feed.state.get('aggtrade_valid') is not True):
            return 'maker_aggtrade_unknown'
        if b.positions or any(o['status'] in ('PENDING','RESTING') and not o['intent']['reduce_only'] for o in b.orders.values()):
            return 'single_position_or_pending'
        cp=self.checkpoint(arm,now)
        if cp['stop_new_entries']:return 'throughput_checkpoint_'+cp['status']
        return None

    def _route_signal(self,signal,now):
        market=self._market(now)
        for arm in ('A','B','C'):
            if signal['direction']=='short' and arm!='C':continue
            diag=dict(arm=arm,signal_id=signal['signal_id'],direction=signal['direction'],at_ms=now,
                      raw=True,target=signal['target'],source_signal_features=signal['features'],
                      cost_qualified=False,status='rejected',reason=None)
            if market is None:
                diag['reason']='market_unknown'
            else:
                bid,ask=market;mode='maker' if arm=='B' else 'taker'
                econ=self._economics(signal['direction'],mode,bid,ask,signal['target'])
                diag['economics']={k:(str(v) if isinstance(v,D) else v) for k,v in econ.items()}
                diag['cost_qualified']=econ['qualified']
                block=self._arm_block_reason(arm,now)
                if block:diag['reason']=block
                elif not econ['qualified']:diag['reason']='insufficient_reward_after_costs'
                else:
                    qty=self._qty(econ,self.brokers[arm].equity)
                    if qty<=0:diag['reason']='below_filters_or_invalid_loss'
                    else:
                        side='BUY' if signal['direction']=='long' else 'SELL'
                        iid=arm+':'+signal['signal_id']
                        kwargs={}
                        if arm=='B':
                            kwargs=dict(limit_price=econ['entry'],queue_ahead_qty=None,
                                        expires_ts=now+self.config['maker']['entry_expiry_ms'],
                                        maker_queue_from_arrival=True)
                        else:
                            kwargs=dict(expires_ts=now+self.config['max_source_age_ms'])
                        order=self.brokers[arm].submit(Intent(iid,'ETHUSDT',side,qty,econ['stop'],now,
                            self.instrument.quantity_step,self.instrument.tick,self.instrument.min_notional,
                            self.instrument.max_quantity,'MAKER' if arm=='B' else 'TAKER',False,**kwargs))
                        diag['status']='submitted' if order['status']=='PENDING' else 'rejected'
                        diag['reason']=order['reason']
                        if order['status']=='PENDING':
                            self.state['targets'][iid]=dict(arm=arm,direction=signal['direction'],target=signal['target'])
            self.state['diagnostics'].append(diag)
            self._evidence('decision:'+arm+':'+signal['signal_id'],'signal_decision',now,dict(diag))

    def _after_b_book(self,event):
        b=self.brokers['B']
        for o in b.orders.values():
            if o['intent']['kind']=='MAKER' and o['status']=='RESTING' and 'queue_observed_at_arrival' in o:
                stress=str(D(o['queue_observed_at_arrival'])*D(self.config['maker']['stress_queue_multiplier']))
                self.state['arm']['B'].setdefault('queue_stress_2x',{})[o['order_id']]=stress
                self._init_b_stress(o,event)
        for o in list(b.orders.values()):
            if o['intent']['reduce_only'] and o['intent']['kind']=='MAKER' and o['status']=='REJECTED' and o.get('reason')=='post_only_cross':
                self._profit_fallback(o,event['ts'])

    def _after_b_fills(self,event):
        b=self.brokers['B']
        for fill in list(b.fills):
            if fill.get('liquidity')!='MAKER' or b.orders[fill['order_id']]['intent']['reduce_only']:
                continue
            pid='B:profit:'+fill['fill_id']
            if 'order:'+pid in b.orders:continue
            target_info=self.state['targets'].get(fill['intent_id'])
            if not target_info:raise ValueError('maker fill lacks frozen target')
            target=_ceil_tick(D(target_info['target']),self.instrument.tick)
            order=b.submit(Intent(pid,'ETHUSDT','SELL',D(fill['qty']),None,event['ts'],
                self.instrument.quantity_step,self.instrument.tick,self.instrument.min_notional,
                self.instrument.max_quantity,'MAKER',True,limit_price=target,queue_ahead_qty=None,
                maker_queue_from_arrival=True))
            self.state['targets'][pid]=dict(arm='B',direction='long',target=str(target),profit_for=fill['fill_id'])

    def _profit_fallback(self,profit_order,now):
        b=self.brokers['B'];p=b.positions.get('ETHUSDT')
        if not p:return
        iid='B:profit-fallback:'+profit_order['intent_id']
        if 'order:'+iid in b.orders:return
        qty=min(abs(D(p['qty'])),D(profit_order['intent']['qty']))
        if qty<=0:return
        b.submit(Intent(iid,'ETHUSDT','SELL',qty,None,now,self.instrument.quantity_step,self.instrument.tick,
                        self.instrument.min_notional,self.instrument.max_quantity,'TAKER',True))
        self.state['targets'][iid]=dict(arm='B',reason='maker_profit_cross_fallback',liquidity='taker')

    def _pre_mark_b(self,event):
        b=self.brokers['B'];p=b.positions.get('ETHUSDT')
        if not p:return
        qty=D(p['qty']);price=D(str(event['price']));stop=D(p['stop'])
        triggered=(qty>0 and price<=stop) or (qty<0 and price>=stop)
        if triggered:self._cancel_b_profit(event['ts'],'stop')

    def _cancel_b_profit(self,now,reason):
        b=self.brokers['B']
        for o in list(b.orders.values()):
            if o['intent']['reduce_only'] and o['intent']['kind']=='MAKER' and o['status'] in ('PENDING','RESTING'):
                b.cancel(o['order_id'],ts=max(now,b.last_ts),reason='protective_'+reason)

    def _cancel_pending_entries(self,arm,now,reason):
        b=self.brokers[arm]
        for o in list(b.orders.values()):
            if not o['intent']['reduce_only'] and o['status'] in ('PENDING','RESTING'):
                b.cancel(o['order_id'],ts=max(now,b.last_ts),reason=reason)

    def _enforce_entry_stops(self,now):
        for arm in ARMS:
            reason=None
            if self.state.get('operator_stop_requested'):
                reason='operator_stop_requested'
            elif self.state.get('shared_source_gap_open'):
                reason='shared_source_unknown'
            elif now>=self.state['deadline_ms']:
                reason='research_window_closed'
            elif not self._storage_ok():
                reason='storage_entry_inhibited'
            elif arm=='B' and (self.state.get('maker_source_gap_open')
                               or (self.feed is not None and self.feed.state.get('aggtrade_valid') is not True)):
                reason='maker_aggtrade_unknown'
            else:
                cp=self.checkpoint(arm,now)
                if cp['stop_new_entries']:reason='throughput_checkpoint_'+cp['status']
            if reason:self._cancel_pending_entries(arm,now,reason)

    def _protective_exit(self,arm,reason,now):
        if arm not in self.brokers:return
        b=self.brokers[arm]
        self._cancel_pending_entries(arm,now,'protective_'+reason)
        p=b.positions.get('ETHUSDT')
        if not p:return
        if arm=='B':self._cancel_b_profit(now,reason)
        if any(o['intent']['reduce_only'] and o['intent']['kind']=='TAKER' and o['status'] in ('PENDING','RESTING') for o in b.orders.values()):
            return
        qty=abs(D(p['qty']));side='SELL' if D(p['qty'])>0 else 'BUY'
        iid=arm+':protect:'+reason+':'+str(now)
        b.submit(Intent(iid,'ETHUSDT',side,qty,None,now,self.instrument.quantity_step,self.instrument.tick,
                        self.instrument.min_notional,self.instrument.max_quantity,'TAKER',True))
        self.state['targets'][iid]=dict(arm=arm,reason=reason,liquidity='taker')

    def _exit_policy_taker_arms(self,event):
        mark=D(str(event['price']))
        for arm in ('A','C'):
            b=self.brokers[arm];p=b.positions.get('ETHUSDT')
            if not p:continue
            entries=[f for f in b.fills if not b.orders[f['order_id']]['intent']['reduce_only']
                     and f['symbol']=='ETHUSDT']
            if not entries:continue
            info=self.state['targets'].get(entries[-1]['intent_id'])
            if not info:raise ValueError('position lacks target')
            target=D(info['target']);q=D(p['qty'])
            hit=(q>0 and mark>=target) or (q<0 and mark<=target)
            if hit:self._protective_exit(arm,'take_profit',event['ts'])

    def _enforce_max_hold(self,now):
        for arm,b in self.brokers.items():
            p=b.positions.get('ETHUSDT')
            if p and now-p['opened_ts']>=self.config['max_holding_ms']:
                self._protective_exit(arm,'max_hold',now)

    def _update_episode_metrics(self,now,mark=None):
        for arm,b in self.brokers.items():
            arm_state=self.state['arm'].setdefault(arm,{})
            p=b.positions.get('ETHUSDT')
            open_episode=arm_state.get('open_episode')
            if p and open_episode is None:
                qty=D(p['qty']);entry=D(p['entry'])
                open_episode=dict(opened_ts=p['opened_ts'],direction='long' if qty>0 else 'short',
                                  entry_price=str(entry),max_adverse_price_distance='0')
                arm_state['open_episode']=open_episode
            if p and open_episode is not None and mark is not None:
                entry=D(open_episode['entry_price'])
                adverse=max(D(0),entry-mark) if open_episode['direction']=='long' else max(D(0),mark-entry)
                if adverse>D(open_episode['max_adverse_price_distance']):
                    open_episode['max_adverse_price_distance']=str(adverse)
            if not p and open_episode is not None:
                completed=arm_state.setdefault('completed_episodes',[])
                completed.append(dict(open_episode,closed_ts=now,holding_ms=now-open_episode['opened_ts']))
                arm_state['completed_episodes']=completed[-3000:]
                arm_state.pop('open_episode',None)

    @staticmethod
    def _flat_to_flat_count(broker):
        qty=D(0);complete=0
        for fill in broker.fills:
            before=qty
            qty += D(fill['qty']) if fill['side']=='BUY' else -D(fill['qty'])
            if before!=0 and qty==0:complete+=1
        return complete

    def checkpoint(self,arm,now):
        expected=self.config['checkpoint_ms']//60000
        covered=sum(1 for k,v in self.state['coverage'].items()
                    if self.state['start_ms']<int(k)<=self.state['checkpoint_ms'] and v.get('timely') and v.get('source_valid'))
        ratio=D(covered)/D(expected)
        diags=[d for d in self.state['diagnostics'] if d['arm']==arm and d.get('cost_qualified')
               and self.state['start_ms']<d['at_ms']<=self.state['checkpoint_ms']]
        unique={d['signal_id'] for d in diags}
        directions={x:len({d['signal_id'] for d in diags if d['direction']==x}) for x in ('long','short')}
        if arm=='B':
            b=self.brokers.get('B')
            filled=set()
            if b:
                for f in b.fills:
                    o=b.orders[f['order_id']]
                    if (not o['intent']['reduce_only'] and f['liquidity']=='MAKER'
                            and self.state['start_ms']<f['ts']<=self.state['checkpoint_ms']):filled.add(f['order_id'])
            throughput=len(filled);metric='actual_maker_filled_entry_orders'
            minimum=self.config['checkpoint']['arm_b_minimum_actual_maker_filled_entry_orders']
        else:
            throughput=len(unique);metric='fresh_full_cost_qualified_candidates'
            minimum=self.config['checkpoint']['arm_'+arm.lower()+'_minimum_cost_qualified_candidates']
        status='pending';stop=False
        if now>=self.state['checkpoint_ms']:
            if ratio<D(self.config['checkpoint']['minimum_timely_source_valid_coverage']):
                status='data_quality_inconclusive';stop=True
            elif throughput<minimum:
                status='throughput_infeasible';stop=True
            else:status='passed'
        return dict(arm=arm,coverage=str(ratio),covered=covered,expected=expected,
                    throughput_metric=metric,throughput=throughput,minimum=minimum,
                    direction_cost_qualified=directions,status=status,stop_new_entries=stop,
                    deadline_ms=self.state['deadline_ms'],pnl_stopping_rule=False)

    def report(self,now):
        arms={}
        by_signal={}
        for d in self.state['diagnostics']:
            by_signal.setdefault(d['signal_id'],{})[d['arm']]=d
        paired=[]
        for signal_id,records in by_signal.items():
            if 'A' in records and 'B' in records:
                paired.append(dict(signal_id=signal_id,
                    A={k:records['A'].get(k) for k in ('direction','cost_qualified','status','reason')},
                    B={k:records['B'].get(k) for k in ('direction','cost_qualified','status','reason')}))
        for arm,b in self.brokers.items():
            entry_orders=[o for o in b.orders.values() if not o['intent']['reduce_only']]
            fills=[f for f in b.fills if not b.orders[f['order_id']]['intent']['reduce_only']]
            fees=-sum((D(x['amount']) for x in b.ledger if x['type']=='fee'),D(0))
            funding=sum((D(x['amount']) for x in b.ledger if x['type']=='funding'),D(0))
            realized=sum((D(x['amount']) for x in b.ledger if x['type']=='realized'),D(0))
            diagnostics=[d for d in self.state['diagnostics'] if d['arm']==arm]
            directions={direction:dict(
                raw=sum(1 for d in diagnostics if d['direction']==direction),
                cost_qualified=sum(1 for d in diagnostics if d['direction']==direction and d.get('cost_qualified')))
                for direction in ('long','short')}
            risk_rejections=sum(1 for o in b.orders.values() if str(o.get('reason') or '').startswith('risk_'))
            protective=sum(1 for o in b.orders.values() if o['intent']['reduce_only'] and o['intent']['kind']=='TAKER')
            arm_state=self.state['arm'].get(arm,{})
            arms[arm]=dict(cash=str(b.cash),initial_cash=str(b.initial_cash),positions=len(b.positions),
                raw_candidates=len(diagnostics),
                cost_qualified=sum(1 for d in diagnostics if d.get('cost_qualified')),
                submitted=sum(1 for d in diagnostics if d.get('status')=='submitted'),
                expired=sum(1 for o in entry_orders if o['status']=='EXPIRED'),
                nonfilled=sum(1 for o in entry_orders if o['status'] in ('EXPIRED','REJECTED','CANCELED') and not any(f['order_id']==o['order_id'] for f in fills)),
                filled_entry_orders=len({f['order_id'] for f in fills}),
                flat_to_flat_count=self._flat_to_flat_count(b),
                direction_counts=directions,risk_rejections=risk_rejections,
                protective_taker_orders=protective,
                completed_episode_metrics=list(arm_state.get('completed_episodes',[])),
                open_episode_metric=arm_state.get('open_episode'),
                queue_stress_2x=dict(arm_state.get('queue_stress_2x',{})) if arm=='B' else {},
                queue_stress_sensitivity=self._stress_report() if arm=='B' else [],
                fees_usdt=str(fees),funding_pnl_usdt=str(funding),gross_realized_pnl_usdt=str(realized),
                net_ledger_usdt=str(sum((D(x['amount']) for x in b.ledger),D(0))),
                checkpoint=self.checkpoint(arm,now))
        return dict(version_id=self.state['version_id'],activated=self.state['activated'],
                    start_ms=self.state['start_ms'],deadline_ms=self.state['deadline_ms'],
                    shared_window=True,capital_pooled=False,default_account_touched=False,
                    storage_used_bytes=self._storage_used(),storage_budget_bytes=self.config['storage']['budget_bytes'],
                    source_gaps=self.state['source_gaps'],unknown_inputs=self.state['unknown_inputs'],
                    operator_stop_requested=bool(self.state.get('operator_stop_requested')),
                    causal_evidence=self.evidence.summary(),
                    paired_ab=paired,arms=arms)
