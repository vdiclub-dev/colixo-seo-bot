"""Analytics fixture adapter; no GA4 client is present in Phase 1."""

from typing import Any, Iterable, Mapping, Tuple

from ..models import TrafficSignal
from .base import FixtureSource, fixture_evidence


class AnalyticsFixtureSource(FixtureSource[TrafficSignal]):
    def collect(self, fixture: Iterable[Mapping[str, Any]]) -> Tuple[TrafficSignal, ...]:
        return tuple(
            TrafficSignal(
                topic=str(item["topic"]),
                organic_sessions=item.get("organic_sessions"),
                engaged_sessions=item.get("engaged_sessions"),
                conversions=item.get("conversions"),
                evidence=fixture_evidence(item.get("evidence", ())),
            )
            for item in fixture
        )
