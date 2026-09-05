"""Fail-closed configuration loader for the three authorized V3 runtimes."""

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional, Tuple


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
SOURCE_NAMES = (
    "search_console",
    "analytics",
    "rank_tracker",
    "competitors",
    "reviews",
    "business_metrics",
)
_SOURCE_NAME_SET = frozenset(SOURCE_NAMES)
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "mode",
    "confidence_levels",
    "dimension_levels",
    "score_weights",
    "recommendation_policy",
    "phase_1_sources",
    "network_policy",
    "ga4_data_api",
    "gsc_search_analytics",
    "privacy",
}


class V3ConfigError(ValueError):
    """Raised when runtime configuration is incomplete, ambiguous, or unsafe."""


class RuntimeState(str, Enum):
    """Complete runtime states accepted by the V3 authorization boundary."""

    OFFLINE = "OFFLINE"
    GA4_READ_ONLY = "GA4_READ_ONLY"
    GSC_READ_ONLY = "GSC_READ_ONLY"


GSC_CONTRACT = MappingProxyType({
    "property": "sc-domain:colixo.ch", "type": "web", "data_state": "final",
    "dimension": "query", "row_limit": 25000,
    "start_offset_days": 28, "end_offset_days": 3,
    "brand": "coverage_only", "unmapped": "coverage_only", "pii": "exclude",
    "pagination": "disabled", "totals_scope": "returned_query_rows_non_exhaustive",
})


@dataclass(frozen=True)
class GSCSearchAnalyticsConfig:
    enabled: bool
    contract: Tuple[Tuple[str, object], ...]


def _load_gsc(value: object) -> GSCSearchAnalyticsConfig:
    if not isinstance(value, dict) or set(value) != {"enabled", *GSC_CONTRACT}:
        raise V3ConfigError("gsc_search_analytics must be explicit and complete")
    if type(value["enabled"]) is not bool or any(
        type(value[key]) is not type(expected) or value[key] != expected
        for key, expected in GSC_CONTRACT.items()
    ):
        raise V3ConfigError("gsc_search_analytics contract is immutable")
    return GSCSearchAnalyticsConfig(value["enabled"], tuple(sorted(GSC_CONTRACT.items())))


@dataclass(frozen=True)
class ModeConfig:
    read_only: bool
    proposal_only: bool
    network_enabled: bool
    site_publication_enabled: bool


@dataclass(frozen=True)
class SourceSelectionConfig:
    search_console: str
    analytics: str
    rank_tracker: str
    competitors: str
    reviews: str
    business_metrics: str

    def value_for(self, source_name: str) -> str:
        values = {
            "search_console": self.search_console,
            "analytics": self.analytics,
            "rank_tracker": self.rank_tracker,
            "competitors": self.competitors,
            "reviews": self.reviews,
            "business_metrics": self.business_metrics,
        }
        try:
            return values[source_name]
        except KeyError:
            raise V3ConfigError("unknown V3 source") from None


@dataclass(frozen=True)
class NetworkPolicyConfig:
    default: str
    search_console: str
    analytics: str
    rank_tracker: str
    competitors: str
    reviews: str
    business_metrics: str

    def value_for(self, source_name: str) -> str:
        values = {
            "search_console": self.search_console,
            "analytics": self.analytics,
            "rank_tracker": self.rank_tracker,
            "competitors": self.competitors,
            "reviews": self.reviews,
            "business_metrics": self.business_metrics,
        }
        try:
            return values[source_name]
        except KeyError:
            raise V3ConfigError("unknown V3 source") from None


@dataclass(frozen=True)
class GA4DataAPIConfig:
    enabled: bool
    property_id: str
    resource: str
    channel: str
    start_date: str
    end_date: str
    landing_page_topics: Tuple[Tuple[str, str], ...]
    unmapped_and_legal_pages: str


@dataclass(frozen=True)
class RecommendationPolicy:
    strong_min_score: int
    strong_min_confidence: str
    strong_min_known_dimensions: int


