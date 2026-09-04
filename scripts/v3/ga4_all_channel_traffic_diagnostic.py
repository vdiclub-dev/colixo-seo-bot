"""Manual aggregate-only GA4 all-channel traffic diagnostic."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable, Mapping, Tuple

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
EXPECTED_REPORT_CALLS = 2
PASS_VERDICT = "GA4_ALL_CHANNEL_TRAFFIC_DIAGNOSTIC_PASS"
FAIL_VERDICT = "GA4_ALL_CHANNEL_TRAFFIC_DIAGNOSTIC_FAILED"

GLOBAL_DIMENSIONS: Tuple[str, ...] = ()
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
    """Forward only the two exact allowlisted GA4 report requests."""

    def __init__(self, client: Any) -> None:
        if client is None:
            raise GA4AllChannelTrafficDiagnosticError("GA4 client is required")
        self.client = client
        self.requests = []
        self.responses = []

    def run_report(self, *, request: Mapping[str, Any]) -> Any:
        expected = (
            _build_request(GLOBAL_DIMENSIONS),
            _build_request(CHANNEL_DIMENSIONS),
        )
        request_index = len(self.requests)
        if request_index >= EXPECTED_REPORT_CALLS or request != expected[request_index]:
            raise GA4AllChannelTrafficDiagnosticError(
                "GA4 request does not match the diagnostic contract"
            )
        self.requests.append(request)
        response = self.client.run_report(request=request)
        self.responses.append(response)
        return response


def run_diagnostic(
    *,
    client_factory: Callable[[], Any] = create_google_analytics_data_client,
    emit: Callable[[str], None] = print,
) -> DiagnosticResult:
    """Execute two aggregate reports and emit only allowlisted channel totals."""

    try:
        client = RecordingClient(client_factory())
        client.run_report(request=_build_request(GLOBAL_DIMENSIONS))
        client.run_report(request=_build_request(CHANNEL_DIMENSIONS))
        if (
            len(client.requests) != EXPECTED_REPORT_CALLS
            or len(client.responses) != EXPECTED_REPORT_CALLS
        ):
            raise GA4AllChannelTrafficDiagnosticError(
                "GA4 report count does not match the diagnostic contract"
            )
        result = _build_result(client.responses)
        output = _safe_output(result)
    except Exception:
        raise GA4AllChannelTrafficDiagnosticError(
            "GA4 all-channel traffic diagnostic failed"
        ) from None

    for line in output:
        emit(line)
    return result


def _build_request(dimensions: Tuple[str, ...]) -> Mapping[str, Any]:
    request = {
        "property": GA4_RESOURCE,
        "date_ranges": [{"start_date": START_DATE, "end_date": END_DATE}],
        "metrics": [{"name": name} for name in GA4_METRICS],
    }
    if dimensions:
        request["dimensions"] = [{"name": name} for name in dimensions]
    return request


def _build_result(responses: Iterable[Any]) -> DiagnosticResult:
    global_response, channel_response = tuple(responses)
    totals = _parse_global_response(global_response)
    channels = _parse_channel_response(channel_response)
    channel_sums = Metrics()
    for channel in channels:
        channel_sums += channel.metrics
    if channel_sums.exceeds(totals):
        raise GA4AllChannelTrafficDiagnosticError(
            "GA4 channel sums exceed global totals"
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


def _parse_global_response(response: Any) -> Metrics:
    _validate_headers(response, GLOBAL_DIMENSIONS)
    rows = tuple(getattr(response, "rows", ()) or ())
    if len(rows) > 1:
        raise GA4AllChannelTrafficDiagnosticError(
            "GA4 global response contains unexpected rows"
        )
    if not rows:
        return Metrics()
    dimensions, metrics = _row_values(rows[0], 0)
    if dimensions:
        raise GA4AllChannelTrafficDiagnosticError(
            "GA4 global response contains an unexpected dimension"
        )
    return metrics


def _parse_channel_response(response: Any) -> Tuple[ChannelSummary, ...]:
    _validate_headers(response, CHANNEL_DIMENSIONS)
    channels = []
    seen = set()
    for row in tuple(getattr(response, "rows", ()) or ()):
        dimensions, metrics = _row_values(row, len(CHANNEL_DIMENSIONS))
        channel = dimensions[0]
        if channel not in ALLOWED_DEFAULT_CHANNELS or channel in seen:
            raise GA4AllChannelTrafficDiagnosticError(
                "GA4 channel response contains an unexpected value"
            )
        seen.add(channel)
        channels.append(ChannelSummary(channel, metrics))
    return tuple(sorted(channels, key=lambda item: item.name))


def _validate_headers(response: Any, dimensions: Tuple[str, ...]) -> None:
    if response is None:
        raise GA4AllChannelTrafficDiagnosticError("GA4 response is missing")
    actual_dimensions = tuple(
        str(getattr(item, "name", ""))
        for item in tuple(getattr(response, "dimension_headers", ()) or ())
    )
    actual_metrics = tuple(
        str(getattr(item, "name", ""))
        for item in tuple(getattr(response, "metric_headers", ()) or ())
    )
    if actual_dimensions != dimensions or actual_metrics != GA4_METRICS:
        raise GA4AllChannelTrafficDiagnosticError(
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
        raise GA4AllChannelTrafficDiagnosticError("GA4 response row is malformed")
    return dimensions, Metrics(*values)


def _parse_metric(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise GA4AllChannelTrafficDiagnosticError("GA4 metric is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GA4AllChannelTrafficDiagnosticError("GA4 metric is invalid") from None
    if not parsed.is_finite() or parsed < 0:
        raise GA4AllChannelTrafficDiagnosticError("GA4 metric is invalid")
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
