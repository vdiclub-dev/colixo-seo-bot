import json
import re
import socket
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.v3 import ga4_organic_coverage_diagnostic as diagnostic_module
from scripts.v3.ga4_organic_coverage_diagnostic import (
    END_DATE,
    EXPECTED_REPORT_CALLS,
    MAPPED_COMMERCIAL,
    NOT_SET,
    OBSERVED_AT,
    PASS_VERDICT,
    PROPERTY_ID,
    START_DATE,
    UNMAPPED_OR_EXCLUDED,
    GA4OrganicCoverageDiagnosticError,
    RecordingClient,
    _classify_landing_page,
    _validate_adapter_topics,
    run_diagnostic,
)
from scripts.v3.models import TrafficSignal
from scripts.v3.sources.analytics import (
    DEFAULT_GA4_LANDING_PAGE_TOPICS,
    GA4_LANDING_PAGE_DIMENSIONS,
    GA4_METRICS,
    GA4_TOTAL_DIMENSION_VALUE,
    ORGANIC_SEARCH_CHANNEL,
    GoogleAnalyticsDataSource,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/v3/ga4_organic_coverage_diagnostic.py"
WORKFLOW_PATH = (
    ROOT / ".github/workflows/seo-v3-ga4-organic-coverage-diagnostic.yml"
)
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
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def run_report(self, *, request):
        self.requests.append(request)
        return self.responses.pop(0)


class CollectSpy(GoogleAnalyticsDataSource):
    collect_calls = 0

    def collect(self):
        type(self).collect_calls += 1
        return super().collect()


def response(
    dimensions=GA4_LANDING_PAGE_DIMENSIONS,
    rows=(),
    totals=(),
    metrics=GA4_METRICS,
):
    return Response(
        dimension_headers=tuple(Header(name) for name in dimensions),
        metric_headers=tuple(Header(name) for name in metrics),
        rows=tuple(rows),
        totals=tuple(totals),
    )


def row(dimensions, metrics):
    return Row(
        dimension_values=tuple(Value(value) for value in dimensions),
        metric_values=tuple(Value(value) for value in metrics),
    )


def client_for(channel_metrics=(20, 15, 4), landing_rows=None):
    if landing_rows is None:
        landing_rows = (
            row(("/", ORGANIC_SEARCH_CHANNEL), (4, 3, 1)),
            row(("/business-plus", ORGANIC_SEARCH_CHANNEL), (2, 2, 0)),
            row(("/privacy-safe-unmapped", ORGANIC_SEARCH_CHANNEL), (5, 4, 1)),
            row(("(not set)", ORGANIC_SEARCH_CHANNEL), (1, 1, 0)),
        )
    total_metrics = (0, 0, 0) if channel_metrics is None else channel_metrics
    total_rows = (row(
        (GA4_TOTAL_DIMENSION_VALUE, GA4_TOTAL_DIMENSION_VALUE),
        total_metrics,
    ),)
    return FakeClient(
        response(rows=landing_rows, totals=total_rows),
    )


def execute(client, **kwargs):
    output = []
    result = run_diagnostic(
        client_factory=lambda: client,
        emit=output.append,
        **kwargs,
    )
    return result, output


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    CollectSpy.collect_calls = 0
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network forbidden in tests")
        ),
    )


def test_collect_runs_once_and_recording_client_forwards_exactly_one_report():
    client = client_for()
    result, _ = execute(client, data_source_class=CollectSpy)
    assert CollectSpy.collect_calls == 1
    assert result.report_calls == EXPECTED_REPORT_CALLS == 1
    assert len(client.requests) == 1


def test_requests_match_property_dates_dimensions_metrics_and_filter_exactly():
    client = client_for()
    execute(client)
    assert PROPERTY_ID == "552715460"
    assert START_DATE == END_DATE == "2026-09-03"
    assert OBSERVED_AT == "2026-09-04"
    request = client.requests[0]
    assert request["property"] == "properties/552715460"
    assert request["date_ranges"] == [{
            "start_date": "2026-09-03", "end_date": "2026-09-03",
    }]
    assert tuple(item["name"] for item in request["metrics"]) == GA4_METRICS
    assert request["dimension_filter"] == {
        "filter": {
            "field_name": "sessionDefaultChannelGroup",
            "string_filter": {
                "match_type": "EXACT",
                "value": "Organic Search",
                "case_sensitive": True,
            }
        }
    }
    assert tuple(item["name"] for item in client.requests[0]["dimensions"]) == (
        "landingPage", "sessionDefaultChannelGroup",
    )
    assert request["metric_aggregations"] == ["TOTAL"]


