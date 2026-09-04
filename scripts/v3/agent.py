"""Offline orchestration for V3 Phase 1."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from .config import V3Config, load_v3_config
from .models import OpportunityScore, Recommendation
from .report import render_markdown
from .scoring import recommendation_for, score_opportunity
from .source_factory import build_source_adapters


@dataclass(frozen=True)
class AgentResult:
    scores: Tuple[OpportunityScore, ...]
    recommendations: Tuple[Recommendation, ...]
    markdown: str
    source_counts: Mapping[str, int]


class MarketIntelligenceAgent:
    """Combine normalized local fixtures without side effects or network access."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.config: V3Config = load_v3_config(config_path)
        self.adapters = build_source_adapters(self.config)

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
            markdown=render_markdown(score_tuple, recommendation_tuple, source_counts),
            source_counts=source_counts,
        )
