"""Manual WIF probe for one sanitized V3 Search Console read-only call."""

import re
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional, Sequence, Tuple

from scripts.v3.models import SearchSignal
from scripts.v3.sources.search_console import (
    GSCDataSourceError,
    GSC_PROPERTY,
    GoogleSearchConsoleDataSource,
    create_google_search_console_transport,
)


AUTH_MODE = "WIF"
EXPECTED_GSC_API_CALLS = 1
PASS_VERDICT = "V3_GSC_READONLY_PROBE_PASS"
FAIL_VERDICT = "V3_GSC_READONLY_PROBE_FAILED"
TOPICS = (
    "general_delivery",
    "business_delivery",
    "parcel_delivery",
    "wine_delivery",
    "secure_watch_delivery",
)
SAFE_FAILURE_CODES = (
    "OBSERVED_AT_INVALID",
    "TRANSPORT_CREATION_FAILED",
    "GSC_API_REQUEST_FAILED",
    "GSC_RESPONSE_INVALID",
    "GSC_ROW_INVALID",
    "REPORT_COUNT_INVALID",
    "REPORT_RENDER_FAILED",
    "UNEXPECTED_PROBE_FAILURE",
)
_SAFE_FAILURE_CODE_SET = frozenset(SAFE_FAILURE_CODES)


class GSCReadOnlyProbeError(ValueError):
    """Carry only allowlisted failure metadata and never an underlying error."""

    __slots__ = ("safe_code", "api_calls_completed")

    def __init__(self, safe_code: str, api_calls_completed: int) -> None:
        if safe_code not in _SAFE_FAILURE_CODE_SET:
            safe_code = "UNEXPECTED_PROBE_FAILURE"
        if api_calls_completed not in (0, 1):
            safe_code = "REPORT_COUNT_INVALID"
            api_calls_completed = 1 if api_calls_completed else 0
        self.safe_code = safe_code
        self.api_calls_completed = api_calls_completed
        super().__init__(safe_code)


class CountingGSCTransport:
    """Forward at most one POST without retaining request or response data."""

    __slots__ = ("post_attempts", "completed_requests", "api_failed")

    def __new__(cls, transport: Any) -> "CountingGSCTransport":
        if cls is not CountingGSCTransport:
            return object.__new__(cls)

        delegate = transport.post

        class BoundCountingGSCTransport(CountingGSCTransport):
            __slots__ = ()

            def _forward(self, url: str, *, json: Any, timeout: int) -> Any:
                return delegate(url, json=json, timeout=timeout)

        return object.__new__(BoundCountingGSCTransport)

    def __init__(self, transport: Any) -> None:
        self.post_attempts = 0
        self.completed_requests = 0
        self.api_failed = False

    def post(self, url: str, *, json: Any, timeout: int) -> Any:
        if self.post_attempts >= EXPECTED_GSC_API_CALLS:
            raise GSCReadOnlyProbeError(
                "REPORT_COUNT_INVALID", self.completed_requests
            )
        self.post_attempts += 1
        try:
            response = self._forward(url, json=json, timeout=timeout)
        except GSCReadOnlyProbeError:
            raise
        except Exception:
            self.api_failed = True
            raise
        self.completed_requests += 1
        return response

    def _forward(self, url: str, *, json: Any, timeout: int) -> Any:
        raise NotImplementedError


@dataclass(frozen=True)
class ProbeExecution:
    signals: Tuple[SearchSignal, ...]
    api_calls: int


