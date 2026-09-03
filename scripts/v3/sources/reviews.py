"""Aggregated public-review fixture adapter; author data and full text are excluded."""

from typing import Any, Iterable, Mapping, Tuple

from ..models import Confidence, ReviewSignal
from .base import FixtureSource, fixture_evidence


class ReviewsFixtureSource(FixtureSource[ReviewSignal]):
    ALLOWED_FIELDS = {
        "topic",
        "source_platform",
        "competitor",
        "rating_average",
        "review_count",
        "observation_window",
        "positive_topics",
        "negative_topics",
        "confidence",
        "evidence",
    }

    def collect(self, fixture: Iterable[Mapping[str, Any]]) -> Tuple[ReviewSignal, ...]:
        signals = []
        for item in fixture:
            unexpected = set(item) - self.ALLOWED_FIELDS
            if unexpected:
                raise ValueError("Unsupported review fixture fields: {}".format(sorted(unexpected)))
            signals.append(ReviewSignal(
                topic=str(item["topic"]),
                source_platform=str(item["source_platform"]),
                competitor=str(item["competitor"]),
                rating_average=item.get("rating_average"),
                review_count=item.get("review_count"),
                observation_window=item.get("observation_window"),
                positive_topics=tuple(item.get("positive_topics", ())),
                negative_topics=tuple(item.get("negative_topics", ())),
                confidence=Confidence(str(item.get("confidence", "very_low"))),
                evidence=fixture_evidence(item.get("evidence", ())),
            ))
        return tuple(signals)
