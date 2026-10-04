"""Operator-preregistered H1-PAPER-004 detector.

The downside/volume trigger is inherited unchanged from H1-PAPER-003. Only the
reversion target changes to the causal volume-weighted close of the fifteen
immediately preceding closed 1m bars (a VWMA proxy, not trade-level VWAP).
"""
from datetime import datetime, timezone
from decimal import Decimal, Context, ROUND_HALF_EVEN, localcontext
from signals_v3 import Detector as BaseDetector

STEP=60000
TARGET_BARS=15


def causal_vwma_target(prior_bars, signal_open_ms):
    """Return frozen Decimal target from exactly the 15 prior contiguous closed bars."""
    if type(signal_open_ms) is not int or signal_open_ms < TARGET_BARS*STEP:
        raise ValueError('invalid signal open time')
    if not isinstance(prior_bars,list) or len(prior_bars)!=TARGET_BARS:
        raise ValueError('target requires exactly 15 preceding bars')
    expected_start=signal_open_ms-TARGET_BARS*STEP
    total_volume=Decimal(0)
    weighted=Decimal(0)
    with localcontext(Context(prec=50,rounding=ROUND_HALF_EVEN)) as ctx:
        for index,bar in enumerate(prior_bars):
            expected_open=expected_start+index*STEP
            if (not isinstance(bar,dict) or bar.get('closed') is not True
                    or type(bar.get('open_time_ms')) is not int
                    or type(bar.get('close_time_ms')) is not int
                    or bar['open_time_ms']!=expected_open
                    or bar['close_time_ms']!=expected_open+STEP):
                raise ValueError('target bars missing/noncontiguous/not closed')
            close=bar.get('close');volume=bar.get('volume')
            if (not isinstance(close,Decimal) or not close.is_finite() or close<=0
                    or not isinstance(volume,Decimal) or not volume.is_finite() or volume<0):
                raise ValueError('target bars contain invalid close/volume')
            weighted += close*volume
            total_volume += volume
        if total_volume<=0:
            raise ValueError('target bars have zero total volume')
        target=weighted/total_volume
        return dict(
            value=target,
            decimal_precision='50',
            rounding=ctx.rounding,
            bars=TARGET_BARS,
            total_volume=total_volume,
            source_open_times_ms=[b['open_time_ms'] for b in prior_bars],
            source_close_times_ms=[b['close_time_ms'] for b in prior_bars],
            source_closes=[str(b['close']) for b in prior_bars],
            source_volumes=[str(b['volume']) for b in prior_bars])


class Detector(BaseDetector):
    interval_ms=STEP
    interval_name='1m'
    downside_sigma=Decimal('1.5')
    volume_multiple=Decimal('1.2')

    def _process(self,bar,*,now,session=None):
        result=super()._process(bar,now=now,session=session)
        if result.get('diagnostic')!='research_intent' or result.get('intent') is None:
            return result
        intent=result['intent']
        prior15=self._bars[bar['symbol']][-16:-1]
        features=intent['features']
        epoch=datetime(1970,1,1,tzinfo=timezone.utc)
        delta=now-epoch
        detection_ms=(delta.days*86400+delta.seconds)*1000+delta.microseconds//1000
        features.update(
            detection_ms=str(detection_ms),
            detection_timestamp_domain='runtime_utc_wall_input',
            target_method='prior_15_closed_1m_volume_weighted_close_vwma_proxy',
            target_excludes_signal_bar=True,
            target_frozen=True)
        try:
            target=causal_vwma_target(prior15,bar['open_time_ms'])
        except ValueError as exc:
            intent['reversion_target']=None
            features.update(target_eligible=False,target_error=str(exc),
                            target_decimal_precision='50')
            return result
        intent['reversion_target']=str(target['value'])
        features.update(
            target_eligible=True,
            target_error=None,
            target_decimal_precision=target['decimal_precision'],
            target_rounding=target['rounding'],
            target_bars=str(target['bars']),
            target_total_volume=str(target['total_volume']),
            target_source_open_times_ms=[str(x) for x in target['source_open_times_ms']],
            target_source_close_times_ms=[str(x) for x in target['source_close_times_ms']],
            target_source_closes=target['source_closes'],
            target_source_volumes=target['source_volumes'])
        return result