def test_recording_client_refuses_unexpected_request_before_network_forwarding():
    client = FakeClient()
    recording = RecordingClient(client)
    with pytest.raises(GA4OrganicCoverageDiagnosticError):
        recording.run_report(request={"property": "properties/552715460"})
    assert client.requests == []
    assert recording.requests == []
    assert recording.responses == []


def test_mapped_unmapped_and_not_set_rows_are_aggregated_separately():
    result, output = execute(client_for())
    assert result.mapped.row_count == 2
    assert result.mapped.metrics.sessions == Decimal(6)
    assert result.mapped.metrics.engaged_sessions == Decimal(5)
    assert result.mapped.metrics.key_events == Decimal(1)
    assert result.unmapped_or_excluded.row_count == 1
    assert result.unmapped_or_excluded.metrics.sessions == Decimal(5)
    assert result.not_set.row_count == 1
    assert result.not_set.metrics.sessions == Decimal(1)
    assert "MAPPED_ROW_COUNT=2" in output
    assert "UNMAPPED_OR_EXCLUDED_ROW_COUNT=1" in output
    assert "NOT_SET_ROW_COUNT=1" in output


def test_landing_page_classifier_accepts_only_three_explicit_categories():
    assert _classify_landing_page("/") == MAPPED_COMMERCIAL
    assert _classify_landing_page("/legal-safe") == UNMAPPED_OR_EXCLUDED
    assert _classify_landing_page("(not set)") == NOT_SET
    for unsafe in (
        "/safe?email=private@example.invalid",
        "/safe#fragment",
        "https://example.invalid/path",
        "/safe\x00hidden",
    ):
        with pytest.raises(GA4OrganicCoverageDiagnosticError):
            _classify_landing_page(unsafe)


def test_unknown_path_and_evidence_fact_never_appear_in_allowlisted_output():
    private_path = "/private-unknown-path"
    client = client_for(landing_rows=(
        row((private_path, ORGANIC_SEARCH_CHANNEL), (2, 1, 0)),
        row(("/", ORGANIC_SEARCH_CHANNEL), (1, 1, 0)),
    ))
    _, output = execute(client)
    rendered = "\n".join(output)
    assert private_path not in rendered
    assert "landing_pages" not in rendered
    assert "Evidence" not in rendered
    allowed = re.compile(
        r"^(PROPERTY_ID|AUTH_MODE|START_DATE|END_DATE|OBSERVED_AT|REPORT_CALLS|"
        r"ORGANIC_(?:SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)_TOTAL|"
        r"MAPPED_(?:ROW_COUNT|SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)|"
        r"UNMAPPED_OR_EXCLUDED_(?:ROW_COUNT|SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)|"
        r"NOT_SET_(?:ROW_COUNT|SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)|"
        r"RESIDUAL_(?:SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)_GAP|"
        r"MAPPED_SESSION_COVERAGE_PCT|ADAPTER_SIGNAL_COUNT|ADAPTER_TOPICS|"
        r"FINAL_VERDICT)="
    )
    assert all(allowed.match(line) for line in output)


@pytest.mark.parametrize("unsafe_path", (
    "/unknown?private=value",
    "https://example.invalid/private",
))
def test_query_string_and_full_url_fail_closed_without_output(unsafe_path):
    client = client_for(landing_rows=(
        row((unsafe_path, ORGANIC_SEARCH_CHANNEL), (1, 1, 0)),
    ))
    output = []
    with pytest.raises(GA4OrganicCoverageDiagnosticError, match="diagnostic failed"):
        run_diagnostic(client_factory=lambda: client, emit=output.append)
    assert output == []


