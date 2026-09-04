"""Manual WIF runner for one sanitized V3 GA4_READ_ONLY runtime report."""

import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from scripts.v3.agent import AgentResult, MarketIntelligenceAgent
from scripts.v3.config import RuntimeState
from scripts.v3.sources.analytics import (
    GA4DataSourceError,
    create_google_analytics_data_client,
)


ROOT = Path(__file__).resolve().parents[2]
GA4_READONLY_CONFIG = ROOT / "config/seo_agent_v3_ga4_readonly.json"
AUTH_MODE = "WIF"
EXPECTED_REPORT_CALLS = 1
PASS_VERDICT = "V3_GA4_READONLY_RUNTIME_PASS"
FAIL_VERDICT = "V3_GA4_READONLY_RUNTIME_FAILED"

SAFE_FAILURE_CODES = (
    "OBSERVED_AT_INVALID",
    "CLIENT_CREATION_FAILED",
    "RUNTIME_CONSTRUCTION_FAILED",
    "GA4_API_REQUEST_FAILED",
    "GA4_TOTAL_ROW_COUNT_INVALID",
    "GA4_TOTAL_DIMENSIONS_INVALID",
    "GA4_RESPONSE_SCHEMA_INVALID",
    "GA4_ROW_CHANNEL_INVALID",
    "GA4_LANDING_VALUE_INVALID",
    "GA4_ROW_METRIC_INVALID",
    "GA4_MAPPED_TOTAL_EXCEEDS",
    "REPORT_COUNT_INVALID",
    "REPORT_RENDER_FAILED",
    "UNEXPECTED_RUNTIME_FAILURE",
)
_SAFE_FAILURE_CODE_SET = frozenset(SAFE_FAILURE_CODES)
_ADAPTER_FAILURE_CODES = {
    "GA4 Data API request failed": "GA4_API_REQUEST_FAILED",
    "GA4 response must contain exactly one total row": (
        "GA4_TOTAL_ROW_COUNT_INVALID"
    ),
    "GA4 total row dimensions are unexpected": "GA4_TOTAL_DIMENSIONS_INVALID",
    "GA4 response is missing": "GA4_RESPONSE_SCHEMA_INVALID",
    "GA4 response dimensions are unexpected": "GA4_RESPONSE_SCHEMA_INVALID",
    "GA4 response metrics are unexpected": "GA4_RESPONSE_SCHEMA_INVALID",
    "GA4 response contains an unexpected channel": "GA4_ROW_CHANNEL_INVALID",
    "GA4 response contains an unsafe landingPage": "GA4_LANDING_VALUE_INVALID",
    "GA4 response row dimensions are malformed": "GA4_ROW_METRIC_INVALID",
    "GA4 response row metrics are malformed": "GA4_ROW_METRIC_INVALID",
    "GA4 metric is missing or invalid": "GA4_ROW_METRIC_INVALID",
    "GA4 commercial landing totals exceed Organic Search channel totals": (
        "GA4_MAPPED_TOTAL_EXCEEDS"
    ),
}


class GA4RuntimeReadOnlyReportError(ValueError):
    """Carry only allowlisted failure metadata and never an underlying error."""

    __slots__ = ("safe_code", "report_calls_completed")

    def __init__(self, safe_code: str, report_calls_completed: int) -> None:
        if safe_code not in _SAFE_FAILURE_CODE_SET:
            safe_code = "UNEXPECTED_RUNTIME_FAILURE"
        if report_calls_completed not in (0, 1):
            safe_code = "REPORT_COUNT_INVALID"
            report_calls_completed = 1 if report_calls_completed else 0
        self.safe_code = safe_code
        self.report_calls_completed = report_calls_completed
        super().__init__(safe_code)


class CountingGA4Client:
    """Forward at most one report without recording request or response data."""

    __slots__ = ("report_calls", "api_failed")

    def __new__(cls, client: Any) -> "CountingGA4Client":
        if cls is not CountingGA4Client:
            return object.__new__(cls)

        # Keep the delegate out of instance state. The returned object's only
        # retained diagnostic fields are the call count and failure boolean.
        delegate = client.run_report

        class BoundCountingGA4Client(CountingGA4Client):
            __slots__ = ()

            def _forward(self, *, request: Any) -> Any:
                return delegate(request=request)

        return object.__new__(BoundCountingGA4Client)

    def __init__(self, client: Any) -> None:
        self.report_calls = 0
        self.api_failed = False

    def run_report(self, *, request: Any) -> Any:
        if self.report_calls >= EXPECTED_REPORT_CALLS:
            raise GA4RuntimeReadOnlyReportError("REPORT_COUNT_INVALID", 1)
        self.report_calls += 1
        try:
            return self._forward(request=request)
        except GA4RuntimeReadOnlyReportError:
            raise
        except Exception:
            self.api_failed = True
            raise

    def _forward(self, *, request: Any) -> Any:
        raise NotImplementedError


@dataclass(frozen=True)
class RuntimeExecution:
    result: AgentResult
    report_calls: int


