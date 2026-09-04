import json
import re
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.v3.ga4_runtime_readonly_report import (
    FAIL_VERDICT,
    GA4RuntimeReadOnlyReportError,
    PASS_VERDICT,
    CountingGA4Client,
    main,
    run_runtime_report,
)
from scripts.v3.models import DimensionLevel
from scripts.v3.sources.analytics import (
    GA4_LANDING_PAGE_DIMENSIONS,
    GA4_METRICS,
    GA4_TOTAL_DIMENSION_VALUE,
    ORGANIC_SEARCH_CHANNEL,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/v3/ga4_runtime_readonly_report.py"
WORKFLOW_PATH = (
    ROOT / ".github/workflows/seo-v3-ga4-runtime-readonly-report.yml"
)
DEFAULT_CONFIG_PATH = ROOT / "config/seo_agent_v3.json"
PROVIDER = (
    "projects/270376484474/locations/global/workloadIdentityPools/"
    "github/providers/colixo-seo-bot"
)
SERVICE_ACCOUNT = "colixo-seo-ga4-reader@colixo-seo-agent.iam.gserviceaccount.com"


@dataclass(frozen=True)
class Header:
    name: str


@dataclass(frozen=True)
class Value:
    value: object


@dataclass(frozen=True)
class Row:
    dimension_values: tuple
    metric_values: tuple


@dataclass(frozen=True)
class Response:
    dimension_headers: tuple
    metric_headers: tuple
    rows: tuple
    totals: tuple


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def run_report(self, *, request):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def row(dimensions, metrics):
    return Row(
        dimension_values=tuple(Value(value) for value in dimensions),
        metric_values=tuple(Value(value) for value in metrics),
    )


def response(rows=(), totals=("0", "0", "0")):
    return Response(
        dimension_headers=tuple(Header(name) for name in GA4_LANDING_PAGE_DIMENSIONS),
        metric_headers=tuple(Header(name) for name in GA4_METRICS),
        rows=tuple(rows),
        totals=(row(
            (GA4_TOTAL_DIMENSION_VALUE, GA4_TOTAL_DIMENSION_VALUE),
            totals,
        ),),
    )


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network is forbidden in runner tests")
        ),
    )


def test_explicit_observed_at_is_required_before_client_creation():
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return FakeClient(response())

    output = []
    assert main((), client_factory=factory, emit=output.append) == 1
    assert output == ["FINAL_VERDICT={}".format(FAIL_VERDICT)]
    assert factory_calls == []


@pytest.mark.parametrize(
    "observed_at",
    ("", "yesterday", "today", "28daysAgo", "2026-99-99", None),
)
def test_invalid_observed_at_fails_before_client_creation(observed_at):
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return FakeClient(response())

    with pytest.raises(GA4RuntimeReadOnlyReportError):
        run_runtime_report(
            observed_at=observed_at,
            client_factory=factory,
            emit=lambda _line: None,
        )
    assert factory_calls == []


def test_zero_ga4_topics_is_a_successful_single_call_runtime_report():
    client = FakeClient(response())
    output = []

    execution = run_runtime_report(
        observed_at="2026-09-04",
        client_factory=lambda: client,
        emit=output.append,
    )

    assert len(client.requests) == execution.report_calls == 1
    assert execution.result.scores == ()
    assert execution.result.recommendations == ()
    assert output[:9] == [
        "RUNTIME_STATE=GA4_READ_ONLY",
        "PROPERTY_ID=552715460",
        "AUTH_MODE=WIF",
        "OBSERVED_AT=2026-09-04",
        "DATE_RANGE_START=28daysAgo",
        "DATE_RANGE_END=yesterday",
        "REPORT_CALLS=1",
        "TOPIC_COUNT=0",
        "RECOMMENDATION_COUNT=0",
    ]
    assert output[-1] == "FINAL_VERDICT={}".format(PASS_VERDICT)
    assert execution.result.markdown in output
    assert "0 opportunity topic(s) evaluated" in execution.result.markdown
    assert "No observed data" in execution.result.markdown
    assert "analytics=ga4_data_api(read-only)" in execution.result.markdown


def test_mapped_ga4_topic_preserves_non_conversion_semantics_and_provenance():
    client = FakeClient(response(
        rows=(row(("/", ORGANIC_SEARCH_CHANNEL), ("6", "3", "2")),),
        totals=("6", "3", "2"),
    ))
    output = []

    execution = run_runtime_report(
        observed_at="2026-09-04",
        client_factory=lambda: client,
        emit=output.append,
    )

    assert len(client.requests) == 1
    assert len(execution.result.scores) == 1
    score = execution.result.scores[0]
    assert score.topic == "general_delivery"
    assert score.conversion_signal is DimensionLevel.UNKNOWN
    assert score.final_score == 0
    assert execution.result.recommendations[0].strength == "weak"
    evidence = execution.result.recommendations[0].evidence[0]
    assert evidence.fact["organic_sessions"] == 6.0
    assert evidence.fact["engaged_sessions"] == 3.0
    assert evidence.fact["key_events"] == 2.0
    assert "key_events=2" not in "\n".join(output)
    assert "analytics=ga4_data_api(read-only)" in execution.result.markdown
    assert "search_console=local_fixture" in execution.result.markdown


