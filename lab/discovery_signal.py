"""Causal 1m signal/target evaluator for Issue #23 staged ETH discovery arms."""
from decimal import Decimal, Context, ROUND_HALF_EVEN, localcontext
import hashlib, json

STEP=60000
PRIOR_RETURNS=60
TARGET_BARS=15

def _validate_bar(bar):
    if not isinstance(bar,dict):
        raise ValueError('bar required')
    keys=('open_time_ms','close_time_ms','open','high','low','close','volume')
    if any(k not in bar for k in keys):
        raise ValueError('bar fields missing')
    if type(bar['open_time_ms']) is not int or type(bar['close_time_ms']) is not int or bar['close_time_ms']!=bar['open_time_ms']+STEP:
        raise ValueError('invalid bar timestamps')
    vals={k:Decimal(str(bar[k])) for k in ('open','high','low','close','volume')}
    if any(not vals[k].is_finite() for k in vals):
        raise ValueError('nonfinite bar')
    if any(vals[k]<=0 for k in ('open','high','low','close')) or vals['volume']<0:
        raise ValueError('invalid bar values')
    if vals['low']>min(vals['open'],vals['close']) or vals['high']<max(vals['open'],vals['close']):
        raise ValueError('invalid OHLC')
    return dict(bar,**vals)

def causal_vwma(prior15):
    if not isinstance(prior15,list) or len(prior15)!=TARGET_BARS:
        raise ValueError('target requires exactly 15 prior bars')
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)):
        total=Decimal(0);weighted=Decimal(0)
        for i,b in enumerate(prior15):
            b=_validate_bar(b)
            if i and b['open_time_ms']!=prior15[i-1]['open_time_ms']+STEP:
                raise ValueError('noncontiguous target bars')
            total+=b['volume'];weighted+=b['close']*b['volume']
        if total<=0:raise ValueError('zero target volume')
        return weighted/total

def evaluate(prior61,signal_bar,*,version_id='ETH-DISCOVERY-LAB-001'):
    """Return causal long/short candidates; no broker side effects."""
    if not isinstance(prior61,list) or len(prior61)!=61:
        raise ValueError('requires exactly 61 prior bars')
    prior=[_validate_bar(x) for x in prior61]
    bar=_validate_bar(signal_bar)
    for a,b in zip(prior,prior[1:]):
        if b['open_time_ms']!=a['open_time_ms']+STEP:
            raise ValueError('noncontiguous return bars')
    if bar['open_time_ms']!=prior[-1]['open_time_ms']+STEP:
        raise ValueError('signal bar not contiguous')
    target=causal_vwma(prior[-15:])
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)) as ctx:
        returns=[b['close']/a['close']-1 for a,b in zip(prior,prior[1:])]
        mean=sum(returns,Decimal(0))/Decimal(60)
        stdev=(sum((r-mean)**2 for r in returns)/Decimal(59)).sqrt()
        volumes=sorted(b['volume'] for b in prior[-60:])
        median=(volumes[29]+volumes[30])/2
        current=bar['close']/prior[-1]['close']-1
        vol_ok=bar['volume']>Decimal('1.2')*median
        long_hit=vol_ok and current<mean-Decimal('1.5')*stdev
        short_hit=vol_ok and current>mean+Decimal('1.5')*stdev
        features=dict(mean=str(mean),sample_stdev=str(stdev),median_volume=str(median),
                      current_return=str(current),downside_threshold=str(mean-Decimal('1.5')*stdev),
                      upside_threshold=str(mean+Decimal('1.5')*stdev),volume_threshold=str(Decimal('1.2')*median),
                      current_volume=str(bar['volume']),target=str(target),target_bars=15,
                      target_excludes_signal_bar=True,target_frozen=True,
                      decimal_precision='50',rounding=ctx.rounding,
                      signal_open_ms=bar['open_time_ms'],signal_close_ms=bar['close_time_ms'])
    out=[]
    for direction,hit in (('long',long_hit),('short',short_hit)):
        if hit:
            raw=json.dumps([version_id,'ETHUSDT',bar['open_time_ms'],direction],separators=(',',':'))
            out.append(dict(signal_id=hashlib.sha256(raw.encode()).hexdigest(),symbol='ETHUSDT',
                            direction=direction,target=str(target),entry_reference=str(prior[-1]['close']),
                            features=features))
    return out
