"""Framework-wide deterministic matched placebo decision sampler; no fills."""
import hashlib
import random
from signal_features import eligible_decisions
from maker_exit_lab import X2Error

DEFAULT_COUNT = 6000


def select_matched(records, framework_minutes, evaluation_start,
                   evaluation_end, count=DEFAULT_COUNT):
    """One canonical set per framework, independent of all 12 variants."""
    if framework_minutes not in (1, 15) or type(framework_minutes) is not int:
        raise X2Error("invalid framework")
    if (type(evaluation_start) is not int or type(evaluation_end) is not int
            or evaluation_start >= evaluation_end):
        raise X2Error("invalid evaluation half-open interval")
    if type(count) is not int or count <= 0:
        raise X2Error("invalid sample size")
    timestamps = []
    seen = set()
    for r in records:
        if r["framework_minutes"] != framework_minutes:
            raise X2Error("mixed framework universe")
        ts = r["signal_time"]
        if type(ts) is not int:
            raise X2Error("invalid decision timestamp")
        if ts in seen:
            raise X2Error("duplicate decision timestamp")
        seen.add(ts)
        if evaluation_start <= ts < evaluation_end:
            timestamps.append(ts)
    timestamps.sort()
    identity = hashlib.sha256(
        ("framework="+str(framework_minutes)+";interval="+
         str(evaluation_start)+":"+str(evaluation_end)+";times="+
         ",".join(map(str, timestamps))).encode("ascii")
    ).hexdigest()
    seed = 20261020 + framework_minutes
    result = {"framework_minutes": framework_minutes, "seed": seed,
              "source_universe_sha256": identity,
              "requested_count": count, "eligible_count": len(timestamps),
              "selected_timestamps": [], "status": "INSUFFICIENT_UNIVERSE"}
    if len(timestamps) < count:
        return result
    selected = random.Random(seed).sample(timestamps, count)
    result["selected_timestamps"] = sorted(selected)
    result["status"] = "READY"
    return result


def sample_completed(bars, framework_minutes, evaluation_start,
                     evaluation_end, count=DEFAULT_COUNT):
    """Eligibility is independent of whether S1/S2 fires or fills afterward."""
    return select_matched(eligible_decisions(bars, framework_minutes),
                          framework_minutes, evaluation_start,
                          evaluation_end, count=count)
