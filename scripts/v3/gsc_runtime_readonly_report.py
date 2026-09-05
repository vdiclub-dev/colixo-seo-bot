"""Manual GSC runtime report: one guarded collection, aggregate output only."""

import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from scripts.v3.agent import AgentResult, MarketIntelligenceAgent
from scripts.v3.config import RuntimeState
from scripts.v3.gsc_readonly_probe import CountingGSCTransport, GSCReadOnlyProbeError
from scripts.v3.source_factory import GSCRuntimeCoverage
from scripts.v3.sources.search_console import (
    GSCDataSourceError, create_google_search_console_transport,
)


GSC_READONLY_CONFIG = Path(__file__).resolve().parents[2] / "config/seo_agent_v3_gsc_readonly.json"
PASS_VERDICT = "V3_GSC_READONLY_RUNTIME_PASS"
FAIL_VERDICT = "V3_GSC_READONLY_RUNTIME_FAILED"
SAFE_FAILURE_CODES = (
    "OBSERVED_AT_INVALID", "TRANSPORT_CREATION_FAILED", "RUNTIME_CONSTRUCTION_FAILED",
    "GSC_API_REQUEST_FAILED", "GSC_RESPONSE_INVALID", "GSC_ROW_INVALID",
    "COVERAGE_INVALID", "REPORT_COUNT_INVALID", "REPORT_RENDER_FAILED",
    "UNEXPECTED_RUNTIME_FAILURE",
)
COUNT_FIELDS = (
    "raw_row_count", "accepted_signal_count", "brand_row_count", "unmapped_row_count",
    "pii_filtered_row_count",
)
METRIC_FIELDS = (
    "all_rows_clicks", "all_rows_impressions", "accepted_clicks", "accepted_impressions",
    "brand_clicks", "brand_impressions",
)


class GSCRuntimeReportError(ValueError):
    """Carry only an allowlisted code and a bounded completed-request count."""

    def __init__(self, code: str, calls: int) -> None:
        self.safe_code = code if code in SAFE_FAILURE_CODES else "UNEXPECTED_RUNTIME_FAILURE"
        self.calls = calls if type(calls) is int and calls in (0, 1) else 0
        super().__init__(self.safe_code)


@dataclass(frozen=True)
class RuntimeExecution:
    result: AgentResult
    api_calls: int


class RuntimeTransport:
    """Preserve the existing guard's rejection marker through adapter masking."""

    def __init__(self, transport: Any) -> None:
        self.guard = CountingGSCTransport(transport)
        self.limit_exceeded = False

    @property
    def completed_requests(self) -> int:
        return self.guard.completed_requests

    @property
    def post_attempts(self) -> int:
        return self.guard.post_attempts

    def post(self, url: str, *, json: Any, timeout: int) -> Any:
        try:
            return self.guard.post(url, json=json, timeout=timeout)
        except GSCReadOnlyProbeError:
            self.limit_exceeded = True
            raise


