"""Manual WIF runner for one sanitized V3 GA4_READ_ONLY runtime report."""

import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from scripts.v3.agent import AgentResult, MarketIntelligenceAgent
from scripts.v3.config import RuntimeState
from scripts.v3.sources.analytics import create_google_analytics_data_client


ROOT = Path(__file__).resolve().parents[2]
GA4_READONLY_CONFIG = ROOT / "config/seo_agent_v3_ga4_readonly.json"
AUTH_MODE = "WIF"
EXPECTED_REPORT_CALLS = 1
PASS_VERDICT = "V3_GA4_READONLY_RUNTIME_PASS"
FAIL_VERDICT = "V3_GA4_READONLY_RUNTIME_FAILED"


class GA4RuntimeReadOnlyReportError(ValueError):
    """Raised when the manual runtime cannot produce a trusted safe report."""


class CountingGA4Client:
    """Forward at most one report without recording request or response data."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.report_calls = 0

    def run_report(self, *, request: Any) -> Any:
        if self.report_calls >= EXPECTED_REPORT_CALLS:
            raise GA4RuntimeReadOnlyReportError("GA4 report call limit exceeded")
        self.report_calls += 1
        return self._client.run_report(request=request)


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

    try:
        normalized_observed_at = _validate_observed_at(observed_at)
        counting_client: Optional[CountingGA4Client] = None

        def counting_factory() -> CountingGA4Client:
            nonlocal counting_client
            counting_client = CountingGA4Client(client_factory())
            return counting_client

        agent = MarketIntelligenceAgent(
            config_path=GA4_READONLY_CONFIG,
            observed_at=normalized_observed_at,
            ga4_client_factory=counting_factory,
        )
        if agent.config.runtime_state is not RuntimeState.GA4_READ_ONLY:
            raise GA4RuntimeReadOnlyReportError("runtime state is not authorized")

        result = agent.run({})
        if counting_client is None or counting_client.report_calls != EXPECTED_REPORT_CALLS:
            raise GA4RuntimeReadOnlyReportError("GA4 report call count is invalid")
    except Exception:
        raise GA4RuntimeReadOnlyReportError(
            "V3 GA4 read-only runtime failed"
        ) from None

    metadata = (
        "RUNTIME_STATE={}".format(agent.config.runtime_state.value),
        "PROPERTY_ID={}".format(agent.config.ga4_data_api.property_id),
        "AUTH_MODE={}".format(AUTH_MODE),
        "OBSERVED_AT={}".format(normalized_observed_at),
        "DATE_RANGE_START={}".format(agent.config.ga4_data_api.start_date),
        "DATE_RANGE_END={}".format(agent.config.ga4_data_api.end_date),
        "REPORT_CALLS={}".format(counting_client.report_calls),
        "TOPIC_COUNT={}".format(len(result.scores)),
        "RECOMMENDATION_COUNT={}".format(len(result.recommendations)),
    )
    for line in metadata:
        emit(line)
    emit(result.markdown)
    emit("FINAL_VERDICT={}".format(PASS_VERDICT))
    return RuntimeExecution(result=result, report_calls=counting_client.report_calls)


def _validate_observed_at(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise GA4RuntimeReadOnlyReportError("observed_at must be a real ISO date")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise GA4RuntimeReadOnlyReportError(
            "observed_at must be a real ISO date"
        ) from None
    return value


def _parse_observed_at(arguments: Sequence[str]) -> str:
    if len(arguments) != 2 or arguments[0] != "--observed-at":
        raise GA4RuntimeReadOnlyReportError("explicit observed_at is required")
    return _validate_observed_at(arguments[1])


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
    except Exception:
        emit("FINAL_VERDICT={}".format(FAIL_VERDICT))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
