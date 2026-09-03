"""Aggregated business-metrics fixture adapter; no customer records are accepted."""

from typing import Any, Iterable, Mapping, Tuple

from ..models import BusinessSignal
from .base import FixtureSource, fixture_evidence


class BusinessMetricsFixtureSource(FixtureSource[BusinessSignal]):
    ALLOWED_METRICS = (
        "organic_sessions",
        "pricing_simulations",
        "accounts_created",
        "orders_started",
        "orders_completed",
        "commercial_contacts",
        "revenue",
        "margin",
    )

    def collect(self, fixture: Iterable[Mapping[str, Any]]) -> Tuple[BusinessSignal, ...]:
        signals = []
        for item in fixture:
            unexpected = set(item) - set(self.ALLOWED_METRICS) - {"topic", "evidence"}
            if unexpected:
                raise ValueError("Unsupported business fixture fields: {}".format(sorted(unexpected)))
            values = {name: item.get(name) for name in self.ALLOWED_METRICS}
            signals.append(
                BusinessSignal(
                    topic=str(item["topic"]),
                    evidence=fixture_evidence(item.get("evidence", ())),
                    **values,
                )
            )
        return tuple(signals)
