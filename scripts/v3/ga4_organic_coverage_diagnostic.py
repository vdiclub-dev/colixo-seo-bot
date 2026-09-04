"""Manual aggregate-only diagnostic for GA4 Organic Search coverage."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Mapping, Tuple

from scripts.v3.models import TrafficSignal
from scripts.v3.sources.analytics import (
    DEFAULT_GA4_LANDING_PAGE_TOPICS,
    GA4_LANDING_PAGE_DIMENSIONS,
    GA4_METRICS,
    GA4_PROPERTY_ID,
    GA4_RESOURCE,
    GA4_TOTAL_DIMENSION_VALUE,
    ORGANIC_SEARCH_CHANNEL,
    GoogleAnalyticsDataSource,
    create_google_analytics_data_client,
)


PROPERTY_ID = GA4_PROPERTY_ID
AUTH_MODE = "WIF"
START_DATE = "2026-09-03"
END_DATE = "2026-09-03"
OBSERVED_AT = "2026-09-04"
EXPECTED_REPORT_CALLS = 1
PASS_VERDICT = "GA4_ORGANIC_COVERAGE_DIAGNOSTIC_PASS"
FAIL_VERDICT = "GA4_ORGANIC_COVERAGE_DIAGNOSTIC_FAILED"

MAPPED_COMMERCIAL = "mapped_commercial"
UNMAPPED_OR_EXCLUDED = "unmapped_or_excluded"
NOT_SET = "not_set"

_KNOWN_TOPICS = frozenset(DEFAULT_GA4_LANDING_PAGE_TOPICS.values())


class GA4OrganicCoverageDiagnosticError(ValueError):
    """Raised when the diagnostic cannot produce a trustworthy safe summary."""


@dataclass(frozen=True)
class Metrics:
    sessions: Decimal = Decimal(0)
    engaged_sessions: Decimal = Decimal(0)
    key_events: Decimal = Decimal(0)

    def __add__(self, other: "Metrics") -> "Metrics":
        return Metrics(
            self.sessions + other.sessions,
            self.engaged_sessions + other.engaged_sessions,
            self.key_events + other.key_events,
        )

    def exceeds(self, other: "Metrics") -> bool:
        return any((
            self.sessions > other.sessions,
            self.engaged_sessions > other.engaged_sessions,
            self.key_events > other.key_events,
        ))

    def subtract(self, other: "Metrics") -> "Metrics":
        return Metrics(
            self.sessions - other.sessions,
            self.engaged_sessions - other.engaged_sessions,
            self.key_events - other.key_events,
        )


@dataclass(frozen=True)
class CategorySummary:
    row_count: int
    metrics: Metrics


@dataclass(frozen=True)
class CoverageResult:
    report_calls: int
    organic_totals: Metrics
    mapped: CategorySummary
    unmapped_or_excluded: CategorySummary
    not_set: CategorySummary
    residual: Metrics
    mapped_session_coverage_pct: str
    adapter_topics: Tuple[str, ...]


class RecordingClient:
    """Forward GA4 calls once while retaining request/response objects in memory."""

    def __init__(self, client: Any) -> None:
        if client is None:
            raise GA4OrganicCoverageDiagnosticError("GA4 client is required")
        self.client = client
        self.requests = []
        self.responses = []

    def run_report(self, *, request: Mapping[str, Any]) -> Any:
        expected = (_expected_request(),)
        request_index = len(self.requests)
        if request_index >= EXPECTED_REPORT_CALLS or request != expected[request_index]:
            raise GA4OrganicCoverageDiagnosticError(
                "GA4 request does not match the diagnostic contract"
            )
        self.requests.append(request)
        response = self.client.run_report(request=request)
        self.responses.append(response)
        return response


def run_diagnostic(
    *,
    client_factory: Callable[[], Any] = create_google_analytics_data_client,
    data_source_class: Any = GoogleAnalyticsDataSource,
    emit: Callable[[str], None] = print,
) -> CoverageResult:
    """Collect once through the adapter and emit only aggregate allowlisted output."""

    try:
        recording_client = RecordingClient(client_factory())
        source = data_source_class(
            property_id=PROPERTY_ID,
            client=recording_client,
            start_date=START_DATE,
            end_date=END_DATE,
            observed_at=OBSERVED_AT,
            landing_page_topics=dict(DEFAULT_GA4_LANDING_PAGE_TOPICS),
        )
        signals = tuple(source.collect())
        _validate_request_contract(recording_client.requests)
        if len(recording_client.responses) != EXPECTED_REPORT_CALLS:
            raise GA4OrganicCoverageDiagnosticError(
                "GA4 response count does not match the diagnostic contract"
            )
        result = _build_coverage_result(recording_client.responses, signals)
        output = _safe_output(result)
    except Exception:
        raise GA4OrganicCoverageDiagnosticError(
            "GA4 Organic Search coverage diagnostic failed"
        ) from None

    for line in output:
        emit(line)
    return result


def _expected_request() -> Mapping[str, Any]:
    return {
        "property": GA4_RESOURCE,
        "date_ranges": [{"start_date": START_DATE, "end_date": END_DATE}],
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


def _validate_request_contract(requests: Iterable[Mapping[str, Any]]) -> None:
    recorded = tuple(requests)
    expected = (_expected_request(),)
    if len(recorded) != EXPECTED_REPORT_CALLS or recorded != expected:
        raise GA4OrganicCoverageDiagnosticError(
            "GA4 requests do not match the diagnostic contract"
        )


def _build_coverage_result(
    responses: Iterable[Any], signals: Iterable[TrafficSignal]
) -> CoverageResult:
    (response,) = tuple(responses)
    organic_totals = _parse_total_response(response)
    categories = _parse_landing_response(response)
    mapped = categories[MAPPED_COMMERCIAL]
    unmapped = categories[UNMAPPED_OR_EXCLUDED]
    not_set = categories[NOT_SET]
    breakdown = mapped.metrics + unmapped.metrics + not_set.metrics
    if breakdown.exceeds(organic_totals):
        raise GA4OrganicCoverageDiagnosticError(
            "GA4 landing breakdown exceeds Organic Search totals"
        )

    topics = _validate_adapter_topics(signals)
    coverage = "NA"
    if organic_totals.sessions > 0:
        coverage = str(
            (Decimal(100) * mapped.metrics.sessions / organic_totals.sessions).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )

    return CoverageResult(
        report_calls=EXPECTED_REPORT_CALLS,
        organic_totals=organic_totals,
        mapped=mapped,
        unmapped_or_excluded=unmapped,
        not_set=not_set,
        residual=organic_totals.subtract(breakdown),
        mapped_session_coverage_pct=coverage,
        adapter_topics=topics,
    )


def _parse_total_response(response: Any) -> Metrics:
    _validate_headers(response, GA4_LANDING_PAGE_DIMENSIONS)
    totals = tuple(getattr(response, "totals", ()) or ())
    if len(totals) != 1:
        raise GA4OrganicCoverageDiagnosticError(
            "GA4 response must contain exactly one total row"
        )
    dimensions, metrics = _row_values(
        totals[0], len(GA4_LANDING_PAGE_DIMENSIONS)
    )
    if dimensions != (
        GA4_TOTAL_DIMENSION_VALUE,
        GA4_TOTAL_DIMENSION_VALUE,
    ):
        raise GA4OrganicCoverageDiagnosticError(
            "GA4 total row dimensions are unexpected"
        )
    return metrics


def _parse_landing_response(response: Any) -> Mapping[str, CategorySummary]:
    _validate_headers(response, GA4_LANDING_PAGE_DIMENSIONS)
    counts = {
        MAPPED_COMMERCIAL: 0,
        UNMAPPED_OR_EXCLUDED: 0,
        NOT_SET: 0,
    }
    totals = {category: Metrics() for category in counts}
    for row in tuple(getattr(response, "rows", ()) or ()):
        dimensions, metrics = _row_values(row, len(GA4_LANDING_PAGE_DIMENSIONS))
        landing_page, channel = dimensions
        if channel != ORGANIC_SEARCH_CHANNEL:
            raise GA4OrganicCoverageDiagnosticError(
                "GA4 landing response channel is unexpected"
            )
        category = _classify_landing_page(landing_page)
        counts[category] += 1
        totals[category] += metrics
    return {
        category: CategorySummary(counts[category], totals[category])
        for category in counts
    }


def _classify_landing_page(landing_page: str) -> str:
    if landing_page in DEFAULT_GA4_LANDING_PAGE_TOPICS:
        return MAPPED_COMMERCIAL
    if landing_page == "(not set)":
        return NOT_SET
    if (
        landing_page.startswith("/")
        and "?" not in landing_page
        and "#" not in landing_page
        and "\x00" not in landing_page
    ):
        return UNMAPPED_OR_EXCLUDED
    raise GA4OrganicCoverageDiagnosticError(
        "GA4 landing response contains an unsafe value"
    )


def _validate_headers(response: Any, dimensions: Tuple[str, ...]) -> None:
    if response is None:
        raise GA4OrganicCoverageDiagnosticError("GA4 response is missing")
    actual_dimensions = tuple(
        str(getattr(item, "name", ""))
        for item in tuple(getattr(response, "dimension_headers", ()) or ())
    )
    actual_metrics = tuple(
        str(getattr(item, "name", ""))
        for item in tuple(getattr(response, "metric_headers", ()) or ())
    )
    if actual_dimensions != dimensions or actual_metrics != GA4_METRICS:
        raise GA4OrganicCoverageDiagnosticError(
            "GA4 response schema is unexpected"
        )


def _row_values(row: Any, dimension_count: int) -> Tuple[Tuple[str, ...], Metrics]:
    dimensions = tuple(
        str(getattr(item, "value", ""))
        for item in tuple(getattr(row, "dimension_values", ()) or ())
    )
    values = tuple(
        _parse_metric(getattr(item, "value", None))
        for item in tuple(getattr(row, "metric_values", ()) or ())
    )
    if (
        len(dimensions) != dimension_count
        or any(not value for value in dimensions)
        or len(values) != len(GA4_METRICS)
    ):
        raise GA4OrganicCoverageDiagnosticError("GA4 response row is malformed")
    return dimensions, Metrics(*values)


def _parse_metric(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise GA4OrganicCoverageDiagnosticError("GA4 metric is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GA4OrganicCoverageDiagnosticError("GA4 metric is invalid") from None
    if not parsed.is_finite() or parsed < 0:
        raise GA4OrganicCoverageDiagnosticError("GA4 metric is invalid")
    return parsed


def _validate_adapter_topics(signals: Iterable[TrafficSignal]) -> Tuple[str, ...]:
    topics = tuple(sorted(str(signal.topic) for signal in signals))
    if len(set(topics)) != len(topics) or any(
        topic not in _KNOWN_TOPICS for topic in topics
    ):
        raise GA4OrganicCoverageDiagnosticError(
            "GA4 adapter returned a non-allowlisted topic"
        )
    return topics


def _safe_output(result: CoverageResult) -> Tuple[str, ...]:
    return (
        "PROPERTY_ID={}".format(PROPERTY_ID),
        "AUTH_MODE={}".format(AUTH_MODE),
        "START_DATE={}".format(START_DATE),
        "END_DATE={}".format(END_DATE),
        "OBSERVED_AT={}".format(OBSERVED_AT),
        "REPORT_CALLS={}".format(result.report_calls),
        "ORGANIC_SESSIONS_TOTAL={}".format(
            _format_decimal(result.organic_totals.sessions)
        ),
        "ORGANIC_ENGAGED_SESSIONS_TOTAL={}".format(
            _format_decimal(result.organic_totals.engaged_sessions)
        ),
        "ORGANIC_KEY_EVENTS_TOTAL={}".format(
            _format_decimal(result.organic_totals.key_events)
        ),
        "MAPPED_ROW_COUNT={}".format(result.mapped.row_count),
        "MAPPED_SESSIONS={}".format(_format_decimal(result.mapped.metrics.sessions)),
        "MAPPED_ENGAGED_SESSIONS={}".format(
            _format_decimal(result.mapped.metrics.engaged_sessions)
        ),
        "MAPPED_KEY_EVENTS={}".format(
            _format_decimal(result.mapped.metrics.key_events)
        ),
        "UNMAPPED_OR_EXCLUDED_ROW_COUNT={}".format(
            result.unmapped_or_excluded.row_count
        ),
        "UNMAPPED_OR_EXCLUDED_SESSIONS={}".format(
            _format_decimal(result.unmapped_or_excluded.metrics.sessions)
        ),
        "UNMAPPED_OR_EXCLUDED_ENGAGED_SESSIONS={}".format(
            _format_decimal(result.unmapped_or_excluded.metrics.engaged_sessions)
        ),
        "UNMAPPED_OR_EXCLUDED_KEY_EVENTS={}".format(
            _format_decimal(result.unmapped_or_excluded.metrics.key_events)
        ),
        "NOT_SET_ROW_COUNT={}".format(result.not_set.row_count),
        "NOT_SET_SESSIONS={}".format(
            _format_decimal(result.not_set.metrics.sessions)
        ),
        "NOT_SET_ENGAGED_SESSIONS={}".format(
            _format_decimal(result.not_set.metrics.engaged_sessions)
        ),
        "NOT_SET_KEY_EVENTS={}".format(
            _format_decimal(result.not_set.metrics.key_events)
        ),
        "RESIDUAL_SESSIONS_GAP={}".format(
            _format_decimal(result.residual.sessions)
        ),
        "RESIDUAL_ENGAGED_SESSIONS_GAP={}".format(
            _format_decimal(result.residual.engaged_sessions)
        ),
        "RESIDUAL_KEY_EVENTS_GAP={}".format(
            _format_decimal(result.residual.key_events)
        ),
        "MAPPED_SESSION_COVERAGE_PCT={}".format(
            result.mapped_session_coverage_pct
        ),
        "ADAPTER_SIGNAL_COUNT={}".format(len(result.adapter_topics)),
        "ADAPTER_TOPICS={}".format(",".join(result.adapter_topics)),
        "FINAL_VERDICT={}".format(PASS_VERDICT),
    )


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def main() -> int:
    try:
        run_diagnostic()
    except Exception:
        print("FINAL_VERDICT={}".format(FAIL_VERDICT))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
