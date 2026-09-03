import hashlib
import json
from dataclasses import fields
from pathlib import Path

import pytest

from scripts.v3.agent import MarketIntelligenceAgent
from scripts.v3.config import load_v3_config
from scripts.v3.models import (
    BusinessSignal,
    CompetitorSignal,
    Confidence,
    DimensionLevel,
    Evidence,
    RankSignal,
    Recommendation,
    ReviewSignal,
    SearchSignal,
    TrafficSignal,
)
from scripts.v3.scoring import recommendation_for, score_opportunity
from scripts.v3.sources import BusinessMetricsFixtureSource, ReviewsFixtureSource


ROOT = Path(__file__).resolve().parents[1]
TOPIC = "livraison entreprise geneve"
PROTECTED_HASHES = {
    "scripts/seo_agent_v2.py": "4d1a79154158da8f1cdbaf4787492f500cba55d9b3e1352bd705d4a105e46325",
    "scripts/gsc_client.py": "066a1cc360f4da394fa1915a1cc34357955e7a76c3e9e38cddb2cc0f97726f59",
    "config/seo_agent_v2.json": "12ae5a2876a01b471a741cfbcc529e3fd80ea0c62fcc12f898eee6622a1aa4df",
    ".github/workflows/seo.yml": "ce969a269d26447a26ebaa266d44a3973f5fd975b4754d39fac2e3f3994a02b7",
}


def evidence(source="fixture", confidence=Confidence.MEDIUM, metric="impressions", fact=300):
    return Evidence(
        source=source,
        observed_at="2026-09-03",
        metric=metric,
        fact=fact,
        confidence=confidence,
    )


def combined_signals(confidence=Confidence.MEDIUM):
    shared = (evidence(confidence=confidence),)
    return {
        "search": SearchSignal(TOPIC, TOPIC, impressions=350, average_position=12, evidence=shared),
        "rank": RankSignal(TOPIC, TOPIC, position=12, evidence=shared),
        "business": BusinessSignal(
            TOPIC,
            pricing_simulations=12,
            orders_started=5,
            commercial_contacts=4,
            revenue=1000,
            evidence=shared,
        ),
    }


def test_score_is_bounded_zero_to_one_hundred():
    assert 0 <= score_opportunity("empty").final_score <= 100
    assert 0 <= score_opportunity(TOPIC, **combined_signals()).final_score <= 100


def test_missing_data_remains_unknown_and_is_not_imputed():
    score = score_opportunity("missing")
    assert set(score.unknown_dimensions) == {
        "search_demand",
        "rank_opportunity",
        "commercial_fit",
        "conversion_signal",
        "competitive_gap",
        "reputation_gap",
        "evidence_confidence",
    }
    assert score.search_demand is DimensionLevel.UNKNOWN
    assert score.final_score == 0


def test_low_confidence_never_produces_strong_recommendation():
    score = score_opportunity(TOPIC, **combined_signals(Confidence.LOW))
    assert recommendation_for(score).strength != "strong"


def test_search_rank_and_commercial_signals_combine():
    score = score_opportunity(TOPIC, **combined_signals())
    assert score.search_demand is DimensionLevel.MEDIUM
    assert score.rank_opportunity is DimensionLevel.HIGH
    assert score.commercial_fit is DimensionLevel.HIGH
    assert score.final_score > 50


def test_competitor_and_aggregated_reviews_combine():
    ev = (evidence(source="public-fixture", metric="coverage_gap", fact="high"),)
    score = score_opportunity(
        TOPIC,
        competitor=CompetitorSignal(TOPIC, "Competitor aggregate", DimensionLevel.HIGH, evidence=ev),
        review=ReviewSignal(
            TOPIC,
            "public-platform",
            "Competitor aggregate",
            rating_average=3.4,
            review_count=50,
            observation_window="90d",
            positive_topics=("speed",),
            negative_topics=("tracking", "support"),
            confidence=Confidence.HIGH,
            evidence=ev,
        ),
    )
    assert score.competitive_gap is DimensionLevel.HIGH
    assert score.reputation_gap is DimensionLevel.HIGH


