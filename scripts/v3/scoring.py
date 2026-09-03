"""Deterministic, explainable opportunity scoring with explicit unknowns."""

from typing import Dict, Iterable, Optional, Sequence, Tuple

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

DEFAULT_WEIGHTS = {
    "search_demand": 1.2,
    "rank_opportunity": 1.2,
    "commercial_fit": 1.4,
    "conversion_signal": 1.2,
    "competitive_gap": 1.0,
    "reputation_gap": 0.8,
    "evidence_confidence": 1.2,
}


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


def _rank_level(position: Optional[float]) -> DimensionLevel:
    if position is None:
        return DimensionLevel.UNKNOWN
    if 4 <= position <= 20:
        return DimensionLevel.HIGH
    if 20 < position <= 50:
        return DimensionLevel.MEDIUM
    if 1 <= position < 4:
        return DimensionLevel.LOW
    if position > 50:
        return DimensionLevel.LOW
    return DimensionLevel.VERY_LOW


def _conversion_level(
    traffic: Optional[TrafficSignal], business: Optional[BusinessSignal]
) -> DimensionLevel:
    completed = business.orders_completed if business else None
    conversions = traffic.conversions if traffic else None
    values = [value for value in (completed, conversions) if value is not None]
    if not values:
        return DimensionLevel.UNKNOWN
    return _level_from_volume(sum(values), 5, 20)


def _commercial_level(business: Optional[BusinessSignal]) -> DimensionLevel:
    if business is None:
        return DimensionLevel.UNKNOWN
    values = (
        business.pricing_simulations,
        business.accounts_created,
        business.orders_started,
        business.commercial_contacts,
        business.revenue,
        business.margin,
    )
    known = [value for value in values if value is not None]
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


def _reputation_level(review: Optional[ReviewSignal]) -> DimensionLevel:
    if review is None or (review.rating_average is None and not review.negative_topics):
        return DimensionLevel.UNKNOWN
    if len(review.negative_topics) >= 3 or (
        review.rating_average is not None and review.rating_average < 3.5
    ):
        return DimensionLevel.HIGH
    if review.negative_topics or (
        review.rating_average is not None and review.rating_average < 4.2
    ):
        return DimensionLevel.MEDIUM
    return DimensionLevel.LOW


def _all_evidence(*signals: object) -> Tuple[Evidence, ...]:
    evidence = []
    for signal in signals:
        if signal is not None:
            evidence.extend(getattr(signal, "evidence", ()))
    return tuple(evidence)


def _aggregate_confidence(evidence: Sequence[Evidence]) -> Optional[Confidence]:
    if not evidence:
        return None
    average = sum(CONFIDENCE_POINTS[item.confidence] for item in evidence) / len(evidence)
    if average >= 80:
        return Confidence.HIGH
    if average >= 55:
        return Confidence.MEDIUM
    if average >= 25:
        return Confidence.LOW
    return Confidence.VERY_LOW


def _confidence_level(confidence: Optional[Confidence]) -> DimensionLevel:
    return (
        DimensionLevel(confidence.value)
        if confidence is not None
        else DimensionLevel.UNKNOWN
    )


def score_opportunity(
    topic: str,
    search: Optional[SearchSignal] = None,
    traffic: Optional[TrafficSignal] = None,
    rank: Optional[RankSignal] = None,
    competitor: Optional[CompetitorSignal] = None,
    review: Optional[ReviewSignal] = None,
    business: Optional[BusinessSignal] = None,
    weights: Optional[Dict[str, float]] = None,
) -> OpportunityScore:
    """Score known evidence only; unknown dimensions never receive invented points."""

    evidence = _all_evidence(search, traffic, rank, competitor, review, business)
    observed_confidence = _aggregate_confidence(evidence)
    confidence = observed_confidence or Confidence.VERY_LOW
    dimensions = {
        "search_demand": _level_from_volume(
            search.impressions if search else None, 100, 1000
        ),
        "rank_opportunity": _rank_level(
            rank.position if rank else (search.average_position if search else None)
        ),
        "commercial_fit": _commercial_level(business),
        "conversion_signal": _conversion_level(traffic, business),
        "competitive_gap": (
            competitor.gap_level if competitor else DimensionLevel.UNKNOWN
        ),
        "reputation_gap": _reputation_level(review),
        "evidence_confidence": _confidence_level(observed_confidence),
    }
    effective_weights = dict(DEFAULT_WEIGHTS)
    if weights:
        effective_weights.update(weights)
    known = {
        name: level
        for name, level in dimensions.items()
        if level is not DimensionLevel.UNKNOWN
    }
    denominator = sum(effective_weights[name] for name in known)
    raw_score = (
        sum(LEVEL_POINTS[level] * effective_weights[name] for name, level in known.items())
        / denominator
        if denominator
        else 0
    )
    final_score = max(0, min(100, int(round(raw_score))))
    unknown = tuple(name for name, level in dimensions.items() if level is DimensionLevel.UNKNOWN)
    explanation = tuple(
        "{}={}".format(name, level.value) for name, level in dimensions.items()
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
) -> Recommendation:
    """Return a proposal whose strength is capped by evidence sufficiency."""

    known_count = 7 - len(score.unknown_dimensions)
    if score.final_score >= 75 and score.confidence in (
        Confidence.MEDIUM,
        Confidence.HIGH,
    ) and known_count >= 4:
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
