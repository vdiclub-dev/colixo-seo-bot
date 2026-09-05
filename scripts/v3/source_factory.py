"""Central, fail-closed construction for the three authorized V3 runtimes."""

import re
from dataclasses import dataclass, fields
from decimal import Decimal
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
from .sources.search_console import (
    SearchConsoleFixtureSource, GoogleSearchConsoleDataSource,
    create_google_search_console_transport,
)
from .models import Evidence, SearchSignal


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


@dataclass(frozen=True)
class GSCRuntimeCoverage:
    """Returned-row aggregates only; never scoring evidence or query content."""

    raw_row_count: int
    accepted_signal_count: int
    brand_row_count: int
    unmapped_row_count: int
    pii_filtered_row_count: int
    all_rows_clicks: Decimal
    all_rows_impressions: Decimal
    accepted_clicks: Decimal
    accepted_impressions: Decimal
    brand_clicks: Decimal
    brand_impressions: Decimal
    row_limit: int = 25000
    row_limit_reached: bool = False
    totals_scope: str = "returned_query_rows_non_exhaustive"


class GSCReadOnlyRuntimeAdapter:
    """Erase query-bearing data before crossing the agent boundary."""

    def __init__(self, source: GoogleSearchConsoleDataSource) -> None:
        self._source = source
        self._coverage: Optional[GSCRuntimeCoverage] = None

    @property
    def coverage(self) -> Optional[GSCRuntimeCoverage]:
        return self._coverage

    def collect(self, fixture: Any) -> tuple:
        self._coverage = None
        if not isinstance(fixture, (tuple, list)) or len(fixture) != 0:
            raise SourceAuthorizationError("GSC_READ_ONLY requires an empty fixture payload")
        result = self._source.collect_with_coverage()
        safe = []
        for signal in result.signals:
            # Reconstruct rather than copying arbitrary raw Evidence.fact fields.
            fact = {
                "property": "sc-domain:colixo.ch",
                "date_range": {"start_date": self._source.start_date,
                               "end_date": self._source.end_date},
                "provenance": "gsc_search_analytics_api",
                "commercial_topic": signal.topic,
                "clicks": signal.clicks, "impressions": signal.impressions,
                "ctr": signal.ctr, "average_position": signal.average_position,
            }
            safe.append(SearchSignal(
                topic=signal.topic, query="commercial_query_redacted",
                clicks=signal.clicks, impressions=signal.impressions,
                ctr=signal.ctr, average_position=signal.average_position,
                evidence=tuple(Evidence(
                    source="google_search_console", observed_at=self._source.observed_at,
                    metric="search_query_aggregate", fact=dict(fact),
                    confidence=e.confidence,
                ) for e in signal.evidence),
            ))
        coverage = result.coverage
        self._coverage = GSCRuntimeCoverage(
            **{f.name: getattr(coverage, f.name) for f in fields(GSCRuntimeCoverage)
               if f.name not in {"row_limit", "row_limit_reached", "totals_scope"}},
            row_limit_reached=coverage.raw_row_count == 25000,
        )
        return tuple(sorted(safe, key=lambda s: (
            s.topic, s.clicks, s.impressions, s.ctr, s.average_position
        )))


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

    if runtime_state is RuntimeState.GSC_READ_ONLY and source_name == "search_console":
        if (requires_network is not True or adapter_name != "gsc_search_analytics_api"
                or policy != "allow" or config.mode.network_enabled is not True
                or config.gsc_search_analytics.enabled is not True):
            raise SourceAuthorizationError("GSC_READ_ONLY authorization failed")
        return

    if runtime_state is RuntimeState.GA4_READ_ONLY and source_name == "analytics":
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
            "read-only fixture source authorization failed"
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
    gsc_transport_factory: Optional[Callable[[], Any]] = None,
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
                or runtime_state is RuntimeState.GSC_READ_ONLY
                and source_name == "search_console"
            ),
        )

    adapters = {}
    for source_name in SOURCE_NAMES:
        if not (
            runtime_state is RuntimeState.GA4_READ_ONLY and source_name == "analytics"
            or runtime_state is RuntimeState.GSC_READ_ONLY and source_name == "search_console"
        ):
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

    if runtime_state is RuntimeState.GSC_READ_ONLY:
        normalized_observed_at = _validate_observed_at(observed_at)
        if gsc_transport_factory is not None and not callable(gsc_transport_factory):
            raise SourceAuthorizationError("gsc_transport_factory must be callable")
        factory = (create_google_search_console_transport
                   if gsc_transport_factory is None else gsc_transport_factory)
        adapters["search_console"] = GSCReadOnlyRuntimeAdapter(
            GoogleSearchConsoleDataSource(
                transport=factory(), observed_at=normalized_observed_at,
            )
        )

    return MappingProxyType(adapters)


def _validate_observed_at(value: Optional[str]) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise SourceAuthorizationError(
            "read-only runtime requires observed_at as a real ISO date (YYYY-MM-DD)"
        )
    try:
        date.fromisoformat(value)
    except ValueError:
        raise SourceAuthorizationError(
            "read-only runtime requires observed_at as a real ISO date (YYYY-MM-DD)"
        ) from None
    return value