def run_runtime_report(
    *,
    observed_at: str,
    client_factory: Callable[[], Any] = create_google_analytics_data_client,
    emit: Callable[[str], None] = print,
) -> RuntimeExecution:
    """Run the authorized agent once and emit only allowlisted safe output."""

    normalized_observed_at = _validate_observed_at(observed_at)
    counting_client: Optional[CountingGA4Client] = None

    def counting_factory() -> CountingGA4Client:
        nonlocal counting_client
        try:
            client = client_factory()
        except Exception:
            raise GA4RuntimeReadOnlyReportError(
                "CLIENT_CREATION_FAILED", 0
            ) from None
        counting_client = CountingGA4Client(client)
        return counting_client

    try:
        agent = MarketIntelligenceAgent(
            config_path=GA4_READONLY_CONFIG,
            observed_at=normalized_observed_at,
            ga4_client_factory=counting_factory,
        )
        if agent.config.runtime_state is not RuntimeState.GA4_READ_ONLY:
            raise GA4RuntimeReadOnlyReportError(
                "RUNTIME_CONSTRUCTION_FAILED", 0
            )
    except GA4RuntimeReadOnlyReportError:
        raise
    except Exception:
        raise GA4RuntimeReadOnlyReportError(
            "RUNTIME_CONSTRUCTION_FAILED", 0
        ) from None

    try:
        result = agent.run({})
    except GA4RuntimeReadOnlyReportError:
        raise
    except GA4DataSourceError as error:
        calls = _report_calls(counting_client)
        raise GA4RuntimeReadOnlyReportError(
            _adapter_failure_code(error, counting_client), calls
        ) from None
    except Exception:
        raise GA4RuntimeReadOnlyReportError(
            "UNEXPECTED_RUNTIME_FAILURE", _report_calls(counting_client)
        ) from None

    calls = _report_calls(counting_client)
    if calls != EXPECTED_REPORT_CALLS:
        raise GA4RuntimeReadOnlyReportError("REPORT_COUNT_INVALID", calls)
    if not isinstance(result.markdown, str):
        raise GA4RuntimeReadOnlyReportError("REPORT_RENDER_FAILED", calls)

    metadata = (
        "RUNTIME_STATE={}".format(agent.config.runtime_state.value),
        "PROPERTY_ID={}".format(agent.config.ga4_data_api.property_id),
        "AUTH_MODE={}".format(AUTH_MODE),
        "OBSERVED_AT={}".format(normalized_observed_at),
        "DATE_RANGE_START={}".format(agent.config.ga4_data_api.start_date),
        "DATE_RANGE_END={}".format(agent.config.ga4_data_api.end_date),
        "REPORT_CALLS={}".format(calls),
        "TOPIC_COUNT={}".format(len(result.scores)),
        "RECOMMENDATION_COUNT={}".format(len(result.recommendations)),
    )
    try:
        for line in metadata:
            emit(line)
        emit(result.markdown)
        emit("FINAL_VERDICT={}".format(PASS_VERDICT))
    except Exception:
        raise GA4RuntimeReadOnlyReportError("REPORT_RENDER_FAILED", calls) from None
    return RuntimeExecution(result=result, report_calls=calls)


def _adapter_failure_code(
    error: GA4DataSourceError,
    client: Optional[CountingGA4Client],
) -> str:
    if client is not None and client.api_failed:
        return "GA4_API_REQUEST_FAILED"
    message = str(error)
    if message == "GA4 Data API request failed":
        return "REPORT_COUNT_INVALID"
    return _ADAPTER_FAILURE_CODES.get(message, "UNEXPECTED_RUNTIME_FAILURE")


def _report_calls(client: Optional[CountingGA4Client]) -> int:
    return 0 if client is None else client.report_calls


def _validate_observed_at(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise GA4RuntimeReadOnlyReportError("OBSERVED_AT_INVALID", 0)
    try:
        date.fromisoformat(value)
    except ValueError:
        raise GA4RuntimeReadOnlyReportError("OBSERVED_AT_INVALID", 0) from None
    return value


def _parse_observed_at(arguments: Sequence[str]) -> str:
    if len(arguments) != 2 or arguments[0] != "--observed-at":
        raise GA4RuntimeReadOnlyReportError("OBSERVED_AT_INVALID", 0)
    return _validate_observed_at(arguments[1])


def _failure_output(error: GA4RuntimeReadOnlyReportError) -> tuple[str, ...]:
    return (
        "SAFE_FAILURE_CODE={}".format(error.safe_code),
        "REPORT_CALLS_COMPLETED={}".format(error.report_calls_completed),
        "FINAL_VERDICT={}".format(FAIL_VERDICT),
    )


def main(
    arguments: Optional[Sequence[str]] = None,
    *,
    client_factory: Callable[[], Any] = create_google_analytics_data_client,
    emit: Callable[[str], None] = print,
) -> int:
    try:
        observed_at = _parse_observed_at(
            tuple(sys.argv[1:]) if arguments is None else tuple(arguments)
        )
        run_runtime_report(
            observed_at=observed_at,
            client_factory=client_factory,
            emit=emit,
        )
    except GA4RuntimeReadOnlyReportError as error:
        for line in _failure_output(error):
            emit(line)
        return 1
    except Exception:
        error = GA4RuntimeReadOnlyReportError("UNEXPECTED_RUNTIME_FAILURE", 0)
        for line in _failure_output(error):
            emit(line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
