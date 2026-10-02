#!/usr/bin/env python
"""Eight-line PAPER brief. No message sending or cron mutation."""
import argparse,json,time
from pathlib import Path
from datetime import datetime,timezone
from dashboard import validate_snapshot
from accept_learning_v3 import verify_freshness

def render(s, *, unavailable_reason=None):
    r=s['research']
    categories=', '.join(f'{k}:{v}' for k,v in sorted(r['rejection_categories'].items())) or '無'
    deadline=datetime.fromtimestamp(r['deadline_ms']/1000,timezone.utc).isoformat()
    lines=[
        (f"PAPER {s['engine']['version_id']} 即時估值不可用 ({unavailable_reason})" if unavailable_reason else f"PAPER {s['engine']['version_id']} 尚未證明優勢 ({r['status']})"),
        (f"最後已驗證快照 {s['updated_at']} (僅schema/資產等式，非即時) " if unavailable_reason else '') + f"本金 {s.get('initial_equity_usdt','未知')}，權益 {s['equity_usdt']} USDT，累計淨損益 {s['total_pnl_usdt']}",
        f"新窗口完整往返 {r['complete_round_trips']}/30 (48h目標，不保證)",
        f"訊號 {r['signals_count']}，受阻 {r['blocked_signals_count']}，{categories}",
        f"新窗口扣全成本現金損益 {r['net_cash_pnl_usdt']} USDT",
        f"帳戶累計費用 {s['fees_usdt']}，funding {s['funding_pnl_usdt']}",
        f"行情/執行阻擋 {', '.join(s['blockers']) or '無'}，截止 {deadline}",
        '學習：樣本不足不下結論，不為達標強迫成交。真錢停用。',
    ]
    return '\n'.join(' '.join(line.splitlines()) for line in lines)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--status',required=True,type=Path);a=p.parse_args()
    try:
        s=json.loads(a.status.read_text());validate_snapshot(s)
        if s['fixture'] or s['mode']!='paper' or s['engine']['version_id']!='H1-PAPER-003':
            raise ValueError('public V3 PAPER snapshot required')
        unavailable=None
        try:
            verify_freshness(s,now_ms=int(time.time()*1000))
        except ValueError as exc:
            unavailable=str(exc)
        if s.get('latest_error'):
            unavailable=(unavailable+'; ' if unavailable else '')+s['latest_error']
        elif not s['feed']['connected']:
            unavailable=(unavailable+'; ' if unavailable else '')+'feed disconnected'
        print(render(s,unavailable_reason=unavailable))
        return 1 if unavailable else 0
    except (ValueError,OSError,KeyError) as e:
        print('PAPER 簡報不可用：'+str(e));return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
