"""Versioned, query-free history values. No storage, clock, or network activity.

Numbers are Decimal strings in JSON, except the explicitly authorized integer
raw_row_count counter and exact boolean flags. History is provenance, not an
authorization mechanism. Projection never serializes the input AgentResult.
"""

import hashlib
import json
import re
from dataclasses import dataclass, fields, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple, Union

from .agent import AgentResult
from .config import RuntimeState, SOURCE_NAMES
from .models import Confidence, DimensionLevel, OpportunityScore, Recommendation
from .source_factory import GSCRuntimeCoverage


HISTORICAL_SNAPSHOT_SCHEMA_VERSION = 1
_PRIVATE_NAMES = (
    "query", "raw", "evidence", "response", "email", "phone", "token",
    "credential", "secret", "reference", "markdown", "action", "reasoning",
)
_METRIC_PRIVATE_NAMES = _PRIVATE_NAMES + ("url", "location")
_STRENGTH_CODES = {
    "strong": "prioritize_reviewed_market_experiment",
    "moderate": "validate_with_additional_evidence",
    "weak": "collect_more_evidence",
}
_MODES = ("local_fixture", "ga4_data_api", "gsc_search_analytics_api")
_SCOPES = ("local_fixture", "configured_read_only", "returned_query_rows_non_exhaustive",
           "consented_traffic_aggregate")
_DIMENSIONS = (
    "search_demand", "rank_opportunity", "commercial_fit", "conversion_signal",
    "competitive_gap", "reputation_gap", "evidence_confidence",
)
_GSC_COUNTS = (
    "raw_row_count", "accepted_signal_count", "brand_row_count", "unmapped_row_count",
    "pii_filtered_row_count", "row_limit",
)
_GSC_TOTALS = (
    "all_rows_clicks", "all_rows_impressions", "accepted_clicks", "accepted_impressions",
    "brand_clicks", "brand_impressions",
)
_GSC_METRICS = frozenset(_GSC_COUNTS + _GSC_TOTALS + ("row_limit_reached",))


