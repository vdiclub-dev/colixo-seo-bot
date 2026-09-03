"""Normalized, privacy-conscious data models for the V3 foundation."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Tuple


class Confidence(str, Enum):
    VERY_LOW = "very_low"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DimensionLevel(str, Enum):
    UNKNOWN = "unknown"
    VERY_LOW = "very_low"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class Evidence:
    source: str
    observed_at: str
    metric: str
    fact: Any
    confidence: Confidence
    reference: Optional[str] = None


@dataclass(frozen=True)
class SearchSignal:
    topic: str
    query: str
    clicks: Optional[float] = None
    impressions: Optional[float] = None
    ctr: Optional[float] = None
    average_position: Optional[float] = None
    evidence: Tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TrafficSignal:
    topic: str
    organic_sessions: Optional[float] = None
    engaged_sessions: Optional[float] = None
    conversions: Optional[float] = None
    evidence: Tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RankSignal:
    topic: str
    query: str
    position: Optional[float] = None
    tracked_reference: Optional[str] = None
    evidence: Tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CompetitorSignal:
    topic: str
    competitor: str
    gap_level: DimensionLevel = DimensionLevel.UNKNOWN
    public_reference: Optional[str] = None
    evidence: Tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReviewSignal:
    topic: str
    source_platform: str
    competitor: str
    rating_average: Optional[float] = None
    review_count: Optional[int] = None
    observation_window: Optional[str] = None
    positive_topics: Tuple[str, ...] = field(default_factory=tuple)
    negative_topics: Tuple[str, ...] = field(default_factory=tuple)
    confidence: Confidence = Confidence.VERY_LOW
    evidence: Tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BusinessSignal:
    topic: str
    organic_sessions: Optional[float] = None
    pricing_simulations: Optional[float] = None
    accounts_created: Optional[float] = None
    orders_started: Optional[float] = None
    orders_completed: Optional[float] = None
    commercial_contacts: Optional[float] = None
    revenue: Optional[float] = None
    margin: Optional[float] = None
    evidence: Tuple[Evidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OpportunityScore:
    topic: str
    search_demand: DimensionLevel
    rank_opportunity: DimensionLevel
    commercial_fit: DimensionLevel
    conversion_signal: DimensionLevel
    competitive_gap: DimensionLevel
    reputation_gap: DimensionLevel
    evidence_confidence: DimensionLevel
    final_score: int
    confidence: Confidence
    explanation: Tuple[str, ...]
    unknown_dimensions: Tuple[str, ...]

    def dimensions(self) -> Mapping[str, DimensionLevel]:
        return {
            "search_demand": self.search_demand,
            "rank_opportunity": self.rank_opportunity,
            "commercial_fit": self.commercial_fit,
            "conversion_signal": self.conversion_signal,
            "competitive_gap": self.competitive_gap,
            "reputation_gap": self.reputation_gap,
            "evidence_confidence": self.evidence_confidence,
        }


@dataclass(frozen=True)
class Recommendation:
    topic: str
    action: str
    strength: str
    score: int
    confidence: Confidence
    evidence: Tuple[Evidence, ...]
    reasoning: Tuple[str, ...]
