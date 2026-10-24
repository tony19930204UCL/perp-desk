"""Matched placebo account playback using the identical strategy replay."""
from matched_signals import sample_completed
from market_replay import replay_cell
from maker_exit_lab import X2Error


def replay_matched(symbol, bars, funding, framework, k, ttl, exit_style,
                   account_config, start, end, delay_minutes=0,
                   sample_count=6000, rates=None):
    sample = sample_completed(bars, framework, start, end, count=sample_count)
    if sample["status"] != "READY":
        return {"sample": sample, "status": "INSUFFICIENT_UNIVERSE",
                "replay": None}
    result = replay_cell(symbol, bars, funding, framework, k, ttl, exit_style,
                         account_config, start, end,
                         signal_times=sample["selected_timestamps"],
                         delay_minutes=delay_minutes, rates=rates)
    return {"sample": sample, "status": "READY", "replay": result}
