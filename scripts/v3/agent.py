"""Authorized orchestration for the V3 offline and GA4 read-only states."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

from .config import V3Config, load_v3_config
from .models import OpportunityScore, Recommendation
from .report import render_markdown
from .scoring import recommendation_for, score_opportunity
from .source_factory import build_source_adapters, source_modes


@dataclass(frozen=True)
class AgentResult:
    scores: Tuple[OpportunityScore, ...]
    recommendations: Tuple[Recommendation, ...]
    markdown: str
    source_counts: Mapping[str, int]
    source_modes: Mapping[str, str]


class MarketIntelligenceAgent:
    """Combine normalized signals under a fully validated runtime profile."""

    def __init__(
        self,
        config_path: Optional[Path] = None,
        *,
        observed_at: Optional[str] = None,
        ga4_client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.config: V3Config = load_v3_config(config_path)
        self.source_modes = source_modes(self.config)
        self.adapters = build_source_adapters(
            self.config,
            observed_at=observed_at,
            ga4_client_factory=ga4_client_factory,
        )

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
                name: tuple(item for item in values if item.topic == topic)
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
                weights=self.config.score_weights,
            )
            evidence = tuple(
                evidence
                for signal_group in signals.values()
                for signal in signal_group
                for evidence in signal.evidence
            )
            scores.append(score)
            recommendations.append(
                recommendation_for(
                    score,
                    evidence,
                    policy=self.config.recommendation_policy,
                )
            )
        source_counts = {name: len(values) for name, values in collected.items()}
        score_tuple = tuple(scores)
        recommendation_tuple = tuple(recommendations)
        return AgentResult(
            scores=score_tuple,
            recommendations=recommendation_tuple,
            markdown=render_markdown(
                score_tuple,
                recommendation_tuple,
                source_counts,
                self.source_modes,
            ),
            source_counts=source_counts,
            source_modes=self.source_modes,
        )
