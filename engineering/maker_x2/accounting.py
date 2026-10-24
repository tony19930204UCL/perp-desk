# accounting.py
"""Independent, offline, Decimal-based perpetual-fill ledger replay.

Public API:
    replay(initial_cash, events) -> dict

Events are an ordered iterable of dictionaries. No sorting is performed.

Every event requires:
    id: unique, nonempty string
    seq: integer starting at 0, increasing by exactly 1
    ts: integer UTC milliseconds, nondecreasing
    type: "fill" or "funding"

Fill event:
    {
        "id": "e1", "seq": 0, "ts": 1000,
        "type": "fill",
        "side": "sell" | "buy",
        "qty": "2",
        "price": "100",
        "liquidity": "maker" | "taker"
    }

Funding event:
    {
        "id": "f1", "seq": 1, "ts": 2000,
        "type": "funding",
        "settlement_id": "settlement-1",
        "mark": "100",
        "rate": "0.001"
    }

All monetary and quantity inputs must be decimal strings or integers.
Binary floats, booleans, NaN, and infinity are rejected.

Signed position convention:
    short quantity < 0
    long quantity > 0

This module supports short positions only.

Funding cash delta:
    -signed_qty * mark * rate

Thus positive funding rates credit shorts and debit longs.

Fill accounting:
    - Opening a short does not add sale notional to cash.
    - Closing a short realizes (entry_price - exit_price) * qty.
    - Every fill deducts its actual execution fee from cash.
    - Funding adjusts cash immediately.
    - Unrealized PnL is not included without an explicit mark.

Same-timestamp events execute in ascending seq order.
For funding before an exit fill, supply the funding event first.
For funding after an exit fill, supply the fill event first.
The caller is responsible for the settlement-boundary ordering contract.

No market data, future funding, synthetic close, liquidation,
margin, or strategy execution is inferred.
"""

from decimal import Decimal, InvalidOperation, localcontext


MAKER_FEE = Decimal("0.0002")
TAKER_FEE = Decimal("0.0005")
ZERO = Decimal("0")
ONE = Decimal("1")


class LedgerError(ValueError):
    """Invalid ledger input or violated accounting invariant."""


def _decimal(value, name):
    if isinstance(value, bool) or isinstance(value, float):
        raise LedgerError(f"{name}: binary floats/bools forbidden")

    if not isinstance(value, (str, int, Decimal)):
        raise LedgerError(f"{name}: expected decimal string or integer")

    if isinstance(value, str):
        if not value or value.strip() != value:
            raise LedgerError(f"{name}: invalid decimal representation")

    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LedgerError(f"{name}: invalid decimal") from exc

    if not result.is_finite():
        raise LedgerError(f"{name}: nonfinite decimal")

    return result


def _positive(value, name):
    result = _decimal(value, name)
    if result <= ZERO:
        raise LedgerError(f"{name}: must be positive")
    return result


def _exact_fields(event, required):
    if set(event) != set(required):
        missing = sorted(set(required) - set(event))
        extra = sorted(set(event) - set(required))
        raise LedgerError(
            f"event fields mismatch: missing={missing}, extra={extra}"
        )


def _identity(event, expected_seq, previous_ts, seen):
    event_id = event.get("id")
    seq = event.get("seq")
    ts = event.get("ts")

    if not isinstance(event_id, str) or not event_id:
        raise LedgerError("event id must be a nonempty string")

    if event_id in seen:
        raise LedgerError(f"duplicate event id: {event_id}")

    if type(seq) is not int or seq != expected_seq:
        raise LedgerError(
            f"sequence gap/order: expected {expected_seq}, got {seq}"
        )

    if type(ts) is not int or ts < 0:
        raise LedgerError("timestamp must be nonnegative integer UTC ms")

    if previous_ts is not None and ts < previous_ts:
        raise LedgerError("event timestamp moved backwards")

    return event_id, ts


