"""Validated loader for the single V3 scoring and recommendation configuration."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "seo_agent_v3.json"
REQUIRED_SCORE_DIMENSIONS = {
    "search_demand",
    "rank_opportunity",
    "commercial_fit",
    "conversion_signal",
    "competitive_gap",
    "reputation_gap",
    "evidence_confidence",
}
CONFIDENCE_ORDER = {"very_low": 0, "low": 1, "medium": 2, "high": 3}


@dataclass(frozen=True)
class RecommendationPolicy:
    strong_min_score: int
    strong_min_confidence: str
    strong_min_known_dimensions: int


@dataclass(frozen=True)
class V3Config:
    score_weights: Mapping[str, float]
    recommendation_policy: RecommendationPolicy
    source_path: Path


def load_v3_config(path: Optional[Path] = None) -> V3Config:
    """Load and validate V3 config; invalid or missing configuration fails closed."""

    source_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    weights = payload.get("score_weights")
    if not isinstance(weights, dict) or set(weights) != REQUIRED_SCORE_DIMENSIONS:
        raise ValueError("score_weights must define exactly the seven V3 dimensions")
    normalized_weights = {name: float(value) for name, value in weights.items()}
    if any(value <= 0 for value in normalized_weights.values()):
        raise ValueError("score_weights must all be positive")

    policy = payload.get("recommendation_policy")
    if not isinstance(policy, dict):
        raise ValueError("recommendation_policy is required")
    min_score = int(policy["strong_min_score"])
    min_confidence = str(policy["strong_min_confidence"])
    min_known = int(policy["strong_min_known_dimensions"])
    if not 0 <= min_score <= 100:
        raise ValueError("strong_min_score must be between 0 and 100")
    if min_confidence not in CONFIDENCE_ORDER:
        raise ValueError("strong_min_confidence is invalid")
    if not 1 <= min_known <= len(REQUIRED_SCORE_DIMENSIONS):
        raise ValueError("strong_min_known_dimensions is invalid")
    return V3Config(
        score_weights=normalized_weights,
        recommendation_policy=RecommendationPolicy(
            strong_min_score=min_score,
            strong_min_confidence=min_confidence,
            strong_min_known_dimensions=min_known,
        ),
        source_path=source_path,
    )
