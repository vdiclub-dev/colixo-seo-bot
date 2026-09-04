"""Conversion semantics and scoring safeguards for the offline V3 runtime."""

from scripts.v3.models import (
    BusinessSignal,
    Confidence,
    DimensionLevel,
    Evidence,
    RankSignal,
    SearchSignal,
    TrafficSignal,
)
from scripts.v3.scoring import (
    MIN_ORGANIC_SESSIONS_FOR_ZERO_CONVERSION_EVIDENCE,
    MIN_SUBSTANTIVE_DIMENSIONS_FOR_MODERATE,
    recommendation_for,
    score_opportunity,
)


TOPIC = "parcel_delivery"


def high_confidence_evidence():
    return (
        Evidence(
            source="trusted_fixture",
            observed_at="2026-09-04",
            metric="sample_safety",
            fact="deterministic",
            confidence=Confidence.HIGH,
        ),
    )


def conversion_level(*, traffic=None, business=None):
    return score_opportunity(
        TOPIC, traffic=traffic, business=business
    ).conversion_signal


def test_zero_conversion_threshold_constants_are_explicit():
    assert MIN_ORGANIC_SESSIONS_FOR_ZERO_CONVERSION_EVIDENCE == 50
    assert MIN_SUBSTANTIVE_DIMENSIONS_FOR_MODERATE == 2


def test_trusted_zero_conversion_below_threshold_is_unknown():
    assert conversion_level(
        traffic=TrafficSignal(TOPIC, organic_sessions=None, conversions=0)
    ) is DimensionLevel.UNKNOWN
    assert conversion_level(
        traffic=TrafficSignal(TOPIC, organic_sessions=10, conversions=0)
    ) is DimensionLevel.UNKNOWN
    assert conversion_level(
        traffic=TrafficSignal(TOPIC, organic_sessions=49, conversions=0)
    ) is DimensionLevel.UNKNOWN


def test_trusted_zero_conversion_at_threshold_is_very_low():
    assert conversion_level(
        traffic=TrafficSignal(TOPIC, organic_sessions=50, conversions=0)
    ) is DimensionLevel.VERY_LOW


def test_positive_conversion_remains_evidence_even_with_small_sample():
    assert conversion_level(
        traffic=TrafficSignal(TOPIC, organic_sessions=5, conversions=1)
    ) is DimensionLevel.LOW


def test_missing_conversion_is_unknown_even_with_large_sample():
    assert conversion_level(
        traffic=TrafficSignal(TOPIC, organic_sessions=100, conversions=None)
    ) is DimensionLevel.UNKNOWN


def test_business_zero_remains_known_without_traffic_threshold():
    assert conversion_level(
        business=BusinessSignal(TOPIC, orders_completed=0)
    ) is DimensionLevel.VERY_LOW


def test_missing_business_conversion_remains_unknown():
    assert conversion_level(
        business=BusinessSignal(TOPIC, orders_completed=None)
    ) is DimensionLevel.UNKNOWN


def test_small_zero_conversion_samples_are_not_combined_to_cross_threshold():
    signals = (
        TrafficSignal(TOPIC, organic_sessions=30, conversions=0),
        TrafficSignal(TOPIC, organic_sessions=30, conversions=0),
    )
    assert conversion_level(traffic=signals) is DimensionLevel.UNKNOWN


def test_evidence_confidence_alone_cannot_create_score_or_recommendation():
    traffic = TrafficSignal(
        TOPIC,
        organic_sessions=6,
        conversions=None,
        evidence=high_confidence_evidence(),
    )
    score = score_opportunity(TOPIC, traffic=traffic)

    assert score.evidence_confidence is DimensionLevel.HIGH
    assert score.final_score == 0
    assert recommendation_for(score).strength == "weak"


def test_one_substantive_dimension_cannot_create_moderate_recommendation():
    search = SearchSignal(
        TOPIC,
        "livraison colis",
        impressions=100,
        evidence=high_confidence_evidence(),
    )
    score = score_opportunity(TOPIC, search=search)

    assert score.search_demand is DimensionLevel.MEDIUM
    assert score.evidence_confidence is DimensionLevel.HIGH
    assert score.final_score >= 50
    assert recommendation_for(score).strength == "weak"


def test_two_substantive_dimensions_can_create_moderate_recommendation():
    proof = high_confidence_evidence()
    search = SearchSignal(
        TOPIC,
        "livraison colis",
        impressions=100,
        evidence=proof,
    )
    rank = RankSignal(
        TOPIC,
        "livraison colis",
        position=10,
        evidence=proof,
    )
    score = score_opportunity(TOPIC, search=search, rank=rank)

    assert score.search_demand is DimensionLevel.MEDIUM
    assert score.rank_opportunity is DimensionLevel.HIGH
    assert score.final_score >= 50
    assert recommendation_for(score).strength == "moderate"
