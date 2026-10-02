"""Read-only flat-to-flat analytics from actual fills and cost ledger.

Never writes account state. Closed statistics exclude open episodes and legacy
entries before this research window. Unsupported reversals or ambiguous cost
attribution are unavailable, never guessed.
"""
from decimal import Decimal as D, localcontext


def money(value):
    if not isinstance(value,str):raise ValueError('amount must be decimal string')
    number=D(value)
    if not number.is_finite():raise ValueError('nonfinite amount')
    return number


def reconcile_account(snapshot):
    """Compare whole-account summaries to the complete cost ledger, not closed trades."""
    ledger=snapshot['cost_ledger']
    if not isinstance(ledger,list):raise ValueError('invalid cost ledger')
    amounts=[]
    for row in ledger:
        if not isinstance(row,dict) or row.get('type') not in ('realized','fee','funding'):
            raise ValueError('invalid cost ledger row')
        amounts.append((row['type'],money(row['amount'])))
    numbers=[n for _,n in amounts]+[money(snapshot[k]) for k in ('gross_realized_pnl_usdt','fees_usdt','funding_pnl_usdt','realized_pnl_usdt') if k in snapshot]+[D(0)]
    with localcontext() as ctx:
        ctx.prec=max(60,max(n.adjusted() for n in numbers)-min(n.as_tuple().exponent for n in numbers)+len(str(len(numbers)))+3)
        gross=sum((n for kind,n in amounts if kind=='realized'),D(0))
        fees=-sum((n for kind,n in amounts if kind=='fee'),D(0))
        funding=sum((n for kind,n in amounts if kind=='funding'),D(0))
        for key,expected in [('gross_realized_pnl_usdt',gross),('fees_usdt',fees),('funding_pnl_usdt',funding),('realized_pnl_usdt',gross-fees+funding)]:
            if key in snapshot and money(snapshot[key])!=expected:
                raise ValueError('account/ledger mismatch: '+key)


