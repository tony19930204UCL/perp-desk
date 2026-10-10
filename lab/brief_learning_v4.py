#!/usr/bin/env python
"""Eight-line PAPER brief for H2 and F1H candidates."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path

LAB = Path(__file__).resolve().parent
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))

from accept_learning_v3 import verify_freshness
from dashboard import validate_snapshot


_TWO_PLACES = Decimal('0.01')


def fmt_usdt(value):
    """Format a USDT amount as a plain decimal string with exactly 2 decimal places.

    Accepts str, int, or Decimal.  Uses ROUND_HALF_EVEN.  Negative zero is
    normalised to '0.00'.
    """
    try:
        d = Decimal(str(value))
    except Exception:
        d = Decimal('0')
    result = d.quantize(_TWO_PLACES, rounding=ROUND_HALF_EVEN)
    # suppress negative zero
    if result == Decimal('0'):
        result = Decimal('0.00')
    return str(result)


def validate_brief_snapshot(s):
    validate_snapshot(s)
    if s.get('fixture'):
        raise ValueError('fixture snapshot rejected')
    if s.get('mode') != 'paper':
        raise ValueError(f"mode paper required, got {s.get('mode')}")
    engine = s.get('engine')
    if not isinstance(engine, dict):
        raise ValueError('missing engine proof')
    vid = engine.get('version_id')
    if not isinstance(vid, str) or not vid.strip():
        raise ValueError('missing version identifier')
    if vid == 'H1-PAPER-003':
        raise ValueError('H1-PAPER-003 rejected: v4 brief accepts only H2-PAPER- or F1H- versions')
    if not (vid.startswith('H2-PAPER-') or vid.startswith('F1H-')):
        raise ValueError(f'unsupported version {vid}: v4 brief accepts only H2-PAPER- or F1H- versions')
    if 'research' not in s or not isinstance(s['research'], dict):
        raise ValueError('missing research section')


def check_freshness(s, now_ms=None):
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    unavailable = None
    try:
        verify_freshness(s, now_ms=now_ms)
    except ValueError as exc:
        unavailable = str(exc)
    if s.get('latest_error'):
        unavailable = (unavailable + '; ' if unavailable else '') + s['latest_error']
    elif not s.get('feed', {}).get('connected'):
        unavailable = (unavailable + '; ' if unavailable else '') + 'feed disconnected'
    return unavailable


def format_days(days):
    rounded = round(days, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.1f}"


def render_single(s, *, unavailable_reason=None, now_ms=None):
    r = s['research']
    categories = ', '.join(f'{k}:{v}' for k, v in sorted(r.get('rejection_categories', {}).items())) or '無'
    deadline_ms = r.get('deadline_ms', 0)
    start_ms = r.get('strategy_start_ms', 0)
    target = r.get('target_complete_round_trips', 30)

    deadline_dt = datetime.fromtimestamp(deadline_ms / 1000, timezone.utc)
    deadline_iso = deadline_dt.isoformat()

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    window_ms = deadline_ms - start_ms
    window_days = window_ms / 86400000
    elapsed_days = max(0.0, (now_ms - start_ms) / 86400000)
    remaining_days = max(0.0, (deadline_ms - now_ms) / 86400000)
    is_expired = now_ms >= deadline_ms

    if is_expired:
        window_part = f"窗口 {format_days(window_days)} 天，已過 {format_days(elapsed_days)} 天，剩餘 0 天，窗口已過期"
    else:
        window_part = f"窗口 {format_days(window_days)} 天，已過 {format_days(elapsed_days)} 天，剩餘 {format_days(remaining_days)} 天"

    version_id = s.get('engine', {}).get('version_id', '未知')
    blockers = ', '.join(s.get('blockers', [])) or '無'

    line1 = (
        f"PAPER {version_id} 即時估值不可用 ({unavailable_reason})"
        if unavailable_reason
        else f"PAPER {version_id} 尚未證明優勢 ({r.get('status', 'unproven')})"
    )

    prefix = (
        f"最後已驗證快照 {s.get('updated_at', '')} (僅schema/資產等式，非即時) "
        if unavailable_reason
        else ''
    )
    line2 = prefix + f"本金 {fmt_usdt(s.get('initial_equity_usdt', '0'))}，權益 {fmt_usdt(s.get('equity_usdt', '0'))} USDT，累計淨損益 {fmt_usdt(s.get('total_pnl_usdt', '0'))}"
    line3 = f"新窗口完整往返 {r.get('complete_round_trips', 0)}/{target} ({window_part}，不保證)"
    line4 = f"訊號 {r.get('signals_count', 0)}，受阻 {r.get('blocked_signals_count', 0)}，{categories}"
    line5 = f"新窗口扣全成本現金損益 {fmt_usdt(r.get('net_cash_pnl_usdt', '0'))} USDT"
    line6 = f"帳戶累計費用 {fmt_usdt(s.get('fees_usdt', '0'))}，funding {fmt_usdt(s.get('funding_pnl_usdt', '0'))}"
    line7 = f"行情/執行阻擋 {blockers}，截止 {deadline_iso}"
    line8 = '學習：樣本不足不下結論，不為達標強迫成交。真錢停用。'

    lines = [line1, line2, line3, line4, line5, line6, line7, line8]
    return '\n'.join(' '.join(line.splitlines()) for line in lines)


def render_family(arms, *, now_ms=None):
    normalized = []
    for arm in arms:
        if isinstance(arm, dict) and 'engine' in arm and 'snapshot' not in arm:
            vid = arm.get('engine', {}).get('version_id', '未知')
            normalized.append({'version': vid, 'unavailable': None, 'snapshot': arm})
        else:
            normalized.append(arm)

    running_arms = [a for a in normalized if not a.get('unavailable')]
    unavailable_arms = [a for a in normalized if a.get('unavailable')]
    running_count = len(running_arms)
    unavailable_count = len(unavailable_arms)

    total_complete = 0
    total_net_cash = Decimal('0')
    for a in running_arms:
        snap = a.get('snapshot')
        if snap and isinstance(snap.get('research'), dict):
            r = snap['research']
            total_complete += r.get('complete_round_trips', 0)
            pnl_str = str(r.get('net_cash_pnl_usdt', '0'))
            try:
                total_net_cash += Decimal(pnl_str)
            except (ArithmeticError, ValueError):
                pass

    line1 = (
        f"PAPER 家族結論：{running_count} 運作中，{unavailable_count} 不可用，"
        f"合計完整往返 {total_complete}，累計淨現金損益 {fmt_usdt(total_net_cash)} USDT（分支相關非獨立證據）"
    )

    arm_lines = []
    for a in normalized:
        vid = a.get('version', '未知')
        unavail = a.get('unavailable')
        if unavail:
            arm_lines.append(f"{vid}: 不可用 ({unavail})")
        else:
            s = a.get('snapshot', {})
            r = s.get('research', {})
            equity = s.get('equity_usdt', '0')
            complete = r.get('complete_round_trips', 0)
            open_episodes = r.get('open_episodes', 0)
            blockers = ', '.join(s.get('blockers', [])) or '無'
            arm_lines.append(
                f"{vid}: 權益 {fmt_usdt(equity)} USDT，完整往返 {complete}，未平倉分集 {open_episodes}，阻擋 {blockers}"
            )

    lines = [line1]
    if len(arm_lines) <= 7:
        lines.extend(arm_lines)
    else:
        omitted = len(arm_lines) - 6
        lines.extend(arm_lines[:6])
        lines.append(f"其餘 {omitted} 個分支省略")

    return '\n'.join(' '.join(line.splitlines()) for line in lines)


def render(target, *, unavailable_reason=None, now_ms=None):
    if isinstance(target, list):
        return render_family(target, now_ms=now_ms)
    return render_single(target, unavailable_reason=unavailable_reason, now_ms=now_ms)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--status', action='append', required=True, type=Path,
                   help='status snapshot path (repeatable)')
    a = p.parse_args(argv)

    status_paths = a.status
    now_ms = int(time.time() * 1000)

    if len(status_paths) == 1:
        path = status_paths[0]
        try:
            s = json.loads(path.read_text(encoding='utf-8'))
            validate_brief_snapshot(s)
            unavailable = check_freshness(s, now_ms=now_ms)
            print(render_single(s, unavailable_reason=unavailable, now_ms=now_ms))
            return 1 if unavailable else 0
        except (ValueError, OSError, KeyError, TypeError) as e:
            print('PAPER 簡報不可用：' + str(e))
            return 1

    arms = []
    any_unavailable = False
    for path in status_paths:
        arm = {'path': path, 'version': path.stem, 'unavailable': None, 'snapshot': None}
        try:
            s = json.loads(path.read_text(encoding='utf-8'))
            vid = s.get('engine', {}).get('version_id')
            if vid:
                arm['version'] = vid
            validate_brief_snapshot(s)
            arm['snapshot'] = s
            unavailable = check_freshness(s, now_ms=now_ms)
            arm['unavailable'] = unavailable
            if unavailable:
                any_unavailable = True
        except (ValueError, OSError, KeyError, TypeError) as e:
            arm['unavailable'] = str(e)
            any_unavailable = True
        arms.append(arm)

    print(render_family(arms, now_ms=now_ms))
    return 1 if any_unavailable else 0


if __name__ == '__main__':
    raise SystemExit(main())
