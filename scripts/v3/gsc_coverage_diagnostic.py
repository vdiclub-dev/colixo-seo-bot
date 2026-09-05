"""Manual, aggregate-only coverage diagnostic using one guarded GSC POST."""

import re
import sys
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

from scripts.v3.gsc_readonly_probe import CountingGSCTransport, GSCReadOnlyProbeError
from scripts.v3.sources.search_console import (
    GSCCollectionCoverage,
    GSCDataSourceError,
    GSC_PROPERTY,
    GoogleSearchConsoleDataSource,
    create_google_search_console_transport,
)


PASS_VERDICT = "V3_GSC_COVERAGE_DIAGNOSTIC_PASS"
FAIL_VERDICT = "V3_GSC_COVERAGE_DIAGNOSTIC_FAILED"
SAFE_FAILURE_CODES = (
    "OBSERVED_AT_INVALID", "TRANSPORT_CREATION_FAILED", "GSC_API_REQUEST_FAILED",
    "GSC_RESPONSE_INVALID", "GSC_ROW_INVALID", "COVERAGE_INVARIANT_INVALID",
    "REPORT_COUNT_INVALID", "REPORT_RENDER_FAILED", "UNEXPECTED_DIAGNOSTIC_FAILURE",
)
COUNT_FIELDS = (
    "raw_row_count", "accepted_signal_count", "brand_row_count", "unmapped_row_count",
    "pii_filtered_row_count",
)
METRIC_FIELDS = (
    "all_rows_clicks", "all_rows_impressions", "accepted_clicks", "accepted_impressions",
    "brand_clicks", "brand_impressions",
)


class GSCCoverageDiagnosticError(ValueError):
    """Expose only a safe code and a bounded completed-request count."""

    def __init__(self, code: str, calls: int) -> None:
        self.safe_code = code if code in SAFE_FAILURE_CODES else "UNEXPECTED_DIAGNOSTIC_FAILURE"
        self.calls = calls if type(calls) is int and calls in (0, 1) else 0
        super().__init__(self.safe_code)


def _validate_date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise GSCCoverageDiagnosticError("OBSERVED_AT_INVALID", 0)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise GSCCoverageDiagnosticError("OBSERVED_AT_INVALID", 0) from None
    return value


def _render_coverage(coverage: GSCCollectionCoverage) -> tuple[str, ...]:
    if not isinstance(coverage, GSCCollectionCoverage):
        raise GSCCoverageDiagnosticError("REPORT_RENDER_FAILED", 1)
    counts = tuple(getattr(coverage, field) for field in COUNT_FIELDS)
    if any(type(value) is not int or value < 0 for value in counts):
        raise GSCCoverageDiagnosticError("COVERAGE_INVARIANT_INVALID", 1)
    if counts[0] != sum(counts[1:]):
        raise GSCCoverageDiagnosticError("COVERAGE_INVARIANT_INVALID", 1)
    lines = [f"{field.upper()}={value}" for field, value in zip(COUNT_FIELDS, counts)]
    for field in METRIC_FIELDS:
        value = getattr(coverage, field)
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise GSCCoverageDiagnosticError("REPORT_RENDER_FAILED", 1)
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        lines.append(f"{field.upper()}={rendered or '0'}")
    return tuple(lines)


def run_diagnostic(
    *, observed_at: str,
    transport_factory: Callable[[], Any] = create_google_search_console_transport,
    emit: Callable[[str], None] = print,
) -> GSCCollectionCoverage:
    observed_at = _validate_date(observed_at)
    try:
        transport = CountingGSCTransport(transport_factory())
    except Exception:
        raise GSCCoverageDiagnosticError("TRANSPORT_CREATION_FAILED", 0) from None
    try:
        source = GoogleSearchConsoleDataSource(transport=transport, observed_at=observed_at)
        result = source.collect_with_coverage()
    except GSCReadOnlyProbeError:
        raise GSCCoverageDiagnosticError("REPORT_COUNT_INVALID", transport.completed_requests) from None
    except GSCDataSourceError as error:
        # Match only adapter-owned messages; never emit the underlying exception.
        message = str(error)
        if transport.api_failed or message == "Search Console API request failed":
            code = "GSC_API_REQUEST_FAILED"
        elif message == "Search Console coverage invariant is invalid":
            code = "COVERAGE_INVARIANT_INVALID"
        elif message.startswith("Search Console row "):
            code = "GSC_ROW_INVALID"
        elif message.startswith("Search Console response ") or message == "Search Console API response is invalid":
            code = "GSC_RESPONSE_INVALID"
        else:
            code = "UNEXPECTED_DIAGNOSTIC_FAILURE"
        raise GSCCoverageDiagnosticError(code, transport.completed_requests) from None
    except Exception:
        raise GSCCoverageDiagnosticError("UNEXPECTED_DIAGNOSTIC_FAILURE", transport.completed_requests) from None
    if transport.post_attempts != 1 or transport.completed_requests != 1:
        raise GSCCoverageDiagnosticError("REPORT_COUNT_INVALID", transport.completed_requests)
    try:
        coverage_lines = _render_coverage(result.coverage)
        lines = (
            f"PROPERTY={GSC_PROPERTY}", "AUTH_MODE=WIF", f"OBSERVED_AT={observed_at}",
            f"DATE_RANGE_START={source.start_date}", f"DATE_RANGE_END={source.end_date}",
            "GSC_API_CALLS=1",
        ) + coverage_lines + (f"FINAL_VERDICT={PASS_VERDICT}",)
        emit("\n".join(lines))
    except GSCCoverageDiagnosticError:
        raise
    except Exception:
        raise GSCCoverageDiagnosticError("REPORT_RENDER_FAILED", transport.completed_requests) from None
    return result.coverage


def main(
    arguments: Optional[Sequence[str]] = None, *,
    transport_factory: Callable[[], Any] = create_google_search_console_transport,
    emit: Callable[[str], None] = print,
) -> int:
    try:
        arguments = tuple(sys.argv[1:]) if arguments is None else tuple(arguments)
        if len(arguments) != 2 or arguments[0] != "--observed-at":
            raise GSCCoverageDiagnosticError("OBSERVED_AT_INVALID", 0)
        run_diagnostic(observed_at=arguments[1], transport_factory=transport_factory, emit=emit)
        return 0
    except GSCCoverageDiagnosticError as error:
        code, calls = error.safe_code, error.calls
    except Exception:
        code, calls = "UNEXPECTED_DIAGNOSTIC_FAILURE", 0
    emit(f"SAFE_FAILURE_CODE={code}\nGSC_API_CALLS_COMPLETED={calls}\nFINAL_VERDICT={FAIL_VERDICT}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
