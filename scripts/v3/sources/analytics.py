"""Offline fixtures and the disabled GA4 Data API adapter for V3."""

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Tuple

from ..models import Confidence, Evidence, TrafficSignal
from .base import FixtureSource, fixture_evidence


GA4_PROPERTY_ID = "552715460"
GA4_RESOURCE = "properties/552715460"
ORGANIC_SEARCH_CHANNEL = "Organic Search"
GA4_CHANNEL_DIMENSIONS = ("sessionDefaultChannelGroup",)
GA4_PAGE_DIMENSIONS = ("pagePath", "sessionDefaultChannelGroup")
GA4_METRICS = ("sessions", "engagedSessions", "keyEvents")
DEFAULT_GA4_PAGE_TOPICS = {
    "/": "general_delivery",
    "/business-plus": "business_delivery",
    "/portail-client/livraison-colis-suisse-romande": "parcel_delivery",
    "/portail-client/livraison-vins-vignerons-suisse-romande": "wine_delivery",
    "/portail-client/envoi-securise-horlogerie": "secure_watch_delivery",
}


class GA4DataSourceError(ValueError):
    """Raised when GA4 input or output cannot be trusted."""


class AnalyticsFixtureSource(FixtureSource[TrafficSignal]):
    def collect(self, fixture: Iterable[Mapping[str, Any]]) -> Tuple[TrafficSignal, ...]:
        return tuple(
            TrafficSignal(
                topic=str(item["topic"]),
                organic_sessions=item.get("organic_sessions"),
                engaged_sessions=item.get("engaged_sessions"),
                conversions=item.get("conversions"),
                evidence=fixture_evidence(item.get("evidence", ())),
            )
            for item in fixture
        )


def create_google_analytics_data_client() -> Any:
    """Explicitly build the official client with ADC for a future live gate.

    Nothing calls this factory from the current V3 runtime. Importing this
    module therefore never imports Google auth or creates a client.
    """

    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
    except ImportError as error:
        raise GA4DataSourceError("google-analytics-data is not installed") from error
    try:
        return BetaAnalyticsDataClient()
    except Exception:
        raise GA4DataSourceError(
            "GA4 Application Default Credentials are unavailable"
        ) from None


