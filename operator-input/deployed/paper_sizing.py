"""Frozen PAPER-RISK-001 sizing. No exchange orders or private access."""
from decimal import Decimal, localcontext, ROUND_FLOOR


def size_long(config, instrument, *, equity, bid, ask, target):
    try:
        if config.get('approved') is not True or config.get('mode') != 'paper':
            return {'status': 'rejected', 'reason': 'unapproved_paper_config'}
        values = [equity, bid, ask, target, instrument['tick_size'], instrument['qty_step'],
                  instrument['min_qty'], instrument['max_qty'], instrument['min_notional'],
                  config['max_loss_per_trade_usdt'], config['max_effective_exposure_x']]
        if any(not isinstance(x, str) or not Decimal(x).is_finite() or Decimal(x) <= 0 for x in values):
            return {'status': 'rejected', 'reason': 'invalid_positive_decimal'}
        if Decimal(bid) >= Decimal(ask):
            return {'status': 'rejected', 'reason': 'crossed_or_locked_book'}
        return _size_long(config, instrument, equity=equity, bid=bid, ask=ask, target=target)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        return {'status': 'rejected', 'reason': 'invalid_sizing_input'}


def _size_long(config, instrument, *, equity, bid, ask, target):
    with localcontext() as ctx:
        ctx.prec = 50
        e, b, a, goal = map(Decimal, (equity, bid, ask, target))
        tick = Decimal(instrument['tick_size'])
        step = Decimal(instrument['qty_step'])
        fee = Decimal(instrument['taker_fee'])
        entry = a + tick * config['execution_model']['entry_slippage_ticks']
        stop_raw = entry * (1 - Decimal(config['strategy']['stop_distance_fraction']))
        stop = (stop_raw / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        exit_bound = stop - tick * config['execution_model']['exit_slippage_ticks']
        planned_loss = entry - exit_bound + fee * (entry + exit_bound)
        exposure = e * Decimal(config['max_effective_exposure_x'])
        raw = min(Decimal(config['max_loss_per_trade_usdt']) / planned_loss,
                  exposure / entry, Decimal(instrument['max_qty']))
        from fractions import Fraction
        from math import lcm
        market_step = Decimal(instrument.get('market_qty_step', '0'))
        if not market_step.is_finite() or market_step < 0:
            return {'status': 'rejected', 'reason': 'invalid_market_step'}
        if market_step > 0:
            left, right = Fraction(step), Fraction(market_step)
            denominator = lcm(left.denominator, right.denominator)
            units = lcm(left.numerator * (denominator // left.denominator),
                        right.numerator * (denominator // right.denominator))
            step = Decimal(units) / Decimal(denominator)
        qty = (raw / step).to_integral_value(rounding=ROUND_FLOOR) * step
        cost = (a-b) + tick * (config['execution_model']['entry_slippage_ticks'] +
                              config['execution_model']['exit_slippage_ticks']) + fee * (entry+goal)
        if (exit_bound <= 0 or planned_loss <= 0 or fee < 0 or not fee.is_finite()
                or qty < Decimal(instrument['min_qty'])
                or qty * entry < Decimal(instrument['min_notional'])):
            return {'status': 'rejected', 'reason': 'below_filters_or_invalid_loss'}
        if goal - entry <= cost * Decimal(config['strategy']['minimum_gross_reward_to_estimated_cost']):
            return {'status': 'rejected', 'reason': 'insufficient_reward_after_costs'}
        return {'status': 'accepted', 'qty': str(qty), 'execution_qty_step': str(step), 'stop_price': str(stop),
                'entry_price_bound': str(entry), 'planned_loss_per_unit': str(planned_loss),
                'estimated_round_trip_cost_per_unit': str(cost), 'target': str(goal)}
