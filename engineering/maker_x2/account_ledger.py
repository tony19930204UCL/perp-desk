"""Independent event-by-event single-account ledger; Decimal50 and no IO."""
from decimal import Decimal, localcontext
from datetime import datetime, timezone
from maker_exit_lab import X2Error, number

D = Decimal
Z = D("0")


def utc_day(ts):
    if type(ts) is not int or ts < 0:
        raise X2Error("UTC event milliseconds required")
    return datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat()


def replay_account(initial_cash, events):
    """Reconstruct real fill/funding/LIQ cash and margin without summaries.

    Account records require contiguous seq and nondecreasing ts. reserve and
    release are liabilities, never cash debits. LIQ is NOT a fill.
    """
    with localcontext() as ctx:
        ctx.prec = 50
        start = number(initial_cash, "initial_cash", True)
        cash = start
        reserved = Z
        qty = Z
        basis = Z
        fees = Z
        funding = Z
        gross = Z
        writeoffs = Z
        episodes = []
        episode = None
        seen, settlements = set(), set()
        last_ts = None
        for seq, event in enumerate(events):
            if type(event) is not dict:
                raise X2Error("account event must be dict")
            eid, ts = event.get("id"), event.get("ts")
            if not isinstance(eid, str) or not eid or eid in seen:
                raise X2Error("duplicate account event id")
            if event.get("seq") != seq or type(event.get("seq")) is not int:
                raise X2Error("account event sequence gap")
            utc_day(ts)
            if last_ts is not None and ts < last_ts:
                raise X2Error("backwards account event")
            seen.add(eid)
            last_ts = ts
            kind = event["type"]
            if kind == "reserve":
                amount = number(event["amount"], "reserve", True)
                if reserved != Z or qty != Z or amount > cash:
                    raise X2Error("double/insufficient reserve")
                reserved += amount
                episode = {"id": event["episode"], "start_cash": cash,
                           "gross": Z, "fees": Z, "funding_cost": Z,
                           "writeoff": Z, "net": Z}
            elif kind == "release":
                amount = number(event["amount"], "release", True)
                if amount > reserved:
                    raise X2Error("overrelease margin")
                reserved -= amount
            elif kind == "fill":
                amount = number(event["qty"], "qty", True)
                price = number(event["price"], "price", True)
                liquidity = event["liquidity"]
                if liquidity not in ("maker", "taker"):
                    raise X2Error("bad fill liquidity")
                fee = amount * price * (
                    D(".0002") if liquidity == "maker" else D(".0005"))
                if event["side"] == "sell":
                    if reserved <= Z or episode is None:
                        raise X2Error("fill without reserved episode")
                    qty += amount
                    basis += amount * price
                    realized = Z
                elif event["side"] == "buy":
                    if amount > qty:
                        raise X2Error("over-close")
                    realized = (basis / qty - price) * amount
                    basis -= basis / qty * amount
                    qty -= amount
                    if qty == Z:
                        basis = Z
                else:
                    raise X2Error("bad fill side")
                cash += realized - fee
                gross += realized
                fees += fee
                episode["gross"] += realized
                episode["fees"] += fee
            elif kind == "funding":
                sid = event["settlement_id"]
                if not isinstance(sid, str) or not sid or sid in settlements:
                    raise X2Error("duplicate settlement")
                settlements.add(sid)
                # Account qty is POSITIVE for a short; canonical replay
                # uses negative signed_qty. Positive rates credit shorts.
                amount = qty * number(event["mark"], "mark", True) * (
                    number(event["rate"], "rate"))
                cash += amount
                funding -= amount
                episode["funding_cost"] -= amount
            elif kind == "liquidation":
                amount = number(event["qty"], "liq_qty", True)
                margin = number(event["margin_writeoff"], "writeoff")
                fee = number(event["fee"], "liq_fee")
                if amount != qty or margin < Z or margin > reserved or fee < Z:
                    raise X2Error("invalid LIQ")
                qty = Z
                basis = Z
                reserved -= margin
                cash -= margin + fee
                writeoffs += margin
                fees += fee
                episode["writeoff"] += margin
                episode["fees"] += fee
            else:
                raise X2Error("unknown account event")
            if episode is not None:
                episode["net"] = (episode["gross"] - episode["fees"]
                                  - episode["funding_cost"] - episode["writeoff"])
            if episode is not None and qty == Z and reserved == Z:
                episodes.append(dict(episode))
                episode = None
        if qty < Z or reserved < Z:
            raise X2Error("negative position or reservation")
        return {"cash": cash, "reserved": reserved, "available": cash-reserved,
                "qty": qty, "basis": basis, "gross": gross, "fees": fees,
                "funding_cost": funding, "writeoff": writeoffs,
                "episodes": episodes, "open_episode": episode,
                "ids": seen, "settlements": settlements}