def test_malformed_dimension_headers_or_rows_fail_closed_without_output():
    valid_total = row(
        (GA4_TOTAL_DIMENSION_VALUE, GA4_TOTAL_DIMENSION_VALUE),
        (1, 1, 0),
    )
    clients = (
        FakeClient(
            response(("wrongDimension",)),
        ),
        FakeClient(
            response(
                rows=(
                    row((ORGANIC_SEARCH_CHANNEL, "extra"), (1, 1, 0)),
                ),
                totals=(valid_total,),
            ),
        ),
    )
    for client in clients:
        output = []
        with pytest.raises(GA4OrganicCoverageDiagnosticError):
            run_diagnostic(client_factory=lambda client=client: client, emit=output.append)
        assert output == []


@pytest.mark.parametrize("bad_metrics", (
    ("1", "2"),
    ("bad", "2", "0"),
    ("-1", "2", "0"),
))
def test_malformed_or_negative_metrics_fail_closed_without_output(bad_metrics):
    bad_total = row(
        (GA4_TOTAL_DIMENSION_VALUE, GA4_TOTAL_DIMENSION_VALUE), bad_metrics
    )
    client = FakeClient(
        response(totals=(bad_total,)),
    )
    output = []
    with pytest.raises(GA4OrganicCoverageDiagnosticError):
        run_diagnostic(client_factory=lambda: client, emit=output.append)
    assert output == []


@pytest.mark.parametrize("landing_metrics", (
    (11, 1, 0),
    (1, 11, 0),
    (1, 1, 11),
))
def test_landing_breakdown_above_channel_fails_closed(landing_metrics):
    client = client_for(
        channel_metrics=(10, 10, 10),
        landing_rows=(row(("/unmapped", ORGANIC_SEARCH_CHANNEL), landing_metrics),),
    )
    with pytest.raises(GA4OrganicCoverageDiagnosticError):
        execute(client)


def test_positive_residual_gap_is_reported_without_reclassification():
    result, output = execute(client_for())
    assert result.residual.sessions == Decimal(8)
    assert result.residual.engaged_sessions == Decimal(5)
    assert result.residual.key_events == Decimal(2)
    assert "RESIDUAL_SESSIONS_GAP=8" in output
    assert "RESIDUAL_ENGAGED_SESSIONS_GAP=5" in output
    assert "RESIDUAL_KEY_EVENTS_GAP=2" in output
    assert "UNMAPPED_OR_EXCLUDED_SESSIONS=5" in output


def test_zero_traffic_is_success_and_coverage_is_na():
    result, output = execute(client_for(channel_metrics=None, landing_rows=()))
    assert result.organic_totals.sessions == 0
    assert result.mapped_session_coverage_pct == "NA"
    assert result.adapter_topics == ()
    assert "ORGANIC_SESSIONS_TOTAL=0" in output
    assert "MAPPED_SESSION_COVERAGE_PCT=NA" in output
    assert "ADAPTER_SIGNAL_COUNT=0" in output
    assert output[-1] == "FINAL_VERDICT={}".format(PASS_VERDICT)


def test_coverage_percentage_is_rounded_deterministically_to_two_decimals():
    client = client_for(
        channel_metrics=(3, 3, 0),
        landing_rows=(row(("/", ORGANIC_SEARCH_CHANNEL), (1, 1, 0)),),
    )
    result, output = execute(client)
    assert result.mapped_session_coverage_pct == "33.33"
    assert "MAPPED_SESSION_COVERAGE_PCT=33.33" in output


def test_adapter_topics_are_known_and_sorted_without_raw_landing_pages():
    client = client_for(
        channel_metrics=(5, 5, 0),
        landing_rows=(
            row((
                "/portail-client/livraison-vins-vignerons-suisse-romande",
                ORGANIC_SEARCH_CHANNEL,
            ), (2, 2, 0)),
            row(("/business-plus", ORGANIC_SEARCH_CHANNEL), (3, 3, 0)),
        ),
    )
    result, output = execute(client)
    assert result.adapter_topics == ("business_delivery", "wine_delivery")
    assert "ADAPTER_TOPICS=business_delivery,wine_delivery" in output
    rendered = "\n".join(output)
    assert "/business-plus" not in rendered
    assert "/portail-client/" not in rendered