@dataclass(frozen=True)
class V3Config:
    runtime_state: RuntimeState
    mode: ModeConfig
    phase_1_sources: SourceSelectionConfig
    network_policy: NetworkPolicyConfig
    ga4_data_api: GA4DataAPIConfig
    gsc_search_analytics: GSCSearchAnalyticsConfig
    score_weights: Mapping[str, float]
    recommendation_policy: RecommendationPolicy
    source_path: Path


def load_v3_config(path: Optional[Path] = None) -> V3Config:
    """Load one exact, fully authorized runtime state and reject all mixtures."""

    source_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise V3ConfigError("V3 configuration cannot be loaded") from error
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_FIELDS:
        raise V3ConfigError("V3 configuration fields are incomplete or unexpected")
    if payload.get("schema_version") != 1:
        raise V3ConfigError("unsupported V3 configuration schema")
    if payload.get("confidence_levels") != ["very_low", "low", "medium", "high"]:
        raise V3ConfigError("confidence_levels are invalid")
    if payload.get("dimension_levels") != [
        "unknown", "very_low", "low", "medium", "high"
    ]:
        raise V3ConfigError("dimension_levels are invalid")

    mode = _load_mode(payload.get("mode"))
    source_selection = _load_source_selection(payload.get("phase_1_sources"))
    network_policy = _load_network_policy(payload.get("network_policy"))
    ga4_data_api = _load_ga4_data_api(payload.get("ga4_data_api"))
    gsc = _load_gsc(payload.get("gsc_search_analytics"))
    _validate_privacy(payload.get("privacy"))

    weights = payload.get("score_weights")
    if not isinstance(weights, dict) or set(weights) != REQUIRED_SCORE_DIMENSIONS:
        raise V3ConfigError("score_weights must define exactly the seven V3 dimensions")
    try:
        normalized_weights = {name: float(value) for name, value in weights.items()}
    except (TypeError, ValueError) as error:
        raise V3ConfigError("score_weights must all be numeric") from error
    if any(value <= 0 for value in normalized_weights.values()):
        raise V3ConfigError("score_weights must all be positive")

    policy = payload.get("recommendation_policy")
    if not isinstance(policy, dict) or set(policy) != {
        "strong_min_score",
        "strong_min_confidence",
        "strong_min_known_dimensions",
    }:
        raise V3ConfigError("recommendation_policy is invalid")
    try:
        min_score = int(policy["strong_min_score"])
        min_confidence = str(policy["strong_min_confidence"])
        min_known = int(policy["strong_min_known_dimensions"])
    except (TypeError, ValueError) as error:
        raise V3ConfigError("recommendation_policy is invalid") from error
    if not 0 <= min_score <= 100:
        raise V3ConfigError("strong_min_score must be between 0 and 100")
    if min_confidence not in CONFIDENCE_ORDER:
        raise V3ConfigError("strong_min_confidence is invalid")
    if not 1 <= min_known <= len(REQUIRED_SCORE_DIMENSIONS):
        raise V3ConfigError("strong_min_known_dimensions is invalid")
    runtime_state = _derive_runtime_state(
        mode,
        source_selection,
        network_policy,
        ga4_data_api,
        gsc,
    )
    config = V3Config(
        runtime_state=runtime_state,
        mode=mode,
        phase_1_sources=source_selection,
        network_policy=network_policy,
        ga4_data_api=ga4_data_api,
        gsc_search_analytics=gsc,
        score_weights=MappingProxyType(normalized_weights),
        recommendation_policy=RecommendationPolicy(
            strong_min_score=min_score,
            strong_min_confidence=min_confidence,
            strong_min_known_dimensions=min_known,
        ),
        source_path=source_path,
    )
    validate_runtime_state(config)
    return config


def validate_runtime_state(config: V3Config) -> RuntimeState:
    """Recheck the complete allowlisted state before adapter construction."""

    if not isinstance(config, V3Config):
        raise V3ConfigError("a validated V3Config is required")
    runtime_state = _derive_runtime_state(
        config.mode,
        config.phase_1_sources,
        config.network_policy,
        config.ga4_data_api,
        config.gsc_search_analytics,
    )
    if config.runtime_state is not runtime_state:
        raise V3ConfigError("V3 runtime state marker does not match its configuration")
    return runtime_state


