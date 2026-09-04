"""Central, fail-closed construction for the two authorized V3 runtimes."""

import re
from datetime import date
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from .config import (
    SOURCE_NAMES,
    RuntimeState,
    V3Config,
    validate_runtime_state,
)
from .sources.analytics import (
    AnalyticsFixtureSource,
    GoogleAnalyticsDataSource,
    create_google_analytics_data_client,
)
from .sources.business_metrics import BusinessMetricsFixtureSource
from .sources.competitors import CompetitorFixtureSource
from .sources.rank_tracker import RankTrackerFixtureSource
from .sources.reviews import ReviewsFixtureSource
from .sources.search_console import SearchConsoleFixtureSource


GA4_READ_ONLY_RUNTIME_SUPPORTED = True


class SourceAuthorizationError(ValueError):
    """Raised before construction when a source is not explicitly authorized."""


class GA4ReadOnlyRuntimeAdapter:
    """Adapt the live GA4 source to the agent's fixture-shaped source contract."""

    def __init__(self, source: GoogleAnalyticsDataSource) -> None:
        self._source = source

    def collect(self, fixture: Any) -> Any:
        if not isinstance(fixture, (tuple, list)) or len(fixture) != 0:
            raise SourceAuthorizationError(
                "GA4_READ_ONLY analytics requires an empty fixture payload"
            )
        return self._source.collect()


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
    """Authorize one source only after validating the complete runtime state."""

    runtime_state = validate_runtime_state(config)
    if source_name not in SOURCE_NAMES:
        raise SourceAuthorizationError("unknown V3 source is denied")
    if config.network_policy.default != "deny":
        raise SourceAuthorizationError("network policy must remain default-deny")

    adapter_name = config.phase_1_sources.value_for(source_name)
    policy = config.network_policy.value_for(source_name)
    if runtime_state is RuntimeState.OFFLINE:
        if requires_network or adapter_name != "local_fixture" or policy != "deny":
            raise SourceAuthorizationError("OFFLINE source authorization failed")
        return

    if source_name == "analytics":
        if (
            requires_network is not True
            or adapter_name != "ga4_data_api"
            or policy != "allow"
            or config.mode.network_enabled is not True
            or config.ga4_data_api.enabled is not True
        ):
            raise SourceAuthorizationError(
                "GA4_READ_ONLY analytics authorization failed"
            )
        return

    if requires_network or adapter_name != "local_fixture" or policy != "deny":
        raise SourceAuthorizationError(
            "GA4_READ_ONLY non-analytics authorization failed"
        )


def source_modes(config: V3Config) -> Mapping[str, str]:
    """Expose deterministic adapter provenance for result and report metadata."""

    validate_runtime_state(config)
    return MappingProxyType({
        name: config.phase_1_sources.value_for(name) for name in SOURCE_NAMES
    })


def build_source_adapters(
    config: V3Config,
    *,
    observed_at: Optional[str] = None,
    ga4_client_factory: Optional[Callable[[], Any]] = None,
) -> Mapping[str, Any]:
    """Build one exact six-source adapter set after complete authorization."""

    runtime_state = validate_runtime_state(config)
    for source_name in SOURCE_NAMES:
        authorize_source(
            config,
            source_name,
            requires_network=(
                runtime_state is RuntimeState.GA4_READ_ONLY
                and source_name == "analytics"
            ),
        )

    adapters = {}
    for source_name in SOURCE_NAMES:
        if runtime_state is RuntimeState.OFFLINE or source_name != "analytics":
            adapters[source_name] = _OFFLINE_SOURCE_FACTORIES[source_name]()

    if runtime_state is RuntimeState.GA4_READ_ONLY:
        normalized_observed_at = _validate_observed_at(observed_at)
        if ga4_client_factory is not None and not callable(ga4_client_factory):
            raise SourceAuthorizationError("ga4_client_factory must be callable")
        client_factory = (
            create_google_analytics_data_client
            if ga4_client_factory is None
            else ga4_client_factory
        )
        client = client_factory()
        ga4 = config.ga4_data_api
        adapters["analytics"] = GA4ReadOnlyRuntimeAdapter(
            GoogleAnalyticsDataSource(
                property_id=ga4.property_id,
                client=client,
                start_date=ga4.start_date,
                end_date=ga4.end_date,
                observed_at=normalized_observed_at,
                landing_page_topics=dict(ga4.landing_page_topics),
            )
        )

    return MappingProxyType(adapters)


def _validate_observed_at(value: Optional[str]) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SourceAuthorizationError(
            "GA4_READ_ONLY requires observed_at as a real ISO date (YYYY-MM-DD)"
        )
    try:
        date.fromisoformat(value)
    except ValueError:
        raise SourceAuthorizationError(
            "GA4_READ_ONLY requires observed_at as a real ISO date (YYYY-MM-DD)"
        ) from None
    return value
