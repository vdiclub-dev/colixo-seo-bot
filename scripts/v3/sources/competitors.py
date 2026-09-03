"""Public competitor-intelligence fixture adapter; no scraping is implemented."""

from typing import Any, Iterable, Mapping, Tuple

from ..models import CompetitorSignal, DimensionLevel
from .base import FixtureSource, fixture_evidence


class CompetitorFixtureSource(FixtureSource[CompetitorSignal]):
    def collect(self, fixture: Iterable[Mapping[str, Any]]) -> Tuple[CompetitorSignal, ...]:
        return tuple(
            CompetitorSignal(
                topic=str(item["topic"]),
                competitor=str(item["competitor"]),
                gap_level=DimensionLevel(str(item.get("gap_level", "unknown"))),
                public_reference=item.get("public_reference"),
                evidence=fixture_evidence(item.get("evidence", ())),
            )
            for item in fixture
        )
