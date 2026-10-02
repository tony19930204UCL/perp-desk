"""Isolated PAPER learning-speed candidate. Public REST only."""
import argparse, time
from pathlib import Path
from paper_runtime_v2 import PaperRuntime as BaseRuntime, publish, canonical
from signals_v3 import Detector
from decimal import Decimal as D
from collections import Counter

from sim_broker import _exact_context

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
    EXTRA_SOURCES = ('paper_runtime_v3.py','signals_v3.py')
    INTERVAL_MS = 60000
    INTERVAL_NAME = '1m'
    def __init__(self,*args,**kwargs):
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
        return result
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
        return s

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    lab=Path(__file__).resolve().parent
    p.add_argument('--once',action='store_true')
    p.add_argument('--state-dir',type=Path,required=True)
    p.add_argument('--status',type=Path,required=True)
    p.add_argument('--config',type=Path,default=lab/'paper_config_v3.json')
    a=p.parse_args(argv)
    r=PaperRuntime(a.state_dir,a.config)
    try:
        while True:
            s=r.poll(); publish(s,a.status); print(canonical(s),flush=True)
            if a.once: return 1 if s['latest_error'] else 0
            time.sleep(r.config['execution_model']['poll_interval_seconds'])
    finally: r.close()
if __name__=='__main__': raise SystemExit(main())
