"""Deterministic, explainable multi-signal scoring with explicit unknowns."""

from collections import defaultdict
from math import fsum, sqrt
from typing import Iterable, Mapping, Optional, Sequence, Tuple, TypeVar, Union

from .config import CONFIDENCE_ORDER, RecommendationPolicy, load_v3_config
from .models import (
    BusinessSignal,
    CompetitorSignal,
    Confidence,
    DimensionLevel,
    Evidence,
    OpportunityScore,
    RankSignal,
    Recommendation,
    ReviewSignal,
    SearchSignal,
    TrafficSignal,
)


LEVEL_POINTS = {
    DimensionLevel.VERY_LOW: 15,
    DimensionLevel.LOW: 35,
    DimensionLevel.MEDIUM: 65,
    DimensionLevel.HIGH: 90,
}

CONFIDENCE_POINTS = {
    Confidence.VERY_LOW: 15,
    Confidence.LOW: 35,
    Confidence.MEDIUM: 65,
    Confidence.HIGH: 90,
}

SignalT = TypeVar("SignalT")
SignalInput = Optional[Union[SignalT, Sequence[SignalT]]]


def _signals(value: SignalInput[SignalT]) -> Tuple[SignalT, ...]:
    if value is None:
        return ()
    if isinstance(value, (tuple, list)):
        return tuple(value)
    return (value,)


def _level_from_volume(value: Optional[float], medium: float, high: float) -> DimensionLevel:
    if value is None:
        return DimensionLevel.UNKNOWN
    if value >= high:
        return DimensionLevel.HIGH
    if value >= medium:
        return DimensionLevel.MEDIUM
    if value > 0:
        return DimensionLevel.LOW
    return DimensionLevel.VERY_LOW


def _level_from_points(points: Optional[float]) -> DimensionLevel:
    if points is None:
        return DimensionLevel.UNKNOWN
    if points >= 77.5:
        return DimensionLevel.HIGH
    if points >= 50:
        return DimensionLevel.MEDIUM
    if points >= 25:
        return DimensionLevel.LOW
    return DimensionLevel.VERY_LOW


def _sum_known(values: Iterable[Optional[float]]) -> Optional[float]:
    known = [float(value) for value in values if value is not None]
    return fsum(known) if known else None


def _search_level(signals: Sequence[SearchSignal]) -> DimensionLevel:
    return _level_from_volume(_sum_known(item.impressions for item in signals), 100, 1000)


def _rank_level(signals: Sequence[RankSignal], searches: Sequence[SearchSignal]) -> DimensionLevel:
    explicit = [float(item.position) for item in signals if item.position is not None]
    fallback = [
        float(item.average_position)
        for item in searches
        if item.average_position is not None
    ]
    positions = explicit or fallback
    if not positions:
        return DimensionLevel.UNKNOWN
    position = fsum(positions) / len(positions)
    if 4 <= position <= 20:
        return DimensionLevel.HIGH
    if 20 < position <= 50:
        return DimensionLevel.MEDIUM
    if 1 <= position < 4 or position > 50:
        return DimensionLevel.LOW
    return DimensionLevel.VERY_LOW


def _conversion_level(
    traffic: Sequence[TrafficSignal], business: Sequence[BusinessSignal]
) -> DimensionLevel:
    total = _sum_known(
        [item.conversions for item in traffic]
        + [item.orders_completed for item in business]
    )
    return _level_from_volume(total, 5, 20)


def _commercial_level(signals: Sequence[BusinessSignal]) -> DimensionLevel:
    if not signals:
        return DimensionLevel.UNKNOWN
    values = {
        metric: _sum_known(getattr(item, metric) for item in signals)
        for metric in (
            "pricing_simulations",
            "accounts_created",
            "orders_started",
            "commercial_contacts",
            "revenue",
            "margin",
        )
    }
    known = [value for value in values.values() if value is not None]
    if not known:
        return DimensionLevel.UNKNOWN
    active = sum(1 for value in known if value > 0)
    if active >= 4:
        return DimensionLevel.HIGH
    if active >= 2:
        return DimensionLevel.MEDIUM
    if active == 1:
        return DimensionLevel.LOW
    return DimensionLevel.VERY_LOW


def _competitor_level(signals: Sequence[CompetitorSignal]) -> DimensionLevel:
    known = [LEVEL_POINTS[item.gap_level] for item in signals if item.gap_level in LEVEL_POINTS]
    return _level_from_points(fsum(known) / len(known) if known else None)


def _single_review_level(review: ReviewSignal) -> DimensionLevel:
    """Sample-size guard: missing counts are unknown; fewer than five are capped low."""

    if review.review_count is None or review.review_count <= 0:
        return DimensionLevel.UNKNOWN
    if review.rating_average is None and not review.negative_topics:
        return DimensionLevel.UNKNOWN
    if review.review_count < 5:
        return DimensionLevel.LOW
    negative_strength = len(review.negative_topics)
    rating = review.rating_average
    raw_high = negative_strength >= 2 and rating is not None and rating < 3.5
    if raw_high and review.review_count >= 50 and review.confidence in (
        Confidence.MEDIUM,
        Confidence.HIGH,
    ):
        return DimensionLevel.HIGH
    if negative_strength or (rating is not None and rating < 4.2):
        return DimensionLevel.MEDIUM
    return DimensionLevel.LOW