def analyze(snapshot, *, now_ms):
    result=dict(available=False,closed_trades=[],statistics=None,remaining_ms=None,error=None)
    try:
        with localcontext() as ctx:
            ctx.prec=60
            research=snapshot.get('research')
            if not isinstance(research,dict):raise ValueError('research window missing')
            start=research['strategy_start_ms']
            deadline=research['deadline_ms']
            if type(start) is not int or start<0 or type(deadline) is not int or deadline<=start:
                raise ValueError('invalid research times')
            if type(research.get('complete_round_trips')) is not int or research['complete_round_trips']<0:
                raise ValueError('invalid research sample count')
            result['remaining_ms']=max(0,deadline-now_ms) if deadline is not None else None
            fills=snapshot['fills'];ledger=snapshot['cost_ledger']
            reconcile_account(snapshot)
            active={};episodes=[];fill_map={};exit_orders={};previous=-1
            for fill in fills:
                ts=fill['ts'];symbol=fill['symbol'];fid=fill['fill_id']
                if type(ts) is not int or ts<previous or not isinstance(symbol,str) or not symbol or fid in fill_map:
                    raise ValueError('invalid/duplicate/unsorted fills')
                previous=ts;fill_map[fid]=fill
                qty=money(fill['qty']);price=money(fill['price']);fee=money(fill['fee'])
                if qty<=0 or price<=0 or fee<0 or fill['side'] not in ('BUY','SELL'):
                    raise ValueError('invalid fill values')
                sign=1 if fill['side']=='BUY' else -1
                episode=active.get(symbol)
                if episode is None:
                    episode=dict(symbol=symbol,direction='LONG' if sign==1 else 'SHORT',sign=sign,opened_ms=ts,closed_ms=None,balance=D(0),entry_qty=D(0),exit_qty=D(0),entry_value=D(0),exit_value=D(0),fees=D(0),gross=D(0),funding=D(0),fill_ids=[],exit_order_ids=set())
                    active[symbol]=episode;episodes.append(episode)
                episode['fill_ids'].append(fid)
                if sign==episode['sign']:
                    episode['entry_qty']+=qty;episode['entry_value']+=qty*price;episode['balance']+=qty
                else:
                    if qty>episode['balance']:raise ValueError('position reversal attribution unsupported')
                    episode['exit_qty']+=qty;episode['exit_value']+=qty*price;episode['balance']-=qty
                    oid=fill['order_id'];old=exit_orders.get(oid)
                    if old is not None and old is not episode:raise ValueError('exit order crosses episodes')
                    exit_orders[oid]=episode;episode['exit_order_ids'].add(oid)
                    if episode['balance']==0:
                        episode['closed_ms']=ts;del active[symbol]
            fees={fid:D(0) for fid in fill_map}
            realized_orders=set();ledger_ids=set();funding_events=set()
            for item in ledger:
                for field,seen in [('ledger_id',ledger_ids),('event_id',funding_events)]:
                    if field in item and (field=='ledger_id' or item.get('type')=='funding'):
                        identifier=item[field]
                        if not isinstance(identifier,str) or not identifier or identifier in seen:
                            raise ValueError('invalid/duplicate '+field)
                        seen.add(identifier)
                amount=money(item['amount']);kind=item['type']
                if kind=='fee':
                    fid=item['fill_id']
                    if fid not in fill_map:raise ValueError('fee lacks matching fill')
                    fees[fid]-=amount
                elif kind=='realized':
                    oid=item['order_id']
                    if oid not in exit_orders:raise ValueError('realized ledger lacks matching exit')
                    exit_orders[oid]['gross']+=amount;realized_orders.add(oid)
                elif kind=='funding':
                    if amount==0:continue
                    ts=item['ts']
                    if type(ts) is not int:raise ValueError('invalid funding time')
                    matches=[e for e in episodes if e['symbol']==item['symbol'] and e['opened_ms']<=ts and (e['closed_ms'] is None or ts<=e['closed_ms'])]
                    if len(matches)!=1:raise ValueError('ambiguous/unmatched funding attribution')
                    matches[0]['funding']+=amount
                else:raise ValueError('unsupported ledger cost type')
            for e in episodes:
                for fid in e['fill_ids']:
                    if fees[fid]!=money(fill_map[fid]['fee']):raise ValueError('fill/ledger fee mismatch')
                    e['fees']+=fees[fid]
                if e['exit_order_ids']-realized_orders:raise ValueError('exit realized ledger missing')
            trades=[]
            for e in episodes:
                if e['closed_ms'] is None or e['opened_ms']<start:continue
                trades.append(dict(symbol=e['symbol'],direction=e['direction'],opened_ms=e['opened_ms'],closed_ms=e['closed_ms'],entry_price=str(e['entry_value']/e['entry_qty']),exit_price=str(e['exit_value']/e['exit_qty']),qty=str(e['entry_qty']),gross_pnl_usdt=str(e['gross']),fees_usdt=str(e['fees']),funding_pnl_usdt=str(e['funding']),net_pnl_usdt=str(e['gross']-e['fees']+e['funding']),holding_ms=e['closed_ms']-e['opened_ms']))
            if research and len(trades)!=research['complete_round_trips']:
                raise ValueError('research round-trip count does not match reconstructed fills')
            n=len(trades);nets=[money(t['net_pnl_usdt']) for t in trades]
            gross=sum((money(t['gross_pnl_usdt']) for t in trades),D(0));fee=sum((money(t['fees_usdt']) for t in trades),D(0))
            statistics=dict(samples=n,wins=sum(v>0 for v in nets),losses=sum(v<0 for v in nets),breakeven=sum(v==0 for v in nets),win_rate_percent=str(D(sum(v>0 for v in nets))*100/n) if n else None,mean_net_pnl_usdt=str(sum(nets,D(0))/n) if n else None,fee_to_gross_percent=str(fee*100/gross) if n and gross>0 else None)
            result.update(available=True,closed_trades=sorted(trades,key=lambda t:t['closed_ms'],reverse=True),statistics=statistics,open_episodes=len(active),scope='研究窗口完整flat-to-flat交易，PnL使用實際成本ledger，均價按實際成交量加權')
    except (KeyError,ValueError,TypeError,ArithmeticError) as exc:
        result['error']=str(exc)
    return result
