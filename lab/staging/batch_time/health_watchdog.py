"""PAPER-only read-only operational observer; never runs or stops trading."""
from datetime import datetime, timezone
from dashboard import validate_snapshot

ROOT_VERSION = 'H1-PAPER-002'
MAX_AGE = 60

def age(stamp, now):
    dt = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if dt.utcoffset() is None:
        raise ValueError('timestamp lacks timezone')
    return (now-dt).total_seconds()

def evaluate(snapshot, work, now, process_present=None, storage=None):
    faults = {}; evidence = {}
    try:
        validate_snapshot(snapshot)
        if (snapshot['mode'] != 'paper' or snapshot.get('candidate_not_deployed') is not False
                or snapshot['engine']['version_id'] != ROOT_VERSION
                or snapshot['engine'].get('candidate_implementation') != 'paper-engine-v2'):
            raise ValueError('expected deployed PAPER v2 identity')
        heartbeat = age(snapshot['updated_at'], now)
        success = age(snapshot['feed']['last_success_at'], now)
        evidence = {k: snapshot.get(k) for k in ('updated_at','latest_error','blockers')}
        evidence.update(errors_count=snapshot['feed']['errors_count'], gaps_count=snapshot['feed']['gaps_count'],
                        heartbeat_age_seconds=heartbeat, last_success_age_seconds=success,
                        engine_version=snapshot['engine']['version_id'], markets=snapshot['markets'])
        if not 0 <= heartbeat <= MAX_AGE:
            faults['heartbeat-stale'] = 'Runtime snapshot stale or future'
        if not snapshot['feed']['connected'] or not 0 <= success <= MAX_AGE:
            faults['feed-disconnected'] = 'Feed disconnected, stale or future last-success timestamp'
        if snapshot.get('latest_error') or snapshot['blockers'] or snapshot['engine']['status'] in ('error','halted'):
            faults['runtime-error'] = str(snapshot.get('latest_error') or snapshot['blockers'] or snapshot['engine']['status'])[:2000]
        if not snapshot['markets']:
            faults['source-freshness'] = 'No raw market timestamps'
        ages = []
        for market in snapshot['markets']:
            try:
                receipt = age(market['last_received_at'], now)
                source_ages = {}
                for endpoint in ('bookTicker','depth5','premiumIndex'):
                    stamp = market['source_timestamps_ms'].get(endpoint)
                    if type(stamp) is not int:
                        raise ValueError('missing or malformed raw source timestamp')
                    source_ages[endpoint] = now.timestamp()-stamp/1000
                ages.append(dict(symbol=market['symbol'], receipt_age_seconds=receipt, source_age_seconds=source_ages))
                if any(not 0 <= v <= MAX_AGE for v in [receipt, *source_ages.values()]):
                    faults['source-freshness'] = 'Raw source or receipt timestamp stale/future'
            except (ValueError, TypeError, KeyError, AttributeError):
                faults['source-freshness'] = 'Raw source or receipt timestamp invalid'
        evidence['market_ages'] = ages
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
        faults['snapshot-unavailable'] = 'Live PAPER snapshot unavailable, malformed or wrong runtime identity'
        if isinstance(snapshot, dict):
            evidence['latest_error'] = snapshot.get('latest_error')
    if process_present is not True:
        faults['runtime-absent'] = 'Exact live runtime process absent or unverified'
    return dict(operational_healthy=not faults, faults=faults, evidence=evidence)
