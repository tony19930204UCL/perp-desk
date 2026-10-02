"""Print a <=8-line factual Traditional Chinese snapshot, never send messages."""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import argparse
import json


def money(value):
    if not isinstance(value, str):
        raise ValueError('financial values must be decimal strings, not floats')
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError('nonfinite financial value')
    return format(number, 'f')


def fresh(snapshot, now):
    def recent(stamp):
        parsed = datetime.fromisoformat(stamp)
        if parsed.utcoffset() is None:
            return False
        return -5 <= (now - parsed).total_seconds() <= 60
    if not snapshot['feed']['connected'] or not recent(snapshot['feed']['last_success_at']) or not recent(snapshot['updated_at']):
        return False
    if {m['symbol'] for m in snapshot['markets']} != {'ETHUSDT', 'XAUUSDT'}:
        return False
    for market in snapshot['markets']:
        if not recent(market['last_received_at']):
            return False
        for endpoint in ('bookTicker', 'premiumIndex', 'depth5'):
            ms = market['source_timestamps_ms'].get(endpoint)
            if ms is None or not -5 <= now.timestamp() - int(ms) / 1000 <= 60:
                return False
    return True


def render(snapshot, now=None):
    try:
        if isinstance(snapshot, dict) and snapshot.get('mode') == 'paper':
            from dashboard import validate_snapshot
            validate_snapshot(snapshot)
            return _render_paper(snapshot, now)
        if not isinstance(snapshot, dict) or snapshot['schema_version'] != 1 or snapshot['mode'] != 'shadow':
            raise ValueError('unsupported snapshot schema/mode')
        if snapshot['paper_trading_enabled'] is not False or snapshot['live_trading_enabled'] is not False:
            raise ValueError('trading enablement inconsistent with observation-only collector')
        if not {'risk_limits_not_set', 'sim_broker_not_implemented', 'signals_not_implemented'}.issubset(snapshot['blockers']):
            raise ValueError('required fail-closed blockers missing')
        if snapshot['feed']['transport'] != 'REST polling':
            raise ValueError('unsupported transport: REST polling required')
        categories = {'ETHUSDT': 'crypto', 'XAUUSDT': 'TradFi'}
        for market in snapshot['markets']:
            if categories.get(market['symbol']) != market['category']:
                raise ValueError('unsupported market category')
        if snapshot['positions'] != [] or snapshot['fills_count'] != 0:
            raise ValueError('unexpected positions or fills: no broker implemented')
        for field in ('initial_equity_usdt', 'cash_usdt', 'equity_usdt', 'total_pnl_usdt', 'realized_pnl_usdt', 'unrealized_pnl_usdt'):
            money(snapshot[field])
        if Decimal(snapshot['initial_equity_usdt']) != Decimal('100'):
            raise ValueError('unexpected canonical initial equity')
        if any(Decimal(snapshot[field]) != 0 for field in ('total_pnl_usdt', 'realized_pnl_usdt', 'unrealized_pnl_usdt')):
            raise ValueError('unexpected PNL: no broker implemented')
        return _render(snapshot, now)
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError) as exc:
        reason = ' '.join(str(exc).split())[:200]
        return ('影子觀察｜100 USDT 僅為 PAPER；實際錢包未知\n'
                f'資料缺失或格式錯誤：{reason}；不推定資產或績效\n'
                '紙上交易／實盤停用；風險限額未設定，模擬券商與訊號未實作')


def _render_paper(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    clean = lambda value: ' '.join(str(value).split())[:200]
    engine = snapshot['engine']
    state = {'warmup': '暖機', 'ready': '就緒', 'running': '運行',
             'halted': '停止', 'error': '錯誤停止'}[engine['status']]
    if engine['status'] == 'warmup' and all(k in engine for k in ('warmup_received', 'warmup_required')):
        state += f" {engine['warmup_received']}/{engine['warmup_required']}"
    traded = '已交易' if snapshot['fills_count'] > 0 else '未交易（成交計數 0）'
    enabled = '啟用' if snapshot['paper_trading_enabled'] else '停用'
    freshness = '新鮮' if fresh(snapshot, now) else '過期／中斷，僅顯示最後快照'
    blockers = '、'.join(clean(b) for b in snapshot['blockers']) or '無'
    accounting = '；'.join(f'{label} {money(snapshot[field])}' for field, label in (
        ('gross_realized_pnl_usdt', '毛已實現'), ('fees_usdt', '費用'),
        ('funding_pnl_usdt', 'funding')) if field in snapshot)
    if accounting:
        accounting = '；' + accounting
    return '\n'.join([
        f"PAPER｜{money(snapshot['initial_equity_usdt'])} USDT 僅為 PAPER；實際錢包未知",
        f"引擎 {state}；{traded}；紙上交易{enabled}／實盤停用",
        f"資料：{freshness}｜REST polling（非 WebSocket）；更新 {clean(snapshot['updated_at'])}",
        f"PAPER 現金 {money(snapshot['cash_usdt'])}／權益 {money(snapshot['equity_usdt'])} USDT",
        f"淨已實現 {money(snapshot['realized_pnl_usdt'])}（含全部費用／funding）／未實現 {money(snapshot['unrealized_pnl_usdt'])}／合計 {money(snapshot['total_pnl_usdt'])} USDT{accounting}",
        f"持倉 {len(snapshot['positions'])}；訊號 {snapshot['signals_count']}、成交 {snapshot['fills_count']}、阻擋 {snapshot['blocked_signals_count']}",
        f"版本 {clean(engine['version_id'])}；風險 {clean(snapshot['risk']['risk_version'])}；前瞻 {clean(engine['forward_start_at'])}",
        f"阻擋原因：{blockers}；錯誤 {clean(snapshot.get('latest_error') or '無')}",
    ])


def _render(snapshot, now=None):
    now = now or datetime.now(timezone.utc)
    is_fresh = fresh(snapshot, now)
    label = '新鮮' if is_fresh else '過期／中斷，僅顯示最後觀察'
    markets = {market['symbol']: market for market in snapshot['markets']}
    lines = [
        f"影子觀察｜{money(snapshot['initial_equity_usdt'])} USDT 僅為 PAPER；實際錢包未知",
        f"資料：{label}｜{snapshot['feed']['transport']}（非 WebSocket）",
        f"PAPER 現金 {money(snapshot['cash_usdt'])}／權益 {money(snapshot['equity_usdt'])} USDT",
        f"記帳損益 {money(snapshot['total_pnl_usdt'])} USDT；成交 {snapshot['fills_count']}、持倉 {len(snapshot['positions'])}；未交易，非績效",
    ]
    for symbol in ('ETHUSDT', 'XAUUSDT'):
        market = markets[symbol]
        lines.append(f"{symbol}（{market['category']}）買 {money(market['bid'])}／賣 {money(market['ask'])}；標記 {money(market['mark_price'])}")
    lines.append('紙上交易停用／實盤停用；風險限額未設定、模擬券商與訊號未實作')
    error = ' '.join(str(snapshot.get('latest_error') or '無').split())[:180]
    lines.append(f"更新 {snapshot['updated_at']}；訊號 {snapshot['signals_count']}（未實作）；錯誤 {error}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', type=Path, default=Path(__file__).resolve().parent / 'shared/status.json')
    args = parser.parse_args(argv)
    try:
        with args.status.open(encoding='utf-8') as stream:
            snapshot = json.load(stream)
    except (OSError, ValueError, UnicodeError):
        snapshot = None
    print(render(snapshot))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
