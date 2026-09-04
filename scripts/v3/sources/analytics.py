"""Offline fixtures and the disabled GA4 Data API adapter for V3."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Tuple

from ..models import Confidence, Evidence, TrafficSignal
from .base import FixtureSource, fixture_evidence


GA4_PROPERTY_ID = "552715460"
GA4_RESOURCE = "properties/552715460"
ORGANIC_SEARCH_CHANNEL = "Organic Search"
# Shared by the standalone diagnostics; the adapter itself uses one landing
# page report and does not execute a separate channel report.
GA4_CHANNEL_DIMENSIONS = ("sessionDefaultChannelGroup",)
GA4_LANDING_PAGE_DIMENSIONS = ("landingPage", "sessionDefaultChannelGroup")
GA4_METRICS = ("sessions", "engagedSessions", "keyEvents")
# google-analytics-data 0.22.0 documents MetricAggregation.TOTAL rows with
# RESERVED_TOTAL in every requested dimension value.
GA4_TOTAL_DIMENSION_VALUE = "RESERVED_TOTAL"
DEFAULT_GA4_LANDING_PAGE_TOPICS = {
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
        observed_at: str,
        landing_page_topics: Optional[Mapping[str, str]] = None,
    ) -> None:
        normalized_property_id = str(property_id or "").strip()
        if not normalized_property_id.isdecimal() or int(normalized_property_id) <= 0:
            raise GA4DataSourceError("property_id must be a positive numeric identifier")
        if client is None:
            raise GA4DataSourceError("an injected GA4 Data API client is required")
        if not str(start_date or "").strip() or not str(end_date or "").strip():
            raise GA4DataSourceError("an explicit GA4 date range is required")
        if not isinstance(observed_at, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", observed_at
        ):
            raise GA4DataSourceError("observed_at must be a real ISO date (YYYY-MM-DD)")
        try:
            date.fromisoformat(observed_at)
        except ValueError:
            raise GA4DataSourceError(
                "observed_at must be a real ISO date (YYYY-MM-DD)"
            ) from None

        mapping = dict(
            DEFAULT_GA4_LANDING_PAGE_TOPICS
            if landing_page_topics is None
            else landing_page_topics
        )
        for landing_page, topic in mapping.items():
            if not _is_safe_landing_page(landing_page) or not str(topic or "").strip():
                raise GA4DataSourceError("landing page topic mapping is invalid")

        self.property_id = normalized_property_id
        self.property_resource = "properties/{}".format(normalized_property_id)
        self.client = client
        self.start_date = str(start_date).strip()
        self.end_date = str(end_date).strip()
        self.observed_at = observed_at
        self.landing_page_topics = mapping

    def report_request(self) -> Mapping[str, Any]:
        return {
            "property": self.property_resource,
            "date_ranges": [{
                "start_date": self.start_date,
                "end_date": self.end_date,
            }],
            "dimensions": [
                {"name": name} for name in GA4_LANDING_PAGE_DIMENSIONS
            ],
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
            "metric_aggregations": ["TOTAL"],
        }

    def collect(self) -> Tuple[TrafficSignal, ...]:
        """Fetch and normalize aggregate data, failing closed on uncertainty."""

        response = self._run_report(self.report_request())
        channel_totals = self._parse_total_response(response)
        rows = self._parse_landing_page_response(response)
        self._validate_commercial_landing_totals(rows, channel_totals)
        return self._aggregate_rows(rows, channel_totals)

    def _run_report(self, request: Mapping[str, Any]) -> Any:
        try:
            return self.client.run_report(request=request)
        except Exception:
            raise GA4DataSourceError("GA4 Data API request failed") from None

    def _parse_total_response(
        self, response: Any
    ) -> Tuple[Decimal, Decimal, Decimal]:
        self._validate_headers(response, GA4_LANDING_PAGE_DIMENSIONS)
        totals = tuple(getattr(response, "totals", ()) or ())
        if len(totals) != 1:
            raise GA4DataSourceError("GA4 response must contain exactly one total row")
        metric_values = getattr(totals[0], "metric_values", None)
        if metric_values is not None:
            try:
                raw_metrics = tuple(metric_values)
            except TypeError:
                raw_metrics = None
            if raw_metrics == () and self._is_verified_empty_response(response):
                return (Decimal(0), Decimal(0), Decimal(0))
        return self._metric_values(totals[0], "TOTAL")

    @staticmethod
    def _is_verified_empty_response(response: Any) -> bool:
        row_count = getattr(response, "row_count", None)
        if type(row_count) is not int or row_count != 0:
            return False
        missing = object()
        rows = getattr(response, "rows", missing)
        if rows is missing:
            return False
        try:
            return tuple(rows or ()) == ()
        except TypeError:
            return False

    def _parse_landing_page_response(
        self, response: Any
    ) -> Tuple[Tuple[str, str, Decimal, Decimal, Decimal], ...]:
        self._validate_headers(response, GA4_LANDING_PAGE_DIMENSIONS)
        normalized = []
        for row in tuple(getattr(response, "rows", ()) or ()):
            dimensions, metrics = self._row_values(row, "LANDING")
            landing_page, channel = dimensions
            if channel != ORGANIC_SEARCH_CHANNEL:
                raise GA4DataSourceError("GA4 response contains an unexpected channel")
            if landing_page == "(not set)":
                continue
            if not _is_safe_landing_page(landing_page):
                raise GA4DataSourceError("GA4 response contains an unsafe landingPage")
            topic = self.landing_page_topics.get(landing_page)
            if topic is None:
                # Unknown and legal paths are intentionally excluded; no topic
                # is inferred from URL fragments, query strings, or free text.
                continue
            normalized.append((topic, landing_page, *metrics))
        return tuple(sorted(normalized))

    def _validate_commercial_landing_totals(
        self,
        rows: Tuple[Tuple[str, str, Decimal, Decimal, Decimal], ...],
        channel_totals: Tuple[Decimal, Decimal, Decimal],
    ) -> None:
        landing_totals = [Decimal(0), Decimal(0), Decimal(0)]
        for _, _, sessions, engaged_sessions, key_events in rows:
            landing_totals[0] += sessions
            landing_totals[1] += engaged_sessions
            landing_totals[2] += key_events
        if any(
            landing > channel
            for landing, channel in zip(landing_totals, channel_totals)
        ):
            raise GA4DataSourceError(
                "GA4 commercial landing totals exceed Organic Search channel totals"
            )

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
        self, row: Any, context: str
    ) -> Tuple[Tuple[str, ...], Tuple[Decimal, Decimal, Decimal]]:
        dimensions = tuple(
            str(getattr(item, "value", ""))
            for item in tuple(getattr(row, "dimension_values", ()) or ())
        )
        if len(dimensions) != len(GA4_LANDING_PAGE_DIMENSIONS):
            raise GA4DataSourceError(
                "{}_ROW_DIMENSION_COUNT_INVALID".format(context)
            )
        if any(not value for value in dimensions):
            raise GA4DataSourceError(
                "{}_ROW_DIMENSION_VALUE_INVALID".format(context)
            )
        return dimensions, self._metric_values(row, context)

    def _metric_values(
        self, row: Any, context: str
    ) -> Tuple[Decimal, Decimal, Decimal]:
        raw_metrics = tuple(
            getattr(item, "value", None)
            for item in tuple(getattr(row, "metric_values", ()) or ())
        )
        if len(raw_metrics) != len(GA4_METRICS):
            raise GA4DataSourceError(
                "{}_ROW_METRIC_COUNT_INVALID".format(context)
            )
        return tuple(_parse_metric(value, context) for value in raw_metrics)

    def _aggregate_rows(
        self,
        rows: Tuple[Tuple[str, str, Decimal, Decimal, Decimal], ...],
        channel_totals: Tuple[Decimal, Decimal, Decimal],
    ) -> Tuple[TrafficSignal, ...]:
        aggregates: dict[str, dict[str, Any]] = {}
        for topic, landing_page, sessions, engaged_sessions, key_events in rows:
            current = aggregates.setdefault(topic, {
                "sessions": Decimal(0),
                "engaged_sessions": Decimal(0),
                "key_events": Decimal(0),
                "landing_pages": set(),
            })
            current["sessions"] += sessions
            current["engaged_sessions"] += engaged_sessions
            current["key_events"] += key_events
            current["landing_pages"].add(landing_page)

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
                "dimensions": GA4_LANDING_PAGE_DIMENSIONS,
                "metrics": GA4_METRICS,
                "channel": ORGANIC_SEARCH_CHANNEL,
                "organic_channel_totals": {
                    "sessions": float(channel_totals[0]),
                    "engaged_sessions": float(channel_totals[1]),
                    "key_events": float(channel_totals[2]),
                },
                "landing_pages": tuple(sorted(aggregate["landing_pages"])),
                "organic_sessions": float(aggregate["sessions"]),
                "engaged_sessions": float(aggregate["engaged_sessions"]),
                "key_events": float(aggregate["key_events"]),
            }
            signals.append(TrafficSignal(
                topic=topic,
                organic_sessions=float(aggregate["sessions"]),
                engaged_sessions=float(aggregate["engaged_sessions"]),
                # GA4 key events remain observational telemetry until Colixo
                # explicitly validates their commercial conversion semantics.
                conversions=None,
                evidence=(Evidence(
                    source="google_analytics_4",
                    observed_at=self.observed_at,
                    metric="organic_search_aggregate",
                    fact=evidence_fact,
                    confidence=Confidence.HIGH,
                    reference=self.property_resource,
                ),),
            ))
        return tuple(signals)


def _parse_metric(value: Any, context: str) -> Decimal:
    failure_code = "{}_METRIC_VALUE_INVALID".format(context)
    if value is None or isinstance(value, bool):
        raise GA4DataSourceError(failure_code)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GA4DataSourceError(failure_code) from None
    if not parsed.is_finite() or parsed < 0:
        raise GA4DataSourceError(failure_code)
    return parsed


def _is_safe_landing_page(value: Any) -> bool:
    landing_page = str(value or "")
    return (
        landing_page.startswith("/")
        and "?" not in landing_page
        and "#" not in landing_page
        and "\x00" not in landing_page
    )