def test_unknown_landing_paths_and_raw_evidence_are_not_written_to_output():
    private_path = "/unmapped-internal-area"
    client = FakeClient(response(
        rows=(row((private_path, ORGANIC_SEARCH_CHANNEL), ("4", "2", "0")),),
        totals=("4", "2", "0"),
    ))
    output = []

    execution = run_runtime_report(
        observed_at="2026-09-04",
        client_factory=lambda: client,
        emit=output.append,
    )
    rendered = "\n".join(output)

    assert execution.result.scores == ()
    assert private_path not in rendered
    assert "Evidence(" not in rendered
    assert "landing_pages" not in rendered
    assert "dimension_values" not in rendered
    assert "metric_values" not in rendered


def test_underlying_api_exception_is_sanitized_and_never_retried():
    sensitive = "token credential /private/path?email=secret@example.invalid"
    client = FakeClient(RuntimeError(sensitive))
    output = []

    assert main(
        ("--observed-at", "2026-09-04"),
        client_factory=lambda: client,
        emit=output.append,
    ) == 1
    assert len(client.requests) == 1
    assert output == ["FINAL_VERDICT={}".format(FAIL_VERDICT)]
    assert sensitive not in "\n".join(output)


def test_counting_client_blocks_a_second_call_without_forwarding_it():
    client = FakeClient(response())
    counted = CountingGA4Client(client)

    counted.run_report(request={"safe": True})
    with pytest.raises(GA4RuntimeReadOnlyReportError, match="limit"):
        counted.run_report(request={"safe": True})
    assert counted.report_calls == 1
    assert len(client.requests) == 1
    assert set(vars(counted)) == {"_client", "report_calls"}


def test_runner_has_no_fixture_fallback_parallel_scoring_or_implicit_clock():
    source = RUNNER_PATH.read_text(encoding="utf-8")

    assert "agent.run({})" in source
    assert "result.markdown" in source
    assert "datetime.now" not in source
    assert "date.today" not in source
    assert "AnalyticsFixtureSource" not in source
    assert "score_opportunity" not in source
    assert "render_markdown" not in source
    assert "print(error" not in source
    assert "print(exception" not in source


def test_workflow_is_manual_only_with_exact_read_only_oidc_permissions():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert len(re.findall(r"(?m)^  workflow_dispatch:$", workflow)) == 1
    for forbidden_trigger in ("push:", "pull_request:", "schedule:", "workflow_call:"):
        assert forbidden_trigger not in workflow
    assert re.search(r"(?m)^permissions: \{\}$", workflow)
    assert re.search(
        r"(?ms)^    permissions:\n      contents: read\n      id-token: write$",
        workflow,
    )
    assert "issues: write" not in workflow
    assert "contents: write" not in workflow


def test_workflow_uses_exact_pins_wif_identity_and_hash_locked_install():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093" in workflow
    assert "persist-credentials: false" in workflow
    assert 'python-version: "3.11"' in workflow
    assert "--disable-pip-version-check" in workflow
    assert "--require-hashes" in workflow
    assert "--only-binary=:all:" in workflow
    assert "--requirement requirements-ga4-probe.lock" in workflow
    assert PROVIDER in workflow
    assert SERVICE_ACCOUNT in workflow
    assert "project_id: colixo-seo-agent" in workflow
    assert "create_credentials_file: true" in workflow
    assert "export_environment_variables: true" in workflow


def test_workflow_passes_explicit_date_to_runner_and_summary_only():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "set -o pipefail" in workflow
    assert 'OBSERVED_AT="$(TZ=Europe/Zurich date +%F)"' in workflow
    assert "python -m scripts.v3.ga4_runtime_readonly_report" in workflow
    assert '--observed-at "$OBSERVED_AT"' in workflow
    assert 'tee -a "$GITHUB_STEP_SUMMARY"' in workflow
    assert "upload-artifact" not in workflow


def test_workflow_has_no_static_key_secret_deployment_or_publication_path():
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    for prohibited in (
        "secrets.",
        "service_account_key",
        "credentials_json",
        "private_key",
        "deploy",
        "wrangler",
        "push_to_site_repo",
        "git push",
    ):
        assert prohibited not in workflow.lower()


def test_default_config_remains_exactly_offline():
    payload = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))

    assert payload["mode"]["network_enabled"] is False
    assert payload["phase_1_sources"]["analytics"] == "local_fixture"
    assert payload["network_policy"]["default"] == "deny"
    assert payload["network_policy"]["sources"]["analytics"] == "deny"
    assert payload["ga4_data_api"]["enabled"] is False
