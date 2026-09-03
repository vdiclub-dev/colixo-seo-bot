"""Offline fixture adapter contract used by every Phase 1 source."""

from abc import ABC, abstractmethod
from typing import Any, Generic, Iterable, Mapping, Tuple, TypeVar

from ..models import Confidence, Evidence


SignalT = TypeVar("SignalT")


def fixture_evidence(items: Iterable[Mapping[str, Any]]) -> Tuple[Evidence, ...]:
    return tuple(
        Evidence(
            source=str(item["source"]),
            observed_at=str(item["observed_at"]),
            metric=str(item["metric"]),
            fact=item.get("fact"),
            confidence=Confidence(str(item.get("confidence", "very_low"))),
            reference=item.get("reference"),
        )
        for item in items
    )


class FixtureSource(ABC, Generic[SignalT]):
    """A local-data-only interface; implementations must not perform network I/O."""

    @abstractmethod
    def collect(self, fixture: Iterable[Mapping[str, Any]]) -> Tuple[SignalT, ...]:
        raise NotImplementedError
