"""Manual aggregate-only GA4 all-channel traffic diagnostic."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Tuple

from scripts.v3.sources.analytics import (
    GA4_CHANNEL_DIMENSIONS,
    GA4_METRICS,
    GA4_PROPERTY_ID,
    GA4_RESOURCE,
    ORGANIC_SEARCH_CHANNEL,
    create_google_analytics_data_client,
)


PROPERTY_ID = GA4_PROPERTY_ID
AUTH_MODE = "WIF"
START_DATE = "2026-09-03"
END_DATE = "2026-09-03"
OBSERVED_AT = "2026-09-04"
EXPECTED_REPORT_CALLS = 1
METRIC_AGGREGATION = "TOTAL"
RESERVED_TOTAL = "RESERVED_TOTAL"
PASS_VERDICT = "GA4_ALL_CHANNEL_TRAFFIC_DIAGNOSTIC_PASS"
FAIL_VERDICT = "GA4_ALL_CHANNEL_TRAFFIC_DIAGNOSTIC_FAILED"

ALLOWED_FAILURE_CODES = frozenset({
    "CLIENT_CREATION_FAILED",
    "CHANNEL_API_REQUEST_FAILED",
    "CHANNEL_RESPONSE_SCHEMA_FAILED",
    "CHANNEL_RESPONSE_ROW_FAILED",
    "CHANNEL_VALUE_NOT_ALLOWLISTED",
    "CHANNEL_VALUE_DUPLICATE",
    "CHANNEL_METRIC_FAILED",
    "TOTAL_RESPONSE_MISSING",
    "TOTAL_RESPONSE_SCHEMA_FAILED",
    "TOTAL_METRIC_FAILED",
    "CHANNEL_SUM_EXCEEDS_TOTAL",
    "REPORT_COUNT_FAILED",
    "UNEXPECTED_DIAGNOSTIC_FAILURE",
})

CHANNEL_DIMENSIONS = GA4_CHANNEL_DIMENSIONS

# Google-maintained GA4 default channel-group labels. Exact membership prevents
# arbitrary source, campaign, URL, or identifier values from reaching output.
ALLOWED_DEFAULT_CHANNELS = frozenset({
    "Affiliates",
    "AI Assistant",
    "Audio",
    "Cross-network",
    "Direct",
    "Display",
    "Email",
    "Mobile Push Notifications",
    "Organic Search",
    "Organic Shopping",
    "Organic Social",
    "Organic Video",
    "Paid Other",
    "Paid Search",
    "Paid Shopping",
    "Paid Social",
    "Paid Video",
    "Referral",
    "SMS",
    "Unassigned",
})


class GA4AllChannelTrafficDiagnosticError(ValueError):
    """Raised when the diagnostic cannot safely trust or summarize GA4 data."""

    def __init__(self, safe_code: str, report_calls_completed: int) -> None:
        if safe_code not in ALLOWED_FAILURE_CODES:
            safe_code = "UNEXPECTED_DIAGNOSTIC_FAILURE"
        if report_calls_completed not in (0, 1):
            safe_code = "REPORT_COUNT_FAILED"
            report_calls_completed = 0
        self.safe_code = safe_code
        self.report_calls_completed = report_calls_completed
        super().__init__(safe_code)


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
class ChannelSummary:
    name: str
    metrics: Metrics


@dataclass(frozen=True)
class DiagnosticResult:
    report_calls: int
    totals: Metrics
    channels: Tuple[ChannelSummary, ...]
    channel_sums: Metrics
    residual: Metrics
    organic_search: Metrics


class RecordingClient:
    """Forward only the one exact allowlisted GA4 report request."""

    def __init__(self, client: Any) -> None:
        if client is None:
            raise GA4AllChannelTrafficDiagnosticError(
                "CLIENT_CREATION_FAILED", 0
            )
        self.client = client
        self.requests = []
        self.responses = []

    def run_report(self, *, request: Mapping[str, Any]) -> Any:
        if self.requests or request != _build_request():
            raise GA4AllChannelTrafficDiagnosticError(
                "REPORT_COUNT_FAILED", len(self.responses)
            )
        self.requests.append(request)
        try:
            response = self.client.run_report(request=request)
        except Exception:
            raise GA4AllChannelTrafficDiagnosticError(
                "CHANNEL_API_REQUEST_FAILED", 0
            ) from None
        self.responses.append(response)
        return response


def run_diagnostic(
    *,
    client_factory: Callable[[], Any] = create_google_analytics_data_client,
    emit: Callable[[str], None] = print,
) -> DiagnosticResult:
    """Execute one aggregate report and emit only allowlisted channel totals."""

    try:
        raw_client = client_factory()
    except Exception:
        raise GA4AllChannelTrafficDiagnosticError(
            "CLIENT_CREATION_FAILED", 0
        ) from None

    try:
        client = RecordingClient(raw_client)
        response = client.run_report(request=_build_request())
        if (
            len(client.requests) != EXPECTED_REPORT_CALLS
            or len(client.responses) != EXPECTED_REPORT_CALLS
        ):
            raise GA4AllChannelTrafficDiagnosticError(
                "REPORT_COUNT_FAILED", len(client.responses)
            )
        result = _build_result(response)
        output = _safe_output(result)
    except GA4AllChannelTrafficDiagnosticError:
        raise
    except Exception:
        raise GA4AllChannelTrafficDiagnosticError(
            "UNEXPECTED_DIAGNOSTIC_FAILURE", len(client.responses)
        ) from None

    for line in output:
        emit(line)
    return result


def _build_request() -> Mapping[str, Any]:
    return {
        "property": GA4_RESOURCE,
        "date_ranges": [{"start_date": START_DATE, "end_date": END_DATE}],
        "dimensions": [{"name": name} for name in CHANNEL_DIMENSIONS],
        "metrics": [{"name": name} for name in GA4_METRICS],
        "metric_aggregations": [METRIC_AGGREGATION],
    }


def _build_result(response: Any) -> DiagnosticResult:
    _validate_headers(
        response, CHANNEL_DIMENSIONS, "CHANNEL_RESPONSE_SCHEMA_FAILED"
    )
    totals = _parse_total_response(response)
    channels = _parse_channel_rows(response)
    channel_sums = Metrics()
    for channel in channels:
        channel_sums += channel.metrics
    if channel_sums.exceeds(totals):
        raise GA4AllChannelTrafficDiagnosticError(
            "CHANNEL_SUM_EXCEEDS_TOTAL", 1
        )
    organic_search = next(
        (
            channel.metrics
            for channel in channels
            if channel.name == ORGANIC_SEARCH_CHANNEL
        ),
        Metrics(),
    )
    return DiagnosticResult(
        report_calls=EXPECTED_REPORT_CALLS,
        totals=totals,
        channels=channels,
        channel_sums=channel_sums,
        residual=totals.subtract(channel_sums),
        organic_search=organic_search,
    )


def _parse_total_response(response: Any) -> Metrics:
    try:
        total_rows = tuple(getattr(response, "totals", ()) or ())
    except Exception:
        raise GA4AllChannelTrafficDiagnosticError(
            "TOTAL_RESPONSE_SCHEMA_FAILED", 1
        ) from None
    if not total_rows:
        raise GA4AllChannelTrafficDiagnosticError(
            "TOTAL_RESPONSE_MISSING", 1
        )
    if len(total_rows) != 1:
        raise GA4AllChannelTrafficDiagnosticError(
            "TOTAL_RESPONSE_SCHEMA_FAILED", 1
        )
    dimensions = _row_dimensions(
        total_rows[0],
        len(CHANNEL_DIMENSIONS),
        "TOTAL_RESPONSE_SCHEMA_FAILED",
    )
    if dimensions != (RESERVED_TOTAL,):
        raise GA4AllChannelTrafficDiagnosticError(
            "TOTAL_RESPONSE_SCHEMA_FAILED", 1
        )
    return _row_metrics(total_rows[0], "TOTAL_METRIC_FAILED")


def _parse_channel_rows(response: Any) -> Tuple[ChannelSummary, ...]:
    channels = []
    seen = set()
    for row in _response_rows(response, "CHANNEL_RESPONSE_ROW_FAILED"):
        dimensions = _row_dimensions(
            row, len(CHANNEL_DIMENSIONS), "CHANNEL_RESPONSE_ROW_FAILED"
        )
        metrics = _row_metrics(row, "CHANNEL_METRIC_FAILED")
        channel = dimensions[0]
        if channel not in ALLOWED_DEFAULT_CHANNELS:
            raise GA4AllChannelTrafficDiagnosticError(
                "CHANNEL_VALUE_NOT_ALLOWLISTED", 1
            )
        if channel in seen:
            raise GA4AllChannelTrafficDiagnosticError(
                "CHANNEL_VALUE_DUPLICATE", 1
            )
        seen.add(channel)
        channels.append(ChannelSummary(channel, metrics))
    return tuple(sorted(channels, key=lambda item: item.name))


def _validate_headers(
    response: Any,
    dimensions: Tuple[str, ...],
    failure_code: str,
) -> None:
    if response is None:
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1)
    try:
        actual_dimensions = tuple(
            str(getattr(item, "name", ""))
            for item in tuple(getattr(response, "dimension_headers", ()) or ())
        )
        actual_metrics = tuple(
            str(getattr(item, "name", ""))
            for item in tuple(getattr(response, "metric_headers", ()) or ())
        )
    except Exception:
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1) from None
    if actual_dimensions != dimensions or actual_metrics != GA4_METRICS:
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1)


def _response_rows(response: Any, failure_code: str) -> Tuple[Any, ...]:
    try:
        return tuple(getattr(response, "rows", ()) or ())
    except Exception:
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1) from None


def _row_dimensions(
    row: Any,
    dimension_count: int,
    failure_code: str,
) -> Tuple[str, ...]:
    if row is None or not hasattr(row, "dimension_values"):
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1)
    try:
        dimensions = tuple(
            str(getattr(item, "value", ""))
            for item in tuple(getattr(row, "dimension_values", ()) or ())
        )
    except Exception:
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1) from None
    if len(dimensions) != dimension_count or any(not value for value in dimensions):
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1)
    return dimensions


def _row_metrics(row: Any, failure_code: str) -> Metrics:
    try:
        raw_values = tuple(getattr(row, "metric_values", ()) or ())
    except Exception:
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1) from None
    if len(raw_values) != len(GA4_METRICS):
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1)
    try:
        values = tuple(
            _parse_metric(getattr(item, "value", None), failure_code)
            for item in raw_values
        )
    except GA4AllChannelTrafficDiagnosticError:
        raise
    except Exception:
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1) from None
    return Metrics(*values)


def _parse_metric(value: Any, failure_code: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1) from None
    if not parsed.is_finite() or parsed < 0:
        raise GA4AllChannelTrafficDiagnosticError(failure_code, 1)
    return parsed


def _safe_output(result: DiagnosticResult) -> Tuple[str, ...]:
    lines = [
        "PROPERTY_ID={}".format(PROPERTY_ID),
        "AUTH_MODE={}".format(AUTH_MODE),
        "START_DATE={}".format(START_DATE),
        "END_DATE={}".format(END_DATE),
        "OBSERVED_AT={}".format(OBSERVED_AT),
        "REPORT_CALLS={}".format(result.report_calls),
        "TOTAL_SESSIONS={}".format(_format_decimal(result.totals.sessions)),
        "TOTAL_ENGAGED_SESSIONS={}".format(
            _format_decimal(result.totals.engaged_sessions)
        ),
        "TOTAL_KEY_EVENTS={}".format(_format_decimal(result.totals.key_events)),
        "CHANNEL_ROW_COUNT={}".format(len(result.channels)),
    ]
    for index, channel in enumerate(result.channels, start=1):
        lines.extend((
            "CHANNEL_{}_NAME={}".format(index, channel.name),
            "CHANNEL_{}_SESSIONS={}".format(
                index, _format_decimal(channel.metrics.sessions)
            ),
            "CHANNEL_{}_ENGAGED_SESSIONS={}".format(
                index, _format_decimal(channel.metrics.engaged_sessions)
            ),
            "CHANNEL_{}_KEY_EVENTS={}".format(
                index, _format_decimal(channel.metrics.key_events)
            ),
        ))
    lines.extend((
        "CHANNEL_SESSIONS_SUM={}".format(
            _format_decimal(result.channel_sums.sessions)
        ),
        "CHANNEL_ENGAGED_SESSIONS_SUM={}".format(
            _format_decimal(result.channel_sums.engaged_sessions)
        ),
        "CHANNEL_KEY_EVENTS_SUM={}".format(
            _format_decimal(result.channel_sums.key_events)
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
        "ORGANIC_SEARCH_SESSIONS={}".format(
            _format_decimal(result.organic_search.sessions)
        ),
        "ORGANIC_SEARCH_ENGAGED_SESSIONS={}".format(
            _format_decimal(result.organic_search.engaged_sessions)
        ),
        "ORGANIC_SEARCH_KEY_EVENTS={}".format(
            _format_decimal(result.organic_search.key_events)
        ),
        "FINAL_VERDICT={}".format(PASS_VERDICT),
    ))
    return tuple(lines)


def _safe_failure_output(
    failure: GA4AllChannelTrafficDiagnosticError,
) -> Tuple[str, ...]:
    return (
        "PROPERTY_ID={}".format(PROPERTY_ID),
        "AUTH_MODE={}".format(AUTH_MODE),
        "START_DATE={}".format(START_DATE),
        "END_DATE={}".format(END_DATE),
        "SAFE_FAILURE_CODE={}".format(failure.safe_code),
        "REPORT_CALLS_COMPLETED={}".format(
            failure.report_calls_completed
        ),
        "FINAL_VERDICT={}".format(FAIL_VERDICT),
    )


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def main() -> int:
    try:
        run_diagnostic()
    except GA4AllChannelTrafficDiagnosticError as failure:
        for line in _safe_failure_output(failure):
            print(line)
        return 1
    except Exception:
        failure = GA4AllChannelTrafficDiagnosticError(
            "UNEXPECTED_DIAGNOSTIC_FAILURE", 0
        )
        for line in _safe_failure_output(failure):
            print(line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
