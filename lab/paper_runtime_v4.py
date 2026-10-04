"""Staged H1-PAPER-004 runtime.

Inherits all accepted H1-PAPER-003 source, execution, broker, storage and risk
contracts. Adds only preregistered candidate diagnostics and the 8h throughput
checkpoint. This module is not a deployment action.
"""
import argparse, json, sqlite3, time
from collections import Counter
from contextlib import closing
from decimal import Decimal as D, Context, ROUND_FLOOR, localcontext
from pathlib import Path

from paper_runtime_v3 import PaperRuntime as BaseRuntime, publish, canonical, research_metrics
from paper_sizing import size_long
from signals_v4 import Detector
from sim_broker import Intent
from storage_protection import StorageProtectionHalt

CONFIG_HASH='335b44e7208642b675db97306f2733069489df1e396bbb505fa3ce3aa9d4cf22'
CHECKPOINT_MS=8*60*60*1000
WINDOW_MS=48*60*60*1000
MINUTE_MS=60000
MAX_CANDIDATE_DIAGNOSTICS=3000
MAX_ERROR_PERIODS=32


def cost_diagnostic(config,spec,*,bid,ask,target):
    """Pure copy of accepted sizing economics for diagnostics; submits nothing."""
    if target is None:
        return dict(computable=False,error='target_unavailable',cost_qualified=False)
    try:
        with localcontext(Context(prec=50)):
            b,a,goal=map(D,(bid,ask,target))
            tick=D(spec['tick_size']);fee=D(spec['taker_fee'])
            entry_slip=tick*D(config['execution_model']['entry_slippage_ticks'])
            exit_slip=tick*D(config['execution_model']['exit_slippage_ticks'])
            entry=a+entry_slip
            stop_raw=entry*(D(1)-D(config['strategy']['stop_distance_fraction']))
            stop=(stop_raw/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
            spread=a-b
            fee_component=fee*(entry+goal)
            total=spread+entry_slip+exit_slip+fee_component
            gross=goal-entry
            ratio=(gross/total) if total>0 else None
            threshold=D(config['strategy']['minimum_gross_reward_to_estimated_cost'])
            qualified=bool(total>0 and gross>total*threshold)
            return dict(
                computable=True,error=None,
                bid=str(b),ask=str(a),entry_price_bound=str(entry),stop_price=str(stop),
                frozen_target=str(goal),gross_favorable_distance=str(gross),
                cost_components=dict(
                    spread=str(spread),
                    entry_slippage=str(entry_slip),
                    exit_slippage=str(exit_slip),
                    taker_fee_round_trip=str(fee_component)),
                estimated_round_trip_cost_per_unit=str(total),
                gross_to_cost_ratio=(str(ratio) if ratio is not None else None),
                minimum_gross_reward_to_estimated_cost=str(threshold),
                cost_qualified=qualified)
    except (KeyError,TypeError,ValueError,ArithmeticError) as exc:
        return dict(computable=False,error=type(exc).__name__,cost_qualified=False)


def throughput_checkpoint(*,strategy_start_ms,now_ms,coverage,candidates):
    """Pure preregistered 8h throughput decision; never a PnL rule."""
    checkpoint_ms=strategy_start_ms+CHECKPOINT_MS
    deadline_ms=strategy_start_ms+WINDOW_MS
    expected=CHECKPOINT_MS//MINUTE_MS
    timely_valid={
        int(slot) for slot,item in coverage.items()
        if isinstance(item,dict) and item.get('timely') is True and item.get('source_valid') is True
        and strategy_start_ms < int(slot) <= checkpoint_ms
    }
    covered=len(timely_valid)
    with localcontext(Context(prec=50)):
        ratio=D(covered)/D(expected)
    qualified={
        item.get('signal_id') for item in candidates
        if isinstance(item,dict)
        and item.get('signal_id')
        and item.get('routing_decision_ms') is not None
        and strategy_start_ms < int(item['routing_decision_ms']) <= checkpoint_ms
        and item.get('timely') is True
        and item.get('source_valid') is True
        and item.get('cost_qualified') is True
    }
    status='pending'
    stop_new_entries=False
    if now_ms>=checkpoint_ms:
        if ratio<D('0.90'):
            status='data_quality_inconclusive';stop_new_entries=True
        elif len(qualified)<4:
            status='throughput_infeasible';stop_new_entries=True
        else:
            status='passed'
    return dict(
        checkpoint_at_ms=checkpoint_ms,deadline_ms=deadline_ms,
        expected_closed_minute_slots=expected,
        timely_source_valid_slots=covered,
        timely_coverage=str(ratio),
        minimum_timely_coverage='0.90',
        independent_cost_qualified_opportunities=len(qualified),
        minimum_cost_qualified_opportunities=4,
        status=status,stop_new_entries=stop_new_entries,
        pnl_stopping_rule=False)


class PaperRuntime(BaseRuntime):
    CONFIG_HASH=CONFIG_HASH
    DETECTOR=Detector
    EXTRA_SOURCES=('paper_runtime_v3.py','signals_v3.py','storage_protection.py',
                   'paper_runtime_v4.py','signals_v4.py')
    INTERVAL_MS=60000
    INTERVAL_NAME='1m'

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.state.setdefault('candidate_diagnostics',[])
        self.state.setdefault('minute_coverage',{})
        self._coverage_batch_keys=[]
        self.state.setdefault('poll_diagnostics',dict(
            poll_error_events=0,error_periods_started=0,recovery_observations=0,
            consecutive_error_polls=0,active_error_period=None,error_periods=[]))
        self.save()

    def _strategy_start(self):
        return self.state.get('strategy_start_ms',self.state['forward_start_ms'])

    def _record_minute_detection(self,event):
        bar=event.get('bar') if isinstance(event,dict) else None
        if not isinstance(bar,dict) or type(bar.get('close_time_ms')) is not int:
            return
        start=self._strategy_start();close_ms=bar['close_time_ms']
        if not start < close_ms <= start+CHECKPOINT_MS:
            return
        now=self.clock()
        key=str(close_ms)
        coverage=self.state.setdefault('minute_coverage',{})
        if key not in coverage:
            coverage[key]=dict(
                close_ms=close_ms,detection_ms=now,
                detection_timestamp_domain='runtime_utc_wall',
                timely=bool(0<=now-close_ms<=self.config['execution_model']['max_source_age_ms']),
                source_valid=False)
            self._coverage_batch_keys.append(key)
        # Exactly 480 one-minute slots can belong to the first checkpoint window.
        if len(coverage)>CHECKPOINT_MS//MINUTE_MS:
            raise ValueError('minute coverage bound exceeded')

    def audit(self,event):
        if isinstance(event,dict) and event.get('type')=='detector':
            self._record_minute_detection(event)
        return super().audit(event)

    def collect(self):
        self._coverage_batch_keys=[]
        try:
            return super().collect()
        finally:
            # Never let a later successful poll validate a prior failed batch.
            self._coverage_batch_keys=[]

    def validate_sources(self,markets,context,*,allow_wait=False):
        observed=super().validate_sources(markets,context,allow_wait=allow_wait)
        if context=='decision after candle work':
            # Only actual successful source validation can mark an evaluated minute
            # source-valid. Late replayed bars remain timely=false and never count.
            coverage=self.state.setdefault('minute_coverage',{})
            for key in list(self._coverage_batch_keys):
                item=coverage.get(key)
                if item is not None and item.get('source_valid') is False:
                    item['source_valid']=True
                    item['source_validation_ms']=observed
                    item['source_validation_timestamp_domain']='runtime_utc_wall'
            self.save()
        return observed

    def _candidate_diagnostic(self,signal,spec,market,now):
        features=signal.get('features',{})
        bar_close=int(features['bar_close_ms']) if features.get('bar_close_ms') is not None else None
        timely=bool(bar_close is not None and 0<=now-bar_close<=self.config['execution_model']['max_source_age_ms'])
        economics=cost_diagnostic(self.config,spec,bid=market['bid'],ask=market['ask'],
                                  target=signal.get('reversion_target'))
        diag=dict(
            signal_id=signal['signal_id'],version_id=signal.get('version_id'),
            symbol=signal.get('symbol'),raw_candidate=True,
            signal_bar_close_ms=bar_close,
            signal_bar_timestamp_domain='exchange_closed_bar_epoch_ms',
            detection_ms=(int(features['detection_ms']) if features.get('detection_ms') is not None else None),
            detection_timestamp_domain=features.get('detection_timestamp_domain'),
            routing_decision_ms=now,
            routing_timestamp_domain='runtime_utc_wall',
            timely=timely,source_valid=True,
            source_timestamps_ms=dict(market.get('source_timestamps_ms',{})),
            target_method=features.get('target_method'),
            target_eligible=features.get('target_eligible'),
            target_error=features.get('target_error'),
            entry_reference=signal.get('entry_reference'),
            frozen_target=signal.get('reversion_target'),
            target_excludes_signal_bar=features.get('target_excludes_signal_bar'),
            target_frozen=features.get('target_frozen'),
            economics=economics,
            cost_qualified=bool(timely and economics.get('cost_qualified') is True),
            admission_status=None,admission_reason=None)
        return diag

    def _save_candidate_diag(self,diag):
        records=self.state.setdefault('candidate_diagnostics',[])
        existing=next((item for item in records if item.get('signal_id')==diag['signal_id']),None)
        if existing is None:
            records.append(diag)
            if len(records)>MAX_CANDIDATE_DIAGNOSTICS:
                raise ValueError('candidate diagnostic bound exceeded')
            return diag
        return existing

    def route_signals(self,spec):
        with closing(sqlite3.connect(self.root/'signals.sqlite3')) as db:
            intents=[json.loads(row[0]) for row in db.execute(
                'SELECT intent FROM h1_signals WHERE version_id=? ORDER BY rowid',
                (self.config['version_id'],))]
        handled=self.state.setdefault('handled_signals',{})
        market=next(m for m in self.state['markets'] if m['symbol']=='ETHUSDT')
        for signal in intents:
            sid=signal['signal_id']
            if sid in handled:
                continue
            now=self.clock()
            diag=self._save_candidate_diag(self._candidate_diagnostic(signal,spec,market,now))
            if 'order:'+sid in self.broker.orders:
                result=dict(status='submitted',target=signal['reversion_target'])
            else:
                blockers=self.risk_blockers()
                if signal.get('reversion_target') is None:
                    result=dict(status='rejected',reason='invalid_frozen_target')
                elif blockers:
                    result=dict(status='rejected',reason=','.join(blockers))
                elif now-int(signal['features']['bar_close_ms'])>self.config['execution_model']['max_source_age_ms']:
                    result=dict(status='rejected',reason='late_closed_bar_signal')
                elif self.broker.positions or any(
                        o['status'] in ('PENDING','RESTING') for o in self.broker.orders.values()):
                    result=dict(status='rejected',reason='single_position_or_pending')
                else:
                    result=size_long(self.config,spec,equity=str(self.broker.equity),
                                     bid=market['bid'],ask=market['ask'],
                                     target=signal['reversion_target'])
                    if result['status']=='accepted':
                        s=self.broker.instruments['ETHUSDT']
                        order=self.broker.submit(Intent(
                            sid,'ETHUSDT','BUY',D(result['qty']),D(result['stop_price']),now,
                            s.quantity_step,s.tick,s.min_notional,s.max_quantity,'TAKER',False,
                            expires_ts=now+self.config['execution_model']['max_source_age_ms'],
                            risk_limited=True,risk_quantity_step=D(result['execution_qty_step'])))
                        result.update(
                            status='submitted' if order['status']=='PENDING' else 'rejected',
                            reason=order['reason'],target=signal['reversion_target'])
            handled[sid]=result
            diag['admission_status']=result['status']
            diag['admission_reason']=result.get('reason')
            diag['actual_routing_result']=dict(result)
            self.audit(dict(type='candidate_diagnostic',signal_id=sid,diagnostic=diag))
            self.audit(dict(type='signal_routing',signal_id=sid,result=result))
            self.save()

    def checkpoint(self):
        return throughput_checkpoint(
            strategy_start_ms=self._strategy_start(),now_ms=self.clock(),
            coverage=self.state.get('minute_coverage',{}),
            candidates=self.state.get('candidate_diagnostics',[]))

    def risk_blockers(self):
        result=super().risk_blockers()
        checkpoint=self.checkpoint()
        if checkpoint['stop_new_entries']:
            result.append('throughput_checkpoint_'+checkpoint['status'])
        return result

    def poll(self):
        before_errors=self.state.get('errors',0)
        before_active=self.state.setdefault('poll_diagnostics',{}).get('active_error_period')
        snapshot=super().poll()
        now=self.clock()
        diag=self.state.setdefault('poll_diagnostics',dict(
            poll_error_events=0,error_periods_started=0,recovery_observations=0,
            consecutive_error_polls=0,active_error_period=None,error_periods=[]))
        delta=self.state.get('errors',0)-before_errors
        if delta>0:
            diag['poll_error_events']=diag.get('poll_error_events',0)+delta
            diag['consecutive_error_polls']=diag.get('consecutive_error_polls',0)+1
            if not diag.get('active_error_period'):
                diag['error_periods_started']=diag.get('error_periods_started',0)+1
                diag['active_error_period']=dict(start_ms=now,error_events=delta)
            else:
                diag['active_error_period']['error_events']+=delta
        elif self.state.get('latest_error') is None and before_active:
            period=dict(before_active,end_ms=now)
            periods=diag.setdefault('error_periods',[])
            periods.append(period)
            del periods[:-MAX_ERROR_PERIODS]
            diag['active_error_period']=None
            diag['consecutive_error_polls']=0
            diag['recovery_observations']=diag.get('recovery_observations',0)+1
        self.save()
        return self.snapshot()

    def snapshot(self):
        s=super().snapshot()
        checkpoint=self.checkpoint()
        records=list(self.state.get('candidate_diagnostics',[]))
        raw_ids={item.get('signal_id') for item in records if item.get('signal_id')}
        qualified_ids={item.get('signal_id') for item in records if item.get('cost_qualified') is True}
        admissions=Counter(item.get('admission_reason') or item.get('admission_status') or 'unknown'
                           for item in records)
        s['candidate_diagnostics']=records
        s['throughput']=dict(
            checkpoint=checkpoint,
            raw_candidates=len(raw_ids),
            cost_qualified_opportunities=len(qualified_ids),
            admissions=dict(admissions),
            minute_coverage_records=len(self.state.get('minute_coverage',{})),
            poll_diagnostics=self.state.get('poll_diagnostics',{}),
            cost_qualified_is_fill=False,
            performance_claim=False)
        s['engine']['candidate_implementation']='paper-engine-v4' if self.state.get('deployment') else 'paper-engine-v4-candidate'
        return s


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    lab=Path(__file__).resolve().parent
    p.add_argument('--once',action='store_true')
    p.add_argument('--state-dir',type=Path,required=True)
    p.add_argument('--status',type=Path,required=True)
    p.add_argument('--config',type=Path,default=lab/'paper_config_v4.json')
    p.add_argument('--storage-policy',type=Path)
    a=p.parse_args(argv)
    r=PaperRuntime(a.state_dir,a.config,storage_policy=a.storage_policy)
    try:
        while True:
            try:
                s=r.poll()
            except StorageProtectionHalt as exc:
                print('StorageProtectionHalt: '+str(exc),flush=True)
                return 2
            publish(s,a.status);print(canonical(s),flush=True)
            if s.get('storage_protection',{}).get('stop_after_publish'):
                return 2
            if a.once:
                return 1 if s['latest_error'] else 0
            time.sleep(r.config['execution_model']['poll_interval_seconds'])
    finally:
        r.close()


if __name__=='__main__':
    raise SystemExit(main())
