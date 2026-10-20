"""Closed-minute strategy/placebo account playback, no hypothetical fills.

Both signal families call the same OfflineAccount: slot, frozen fees,
funding, SL/LIQ, closed episodes and tail MTM are never bypassed.
"""
from decimal import Decimal
from collections import Counter

from account_lifecycle import OfflineAccount
from account_ledger import replay_account
from entry_lifecycle import MINUTE
from maker_exit_lab import X2Error
from signal_features import eligible_decisions

D = Decimal


def _account(config, rates=None):
    required = ("capital", "per_trade_margin", "leverage", "daily_loss_limit",
                "loss_streak_limit", "halt_hours", "liq_pct", "liq_fee_pct")
    if any(key not in config for key in required):
        raise X2Error("explicit offline account config missing required values")
    return OfflineAccount(**{key: config[key] for key in required}, rates=rates)


def _funding_for_bar(events, cursor, bar_end, prev_close, close):
    """Only finalized events up to current close; no future mark proxy."""
    accepted = []
    while cursor < len(events) and events[cursor]["ts"] <= bar_end:
        event = events[cursor]
        mark = event["mark"]
        if mark is None:
            mark = close if event["ts"] == bar_end else prev_close
        if mark is None:
            raise X2Error("no prior completed price for historical funding")
        accepted.append({"id": event["settlement_id"],
                         "settlement_id": event["settlement_id"],
                         "ts": event["ts"], "mark": str(mark),
                         "rate": event["rate"]})
        cursor += 1
    return accepted, cursor