def replay(initial_cash, events):
    """Replay short fill/funding events independently of any trading engine.

    Returns Decimal-valued cash, gross realized PnL, total fees,
    funding cost, signed remaining quantity, entry basis, average
    entry price, and detailed execution/funding records.

    Accounting identity:
        cash = initial_cash + realized_gross - fees - funding_cost

    For an explicitly supplied external mark M:
        unrealized = remaining_qty * (M - average_entry_price)
        equity = cash + unrealized

    Here remaining_qty is signed (negative for shorts).
    No mark or terminal close is invented by this function.

    Raises LedgerError on malformed events or invalid transitions.
    """
    with localcontext() as ctx:
        ctx.prec = 50

        starting_cash = _decimal(initial_cash, "initial_cash")
        if starting_cash < ZERO:
            raise LedgerError("initial_cash must be nonnegative")

        if isinstance(events, (str, bytes, dict)):
            raise LedgerError("events must be an ordered iterable")

        try:
            iterator = iter(events)
        except TypeError as exc:
            raise LedgerError("events must be iterable") from exc

        cash = starting_cash
        realized = ZERO
        fees = ZERO
        funding_cost = ZERO
        signed_qty = ZERO
        entry_basis = ZERO

        seen_ids = set()
        seen_settlements = set()
        previous_ts = None
        count = 0
        fills = []
        settlements = []

        for event in iterator:
            if not isinstance(event, dict):
                raise LedgerError("event must be a dictionary")

            event_id, ts = _identity(
                event, count, previous_ts, seen_ids
            )
            kind = event.get("type")

            if kind == "fill":
                _exact_fields(
                    event,
                    (
                        "id", "seq", "ts", "type",
                        "side", "qty", "price", "liquidity"
                    ),
                )

                side = event["side"]
                liquidity = event["liquidity"]

                if side not in ("sell", "buy"):
                    raise LedgerError("fill side must be sell or buy")

                if liquidity not in ("maker", "taker"):
                    raise LedgerError("invalid liquidity")

                qty = _positive(event["qty"], "qty")
                price = _positive(event["price"], "price")
                rate = (
                    MAKER_FEE if liquidity == "maker"
                    else TAKER_FEE
                )
                fee = qty * price * rate
                gross = ZERO

                if side == "sell":
                    if signed_qty > ZERO:
                        raise LedgerError("long position unsupported")

                    signed_qty -= qty
                    entry_basis += qty * price

                else:
                    available = -signed_qty

                    if available <= ZERO:
                        raise LedgerError("buy without short position")

                    if qty > available:
                        raise LedgerError("buy exceeds remaining short qty")

                    average_entry = entry_basis / available
                    gross = (average_entry - price) * qty

                    signed_qty += qty
                    entry_basis -= average_entry * qty

                    if signed_qty == ZERO:
                        entry_basis = ZERO

                    realized += gross
                    cash += gross

                fees += fee
                cash -= fee

                fills.append({
                    "id": event_id,
                    "seq": count,
                    "ts": ts,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "liquidity": liquidity,
                    "fee_rate": rate,
                    "fee": fee,
                    "realized_gross": gross,
                    "remaining_signed_qty": signed_qty,
                })

            elif kind == "funding":
                _exact_fields(
                    event,
                    (
                        "id", "seq", "ts", "type",
                        "settlement_id", "mark", "rate"
                    ),
                )

                settlement_id = event["settlement_id"]

                if (
                    not isinstance(settlement_id, str)
                    or not settlement_id
                ):
                    raise LedgerError("invalid settlement_id")

                if settlement_id in seen_settlements:
                    raise LedgerError("duplicate funding settlement")

                mark = _positive(event["mark"], "mark")
                rate = _decimal(event["rate"], "rate")

                cost = signed_qty * mark * rate
                funding_cost += cost
                cash -= cost

                settlements.append({
                    "id": event_id,
                    "settlement_id": settlement_id,
                    "seq": count,
                    "ts": ts,
                    "signed_qty": signed_qty,
                    "mark": mark,
                    "rate": rate,
                    "funding_cost": cost,
                    "cash_delta": -cost,
                })

                seen_settlements.add(settlement_id)

            else:
                raise LedgerError("unsupported event type")

            seen_ids.add(event_id)
            previous_ts = ts
            count += 1

        if signed_qty > ZERO or entry_basis < ZERO:
            raise LedgerError("invalid final short position")

        if signed_qty == ZERO and entry_basis != ZERO:
            raise LedgerError("flat position retains entry basis")

        if signed_qty < ZERO and entry_basis <= ZERO:
            raise LedgerError("open short has invalid entry basis")

        average_entry = (
            entry_basis / -signed_qty
            if signed_qty < ZERO else None
        )

        expected_cash = (
            starting_cash + realized - fees - funding_cost
        )

        if cash != expected_cash:
            raise LedgerError("cash reconciliation failed")

        return {
            "initial_cash": starting_cash,
            "cash": cash,
            "realized_gross": realized,
            "fees_paid": fees,
            "funding_cost": funding_cost,
            "realized_net": realized - fees - funding_cost,
            "remaining_signed_qty": signed_qty,
            "remaining_qty": -signed_qty,
            "entry_basis": entry_basis,
            "average_entry_price": average_entry,
            "is_flat": signed_qty == ZERO,
            "event_count": count,
            "fills": fills,
            "settlements": settlements,
        }