def _reputation_level(signals: Sequence[ReviewSignal]) -> DimensionLevel:
    weighted = []
    for review in signals:
        level = _single_review_level(review)
        if level is DimensionLevel.UNKNOWN:
            continue
        # sqrt and a 100-review cap make sample size relevant without allowing a
        # single large platform to erase every other platform's observation.
        weight = sqrt(min(review.review_count or 0, 100))
        confidence_factor = CONFIDENCE_POINTS[review.confidence] / 100
        weighted.append((LEVEL_POINTS[level] * confidence_factor, weight))
    if not weighted:
        return DimensionLevel.UNKNOWN
    total_weight = fsum(weight for _, weight in weighted)
    return _level_from_points(
        fsum(points * weight for points, weight in weighted) / total_weight
    )


def _all_evidence(*signal_groups: Sequence[object]) -> Tuple[Evidence, ...]:
    return tuple(
        evidence
        for group in signal_groups
        for signal in group
        for evidence in getattr(signal, "evidence", ())
    )


def _aggregate_confidence(evidence: Sequence[Evidence]) -> Tuple[Optional[Confidence], int]:
    """Average within provenance first, then weight every source equally."""

    by_source = defaultdict(list)
    for item in evidence:
        by_source[item.source].append(CONFIDENCE_POINTS[item.confidence])
    if not by_source:
        return None, 0
    source_means = [
        fsum(by_source[source]) / len(by_source[source])
        for source in sorted(by_source)
    ]
    average = fsum(source_means) / len(source_means)
    if average >= 80:
        confidence = Confidence.HIGH
    elif average >= 55:
        confidence = Confidence.MEDIUM
    elif average >= 25:
        confidence = Confidence.LOW
    else:
        confidence = Confidence.VERY_LOW
    return confidence, len(by_source)


def _confidence_level(confidence: Optional[Confidence]) -> DimensionLevel:
    return DimensionLevel(confidence.value) if confidence else DimensionLevel.UNKNOWN


def score_opportunity(
    topic: str,
    search: SignalInput[SearchSignal] = None,
    traffic: SignalInput[TrafficSignal] = None,
    rank: SignalInput[RankSignal] = None,
    competitor: SignalInput[CompetitorSignal] = None,
    review: SignalInput[ReviewSignal] = None,
    business: SignalInput[BusinessSignal] = None,
    weights: Optional[Mapping[str, float]] = None,
) -> OpportunityScore:
    """Aggregate all topic signals; fixture order cannot affect the score."""

    searches = _signals(search)
    traffics = _signals(traffic)
    ranks = _signals(rank)
    competitors = _signals(competitor)
    reviews = _signals(review)
    businesses = _signals(business)
    evidence = _all_evidence(searches, traffics, ranks, competitors, reviews, businesses)
    observed_confidence, provenance_count = _aggregate_confidence(evidence)
    confidence = observed_confidence or Confidence.VERY_LOW
    dimensions = {
        "search_demand": _search_level(searches),
        "rank_opportunity": _rank_level(ranks, searches),
        "commercial_fit": _commercial_level(businesses),
        "conversion_signal": _conversion_level(traffics, businesses),
        "competitive_gap": _competitor_level(competitors),
        "reputation_gap": _reputation_level(reviews),
        "evidence_confidence": _confidence_level(observed_confidence),
    }
    effective_weights = dict(weights or load_v3_config().score_weights)
    known = {
        name: level
        for name, level in dimensions.items()
        if level is not DimensionLevel.UNKNOWN
    }
    denominator = fsum(effective_weights[name] for name in known)
    raw_score = (
        fsum(LEVEL_POINTS[level] * effective_weights[name] for name, level in known.items())
        / denominator
        if denominator
        else 0
    )
    final_score = max(0, min(100, int(round(raw_score))))
    unknown = tuple(name for name, level in dimensions.items() if level is DimensionLevel.UNKNOWN)
    counts = {
        "search_signals": len(searches),
        "traffic_signals": len(traffics),
        "rank_signals": len(ranks),
        "competitor_signals": len(competitors),
        "review_signals": len(reviews),
        "business_signals": len(businesses),
        "evidence_sources": provenance_count,
    }
    explanation = tuple(
        ["{}={}".format(name, level.value) for name, level in dimensions.items()]
        + ["{}={}".format(name, count) for name, count in counts.items()]
        + ["confidence_aggregation=equal_weight_per_evidence_source"]
    )
    return OpportunityScore(
        topic=topic,
        final_score=final_score,
        confidence=confidence,
        explanation=explanation,
        unknown_dimensions=unknown,
        **dimensions,
    )


def recommendation_for(
    score: OpportunityScore,
    evidence: Iterable[Evidence] = (),
    policy: Optional[RecommendationPolicy] = None,
) -> Recommendation:
    """Apply configured evidence sufficiency limits to recommendation strength."""

    effective_policy = policy or load_v3_config().recommendation_policy
    known_count = 7 - len(score.unknown_dimensions)
    confidence_sufficient = (
        CONFIDENCE_ORDER[score.confidence.value]
        >= CONFIDENCE_ORDER[effective_policy.strong_min_confidence]
    )
    if (
        score.final_score >= effective_policy.strong_min_score
        and confidence_sufficient
        and known_count >= effective_policy.strong_min_known_dimensions
    ):
        strength = "strong"
        action = "Prioritize a reviewed market opportunity experiment."
    elif score.final_score >= 50 and score.confidence is not Confidence.VERY_LOW:
        strength = "moderate"
        action = "Validate the opportunity with additional evidence before action."
    else:
        strength = "weak"
        action = "Collect more evidence; do not make a strong recommendation."
    return Recommendation(
        topic=score.topic,
        action=action,
        strength=strength,
        score=score.final_score,
        confidence=score.confidence,
        evidence=tuple(evidence),
        reasoning=score.explanation,
    )