def validate_offline_runtime(config: V3Config) -> None:
    """Require the default offline state for offline-only call sites."""

    if validate_runtime_state(config) is not RuntimeState.OFFLINE:
        raise V3ConfigError("this operation permits the OFFLINE runtime only")


def _derive_runtime_state(
    mode: ModeConfig,
    source_selection: SourceSelectionConfig,
    network_policy: NetworkPolicyConfig,
    ga4_data_api: GA4DataAPIConfig,
    gsc: GSCSearchAnalyticsConfig,
) -> RuntimeState:
    """Return one exact state; partial, mixed, and unknown states fail closed."""

    if not isinstance(gsc, GSCSearchAnalyticsConfig):
        raise V3ConfigError("validated GSC configuration required")
    if type(gsc.enabled) is not bool or gsc.contract != tuple(sorted(GSC_CONTRACT.items())):
        raise V3ConfigError("gsc_search_analytics contract is immutable")
    if (
        mode.read_only is not True
        or mode.proposal_only is not True
        or mode.site_publication_enabled is not False
    ):
        raise V3ConfigError("V3 runtime must remain read-only and proposal-only")
    if network_policy.default != "deny":
        raise V3ConfigError("V3 runtime requires a default-deny network policy")

    all_fixtures = all(
        source_selection.value_for(name) == "local_fixture" for name in SOURCE_NAMES
    )
    all_denied = all(
        network_policy.value_for(name) == "deny" for name in SOURCE_NAMES
    )
    if (
        mode.network_enabled is False
        and all_fixtures
        and all_denied
        and ga4_data_api.enabled is False
        and gsc.enabled is False
    ):
        return RuntimeState.OFFLINE

    non_analytics_sources_are_fixtures = all(
        source_selection.value_for(name) == "local_fixture"
        for name in SOURCE_NAMES
        if name != "analytics"
    )
    non_analytics_network_is_denied = all(
        network_policy.value_for(name) == "deny"
        for name in SOURCE_NAMES
        if name != "analytics"
    )
    if (
        mode.network_enabled is True
        and source_selection.analytics == "ga4_data_api"
        and non_analytics_sources_are_fixtures
        and network_policy.analytics == "allow"
        and non_analytics_network_is_denied
        and ga4_data_api.enabled is True
        and gsc.enabled is False
    ):
        return RuntimeState.GA4_READ_ONLY

    if (
        mode.network_enabled is True and gsc.enabled is True
        and ga4_data_api.enabled is False
        and source_selection.search_console == "gsc_search_analytics_api"
        and network_policy.search_console == "allow"
        and all(source_selection.value_for(n) == "local_fixture"
                and network_policy.value_for(n) == "deny"
                for n in SOURCE_NAMES if n != "search_console")
    ):
        return RuntimeState.GSC_READ_ONLY

    raise V3ConfigError("configuration is not an authorized V3 runtime state")


