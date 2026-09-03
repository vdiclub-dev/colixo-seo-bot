"""Search Console fixture adapter; it performs no Search Console calls."""

from typing import Any, Iterable, Mapping, Tuple

from ..models import SearchSignal
from .base import FixtureSource, fixture_evidence


class SearchConsoleFixtureSource(FixtureSource[SearchSignal]):
    def collect(self, fixture: Iterable[Mapping[str, Any]]) -> Tuple[SearchSignal, ...]:
        return tuple(
            SearchSignal(
                topic=str(item["topic"]),
                query=str(item["query"]),
                clicks=item.get("clicks"),
                impressions=item.get("impressions"),
                ctr=item.get("ctr"),
                average_position=item.get("average_position"),
                evidence=fixture_evidence(item.get("evidence", ())),
            )
            for item in fixture
        )