def test_three_competitors_and_two_review_platforms_are_all_counted():
    competitors = tuple(
        CompetitorSignal(
            TOPIC,
            "competitor-{}".format(index),
            level,
            evidence=(evidence("competitor-{}".format(index)),),
        )
        for index, level in enumerate(
            (DimensionLevel.HIGH, DimensionLevel.MEDIUM, DimensionLevel.LOW),
            start=1,
        )
    )
    reviews = tuple(
        ReviewSignal(
            TOPIC,
            "platform-{}".format(index),
            "aggregate competitor",
            rating_average=3.2,
            review_count=100,
            observation_window="90d",
            negative_topics=("tracking", "support"),
            confidence=Confidence.HIGH,
            evidence=(evidence("reviews-{}".format(index)),),
        )
        for index in (1, 2)
    )
    score = score_opportunity(TOPIC, competitor=competitors, review=reviews)
    assert "competitor_signals=3" in score.explanation
    assert "review_signals=2" in score.explanation
    assert score.competitive_gap is DimensionLevel.MEDIUM
    assert score.reputation_gap is DimensionLevel.HIGH


def test_fixture_order_does_not_change_score_or_explanation():
    competitors = (
        CompetitorSignal(TOPIC, "a", DimensionLevel.HIGH),
        CompetitorSignal(TOPIC, "b", DimensionLevel.LOW),
        CompetitorSignal(TOPIC, "c", DimensionLevel.MEDIUM),
    )
    reviews = (
        ReviewSignal(TOPIC, "one", "a", 3.2, 100, "90d", (), ("support", "tracking"), Confidence.HIGH),
        ReviewSignal(TOPIC, "two", "b", 4.5, 20, "90d", ("speed",), (), Confidence.MEDIUM),
    )
    forward = score_opportunity(TOPIC, competitor=competitors, review=reviews)
    reverse = score_opportunity(
        TOPIC,
        competitor=tuple(reversed(competitors)),
        review=tuple(reversed(reviews)),
    )
    assert forward == reverse


def test_duplicate_and_additional_signals_are_not_silently_dropped():
    repeated = CompetitorSignal(TOPIC, "same fixture", DimensionLevel.HIGH)
    score = score_opportunity(
        TOPIC,
        competitor=(repeated, repeated, repeated),
        rank=(
            RankSignal(TOPIC, TOPIC, position=8),
            RankSignal(TOPIC, TOPIC, position=12),
        ),
    )
    assert "competitor_signals=3" in score.explanation
    assert "rank_signals=2" in score.explanation


def test_agent_preserves_every_topic_signal_and_evidence():
    def proof(source, index):
        return [{
            "source": "{}-{}".format(source, index),
            "observed_at": "2026-09-03",
            "metric": "fixture",
            "fact": index,
            "confidence": "medium",
        }]

    fixtures = {
        "search_console": [
            {"topic": TOPIC, "query": TOPIC, "impressions": 100, "evidence": proof("search", index)}
            for index in (1, 2)
        ],
        "analytics": [
            {"topic": TOPIC, "conversions": 2, "evidence": proof("analytics", index)}
            for index in (1, 2)
        ],
        "rank_tracker": [
            {"topic": TOPIC, "query": TOPIC, "position": 10, "evidence": proof("rank", index)}
            for index in (1, 2)
        ],
        "competitors": [
            {
                "topic": TOPIC,
                "competitor": "competitor-{}".format(index),
                "gap_level": "medium",
                "evidence": proof("competitor", index),
            }
            for index in (1, 2, 3)
        ],
        "reviews": [
            {
                "topic": TOPIC,
                "source_platform": "platform-{}".format(index),
                "competitor": "aggregate competitor",
                "rating_average": 4.0,
                "review_count": 20,
                "observation_window": "90d",
                "negative_topics": ["tracking"],
                "confidence": "medium",
                "evidence": proof("review", index),
            }
            for index in (1, 2)
        ],
        "business_metrics": [
            {"topic": TOPIC, "orders_started": 2, "evidence": proof("business", index)}
            for index in (1, 2)
        ],
    }
    result = MarketIntelligenceAgent().run(fixtures)
    explanation = result.scores[0].explanation
    for label, count in (
        ("search_signals", 2),
        ("traffic_signals", 2),
        ("rank_signals", 2),
        ("competitor_signals", 3),
        ("review_signals", 2),
        ("business_signals", 2),
        ("evidence_sources", 13),
    ):
        assert "{}={}".format(label, count) in explanation
    assert len(result.recommendations[0].evidence) == 13