def test_non_allowlisted_adapter_topic_fails_closed():
    signal = TrafficSignal(
        topic="unknown_private_topic",
        organic_sessions=1,
        engaged_sessions=1,
        conversions=0,
        evidence=(),
    )
    with pytest.raises(GA4OrganicCoverageDiagnosticError):
        _validate_adapter_topics((signal,))


def test_api_failure_is_sanitized_and_main_outputs_only_failure_verdict(
    monkeypatch, capsys
):
    class FailingClient:
        def run_report(self, *, request):
            raise RuntimeError("token /credential/path?email=private@example.invalid")

    output = []
    with pytest.raises(GA4OrganicCoverageDiagnosticError) as failure:
        run_diagnostic(client_factory=FailingClient, emit=output.append)
    assert output == []
    assert all(value not in str(failure.value) for value in (
        "token", "credential", "email", "private@example",
    ))

    monkeypatch.setattr(
        diagnostic_module,
        "run_diagnostic",
        lambda: (_ for _ in ()).throw(RuntimeError("secret token")),
    )
    assert diagnostic_module.main() == 1
    assert capsys.readouterr().out == (
        "FINAL_VERDICT=GA4_ORGANIC_COVERAGE_DIAGNOSTIC_FAILED\n"
    )


def test_runner_reuses_existing_mapping_and_has_one_collect_and_one_forwarder():
    source = RUNNER_PATH.read_text()
    assert "DEFAULT_GA4_LANDING_PAGE_TOPICS" in source
    assert source.count("source.collect()") == 1
    assert source.count("self.client.run_report(") == 1
    assert all(value not in source for value in (
        "landingPagePlusQueryString", "pagePathPlusQueryString", "pageLocation",
        "userPseudoId", "clientId", "transactionId",
    ))


def test_workflow_is_manual_only_with_empty_global_permissions_and_job_oidc():
    workflow = WORKFLOW_PATH.read_text()
    trigger = workflow.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    assert trigger.strip() == "workflow_dispatch:"
    assert all(item not in trigger for item in (
        "push:", "pull_request:", "schedule:", "workflow_call:",
    ))
    assert workflow.split("permissions:", 1)[1].split("\njobs:", 1)[0].strip() == "{}"
    assert workflow.count("id-token: write") == 1
    job = workflow.split("  ga4-organic-coverage-diagnostic:\n", 1)[1]
    permissions = job.split("    permissions:\n", 1)[1].split("\n\n    steps:", 1)[0]
    assert permissions.strip().splitlines() == [
        "contents: read", "      id-token: write",
    ]


def test_workflow_pins_actions_reuses_lock_and_has_no_static_key_or_cache():
    workflow = WORKFLOW_PATH.read_text()
    expected_actions = {
        "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
        "google-github-actions/auth": "7c6bc770dae815cd3e89ee6cdf493a5fab2cc093",
    }
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)
    assert set(uses) == {
        "{}@{}".format(action, sha) for action, sha in expected_actions.items()
    }
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", item) for item in uses)
    assert "persist-credentials: false" in workflow
    assert 'python-version: "3.11"' in workflow
    assert "cache: pip" not in workflow
    assert "--requirement requirements-ga4-probe.lock" in workflow
    assert "--require-hashes" in workflow
    assert "--only-binary=:all:" in workflow
    assert "requirements.txt" not in workflow
    assert "workload_identity_provider: {}".format(PROVIDER) in workflow
    assert "service_account: {}".format(SERVICE_ACCOUNT) in workflow
    assert "project_id: colixo-seo-agent" in workflow
    assert "create_credentials_file: true" in workflow
    assert "export_environment_variables: true" in workflow
    assert "secrets." not in workflow
    assert "run: python -m scripts.v3.ga4_organic_coverage_diagnostic" in workflow


def test_runtime_remains_fixture_backed_offline_and_adapter_disabled():
    config = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    assert config["phase_1_sources"]["analytics"] == "local_fixture"
    assert config["mode"]["network_enabled"] is False
    assert config["ga4_data_api"]["enabled"] is False
