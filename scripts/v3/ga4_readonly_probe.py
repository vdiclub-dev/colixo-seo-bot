"""One-call, read-only GA4 diagnostic probe for an explicitly authorized gate."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Tuple

from scripts.v3.sources.analytics import create_google_analytics_data_client


PROPERTY_ID = "552715460"
PROPERTY_RESOURCE = "properties/552715460"
AUTH_MODE = "WIF"
DIMENSIONS = ("sessionDefaultChannelGroup",)
METRICS = ("sessions", "engagedSessions", "keyEvents")
CHANNEL = "Organic Search"


class GA4ReadOnlyProbeError(ValueError):
    """Raised when the single read-only report cannot be trusted."""


@dataclass(frozen=True)
class ProbeResult:
    row_count: int
    organic_sessions_total: Decimal
    engaged_sessions_total: Decimal
    key_events_total: Decimal


def build_request() -> Mapping[str, Any]:
    """Build the fixed aggregate-only request for the current GA4 property day."""

    return {
        "property": PROPERTY_RESOURCE,
        "date_ranges": [{"start_date": "today", "end_date": "today"}],
        "dimensions": [{"name": DIMENSIONS[0]}],
        "metrics": [{"name": name} for name in METRICS],
        "dimension_filter": {
            "filter": {
                "field_name": DIMENSIONS[0],
                "string_filter": {
                    "match_type": "EXACT",
                    "value": CHANNEL,
                    "case_sensitive": True,
                },
            }
        },
    }


def run_probe(
    *, client: Any = None, emit: Callable[[str], None] = print
) -> ProbeResult:
    """Execute exactly one injected or ADC-backed ``run_report`` call."""

    ga4_client = client if client is not None else create_google_analytics_data_client()
    try:
        response = ga4_client.run_report(request=build_request())
    except Exception:
        raise GA4ReadOnlyProbeError("GA4 read-only report failed") from None

    result = _parse_response(response)
    for line in _safe_output(result):
        emit(line)
    return result


def _parse_response(response: Any) -> ProbeResult:
    if response is None:
        raise GA4ReadOnlyProbeError("GA4 response is missing")

    dimensions = tuple(
        str(getattr(header, "name", ""))
        for header in tuple(getattr(response, "dimension_headers", ()) or ())
    )
    metrics = tuple(
        str(getattr(header, "name", ""))
        for header in tuple(getattr(response, "metric_headers", ()) or ())
    )
    if dimensions != DIMENSIONS or metrics != METRICS:
        raise GA4ReadOnlyProbeError("GA4 response schema is unexpected")

    rows = tuple(getattr(response, "rows", ()) or ())
    totals = [Decimal(0), Decimal(0), Decimal(0)]
    for row in rows:
        dimension_values = tuple(
            str(getattr(value, "value", ""))
            for value in tuple(getattr(row, "dimension_values", ()) or ())
        )
        metric_values = tuple(
            _parse_metric(getattr(value, "value", None))
            for value in tuple(getattr(row, "metric_values", ()) or ())
        )
        if dimension_values != (CHANNEL,):
            raise GA4ReadOnlyProbeError("GA4 response channel is unexpected")
        if len(metric_values) != len(METRICS):
            raise GA4ReadOnlyProbeError("GA4 response metrics are malformed")
        for index, value in enumerate(metric_values):
            totals[index] += value

    return ProbeResult(
        row_count=len(rows),
        organic_sessions_total=totals[0],
        engaged_sessions_total=totals[1],
        key_events_total=totals[2],
    )


def _parse_metric(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise GA4ReadOnlyProbeError("GA4 metric is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GA4ReadOnlyProbeError("GA4 metric is invalid") from None
    if not parsed.is_finite() or parsed < 0:
        raise GA4ReadOnlyProbeError("GA4 metric is invalid")
    return parsed


def _safe_output(result: ProbeResult) -> Tuple[str, ...]:
    return (
        "PROPERTY_ID={}".format(PROPERTY_ID),
        "AUTH_MODE={}".format(AUTH_MODE),
        "REPORT_CALLS=1",
        "DIMENSIONS={}".format(",".join(DIMENSIONS)),
        "METRICS={}".format(",".join(METRICS)),
        "CHANNEL={}".format(CHANNEL),
        "ROW_COUNT={}".format(result.row_count),
        "ORGANIC_SESSIONS_TOTAL={}".format(_format_decimal(result.organic_sessions_total)),
        "ENGAGED_SESSIONS_TOTAL={}".format(_format_decimal(result.engaged_sessions_total)),
        "KEY_EVENTS_TOTAL={}".format(_format_decimal(result.key_events_total)),
        "FINAL_VERDICT=GA4_READONLY_PROBE_PASS",
    )


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def main() -> int:
    try:
        run_probe()
    except Exception:
        print("FINAL_VERDICT=GA4_READONLY_PROBE_FAILED")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