class HistoricalSnapshotError(ValueError):
    """Safe contract error; invalid input values are never included."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HistoricalSnapshotError(message)


def _machine(value: object, pattern: str, limit: int) -> None:
    _require(type(value) is str and 0 < len(value) <= limit
             and re.fullmatch(pattern, value) is not None, "invalid machine identifier")


def _privacy_name(name: str, *, context: str) -> None:
    # Exceptions are exact and restricted to their designated field contexts.
    if context == "metric" and name == "raw_row_count":
        return
    if context == "topic_field" and name == "evidence_confidence":
        return
    prohibited = _METRIC_PRIVATE_NAMES if context == "metric" else _PRIVATE_NAMES
    _require(not any(word in name for word in prohibited), "privacy-sensitive name refused")


def _enum(value: object, enum_type):
    if type(value) is enum_type:
        return value
    _require(type(value) is str, "invalid enum value")
    try:
        return enum_type(value)
    except ValueError:
        raise HistoricalSnapshotError("invalid enum value") from None


def _iso(value: object) -> date:
    _machine(value, r"\d{4}-\d{2}-\d{2}", 10)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HistoricalSnapshotError("invalid ISO date") from None


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _decimal(value: object) -> Decimal:
    _require(type(value) in (Decimal, int, str), "numeric value must be Decimal, int or decimal string")
    if type(value) is str:
        _require(len(value) <= 256 and re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", value) is not None,
                 "invalid decimal input")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError, OverflowError):
        raise HistoricalSnapshotError("invalid decimal input") from None
    _require(number.is_finite(), "nonfinite decimal refused")
    _require(len(number.as_tuple().digits) <= 256 and abs(number.as_tuple().exponent) <= 1024,
             "decimal exceeds contract bounds")
    return Decimal(_decimal_text(number))


@dataclass(frozen=True, slots=True)
class HistoricalMetric:
    name: str
    value: Union[Decimal, bool, int]
    unit: str

    def __post_init__(self):
        _machine(self.name, r"[a-z]+(?:_[a-z0-9]+)*", 64)
        _privacy_name(self.name, context="metric")
        _require(self.unit in ("count", "ratio", "position", "score", "currency_chf", "flag")
                 and type(self.unit) is str, "invalid metric unit")
        if self.name == "raw_row_count":
            _require(self.unit == "count" and type(self.value) is int and self.value >= 0,
                     "raw_row_count requires an exact nonnegative integer")
            return
        if self.unit == "flag":
            _require(type(self.value) is bool, "flag requires an exact boolean")
            return
        number = _decimal(self.value)
        _require(self.unit != "count" or number >= 0, "invalid count")
        _require(self.unit != "ratio" or 0 <= number <= 1, "invalid ratio")
        _require(self.unit != "position" or number > 0, "invalid position")
        _require(self.unit != "score" or 0 <= number <= 100, "invalid score")
        object.__setattr__(self, "value", number)


@dataclass(frozen=True, slots=True)
class HistoricalSourceSnapshot:
    source: str
    mode: str
    signal_count: int
    scope_code: str
    metrics: Tuple[HistoricalMetric, ...]

    def __post_init__(self):
        _require(type(self.source) is str and self.source in SOURCE_NAMES, "invalid source")
        _require(type(self.mode) is str and self.mode in _MODES, "invalid source mode")
        _require(type(self.signal_count) is int and self.signal_count >= 0, "invalid signal count")
        _require(type(self.scope_code) is str and self.scope_code in _SCOPES, "invalid scope code")
        _require(self.mode != "ga4_data_api" or self.source == "analytics", "source mode mismatch")
        _require(self.mode != "gsc_search_analytics_api" or self.source == "search_console", "source mode mismatch")
        _require(type(self.metrics) is tuple and all(type(m) is HistoricalMetric for m in self.metrics),
                 "metrics must be typed immutable values")
        metrics = tuple(sorted((replace(m) for m in self.metrics), key=lambda m: m.name))
        _require(len({m.name for m in metrics}) == len(metrics), "duplicate metric")
        if any(m.name == "raw_row_count" for m in metrics):
            _require(self.source == "search_console" and self.mode == "gsc_search_analytics_api"
                     and self.scope_code == "returned_query_rows_non_exhaustive",
                     "raw_row_count is restricted to GSC aggregates")
        if self.mode == "gsc_search_analytics_api":
            _require(self.scope_code == "returned_query_rows_non_exhaustive", "invalid GSC scope")
            if metrics:
                _validate_gsc_metrics(metrics, self.signal_count)
        object.__setattr__(self, "metrics", metrics)


def _validate_gsc_metrics(metrics: Tuple[HistoricalMetric, ...], signal_count: int) -> None:
    by_name = {m.name: m for m in metrics}
    _require(set(by_name) == _GSC_METRICS, "incomplete GSC aggregate set")
    for name in _GSC_COUNTS + _GSC_TOTALS:
        _require(by_name[name].unit == "count", "invalid GSC metric unit")
    for name in _GSC_COUNTS:
        v = by_name[name].value
        _require(v == int(v), "GSC row counter must be integral")
    v = {name: metric.value for name, metric in by_name.items()}
    _require(v["raw_row_count"] == sum(v[n] for n in _GSC_COUNTS[1:5]), "invalid GSC partition")
    _require(v["accepted_signal_count"] == signal_count, "GSC signal count mismatch")
    _require(v["row_limit"] == 25000 and v["raw_row_count"] <= 25000, "invalid GSC row limit")
    _require(by_name["row_limit_reached"].unit == "flag"
             and v["row_limit_reached"] is (v["raw_row_count"] == 25000), "invalid GSC limit flag")
    for suffix in ("clicks", "impressions"):
        _require(v["accepted_" + suffix] + v["brand_" + suffix] <= v["all_rows_" + suffix],
                 "invalid GSC aggregate totals")


@dataclass(frozen=True, slots=True)
class HistoricalTopicSnapshot:
    topic: str
    final_score: Decimal
    confidence: Confidence
    search_demand: DimensionLevel
    rank_opportunity: DimensionLevel
    commercial_fit: DimensionLevel
    conversion_signal: DimensionLevel
    competitive_gap: DimensionLevel
    reputation_gap: DimensionLevel
    evidence_confidence: DimensionLevel
    recommendation_strength: str
    recommendation_code: str

    def __post_init__(self):
        _machine(self.topic, r"[a-z]+(?:_[a-z0-9]+)*", 64)
        _privacy_name(self.topic, context="topic_identifier")
        number = _decimal(self.final_score)
        _require(0 <= number <= 100, "invalid final score")
        object.__setattr__(self, "final_score", number)
        object.__setattr__(self, "confidence", _enum(self.confidence, Confidence))
        for name in _DIMENSIONS:
            object.__setattr__(self, name, _enum(getattr(self, name), DimensionLevel))
        _require(type(self.recommendation_strength) is str
                 and self.recommendation_strength in _STRENGTH_CODES, "invalid recommendation strength")
        _require(type(self.recommendation_code) is str
                 and self.recommendation_code == _STRENGTH_CODES[self.recommendation_strength],
                 "invalid recommendation code")


@dataclass(frozen=True, slots=True)
class HistoricalSnapshot:
    schema_version: int
    agent: str
    execution_id: str
    code_revision: str
    runtime_state: RuntimeState
    observed_at: str
    period_start: str
    period_end: str
    outcome: str
    recommendation_count: int
    sources: Tuple[HistoricalSourceSnapshot, ...]
    topics: Tuple[HistoricalTopicSnapshot, ...]
    safe_failure_code: Optional[str]

    def __post_init__(self):
        _require(type(self.schema_version) is int and self.schema_version == HISTORICAL_SNAPSHOT_SCHEMA_VERSION,
                 "unsupported snapshot schema")
        _machine(self.agent, r"[a-z]+(?:_[a-z0-9]+)*", 64)
        _privacy_name(self.agent, context="agent_identifier")
        _machine(self.execution_id, r"[A-Za-z0-9]+(?:[:_-][A-Za-z0-9]+)*", 128)
        _machine(self.code_revision, r"[0-9a-f]{40}", 40)
        object.__setattr__(self, "runtime_state", _enum(self.runtime_state, RuntimeState))
        _require(_iso(self.period_start) <= _iso(self.period_end) <= _iso(self.observed_at), "invalid period ordering")
        _require(type(self.outcome) is str and self.outcome in ("success", "failed"), "invalid outcome")
        if self.outcome == "success":
            _require(self.safe_failure_code is None, "success cannot carry a failure code")
        else:
            _machine(self.safe_failure_code, r"[A-Z]+(?:_[A-Z0-9]+)*", 64)
        _require(type(self.recommendation_count) is int and self.recommendation_count >= 0,
                 "invalid recommendation count")
        _require(type(self.sources) is tuple and all(type(s) is HistoricalSourceSnapshot for s in self.sources),
                 "sources must be typed immutable values")
        _require(type(self.topics) is tuple and all(type(t) is HistoricalTopicSnapshot for t in self.topics),
                 "topics must be typed immutable values")
        sources = tuple(sorted((replace(s) for s in self.sources), key=lambda s: s.source))
        topics = tuple(sorted((replace(t) for t in self.topics), key=lambda t: t.topic))
        _require(len(sources) == 6 and {s.source for s in sources} == set(SOURCE_NAMES), "exactly six unique sources required")
        _require(len({t.topic for t in topics}) == len(topics), "duplicate topic")
        _require(self.recommendation_count == len(topics), "recommendation count mismatch")
        live = {RuntimeState.GA4_READ_ONLY: ("analytics", "ga4_data_api"),
                RuntimeState.GSC_READ_ONLY: ("search_console", "gsc_search_analytics_api")}.get(self.runtime_state)
        for source in sources:
            expected = live[1] if live and source.source == live[0] else "local_fixture"
            _require(source.mode == expected, "runtime provenance mismatch")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "topics", topics)


# Check the persisted field vocabulary, without giving metric names a blanket
# exception for either privacy-sensitive substring.
for _model in (HistoricalMetric, HistoricalSourceSnapshot, HistoricalTopicSnapshot, HistoricalSnapshot):
    for _field in fields(_model):
        _privacy_name(_field.name, context="topic_field" if _model is HistoricalTopicSnapshot else "field")


def snapshot_from_agent_result(
    *, result: AgentResult, execution_id: str, code_revision: str,
    runtime_state: RuntimeState, observed_at: str, period_start: str, period_end: str,
) -> HistoricalSnapshot:
    """Copy only approved values. Never inspect prose, queries, or evidence."""
    _require(type(result) is AgentResult, "AgentResult required")
    state = _enum(runtime_state, RuntimeState)
    _require(set(result.source_counts) == set(SOURCE_NAMES)
             and set(result.source_modes) == set(SOURCE_NAMES), "source inventory mismatch")
    coverage = result.gsc_coverage
    if coverage is not None:
        _require(state is RuntimeState.GSC_READ_ONLY and type(coverage) is GSCRuntimeCoverage,
                 "GSC coverage provenance mismatch")
        _require(coverage.totals_scope == "returned_query_rows_non_exhaustive", "invalid GSC totals scope")
    sources = []
    for name in SOURCE_NAMES:
        mode = result.source_modes[name]
        scope = {"local_fixture": "local_fixture", "ga4_data_api": "consented_traffic_aggregate",
                 "gsc_search_analytics_api": "returned_query_rows_non_exhaustive"}.get(mode)
        metrics = ()
        if name == "search_console" and coverage is not None:
            for field in _GSC_COUNTS:
                _require(type(getattr(coverage, field)) is int and getattr(coverage, field) >= 0,
                         "invalid GSC aggregate counter")
            metrics = tuple(HistoricalMetric(field, getattr(coverage, field), "count")
                            for field in _GSC_COUNTS + _GSC_TOTALS) + (
                HistoricalMetric("row_limit_reached", coverage.row_limit_reached, "flag"),)
        sources.append(HistoricalSourceSnapshot(name, mode, result.source_counts[name], scope, metrics))
    _require(type(result.scores) is tuple and all(type(s) is OpportunityScore for s in result.scores), "invalid scores")
    _require(type(result.recommendations) is tuple
             and all(type(r) is Recommendation for r in result.recommendations), "invalid recommendations")
    recommendations = {}
    for rec in result.recommendations:
        _require(rec.topic not in recommendations, "duplicate recommendation topic")
        recommendations[rec.topic] = rec
    _require(len({s.topic for s in result.scores}) == len(result.scores), "duplicate score topic")
    _require({s.topic for s in result.scores} == set(recommendations), "recommendation topic mismatch")
    topics = []
    for score in result.scores:
        rec = recommendations[score.topic]
        _require(type(rec.strength) is str and rec.strength in _STRENGTH_CODES, "invalid recommendation strength")
        topics.append(HistoricalTopicSnapshot(
            topic=score.topic, final_score=score.final_score, confidence=score.confidence,
            **{name: getattr(score, name) for name in _DIMENSIONS},
            recommendation_strength=rec.strength, recommendation_code=_STRENGTH_CODES[rec.strength],
        ))
    return HistoricalSnapshot(
        HISTORICAL_SNAPSHOT_SCHEMA_VERSION, "market_intelligence_v3", execution_id, code_revision,
        state, observed_at, period_start, period_end, "success", len(topics), tuple(sources), tuple(topics), None,
    )


def snapshot_to_primitive(snapshot: HistoricalSnapshot) -> dict:
    """Explicit projection of validated contract values only."""
    _require(type(snapshot) is HistoricalSnapshot, "HistoricalSnapshot required")
    snapshot = replace(snapshot)  # Revalidate nested values before serialization.
    return {
        "schema_version": snapshot.schema_version, "agent": snapshot.agent,
        "execution_id": snapshot.execution_id, "code_revision": snapshot.code_revision,
        "runtime_state": snapshot.runtime_state.value, "observed_at": snapshot.observed_at,
        "period_start": snapshot.period_start, "period_end": snapshot.period_end,
        "outcome": snapshot.outcome, "recommendation_count": snapshot.recommendation_count,
        "safe_failure_code": snapshot.safe_failure_code,
        "sources": [{"source": s.source, "mode": s.mode, "signal_count": s.signal_count,
                     "scope_code": s.scope_code,
                     "metrics": [{"name": m.name, "unit": m.unit,
                                  "value": _decimal_text(m.value) if type(m.value) is Decimal else m.value}
                                 for m in s.metrics]} for s in snapshot.sources],
        "topics": [{"topic": t.topic, "final_score": _decimal_text(t.final_score),
                    "confidence": t.confidence.value,
                    **{name: getattr(t, name).value for name in _DIMENSIONS},
                    "recommendation_strength": t.recommendation_strength,
                    "recommendation_code": t.recommendation_code} for t in snapshot.topics],
    }


def snapshot_to_json(snapshot: HistoricalSnapshot) -> str:
    return json.dumps(snapshot_to_primitive(snapshot), ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def snapshot_fingerprint(snapshot: HistoricalSnapshot) -> str:
    return hashlib.sha256(snapshot_to_json(snapshot).encode("utf-8")).hexdigest()