def _validate_date(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise GSCRuntimeReportError("OBSERVED_AT_INVALID", 0)
    try:
        observed = date.fromisoformat(value)
        observed - timedelta(days=28)
    except (ValueError, OverflowError):
        raise GSCRuntimeReportError("OBSERVED_AT_INVALID", 0) from None
    return value


def _coverage_lines(coverage: Optional[GSCRuntimeCoverage]) -> tuple[str, ...]:
    if not isinstance(coverage, GSCRuntimeCoverage):
        raise GSCRuntimeReportError("COVERAGE_INVALID", 1)
    counts = tuple(getattr(coverage, name) for name in COUNT_FIELDS)
    if (any(type(n) is not int or n < 0 for n in counts)
            or counts[0] != sum(counts[1:])
            or type(coverage.row_limit) is not int or coverage.row_limit != 25000
            or counts[0] > coverage.row_limit
            or coverage.row_limit_reached is not (counts[0] == 25000)
            or coverage.totals_scope != "returned_query_rows_non_exhaustive"):
        raise GSCRuntimeReportError("COVERAGE_INVALID", 1)
    lines = [f"{name.upper()}={value}" for name, value in zip(COUNT_FIELDS, counts)]
    for name in METRIC_FIELDS:
        value = getattr(coverage, name)
        if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
            raise GSCRuntimeReportError("COVERAGE_INVALID", 1)
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        lines.append(f"{name.upper()}={rendered or '0'}")
    lines.extend(("ROW_LIMIT=25000",
                  f"ROW_LIMIT_REACHED={str(coverage.row_limit_reached).lower()}",
                  "TOTALS_SCOPE=returned_query_rows_non_exhaustive"))
    return tuple(lines)


def _deny_ga4() -> Any:
    raise GSCRuntimeReportError("RUNTIME_CONSTRUCTION_FAILED", 0)


def run_runtime_report(
    *, observed_at: str,
    transport_factory: Callable[[], Any] = create_google_search_console_transport,
    emit: Callable[[str], None] = print,
) -> RuntimeExecution:
    observed_at = _validate_date(observed_at)
    transport: Optional[RuntimeTransport] = None

    def counting_factory() -> RuntimeTransport:
        nonlocal transport
        if transport is not None:
            raise GSCRuntimeReportError("RUNTIME_CONSTRUCTION_FAILED", transport.completed_requests)
        try:
            transport = RuntimeTransport(transport_factory())
        except Exception:
            raise GSCRuntimeReportError("TRANSPORT_CREATION_FAILED", 0) from None
        return transport

    try:
        agent = MarketIntelligenceAgent(
            config_path=GSC_READONLY_CONFIG, observed_at=observed_at,
            gsc_transport_factory=counting_factory, ga4_client_factory=_deny_ga4,
        )
        if agent.config.runtime_state is not RuntimeState.GSC_READ_ONLY:
            raise GSCRuntimeReportError("RUNTIME_CONSTRUCTION_FAILED", 0)
    except GSCRuntimeReportError:
        raise
    except Exception:
        raise GSCRuntimeReportError("RUNTIME_CONSTRUCTION_FAILED", 0) from None

    try:
        result = agent.run({})
    except GSCReadOnlyProbeError:
        raise GSCRuntimeReportError("REPORT_COUNT_INVALID", _calls(transport)) from None
    except GSCDataSourceError as error:
        message = str(error)
        if message == "Search Console API request failed":
            # HTTP errors and guard errors share the adapter's safe message.
            code = ("REPORT_COUNT_INVALID" if transport is not None
                    and transport.limit_exceeded
                    else "GSC_API_REQUEST_FAILED")
        elif message == "Search Console coverage invariant is invalid":
            code = "COVERAGE_INVALID"
        elif message.startswith("Search Console row "):
            code = "GSC_ROW_INVALID"
        elif message.startswith("Search Console response ") or message == "Search Console API response is invalid":
            code = "GSC_RESPONSE_INVALID"
        else:
            code = "UNEXPECTED_RUNTIME_FAILURE"
        raise GSCRuntimeReportError(code, _calls(transport)) from None
    except Exception:
        raise GSCRuntimeReportError("UNEXPECTED_RUNTIME_FAILURE", _calls(transport)) from None

    calls = _calls(transport)
    if transport is None or transport.limit_exceeded or transport.post_attempts != 1 or calls != 1:
        raise GSCRuntimeReportError("REPORT_COUNT_INVALID", calls)
    try:
        coverage_lines = _coverage_lines(result.gsc_coverage)
        if not isinstance(result.markdown, str):
            raise GSCRuntimeReportError("REPORT_RENDER_FAILED", calls)
        observed = date.fromisoformat(observed_at)
        metadata = (
            "RUNTIME_STATE=GSC_READ_ONLY", "PROPERTY=sc-domain:colixo.ch", "AUTH_MODE=WIF",
            f"OBSERVED_AT={observed_at}",
            f"DATE_RANGE_START={(observed - timedelta(days=28)).isoformat()}",
            f"DATE_RANGE_END={(observed - timedelta(days=3)).isoformat()}",
            "GSC_API_CALLS=1", f"TOPIC_COUNT={len(result.scores)}",
            f"RECOMMENDATION_COUNT={len(result.recommendations)}",
        )
        # Validate everything before emitting; never serialize runtime objects.
        output = "\n".join(metadata + coverage_lines + (
            result.markdown, f"FINAL_VERDICT={PASS_VERDICT}",
        ))
        emit(output)
    except GSCRuntimeReportError:
        raise
    except Exception:
        raise GSCRuntimeReportError("REPORT_RENDER_FAILED", calls) from None
    return RuntimeExecution(result=result, api_calls=calls)


def _calls(transport: Optional[RuntimeTransport]) -> int:
    return 0 if transport is None else transport.completed_requests


def main(
    arguments: Optional[Sequence[str]] = None, *,
    transport_factory: Callable[[], Any] = create_google_search_console_transport,
    emit: Callable[[str], None] = print,
) -> int:
    try:
        arguments = tuple(sys.argv[1:]) if arguments is None else tuple(arguments)
        if len(arguments) != 2 or arguments[0] != "--observed-at":
            raise GSCRuntimeReportError("OBSERVED_AT_INVALID", 0)
        run_runtime_report(observed_at=arguments[1], transport_factory=transport_factory, emit=emit)
        return 0
    except GSCRuntimeReportError as error:
        code, calls = error.safe_code, error.calls
    except Exception:
        code, calls = "UNEXPECTED_RUNTIME_FAILURE", 0
    emit(f"SAFE_FAILURE_CODE={code}\nGSC_API_CALLS_COMPLETED={calls}\nFINAL_VERDICT={FAIL_VERDICT}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
