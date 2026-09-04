"""Manual read-only gate for the existing V3 GA4 analytics adapter."""

from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable, Iterable, Tuple

from scripts.v3.models import TrafficSignal
from scripts.v3.sources.analytics import (
    DEFAULT_GA4_LANDING_PAGE_TOPICS,
    GoogleAnalyticsDataSource,
    create_google_analytics_data_client,
)


PROPERTY_ID = "552715460"
AUTH_MODE = "WIF"
START_DATE = "2026-09-03"
END_DATE = "2026-09-03"
OBSERVED_AT = "2026-09-04"
EXPECTED_REPORT_CALLS = 1
PASS_VERDICT = "GA4_ADAPTER_READONLY_TEST_PASS"
FAIL_VERDICT = "GA4_ADAPTER_READONLY_TEST_FAILED"

_SAFE_TOPIC = re.compile(r"^[a-z0-9_]+$")
_KNOWN_TOPICS = frozenset(DEFAULT_GA4_LANDING_PAGE_TOPICS.values())


class GA4AdapterReadOnlyTestError(ValueError):
    """Raised when the read-only adapter result cannot be safely reported."""


def run_adapter_test(
    *,
    client_factory: Callable[[], Any] = create_google_analytics_data_client,
    data_source_class: Any = GoogleAnalyticsDataSource,
    emit: Callable[[str], None] = print,
) -> Tuple[TrafficSignal, ...]:
    """Collect once through the existing adapter and emit aggregate-only output."""

    try:
        client = client_factory()
        source = data_source_class(
            property_id=PROPERTY_ID,
            client=client,
            start_date=START_DATE,
            end_date=END_DATE,
            observed_at=OBSERVED_AT,
            landing_page_topics=dict(DEFAULT_GA4_LANDING_PAGE_TOPICS),
        )
        signals = tuple(source.collect())
        output = _safe_output(signals)
    except Exception:
        raise GA4AdapterReadOnlyTestError(
            "GA4 adapter read-only test failed"
        ) from None

    for line in output:
        emit(line)
    return tuple(sorted(signals, key=lambda signal: signal.topic))


def _safe_output(signals: Iterable[TrafficSignal]) -> Tuple[str, ...]:
    ordered = tuple(sorted(signals, key=lambda signal: signal.topic))
    topics = tuple(signal.topic for signal in ordered)
    if len(set(topics)) != len(topics):
        raise GA4AdapterReadOnlyTestError("GA4 adapter topics are duplicated")
    if any(
        topic not in _KNOWN_TOPICS or not _SAFE_TOPIC.fullmatch(topic)
        for topic in topics
    ):
        raise GA4AdapterReadOnlyTestError("GA4 adapter topic is not allowlisted")

    lines = [
        "PROPERTY_ID={}".format(PROPERTY_ID),
        "AUTH_MODE={}".format(AUTH_MODE),
        "START_DATE={}".format(START_DATE),
        "END_DATE={}".format(END_DATE),
        "OBSERVED_AT={}".format(OBSERVED_AT),
        "EXPECTED_REPORT_CALLS={}".format(EXPECTED_REPORT_CALLS),
        "SIGNAL_COUNT={}".format(len(ordered)),
        "TOPICS={}".format(",".join(topics)),
    ]
    for index, signal in enumerate(ordered, start=1):
        lines.extend((
            "SIGNAL_{}_TOPIC={}".format(index, signal.topic),
            "SIGNAL_{}_ORGANIC_SESSIONS={}".format(
                index, _format_metric(signal.organic_sessions)
            ),
            "SIGNAL_{}_ENGAGED_SESSIONS={}".format(
                index, _format_metric(signal.engaged_sessions)
            ),
            "SIGNAL_{}_CONVERSIONS={}".format(
                index, _format_optional_metric(signal.conversions)
            ),
        ))
    lines.append("FINAL_VERDICT={}".format(PASS_VERDICT))
    return tuple(lines)


def _format_optional_metric(value: Any) -> str:
    return "UNKNOWN" if value is None else _format_metric(value)


def _format_metric(value: Any) -> str:
    if value is None or isinstance(value, bool):
        raise GA4AdapterReadOnlyTestError("GA4 adapter metric is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GA4AdapterReadOnlyTestError("GA4 adapter metric is invalid") from None
    if not parsed.is_finite() or parsed < 0:
        raise GA4AdapterReadOnlyTestError("GA4 adapter metric is invalid")
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def main() -> int:
    try:
        run_adapter_test()
    except Exception:
        print("FINAL_VERDICT={}".format(FAIL_VERDICT))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