def _load_mode(value: object) -> ModeConfig:
    expected = {
        "read_only",
        "proposal_only",
        "network_enabled",
        "site_publication_enabled",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise V3ConfigError("mode is incomplete or unexpected")
    if value["read_only"] is not True:
        raise V3ConfigError("V3 runtime requires read_only=true")
    if value["proposal_only"] is not True:
        raise V3ConfigError("V3 runtime requires proposal_only=true")
    if not isinstance(value["network_enabled"], bool):
        raise V3ConfigError("mode.network_enabled must be boolean")
    if value["site_publication_enabled"] is not False:
        raise V3ConfigError("V3 runtime requires site_publication_enabled=false")
    return ModeConfig(
        read_only=True,
        proposal_only=True,
        network_enabled=value["network_enabled"],
        site_publication_enabled=False,
    )


def _load_source_selection(value: object) -> SourceSelectionConfig:
    if not isinstance(value, dict) or set(value) != _SOURCE_NAME_SET:
        raise V3ConfigError("phase_1_sources must define exactly the six V3 sources")
    if value["analytics"] not in {"local_fixture", "ga4_data_api"}:
        raise V3ConfigError("analytics source adapter is invalid")
    if any(
        value[name] != "local_fixture" for name in SOURCE_NAMES if name not in {"analytics", "search_console"}
    ):
        raise V3ConfigError("non-analytics V3 sources must use local_fixture")
    if value["search_console"] not in {"local_fixture", "gsc_search_analytics_api"}:
        raise V3ConfigError("search_console source adapter is invalid")
    return SourceSelectionConfig(**{name: value[name] for name in SOURCE_NAMES})


def _load_network_policy(value: object) -> NetworkPolicyConfig:
    if not isinstance(value, dict) or set(value) != {"default", "sources"}:
        raise V3ConfigError("network_policy is required and must be explicit")
    if value["default"] not in {"deny", "allow"}:
        raise V3ConfigError("network_policy.default is invalid")
    if value["default"] != "deny":
        raise V3ConfigError("V3 runtime requires a default-deny network policy")
    sources = value["sources"]
    if not isinstance(sources, dict) or set(sources) != _SOURCE_NAME_SET:
        raise V3ConfigError("network_policy must define exactly the six V3 sources")
    if any(sources[name] not in {"deny", "allow"} for name in SOURCE_NAMES):
        raise V3ConfigError("network_policy source value is invalid")
    if any(
        sources[name] != "deny" for name in SOURCE_NAMES if name not in {"analytics", "search_console"}
    ):
        raise V3ConfigError("non-analytics V3 sources must deny network access")
    return NetworkPolicyConfig(
        default="deny",
        **{name: sources[name] for name in SOURCE_NAMES},
    )


def _load_ga4_data_api(value: object) -> GA4DataAPIConfig:
    expected = {
        "enabled",
        "property_id",
        "resource",
        "channel",
        "date_range",
        "landing_page_topics",
        "unmapped_and_legal_pages",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise V3ConfigError("ga4_data_api is incomplete or unexpected")
    if not isinstance(value["enabled"], bool):
        raise V3ConfigError("ga4_data_api.enabled must be boolean")
    property_id = value["property_id"]
    if not isinstance(property_id, str) or not property_id.isdecimal() or int(property_id) <= 0:
        raise V3ConfigError("ga4_data_api.property_id is invalid")
    resource = value["resource"]
    if resource != "properties/{}".format(property_id):
        raise V3ConfigError("ga4_data_api.resource does not match property_id")
    if value["channel"] != "Organic Search":
        raise V3ConfigError("ga4_data_api.channel is invalid")
    date_range = value["date_range"]
    if not isinstance(date_range, dict) or set(date_range) != {"start_date", "end_date"}:
        raise V3ConfigError("ga4_data_api.date_range is invalid")
    if any(
        not isinstance(date_range[name], str) or not date_range[name].strip()
        for name in ("start_date", "end_date")
    ):
        raise V3ConfigError("ga4_data_api.date_range is invalid")
    landing_page_topics = value["landing_page_topics"]
    if not isinstance(landing_page_topics, dict) or not landing_page_topics:
        raise V3ConfigError("ga4_data_api.landing_page_topics is invalid")
    for landing_page, topic in landing_page_topics.items():
        if (
            not isinstance(landing_page, str)
            or not landing_page.startswith("/")
            or "?" in landing_page
            or "#" in landing_page
            or not isinstance(topic, str)
            or not topic.strip()
        ):
            raise V3ConfigError("ga4_data_api.landing_page_topics is invalid")
    unmapped_policy = value["unmapped_and_legal_pages"]
    if unmapped_policy != "exclude_from_commercial_signals":
        raise V3ConfigError("ga4_data_api unmapped-page policy is invalid")
    return GA4DataAPIConfig(
        enabled=value["enabled"],
        property_id=property_id,
        resource=resource,
        channel="Organic Search",
        start_date=date_range["start_date"],
        end_date=date_range["end_date"],
        landing_page_topics=tuple(sorted(landing_page_topics.items())),
        unmapped_and_legal_pages=unmapped_policy,
    )


def _validate_privacy(value: object) -> None:
    if value != {
        "aggregated_only": True,
        "personal_data_allowed": False,
        "full_review_text_allowed": False,
    }:
        raise V3ConfigError("privacy policy is invalid")
