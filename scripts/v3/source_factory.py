"""Central, static, offline-only V3 source construction."""

from types import MappingProxyType
from typing import Any, Mapping

from .config import SOURCE_NAMES, V3Config, validate_offline_runtime
from .sources.analytics import AnalyticsFixtureSource
from .sources.business_metrics import BusinessMetricsFixtureSource
from .sources.competitors import CompetitorFixtureSource
from .sources.rank_tracker import RankTrackerFixtureSource
from .sources.reviews import ReviewsFixtureSource
from .sources.search_console import SearchConsoleFixtureSource


LIVE_GA4_RUNTIME_ALLOWED = False


class SourceAuthorizationError(ValueError):
    """Raised before construction when a source is not explicitly authorized."""


_OFFLINE_SOURCE_FACTORIES = MappingProxyType({
    "search_console": SearchConsoleFixtureSource,
    "analytics": AnalyticsFixtureSource,
    "rank_tracker": RankTrackerFixtureSource,
    "competitors": CompetitorFixtureSource,
    "reviews": ReviewsFixtureSource,
    "business_metrics": BusinessMetricsFixtureSource,
})


def authorize_source(
    config: V3Config,
    source_name: str,
    *,
    requires_network: bool,
) -> None:
    """Authorize one known source; Phase 2F-B permits fixture-only construction."""

    validate_offline_runtime(config)
    if source_name not in SOURCE_NAMES:
        raise SourceAuthorizationError("unknown V3 source is denied")
    if config.network_policy.default != "deny":
        raise SourceAuthorizationError("network policy must remain default-deny")
    policy = config.network_policy.value_for(source_name)
    if policy not in {"deny", "allow"}:
        raise SourceAuthorizationError("source network policy is invalid")
    if requires_network:
        if not config.mode.network_enabled:
            raise SourceAuthorizationError("global network access is disabled")
        if policy != "allow":
            raise SourceAuthorizationError("source network access is denied")
        if not LIVE_GA4_RUNTIME_ALLOWED:
            raise SourceAuthorizationError("live GA4 runtime is not authorized")
    if config.phase_1_sources.value_for(source_name) != "local_fixture":
        raise SourceAuthorizationError("non-fixture source is not authorized")
    if policy != "deny":
        raise SourceAuthorizationError("Phase 2F-B requires source network denial")


def build_source_adapters(config: V3Config) -> Mapping[str, Any]:
    """Build the exact deterministic six-source offline adapter set."""

    validate_offline_runtime(config)
    adapters = {}
    for source_name in SOURCE_NAMES:
        authorize_source(config, source_name, requires_network=False)
        factory = _OFFLINE_SOURCE_FACTORIES[source_name]
        adapters[source_name] = factory()
    return MappingProxyType(adapters)