def _write_config(tmp_path, *, search_weight=None, strong_min_score=None):
    payload = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    if search_weight is not None:
        payload["score_weights"]["search_demand"] = search_weight
    if strong_min_score is not None:
        payload["recommendation_policy"]["strong_min_score"] = strong_min_score
    path = tmp_path / "v3-config.json"
    path.write_text(json.dumps(payload))
    return path


def test_configured_weights_change_score_deterministically(tmp_path):
    fixture = {
        "search_console": [{"topic": TOPIC, "query": TOPIC, "impressions": 1}],
        "rank_tracker": [{"topic": TOPIC, "query": TOPIC, "position": 10}],
    }
    default_score = MarketIntelligenceAgent().run(fixture).scores[0].final_score
    weighted_path = _write_config(tmp_path, search_weight=100)
    weighted_agent = MarketIntelligenceAgent(weighted_path)
    first = weighted_agent.run(fixture).scores[0].final_score
    second = weighted_agent.run(fixture).scores[0].final_score
    assert first == second
    assert first != default_score


def test_configured_recommendation_threshold_is_effective(tmp_path):
    fixture = {
        "search_console": [{
            "topic": TOPIC,
            "query": TOPIC,
            "impressions": 350,
            "average_position": 12,
            "evidence": [{
                "source": "search",
                "observed_at": "2026-09-03",
                "metric": "impressions",
                "fact": 350,
                "confidence": "medium",
            }],
        }],
        "rank_tracker": [{
            "topic": TOPIC,
            "query": TOPIC,
            "position": 12,
            "evidence": [{
                "source": "rank",
                "observed_at": "2026-09-03",
                "metric": "position",
                "fact": 12,
                "confidence": "medium",
            }],
        }],
        "business_metrics": [{
            "topic": TOPIC,
            "pricing_simulations": 12,
            "orders_started": 5,
            "commercial_contacts": 4,
            "revenue": 1000,
            "evidence": [{
                "source": "business",
                "observed_at": "2026-09-03",
                "metric": "aggregate_activity",
                "fact": "observed",
                "confidence": "medium",
            }],
        }],
    }
    default_result = MarketIntelligenceAgent().run(fixture)
    assert default_result.recommendations[0].strength == "strong"
    strict_path = _write_config(tmp_path, strong_min_score=100)
    strict_result = MarketIntelligenceAgent(strict_path).run(fixture)
    assert strict_result.recommendations[0].strength != "strong"


def test_real_config_is_loaded_by_default():
    agent = MarketIntelligenceAgent()
    assert agent.config == load_v3_config()
    assert agent.config.source_path == ROOT / "config/seo_agent_v3.json"


def test_one_bad_review_cannot_create_high_reputation_gap():
    review = ReviewSignal(
        TOPIC,
        "public-platform",
        "aggregate competitor",
        rating_average=2.5,
        review_count=1,
        observation_window="30d",
        negative_topics=("support", "tracking", "delays"),
        confidence=Confidence.HIGH,
    )
    score = score_opportunity(TOPIC, review=review)
    assert score.reputation_gap is not DimensionLevel.HIGH
    assert recommendation_for(score).strength != "strong"


def test_large_coherent_review_sample_can_create_high_reputation_gap():
    review = ReviewSignal(
        TOPIC,
        "public-platform",
        "aggregate competitor",
        rating_average=3.2,
        review_count=500,
        observation_window="12m",
        negative_topics=("support", "tracking", "delays"),
        confidence=Confidence.HIGH,
    )
    assert score_opportunity(TOPIC, review=review).reputation_gap is DimensionLevel.HIGH


def test_missing_review_count_remains_unknown_without_estimation():
    review = ReviewSignal(
        TOPIC,
        "public-platform",
        "aggregate competitor",
        rating_average=2.5,
        review_count=None,
        observation_window="unknown",
        negative_topics=("support",),
        confidence=Confidence.HIGH,
    )
    score = score_opportunity(TOPIC, review=review)
    assert score.reputation_gap is DimensionLevel.UNKNOWN


