"""Rank-tracking fixture adapter; no provider API is called."""

from typing import Any, Iterable, Mapping, Tuple

from ..models import RankSignal
from .base import FixtureSource, fixture_evidence


class RankTrackerFixtureSource(FixtureSource[RankSignal]):
    def collect(self, fixture: Iterable[Mapping[str, Any]]) -> Tuple[RankSignal, ...]:
        return tuple(
            RankSignal(
                topic=str(item["topic"]),
                query=str(item["query"]),
                position=item.get("position"),
                tracked_reference=item.get("tracked_reference"),
                evidence=fixture_evidence(item.get("evidence", ())),
            )
            for item in fixture
        )