def replay_cell(symbol, bars, funding, framework, k, ttl, exit_style,
                account_config, evaluation_start, evaluation_end,
                signal_times=None, delay_minutes=0, eligible=None, rates=None):
    """Return actual event stream and account results for one isolated cell.

    signal_times=None means S1/S2; supplied times mean matched placebo.
    Delay changes submission/earliest delivery, NOT frozen feature history.
    Entry and passive fills use the frozen 2bp OHLC proxy and full posted
    quantity (OHLC has no partial depth or real queue observation).
    """
    if exit_style not in ("X1", "X2", "X3") or k not in (".25", ".50"):
        raise X2Error("invalid frozen grid cell")
    if ttl not in (2, 5) or delay_minutes not in (0, 2):
        raise X2Error("invalid frozen TTL/delay")
    if eligible is None:
        eligible = eligible_decisions(bars, framework)
    desired = None if signal_times is None else set(signal_times)
    queue = {}
    signals = []
    for r in eligible:
        ts = r["signal_time"]
        if ts < evaluation_start or ts >= evaluation_end:
            continue
        if (r["triggered"] if desired is None else ts in desired):
            queue.setdefault(ts + delay_minutes * MINUTE, []).append(r)
            signals.append(r)
    if desired is not None:
        present = {r["signal_time"] for r in eligible
                   if evaluation_start <= r["signal_time"] < evaluation_end}
        if desired - present:
            raise X2Error("matched timestamp not in causal eligible universe")
    account = _account(account_config, rates=rates)
    events = []
    counts = Counter()
    funding_index = 0
    while (funding_index < len(funding) and
           funding[funding_index]["ts"] < evaluation_start):
        funding_index += 1
    prev_close = None
    previous_open = None
    episode_starts = {}
    equity_path = []
    last_daily_point = None
    for bar in bars:
        start = bar["ts"]
        end = start + MINUTE
        if end < evaluation_start:
            prev_close = bar["close"]
            previous_open = start
            continue
        if start >= evaluation_end:
            break
        if previous_open is not None and start != previous_open + MINUTE:
            account.tail(gap=True)
            events.append({"type": "DATA_GAP", "ts": start,
                           "previous_open": previous_open})
            counts["data_gap"] += 1
        finalized, funding_index = _funding_for_bar(
            funding, funding_index, end, prev_close, bar["close"])
        qty = None
        maker = False
        if account.entry is not None and account.exit is None:
            entry = account.entry
            if entry.status in ("PENDING", "PARTIAL") and (
                not (start == entry.signal_time and
                     D(bar["open"]) >= entry.limit)
                    and D(bar["high"]) >= entry.limit * D("1.0002")):
                qty = str(entry.remaining)
        active_before = account.exit
        history_before = len(active_before.history) if active_before else 0
        if account.exit is not None:
            active = account.exit
            if active.withdraw:
                pending = active.pending
                if pending and start >= pending["start"]:
                    maker = D(bar["low"]) <= pending["limit"] * D(".9998")
            else:
                for name in ("tp1", "tp2"):
                    if active.core.orders[name] > 0:
                        limit = active.core.targets[name]
                        maker = D(bar["low"]) <= limit * D(".9998")
                        break
        # Conservative existing risk wins if passive profit is also in
        # the bar's range. Report the conflict instead of inventing ticks.
        if active_before and maker and D(bar["high"]) >= active_before.core.stop:
            counts["samebar_risk_profit_conflict"] += 1
            # Descriptive counterfactual: risk buyback vs posted Maker
            # limit for THIS one bar, capped to actual remaining qty.
            # It is not an executable second trade or a tick-order claim.
            core=active_before.core
            limit=(active_before.pending["limit"]
                   if active_before.withdraw and active_before.pending
                   else core.targets["tp1"] if core.orders["tp1"]>0
                   else core.targets["tp2"])
            risk_px=max(D(bar["open"]),core.stop)*(1+account.rates.slip)
            maker_qty=(core.qty if active_before.withdraw
                       else min(core.qty,core.orders["tp1"]
                                if core.orders["tp1"]>0 else
                                core.orders["tp2"]))
            counterfactual=max(D(0),maker_qty*(
                risk_px-limit + risk_px*account.rates.taker_fee -
                limit*account.rates.maker_fee))
            counts["favorable_order_upper_bound_usd"] += counterfactual
        result = account.bar(bar, entry_qty=qty, maker_fill=maker,
                             settlements=finalized)
        if active_before is not None:
            additions = active_before.history[history_before:]
            counts["admin_4h_triggers"] += sum(
                one[0] == "DEADLINE_INTENT" for one in additions)
            counts["post_only_rejects"] += sum(
                one[0] == "POST_ONLY_REJECT" for one in additions)
        if result not in ("EMPTY", "OPEN", "PENDING", "PARTIAL"):
            counts[result] += 1
        # After the minute closes, accept only signals that are now known.
        for r in queue.get(end, ()):
            counts["signals"] += 1
            if account.snapshot()["slot"] or account.stale:
                counts["slot_or_gap_blocked"] += 1
                continue
            try:
                status = account.submit(
                    symbol, r["signal_id"], end, str(r["signal_high"]),
                    str(r["signal_close"]), str(r["atr14"]), k, ttl,
                    str((D(account_config["per_trade_margin"]) *
                         D(account_config["leverage"])) /
                        (D(r["signal_high"]) + max(D(k)*D(r["atr14"]),
                               D(r["signal_close"])*D(".0002")))), exit_style,
                    signal_vwap=str(r["vwap60"]) if exit_style=="X1" else None)
                counts["attempts"] += 1
                counts[status] += 1
                if status == "PENDING":
                    episode_starts[r["signal_id"]] = {
                        "decision_ts": r["signal_time"],
                        "arrival_ts": end, "signal_type": r["signal_type"],
                        "framework": framework}
            except X2Error as exc:
                counts["admission_rejected"] += 1
                events.append({"type": "ADMISSION_REJECTED", "ts": end,
                               "signal_id": r["signal_id"],
                               "reason": str(exc)})
        snap = account.snapshot()
        point={"ts": end, "cash": str(snap["ledger"]["cash"]),
               "equity": str(snap["equity"]),
               "qty": str(snap["ledger"]["qty"]),
               "unrealized": (None if snap["unrealized"] is None
                              else str(snap["unrealized"])),
               "utc_day": snap["day"], "stale": bool(snap["stale"])}
        # Keep the last observed close valuation per UTC day; no invented
        # midnight price, and zero-trade calendar days remain visible.
        if last_daily_point is not None and (
                last_daily_point["utc_day"] != point["utc_day"]):
            equity_path.append(last_daily_point)
        last_daily_point=point
        prev_close = bar["close"]
        previous_open = start
    if last_daily_point is not None:
        equity_path.append(last_daily_point)
    account.tail()
    snap = account.snapshot()
    ledger = replay_account(account.capital, account.events)
    if ledger != account.ledger():
        raise X2Error("independent full account ledger divergence")
    completed = []
    for episode in ledger["episodes"]:
        original = episode_starts.get(episode["id"], {})
        entry_notional = sum((D(e["qty"]) * D(e["price"])
                             for e in account.events
                             if e["type"]=="fill" and e["side"]=="sell"
                             and e["id"].startswith(episode["id"]+":")),
                            D(0))
        reserves = [i for i, ev in enumerate(account.events)
                    if ev["type"]=="reserve" and
                    ev.get("episode")==episode["id"]]
        if len(reserves)!=1:
            raise X2Error("episode reserve identity missing")
        offset=reserves[0]+1
        boundary=next((j for j in range(offset,len(account.events))
                       if account.events[j]["type"]=="reserve"),
                      len(account.events))
        related=account.events[offset:boundary]
        liquidated = any(ev["type"]=="liquidation" for ev in related)
        buys = [ev for ev in related if ev["type"]=="fill"
                and ev.get("side")=="buy"]
        terminal = buys[-1] if buys else {}
        source = terminal.get("source_id", terminal.get("id",""))
        exit_type = ("LIQ" if liquidated else
                     "SL_GAP" if "gap-risk" in source else
                     "SL" if ("risk-" in source) else
                     "TP" if "profit-" in source else
                     "PASSIVE_X3" if "withdraw-" in source else
                     "NO_FILL")
        completed.append({**{key: str(val) if isinstance(val, D) else val
                              for key, val in episode.items()},
                          "entry_notional": str(entry_notional),
                          "exit_type": exit_type, **original})
    if "favorable_order_upper_bound_usd" in counts:
        counts["favorable_order_upper_bound_usd"] = str(
            counts["favorable_order_upper_bound_usd"])
    counts["complete_episodes"] = sum(
        1 for ep in completed if D(ep["entry_notional"]) > 0)
    counts["open"] = int(ledger["qty"] > 0)
    counts["filled_episodes"] = counts["complete_episodes"] + counts["open"]
    raw = [{key: str(value) if isinstance(value, D) else value
            for key,value in event.items()} for event in account.events]
    return {"symbol": symbol, "framework": framework, "k": k, "ttl": ttl,
            "exit": exit_style, "delay_minutes": delay_minutes,
            "matched": signal_times is not None,
            "signal_count": len(signals), "counts": dict(counts),
            "account": {key: str(v) if isinstance(v, D) else v
                        for key,v in snap.items() if key != "ledger"},
            "ledger": _json_value(ledger), "episodes": completed,
            "events": raw, "other_events": events,
            "equity_path": equity_path}


def _json_value(item):
    if isinstance(item, D):
        return str(item)
    if isinstance(item, (set, tuple)):
        return sorted((_json_value(x) for x in item), key=str)
    if isinstance(item, list):
        return [_json_value(v) for v in item]
    if isinstance(item, dict):
        return {k:_json_value(v) for k,v in item.items()}
    return item
