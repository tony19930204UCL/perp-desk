"""Isolated PAPER learning-speed candidate. Public REST only."""
import argparse, time
from pathlib import Path
from paper_runtime_v2 import PaperRuntime as BaseRuntime, publish, canonical
from signals_v3 import Detector
from decimal import Decimal as D
from collections import Counter

from sim_broker import _exact_context
from storage_protection import StorageGuard, StorageProtectionHalt, load_policy

@_exact_context
def research_metrics(fills,ledger,*,baseline_fills,baseline_ledger):
    quantities={}; complete=0
    for f in fills[baseline_fills:]:
        symbol=f['symbol']; before=quantities.get(symbol,D(0))
        after=before+D(f['qty'])*(1 if f['side']=='BUY' else -1)
        if before and after==0: complete+=1
        quantities[symbol]=after
    values=ledger[baseline_ledger:]
    return dict(complete_round_trips=complete,open_episodes=sum(q!=0 for q in quantities.values()),gross_realized_pnl_usdt=str(sum((D(x['amount']) for x in values if x['type']=='realized'),D(0))),fees_usdt=str(-sum((D(x['amount']) for x in values if x['type']=='fee'),D(0))),funding_pnl_usdt=str(sum((D(x['amount']) for x in values if x['type']=='funding'),D(0))),net_cash_pnl_usdt=str(sum((D(x['amount']) for x in values),D(0))))

