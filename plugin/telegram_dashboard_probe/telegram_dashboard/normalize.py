from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from .schema import Coverage, Incident, Severity, SourceObservation, SourceState
from .timeparse import age_seconds, is_from_the_future, parse_timestamp


def classify_freshness(
    observed_at: object,
    *,
    now: datetime,
    ttl_seconds: int,
) -> SourceState:
    observed = parse_timestamp(observed_at)
    if observed is None:
        return "unavailable"
    age = age_seconds(observed, now)
    if age is None or is_from_the_future(age):
        # A stamp ahead of now cannot be proven; a negative age would otherwise pass any ttl.
        return "unavailable"
    return "stale" if age > ttl_seconds else "fresh"


def derive_overall(
    *,
    coverage: Coverage,
    sources: Sequence[SourceObservation],
    incidents: Sequence[Incident],
) -> Severity:
    if any(incident.severity == "critical" for incident in incidents):
        return "critical"
    if any(incident.severity == "warning" for incident in incidents):
        return "warning"
    if coverage.observed_profiles < coverage.expected_profiles or coverage.failed_sources:
        return "unknown"
    if any(source.state != "fresh" for source in sources):
        return "unknown"
    return "normal"