def test_confidence_is_balanced_by_source_not_evidence_row_volume():
    dominant = tuple(evidence("many", Confidence.HIGH) for _ in range(20))
    minority = (evidence("one", Confidence.VERY_LOW),)
    score = score_opportunity(
        TOPIC,
        search=(SearchSignal(TOPIC, TOPIC, impressions=300, evidence=dominant),),
        rank=(RankSignal(TOPIC, TOPIC, position=12, evidence=minority),),
    )
    assert score.confidence is Confidence.LOW
    assert "evidence_sources=2" in score.explanation
    assert "confidence_aggregation=equal_weight_per_evidence_source" in score.explanation


def test_report_models_have_no_pii_fields():
    prohibited = {
        "name",
        "email",
        "address",
        "phone",
        "parcel_number",
        "review_author",
        "review_text",
    }
    models = [Evidence, SearchSignal, TrafficSignal, RankSignal, CompetitorSignal,
              ReviewSignal, BusinessSignal, Recommendation]
    for model in models:
        assert prohibited.isdisjoint(field.name for field in fields(model))


def test_scoring_is_deterministic():
    first = score_opportunity(TOPIC, **combined_signals())
    second = score_opportunity(TOPIC, **combined_signals())
    assert first == second


def test_report_distinguishes_fact_inference_and_recommendation():
    fixtures = {
        "search_console": [
            {
                "topic": TOPIC,
                "query": TOPIC,
                "impressions": 300,
                "average_position": 12,
                "evidence": [
                    {
                        "source": "local-search-fixture",
                        "observed_at": "2026-09-03",
                        "metric": "impressions",
                        "fact": 300,
                        "confidence": "medium",
                    }
                ],
            }
        ]
    }
    report = MarketIntelligenceAgent().run(fixtures).markdown
    assert "FACT:" in report
    assert "INFERENCE:" in report
    assert "RECOMMENDATION:" in report
    for number, title in enumerate(
        [
            "Executive summary",
            "Search demand",
            "Rank opportunities",
            "Conversion signals",
            "Competitive gaps",
            "Customer reputation signals",
            "Commercial value",
            "Recommended actions",
            "Unknown / insufficient evidence",
            "Safety / data provenance",
        ],
        start=1,
    ):
        assert "## {}. {}".format(number, title) in report


def test_review_adapter_keeps_aggregates_and_synthetic_topics_only():
    result = ReviewsFixtureSource().collect(
        [
            {
                "topic": TOPIC,
                "source_platform": "public-platform",
                "competitor": "aggregate competitor",
                "rating_average": 4.1,
                "review_count": 21,
                "observation_window": "90d",
                "positive_topics": ["speed"],
                "negative_topics": ["tracking"],
                "confidence": "low",
            }
        ]
    )[0]
    assert result.positive_topics == ("speed",)
    assert result.negative_topics == ("tracking",)
    assert not hasattr(result, "author")
    assert not hasattr(result, "text")


def test_review_adapter_rejects_author_and_full_text_fields():
    with pytest.raises(ValueError, match="Unsupported review fixture fields"):
        ReviewsFixtureSource().collect(
            [
                {
                    "topic": TOPIC,
                    "source_platform": "public-platform",
                    "competitor": "aggregate competitor",
                    "author": "forbidden",
                    "review_text": "forbidden",
                }
            ]
        )


def test_business_adapter_rejects_non_aggregate_fields():
    with pytest.raises(ValueError, match="Unsupported business fixture fields"):
        BusinessMetricsFixtureSource().collect(
            [{"topic": TOPIC, "organic_sessions": 10, "email": "forbidden@example.invalid"}]
        )


def test_v3_config_is_offline_read_only_and_proposal_only():
    config = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    assert config["mode"] == {
        "read_only": True,
        "proposal_only": True,
        "network_enabled": False,
        "site_publication_enabled": False,
    }
    assert set(config["phase_1_sources"].values()) == {"local_fixture"}


def test_v2_and_production_workflow_are_byte_unchanged():
    for relative_path, expected_hash in PROTECTED_HASHES.items():
        actual = hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()
        assert actual == expected_hash, relative_path


def test_v3_source_tree_contains_no_network_or_supabase_clients():
    forbidden = ("import requests", "from requests", "urllib.request", "supabase", "google.analytics")
    content = "\n".join(
        path.read_text() for path in sorted((ROOT / "scripts/v3").rglob("*.py"))
    ).lower()
    assert all(token not in content for token in forbidden)