class PaperRuntime(BaseRuntime):
    CONFIG_HASH = '622b8de7d554d36ae77f913944746c8c32b5ea00e59e8385fe5716553fe6a069'
    DETECTOR = Detector
    EXTRA_SOURCES = ('paper_runtime_v3.py','signals_v3.py','storage_protection.py')
    INTERVAL_MS = 60000
    INTERVAL_NAME = '1m'
    def __init__(self,*args,storage_policy=None,storage_probe=None,**kwargs):
        self.storage_guard=None
        self._storage_status=None
        self._storage_stop_after_publish=False
        self._storage_cycle_start_bytes=None
        root=Path(args[0] if args else kwargs.get('root')).resolve()
        if storage_policy is not None:
            policy=load_policy(storage_policy) if isinstance(storage_policy,(str,Path)) else storage_policy
            self.storage_guard=StorageGuard(root,policy,probe=storage_probe)
        super().__init__(*args,**kwargs)
        self.state.setdefault('strategy_start_ms',self.state['forward_start_ms'])
        self.state.setdefault('research_deadline_ms',self.state['strategy_start_ms']+172800000)
        self.state.setdefault('strategy_fill_baseline',0)
        self.state.setdefault('strategy_ledger_baseline',0)
        self.state.setdefault('strategy_signal_baseline',[])
        self.save()
        try:
            if (self.root/'broker.sqlite3').exists():
                import sqlite3,json
                from contextlib import closing
                with closing(sqlite3.connect(self.root/'broker.sqlite3')) as db:
                    row=db.execute('SELECT payload FROM sim_broker_state WHERE singleton=1').fetchone()
                saved=json.loads(row[0]);i=saved['meta']['instruments'][0]
                self.ensure_broker(dict(symbol=i['symbol'],maker_fee=i['maker_fee'],taker_fee=i['taker_fee'],qty_step=i['quantity_step'],tick_size=i['tick'],min_notional=i['min_notional'],max_qty=i['max_quantity'],min_qty=i['min_quantity'],category=i['category']))
        except Exception:
            self.close()
            raise
    def risk_blockers(self):
        result=super().risk_blockers()
        if self.clock()>=self.state.get('research_deadline_ms',self.state['forward_start_ms']+172800000):
            result.append('research_window_closed')
        if self.storage_guard:
            self._storage_status=self.storage_guard.observe()
            if not self._storage_status['new_risk_allowed']:
                result.append('storage_new_risk_inhibited')
        return result

    def _pending_entry_orders(self):
        if not self.broker:
            return []
        return [o for o in self.broker.orders.values()
                if not o['intent']['reduce_only'] and o['status'] in ('PENDING','RESTING')]

    def _storage_exposed(self):
        return bool(self.broker and (self.broker.positions or any(
            o['intent']['reduce_only'] and o['status'] in ('PENDING','RESTING')
            for o in self.broker.orders.values())))

    def _storage_cancel_pending_entries(self,state):
        pending=self._pending_entry_orders()
        if not pending:
            return state
        if not state['exit_accounting_cycle_allowed']:
            raise StorageProtectionHalt(
                'pending entry cannot be durably canceled before further market delivery')
        for order in pending:
            self.broker.cancel(order['order_id'],ts=max(self.clock(),self.broker.last_ts))
        state=self.storage_guard.observe()
        self._storage_status=state
        return state

    def _storage_before_durable_runtime_write(self,context):
        if not self.storage_guard:
            return None
        state=self.storage_guard.observe()
        self._storage_status=state
        if state['new_risk_allowed']:
            return state
        if self._storage_exposed() and state['exit_accounting_cycle_allowed']:
            return state
        raise StorageProtectionHalt(
            context+': storage protection refuses further non-exit durable growth; '+','.join(state['reasons']))

    def audit(self,event):
        if self.storage_guard:
            self._storage_before_durable_runtime_write('runtime audit')
        return super().audit(event)

    def emit(self,event):
        if self.storage_guard:
            state=self.storage_guard.observe()
            self._storage_status=state
            if not state['new_risk_allowed']:
                state=self._storage_cancel_pending_entries(state)
            if self._storage_exposed() and not state['exit_accounting_cycle_allowed']:
                raise StorageProtectionHalt(
                    'broker market delivery blocked: durable exit/accounting headroom unavailable')
        return super().emit(event)

    def _storage_preflight(self):
        if not self.storage_guard:
            return None
        state=self.storage_guard.observe()
        self._storage_status=state
        if state['new_risk_allowed']:
            return state
        state=self._storage_cancel_pending_entries(state)
        exposed=self._storage_exposed()
        if exposed:
            self.storage_guard.require_exit_cycle('open PAPER exposure')
            self._storage_cycle_start_bytes=state['used_bytes']
            return state
        if not state['exit_accounting_cycle_allowed']:
            raise StorageProtectionHalt(
                'flat PAPER runtime reached storage hard stop without safe durable-write headroom')
        self._storage_stop_after_publish=True
        self._storage_cycle_start_bytes=state['used_bytes']
        return state

    def exit_policy(self,now,mark):
        if self.storage_guard and self.broker and self.broker.positions:
            self.storage_guard.require_exit_cycle('reduce-only exit')
        return super().exit_policy(now,mark)

    def poll(self):
        if not self.storage_guard:
            return super().poll()
        self._storage_stop_after_publish=False
        self._storage_cycle_start_bytes=None
        pre=self._storage_preflight()
        if self._storage_stop_after_publish:
            snapshot=self.snapshot()
            snapshot['storage_protection']['stop_after_publish']=True
            return snapshot
        try:
            snapshot=super().poll()
        except Exception as exc:
            self._storage_status=self.storage_guard.observe()
            raise StorageProtectionHalt(
                'durable PAPER write failed or runtime could not complete protected cycle: '
                +type(exc).__name__+': '+str(exc)) from exc
        post=self.storage_guard.observe()
        if self._storage_cycle_start_bytes is not None:
            growth=post['used_bytes']-self._storage_cycle_start_bytes
            post['observed_cycle_growth_bytes']=growth
            if growth>post['max_exit_cycle_bytes']:
                post['level']='halt'
                post['reasons']=list(dict.fromkeys(post['reasons']+['exit_cycle_growth_bound_exceeded']))
                post['new_risk_allowed']=False
                post['exit_accounting_cycle_allowed']=False
                self._storage_stop_after_publish=True
        self._storage_status=post
        snapshot['storage_protection']=dict(post,stop_after_publish=self._storage_stop_after_publish)
        return snapshot
    def snapshot(self):
        s=super().snapshot()
        start=self.state.get('strategy_start_ms',self.state['forward_start_ms'])
        metrics=research_metrics(s['fills'],s['cost_ledger'],baseline_fills=self.state.get('strategy_fill_baseline',0),baseline_ledger=self.state.get('strategy_ledger_baseline',0))
        old=set(self.state.get('strategy_signal_baseline',[]))
        signals={k:v for k,v in self.state.get('handled_signals',{}).items() if k not in old}
        blocked={}
        for k,v in signals.items():
            if v['status']=='rejected': blocked[k]=v.get('reason','unknown')
        for o in s['orders']:
            if o['intent_id'] in signals and o['status'] in ('REJECTED','EXPIRED'):
                blocked[o['intent_id']]=o.get('reason','unknown')
        deadline=self.state.get('research_deadline_ms',start+172800000)
        status='insufficient_samples' if self.clock()>=deadline and metrics['complete_round_trips']<30 else 'review_due_unproven' if self.clock()>=deadline else 'unproven'
        s['research']=dict(strategy_start_ms=start,deadline_ms=deadline,target_complete_round_trips=30,target_not_guarantee=True,status=status,signals_count=len(signals),blocked_signals_count=len(blocked),rejection_categories=dict(Counter(blocked.values())),**metrics)
        s['engine']['candidate_implementation']='paper-engine-v3' if self.state.get('deployment') else 'paper-engine-v3-candidate'
        if self.storage_guard:
            status=self._storage_status or self.storage_guard.observe()
            s['storage_protection']=dict(status,stop_after_publish=self._storage_stop_after_publish)
        return s

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    lab=Path(__file__).resolve().parent
    p.add_argument('--once',action='store_true')
    p.add_argument('--state-dir',type=Path,required=True)
    p.add_argument('--status',type=Path,required=True)
    p.add_argument('--config',type=Path,default=lab/'paper_config_v3.json')
    p.add_argument('--storage-policy',type=Path,
                   help='operator-owned storage policy; omitted means legacy behavior with no capacity enforcement')
    a=p.parse_args(argv)
    r=PaperRuntime(a.state_dir,a.config,storage_policy=a.storage_policy)
    try:
        while True:
            try:
                s=r.poll()
            except StorageProtectionHalt as exc:
                print('StorageProtectionHalt: '+str(exc),flush=True)
                return 2
            publish(s,a.status); print(canonical(s),flush=True)
            if s.get('storage_protection',{}).get('stop_after_publish'):
                return 2
            if a.once: return 1 if s['latest_error'] else 0
            time.sleep(r.config['execution_model']['poll_interval_seconds'])
    finally: r.close()
if __name__=='__main__': raise SystemExit(main())