def run_probe(
    *,
    observed_at: str,
    transport_factory: Callable[[], Any] = create_google_search_console_transport,
    emit: Callable[[str], None] = print,
) -> ProbeExecution:
    """Execute one live adapter collection and emit aggregate metadata only."""

    normalized_observed_at = _validate_observed_at(observed_at)
    counting_transport: Optional[CountingGSCTransport] = None
    try:
        transport = transport_factory()
        counting_transport = CountingGSCTransport(transport)
    except Exception:
        raise GSCReadOnlyProbeError("TRANSPORT_CREATION_FAILED", 0) from None

    try:
        source = GoogleSearchConsoleDataSource(
            transport=counting_transport,
            observed_at=normalized_observed_at,
        )
        signals = source.collect()
    except GSCReadOnlyProbeError:
        raise
    except GSCDataSourceError as error:
        raise GSCReadOnlyProbeError(
            _adapter_failure_code(error, counting_transport),
            counting_transport.completed_requests,
        ) from None
    except Exception:
        raise GSCReadOnlyProbeError(
            "UNEXPECTED_PROBE_FAILURE",
            counting_transport.completed_requests,
        ) from None

    if (
        counting_transport.post_attempts != EXPECTED_GSC_API_CALLS
        or counting_transport.completed_requests != EXPECTED_GSC_API_CALLS
    ):
        raise GSCReadOnlyProbeError(
            "REPORT_COUNT_INVALID", counting_transport.completed_requests
        )

    try:
        totals = _aggregate_signals(signals)
        metadata = (
            "PROPERTY={}".format(GSC_PROPERTY),
            "AUTH_MODE={}".format(AUTH_MODE),
            "OBSERVED_AT={}".format(normalized_observed_at),
            "DATE_RANGE_START={}".format(source.start_date),
            "DATE_RANGE_END={}".format(source.end_date),
            "GSC_API_CALLS={}".format(counting_transport.completed_requests),
            "SIGNAL_COUNT={}".format(len(signals)),
            "TOTAL_CLICKS={}".format(_format_decimal(totals[0])),
            "TOTAL_IMPRESSIONS={}".format(_format_decimal(totals[1])),
        ) + tuple(
            "{}_SIGNAL_COUNT={}".format(topic.upper(), totals[2][topic])
            for topic in TOPICS
        )
        for line in metadata:
            emit(line)
        emit("FINAL_VERDICT={}".format(PASS_VERDICT))
    except GSCReadOnlyProbeError:
        raise
    except Exception:
        raise GSCReadOnlyProbeError(
            "REPORT_RENDER_FAILED", counting_transport.completed_requests
        ) from None

    return ProbeExecution(
        signals=signals,
        api_calls=counting_transport.completed_requests,
    )


def _aggregate_signals(
    signals: Tuple[SearchSignal, ...],
) -> Tuple[Decimal, Decimal, dict[str, int]]:
    clicks = Decimal(0)
    impressions = Decimal(0)
    topic_counts = {topic: 0 for topic in TOPICS}
    for signal in signals:
        if not isinstance(signal, SearchSignal) or signal.topic not in topic_counts:
            raise GSCReadOnlyProbeError("REPORT_RENDER_FAILED", 1)
        clicks += _safe_decimal(signal.clicks)
        impressions += _safe_decimal(signal.impressions)
        topic_counts[signal.topic] += 1
    return clicks, impressions, topic_counts


def _safe_decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise GSCReadOnlyProbeError("REPORT_RENDER_FAILED", 1)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise GSCReadOnlyProbeError("REPORT_RENDER_FAILED", 1) from None
    if not parsed.is_finite() or parsed < 0:
        raise GSCReadOnlyProbeError("REPORT_RENDER_FAILED", 1)
    return parsed


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _adapter_failure_code(
    error: GSCDataSourceError,
    transport: CountingGSCTransport,
) -> str:
    if transport.api_failed:
        return "GSC_API_REQUEST_FAILED"
    message = str(error)
    if message == "Search Console API request failed":
        return "GSC_API_REQUEST_FAILED"
    if "row" in message.lower():
        return "GSC_ROW_INVALID"
    if "response" in message.lower():
        return "GSC_RESPONSE_INVALID"
    return "UNEXPECTED_PROBE_FAILURE"


def _validate_observed_at(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise GSCReadOnlyProbeError("OBSERVED_AT_INVALID", 0)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise GSCReadOnlyProbeError("OBSERVED_AT_INVALID", 0) from None
    return value


def _parse_observed_at(arguments: Sequence[str]) -> str:
    if len(arguments) != 2 or arguments[0] != "--observed-at":
        raise GSCReadOnlyProbeError("OBSERVED_AT_INVALID", 0)
    return _validate_observed_at(arguments[1])


def _failure_output(error: GSCReadOnlyProbeError) -> tuple[str, ...]:
    return (
        "SAFE_FAILURE_CODE={}".format(error.safe_code),
        "GSC_API_CALLS_COMPLETED={}".format(error.api_calls_completed),
        "FINAL_VERDICT={}".format(FAIL_VERDICT),
    )


def main(
    arguments: Optional[Sequence[str]] = None,
    *,
    transport_factory: Callable[[], Any] = create_google_search_console_transport,
    emit: Callable[[str], None] = print,
) -> int:
    try:
        observed_at = _parse_observed_at(
            tuple(sys.argv[1:]) if arguments is None else tuple(arguments)
        )
        run_probe(
            observed_at=observed_at,
            transport_factory=transport_factory,
            emit=emit,
        )
    except GSCReadOnlyProbeError as error:
        for line in _failure_output(error):
            emit(line)
        return 1
    except Exception:
        error = GSCReadOnlyProbeError("UNEXPECTED_PROBE_FAILURE", 0)
        for line in _failure_output(error):
            emit(line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