class GoogleAnalyticsDataSource:
    """Read aggregate Organic Search metrics through an injected GA4 client.

    This adapter is present for Phase 2C but is deliberately not wired into
    ``MarketIntelligenceAgent``. The active analytics source remains
    ``local_fixture`` and network access remains disabled.
    """

    def __init__(
        self,
        *,
        property_id: str,
        client: Any,
        start_date: str,
        end_date: str,
        page_topics: Optional[Mapping[str, str]] = None,
    ) -> None:
        normalized_property_id = str(property_id or "").strip()
        if not normalized_property_id.isdecimal() or int(normalized_property_id) <= 0:
            raise GA4DataSourceError("property_id must be a positive numeric identifier")
        if client is None:
            raise GA4DataSourceError("an injected GA4 Data API client is required")
        if not str(start_date or "").strip() or not str(end_date or "").strip():
            raise GA4DataSourceError("an explicit GA4 date range is required")

        mapping = dict(DEFAULT_GA4_PAGE_TOPICS if page_topics is None else page_topics)
        for page_path, topic in mapping.items():
            if not _is_safe_page_path(page_path) or not str(topic or "").strip():
                raise GA4DataSourceError("page topic mapping is invalid")

        self.property_id = normalized_property_id
        self.property_resource = "properties/{}".format(normalized_property_id)
        self.client = client
        self.start_date = str(start_date).strip()
        self.end_date = str(end_date).strip()
        self.page_topics = mapping

    def channel_request(self) -> Mapping[str, Any]:
        return self._request(GA4_CHANNEL_DIMENSIONS)

    def page_request(self) -> Mapping[str, Any]:
        return self._request(GA4_PAGE_DIMENSIONS)

    def collect(self) -> Tuple[TrafficSignal, ...]:
        """Fetch and normalize aggregate data, failing closed on uncertainty."""

        channel_response = self._run_report(self.channel_request())
        self._parse_channel_response(channel_response)
        page_response = self._run_report(self.page_request())
        rows = self._parse_page_response(page_response)
        return self._aggregate_rows(rows)

    def _request(self, dimensions: Tuple[str, ...]) -> Mapping[str, Any]:
        return {
            "property": self.property_resource,
            "date_ranges": [{
                "start_date": self.start_date,
                "end_date": self.end_date,
            }],
            "dimensions": [{"name": name} for name in dimensions],
            "metrics": [{"name": name} for name in GA4_METRICS],
            "dimension_filter": {
                "filter": {
                    "field_name": "sessionDefaultChannelGroup",
                    "string_filter": {
                        "match_type": "EXACT",
                        "value": ORGANIC_SEARCH_CHANNEL,
                        "case_sensitive": True,
                    },
                }
            },
        }

    def _run_report(self, request: Mapping[str, Any]) -> Any:
        try:
            return self.client.run_report(request=request)
        except Exception:
            raise GA4DataSourceError("GA4 Data API request failed") from None

    def _parse_channel_response(self, response: Any) -> Tuple[Decimal, Decimal, Decimal]:
        self._validate_headers(response, GA4_CHANNEL_DIMENSIONS)
        rows = tuple(getattr(response, "rows", ()) or ())
        if not rows:
            return (Decimal(0), Decimal(0), Decimal(0))
        totals = [Decimal(0), Decimal(0), Decimal(0)]
        for row in rows:
            dimensions, metrics = self._row_values(row, 1)
            if dimensions[0] != ORGANIC_SEARCH_CHANNEL:
                raise GA4DataSourceError("GA4 response contains an unexpected channel")
            for index, value in enumerate(metrics):
                totals[index] += value
        return tuple(totals)

    def _parse_page_response(
        self, response: Any
    ) -> Tuple[Tuple[str, str, Decimal, Decimal, Decimal], ...]:
        self._validate_headers(response, GA4_PAGE_DIMENSIONS)
        normalized = []
        for row in tuple(getattr(response, "rows", ()) or ()):
            dimensions, metrics = self._row_values(row, 2)
            page_path, channel = dimensions
            if channel != ORGANIC_SEARCH_CHANNEL:
                raise GA4DataSourceError("GA4 response contains an unexpected channel")
            if not _is_safe_page_path(page_path):
                raise GA4DataSourceError("GA4 response contains an unsafe pagePath")
            topic = self.page_topics.get(page_path)
            if topic is None:
                # Unknown and legal paths are intentionally excluded; no topic
                # is inferred from URL fragments, query strings, or free text.
                continue
            normalized.append((topic, page_path, *metrics))
        return tuple(sorted(normalized))

    def _validate_headers(self, response: Any, dimensions: Tuple[str, ...]) -> None:
        if response is None:
            raise GA4DataSourceError("GA4 response is missing")
        dimension_headers = tuple(
            str(getattr(item, "name", ""))
            for item in tuple(getattr(response, "dimension_headers", ()) or ())
        )
        metric_headers = tuple(
            str(getattr(item, "name", ""))
            for item in tuple(getattr(response, "metric_headers", ()) or ())
        )
        if dimension_headers != dimensions:
            raise GA4DataSourceError("GA4 response dimensions are unexpected")
        if metric_headers != GA4_METRICS:
            raise GA4DataSourceError("GA4 response metrics are unexpected")

    def _row_values(
        self, row: Any, dimension_count: int
    ) -> Tuple[Tuple[str, ...], Tuple[Decimal, Decimal, Decimal]]:
        dimensions = tuple(
            str(getattr(item, "value", ""))
            for item in tuple(getattr(row, "dimension_values", ()) or ())
        )
        metrics = tuple(
            _parse_metric(getattr(item, "value", None))
            for item in tuple(getattr(row, "metric_values", ()) or ())
        )
        if len(dimensions) != dimension_count or any(not value for value in dimensions):
            raise GA4DataSourceError("GA4 response row dimensions are malformed")
        if len(metrics) != len(GA4_METRICS):
            raise GA4DataSourceError("GA4 response row metrics are malformed")
        return dimensions, metrics

    def _aggregate_rows(
        self, rows: Tuple[Tuple[str, str, Decimal, Decimal, Decimal], ...]
    ) -> Tuple[TrafficSignal, ...]:
        aggregates: dict[str, dict[str, Any]] = {}
        for topic, page_path, sessions, engaged_sessions, key_events in rows:
            current = aggregates.setdefault(topic, {
                "sessions": Decimal(0),
                "engaged_sessions": Decimal(0),
                "key_events": Decimal(0),
                "page_paths": set(),
            })
            current["sessions"] += sessions
            current["engaged_sessions"] += engaged_sessions
            current["key_events"] += key_events
            current["page_paths"].add(page_path)

        signals = []
        for topic in sorted(aggregates):
            aggregate = aggregates[topic]
            evidence_fact = {
                "provenance": "ga4_data_api",
                "property": self.property_id,
                "date_range": {
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                },
                "dimensions": GA4_PAGE_DIMENSIONS,
                "metrics": GA4_METRICS,
                "channel": ORGANIC_SEARCH_CHANNEL,
                "page_paths": tuple(sorted(aggregate["page_paths"])),
                "organic_sessions": float(aggregate["sessions"]),
                "engaged_sessions": float(aggregate["engaged_sessions"]),
                "key_events": float(aggregate["key_events"]),
            }
            signals.append(TrafficSignal(
                topic=topic,
                organic_sessions=float(aggregate["sessions"]),
                engaged_sessions=float(aggregate["engaged_sessions"]),
                conversions=float(aggregate["key_events"]),
                evidence=(Evidence(
                    source="google_analytics_4",
                    observed_at=self.end_date,
                    metric="organic_search_aggregate",
                    fact=evidence_fact,
                    confidence=Confidence.HIGH,
                    reference=self.property_resource,
                ),),
            ))
        return tuple(signals)


def _parse_metric(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise GA4DataSourceError("GA4 metric is missing or invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GA4DataSourceError("GA4 metric is missing or invalid") from None
    if not parsed.is_finite() or parsed < 0:
        raise GA4DataSourceError("GA4 metric is missing or invalid")
    return parsed


def _is_safe_page_path(value: Any) -> bool:
    page_path = str(value or "")
    return (
        page_path.startswith("/")
        and "?" not in page_path
        and "#" not in page_path
        and "\x00" not in page_path
    )
