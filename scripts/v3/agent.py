"""Offline orchestration for V3 Phase 1."""

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from .models import OpportunityScore, Recommendation
from .report import render_markdown
from .scoring import recommendation_for, score_opportunity
from .sources import (
    AnalyticsFixtureSource,
    BusinessMetricsFixtureSource,
    CompetitorFixtureSource,
    RankTrackerFixtureSource,
    ReviewsFixtureSource,
    SearchConsoleFixtureSource,
)


@dataclass(frozen=True)
class AgentResult:
    scores: Tuple[OpportunityScore, ...]
    recommendations: Tuple[Recommendation, ...]
    markdown: str
    source_counts: Mapping[str, int]


class MarketIntelligenceAgent:
    """Combine normalized local fixtures without side effects or network access."""

    def __init__(self, weights: Optional[Dict[str, float]] = None) -> None:
        self.weights = weights
        self.adapters = {
            "search_console": SearchConsoleFixtureSource(),
            "analytics": AnalyticsFixtureSource(),
            "rank_tracker": RankTrackerFixtureSource(),
            "competitors": CompetitorFixtureSource(),
            "reviews": ReviewsFixtureSource(),
            "business_metrics": BusinessMetricsFixtureSource(),
        }

    def run(self, fixtures: Mapping[str, Any]) -> AgentResult:
        collected = {
            name: adapter.collect(fixtures.get(name, ()))
            for name, adapter in self.adapters.items()
        }
        topics = sorted(
            {
                signal.topic
                for signals in collected.values()
                for signal in signals
            }
        )
        scores = []
        recommendations = []
        for topic in topics:
            signals = {
                name: next((item for item in values if item.topic == topic), None)
                for name, values in collected.items()
            }
            score = score_opportunity(
                topic,
                search=signals["search_console"],
                traffic=signals["analytics"],
                rank=signals["rank_tracker"],
                competitor=signals["competitors"],
                review=signals["reviews"],
                business=signals["business_metrics"],
                weights=self.weights,
            )
            evidence = tuple(
                evidence
                for signal in signals.values()
                if signal is not None
                for evidence in signal.evidence
            )
            scores.append(score)
            recommendations.append(recommendation_for(score, evidence))
        source_counts = {name: len(values) for name, values in collected.items()}
        score_tuple = tuple(scores)
        recommendation_tuple = tuple(recommendations)
        return AgentResult(
            scores=score_tuple,
            recommendations=recommendation_tuple,
            markdown=render_markdown(score_tuple, recommendation_tuple, source_counts),
            source_counts=source_counts,
        )
