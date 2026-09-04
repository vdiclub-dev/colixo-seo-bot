import json
import re
import socket
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.v3 import ga4_all_channel_traffic_diagnostic as diagnostic_module
from scripts.v3.ga4_all_channel_traffic_diagnostic import (
    ALLOWED_DEFAULT_CHANNELS,
    AUTH_MODE,
    END_DATE,
    EXPECTED_REPORT_CALLS,
    OBSERVED_AT,
    PASS_VERDICT,
    PROPERTY_ID,
    START_DATE,
    GA4AllChannelTrafficDiagnosticError,
    RecordingClient,
    run_diagnostic,
)
from scripts.v3.sources.analytics import GA4_CHANNEL_DIMENSIONS, GA4_METRICS


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/v3/ga4_all_channel_traffic_diagnostic.py"
WORKFLOW_PATH = (
    ROOT / ".github/workflows/seo-v3-ga4-all-channel-traffic-diagnostic.yml"
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


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def run_report(self, *, request):
        self.requests.append(request)
        return self.responses.pop(0)


def response(dimensions, rows=(), metrics=GA4_METRICS):
    return Response(
        dimension_headers=tuple(Header(name) for name in dimensions),
        metric_headers=tuple(Header(name) for name in metrics),
        rows=tuple(rows),
    )


def row(dimensions, metrics):
    return Row(
        dimension_values=tuple(Value(value) for value in dimensions),
        metric_values=tuple(Value(value) for value in metrics),
    )


def client_for(totals=(12, 9, 3), channel_rows=None):
    if channel_rows is None:
        channel_rows = (
            row(("Organic Search",), (2, 2, 1)),
            row(("Direct",), (7, 5, 1)),
        )
    global_rows = () if totals is None else (row((), totals),)
    return FakeClient(
        response((), global_rows),
        response(GA4_CHANNEL_DIMENSIONS, channel_rows),
    )


def execute(client):
    output = []
    result = run_diagnostic(client_factory=lambda: client, emit=output.append)
    return result, output


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network forbidden in tests")
        ),
    )


def test_exactly_two_reports_use_fixed_property_dates_metrics_and_no_filter():
    client = client_for()
    result, _ = execute(client)
    assert result.report_calls == EXPECTED_REPORT_CALLS == 2
    assert PROPERTY_ID == "552715460"
    assert START_DATE == END_DATE == "2026-09-03"
    assert OBSERVED_AT == "2026-09-04"
    assert len(client.requests) == 2
    for request in client.requests:
        assert request["property"] == "properties/552715460"
        assert request["date_ranges"] == [{
            "start_date": "2026-09-03", "end_date": "2026-09-03",
        }]
        assert tuple(item["name"] for item in request["metrics"]) == (
            "sessions", "engagedSessions", "keyEvents",
        )
        assert "dimension_filter" not in request
    assert "dimensions" not in client.requests[0]
    assert client.requests[1]["dimensions"] == [{
        "name": "sessionDefaultChannelGroup",
    }]


def test_recording_client_refuses_unexpected_or_third_request_before_forwarding():
    underlying = FakeClient()
    recording = RecordingClient(underlying)
    with pytest.raises(GA4AllChannelTrafficDiagnosticError):
        recording.run_report(request={"property": "properties/552715460"})
    assert underlying.requests == []
    assert recording.requests == []


def test_channels_are_sorted_deterministically_with_exact_aggregate_output():
    client = client_for(channel_rows=(
        row(("Organic Search",), (2, 2, 1)),
        row(("AI Assistant",), (1, 1, 0)),
        row(("Direct",), (7, 5, 1)),
    ))
    result, output = execute(client)
    assert tuple(item.name for item in result.channels) == (
        "AI Assistant", "Direct", "Organic Search",
    )
    assert output[10:22] == [
        "CHANNEL_1_NAME=AI Assistant",
        "CHANNEL_1_SESSIONS=1",
        "CHANNEL_1_ENGAGED_SESSIONS=1",
        "CHANNEL_1_KEY_EVENTS=0",
        "CHANNEL_2_NAME=Direct",
        "CHANNEL_2_SESSIONS=7",
        "CHANNEL_2_ENGAGED_SESSIONS=5",
        "CHANNEL_2_KEY_EVENTS=1",
        "CHANNEL_3_NAME=Organic Search",
        "CHANNEL_3_SESSIONS=2",
        "CHANNEL_3_ENGAGED_SESSIONS=2",
        "CHANNEL_3_KEY_EVENTS=1",
    ]


def test_zero_total_traffic_is_a_successful_diagnostic():
    result, output = execute(client_for(totals=None, channel_rows=()))
    assert result.totals.sessions == 0
    assert result.channels == ()
    assert "TOTAL_SESSIONS=0" in output
    assert "CHANNEL_ROW_COUNT=0" in output
    assert "ORGANIC_SEARCH_SESSIONS=0" in output
    assert output[-1] == "FINAL_VERDICT={}".format(PASS_VERDICT)


def test_organic_search_can_be_zero_while_direct_traffic_exists():
    result, output = execute(client_for(
        totals=(5, 4, 1),
        channel_rows=(row(("Direct",), (5, 4, 1)),),
    ))
    assert result.totals.sessions == 5
    assert result.organic_search.sessions == 0
    assert "CHANNEL_1_NAME=Direct" in output
    assert "ORGANIC_SEARCH_SESSIONS=0" in output


def test_positive_organic_search_metrics_are_reported():
    result, output = execute(client_for(
        totals=(4, 3, 2),
        channel_rows=(row(("Organic Search",), (4, 3, 2)),),
    ))
    assert result.organic_search.sessions == 4
    assert result.organic_search.engaged_sessions == 3
    assert result.organic_search.key_events == 2
    assert "ORGANIC_SEARCH_SESSIONS=4" in output
    assert "ORGANIC_SEARCH_ENGAGED_SESSIONS=3" in output
    assert "ORGANIC_SEARCH_KEY_EVENTS=2" in output


def test_channel_sums_below_global_totals_report_positive_residuals():
    result, output = execute(client_for())
    assert result.channel_sums.sessions == Decimal(9)
    assert result.channel_sums.engaged_sessions == Decimal(7)
    assert result.channel_sums.key_events == Decimal(2)
    assert result.residual.sessions == Decimal(3)
    assert result.residual.engaged_sessions == Decimal(2)
    assert result.residual.key_events == Decimal(1)
    assert "RESIDUAL_SESSIONS_GAP=3" in output
    assert "RESIDUAL_ENGAGED_SESSIONS_GAP=2" in output
    assert "RESIDUAL_KEY_EVENTS_GAP=1" in output


@pytest.mark.parametrize("channel_metrics", (
    (11, 1, 0),
    (1, 11, 0),
    (1, 1, 11),
))
def test_channel_sums_above_global_totals_fail_closed(channel_metrics):
    client = client_for(
        totals=(10, 10, 10),
        channel_rows=(row(("Direct",), channel_metrics),),
    )
    output = []
    with pytest.raises(GA4AllChannelTrafficDiagnosticError):
        run_diagnostic(client_factory=lambda: client, emit=output.append)
    assert output == []


@pytest.mark.parametrize("invalid_channel", (
    "",
    "A" * 200,
    "Direct\nInjected",
    "https://example.invalid/path",
    "Direct?campaign=private",
    "private@example.invalid",
    "Some New Free Text",
    "(other)",
))
def test_invalid_url_email_control_or_free_text_channel_fails_closed(
    invalid_channel,
):
    client = client_for(
        totals=(1, 1, 0),
        channel_rows=(row((invalid_channel,), (1, 1, 0)),),
    )
    output = []
    with pytest.raises(GA4AllChannelTrafficDiagnosticError):
        run_diagnostic(client_factory=lambda: client, emit=output.append)
    assert output == []


def test_all_official_allowlisted_channel_names_are_output_safe():
    assert "Organic Search" in ALLOWED_DEFAULT_CHANNELS
    assert "Direct" in ALLOWED_DEFAULT_CHANNELS
    assert "Unassigned" in ALLOWED_DEFAULT_CHANNELS
    assert "AI Assistant" in ALLOWED_DEFAULT_CHANNELS
    assert all(
        channel
        and len(channel) <= 40
        and not re.search(r"[\x00-\x1f\x7f]", channel)
        and "://" not in channel
        and "?" not in channel
        and "@" not in channel
        for channel in ALLOWED_DEFAULT_CHANNELS
    )


def test_duplicate_channel_rows_fail_closed():
    client = client_for(
        totals=(2, 2, 0),
        channel_rows=(
            row(("Direct",), (1, 1, 0)),
            row(("Direct",), (1, 1, 0)),
        ),
    )
    with pytest.raises(GA4AllChannelTrafficDiagnosticError):
        execute(client)


def test_malformed_headers_rows_metrics_and_negative_values_fail_closed():
    clients = (
        FakeClient(
            response(("unexpectedDimension",), (row((), (1, 1, 0)),)),
            response(GA4_CHANNEL_DIMENSIONS),
        ),
        FakeClient(
            response((), (row(("unexpected",), (1, 1, 0)),)),
            response(GA4_CHANNEL_DIMENSIONS),
        ),
        FakeClient(
            response((), (row((), (1, 1)),)),
            response(GA4_CHANNEL_DIMENSIONS),
        ),
        FakeClient(
            response((), (row((), (-1, 1, 0)),)),
            response(GA4_CHANNEL_DIMENSIONS),
        ),
    )
    for client in clients:
        output = []
        with pytest.raises(GA4AllChannelTrafficDiagnosticError):
            run_diagnostic(client_factory=lambda client=client: client, emit=output.append)
        assert output == []


def test_output_uses_only_allowlisted_aggregate_keys_and_no_forbidden_dimensions():
    _, output = execute(client_for())
    allowed = re.compile(
        r"^(PROPERTY_ID|AUTH_MODE|START_DATE|END_DATE|OBSERVED_AT|REPORT_CALLS|"
        r"TOTAL_(?:SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)|CHANNEL_ROW_COUNT|"
        r"CHANNEL_\d+_(?:NAME|SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)|"
        r"CHANNEL_(?:SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)_SUM|"
        r"RESIDUAL_(?:SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)_GAP|"
        r"ORGANIC_SEARCH_(?:SESSIONS|ENGAGED_SESSIONS|KEY_EVENTS)|"
        r"FINAL_VERDICT)="
    )
    assert all(allowed.match(line) for line in output)
    rendered = "\n".join(output).lower()
    assert all(value not in rendered for value in (
        "source=", "medium=", "campaign=", "landingpage", "pagepath",
        "credential", "token", "authorization", "clientid", "userid",
    ))


def test_api_failure_is_sanitized_and_main_emits_only_failure_verdict(
    monkeypatch, capsys
):
    class FailingClient:
        def run_report(self, *, request):
            raise RuntimeError("token /credential/path?email=private@example.invalid")

    output = []
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
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
        "FINAL_VERDICT=GA4_ALL_CHANNEL_TRAFFIC_DIAGNOSTIC_FAILED\n"
    )


def test_runner_has_two_forward_calls_and_no_disallowed_data_dimensions():
    source = RUNNER_PATH.read_text()
    assert source.count("\n        client.run_report(request=") == 2
    assert source.count("self.client.run_report(") == 1
    assert "request_index >= EXPECTED_REPORT_CALLS" in source
    assert "dimension_filter" not in source
    assert all(value not in source for value in (
        "landingPage", "pagePath", "pageLocation", "queryString",
        "userPseudoId", "clientId", "transactionId", "country", "device",
    ))
    assert AUTH_MODE == "WIF"


def test_workflow_is_manual_only_with_empty_global_permissions_and_job_oidc():
    workflow = WORKFLOW_PATH.read_text()
    trigger = workflow.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    assert trigger.strip() == "workflow_dispatch:"
    assert all(item not in trigger for item in (
        "push:", "pull_request:", "schedule:", "workflow_call:",
    ))
    assert workflow.split("permissions:", 1)[1].split("\njobs:", 1)[0].strip() == "{}"
    assert workflow.count("id-token: write") == 1
    job = workflow.split("  ga4-all-channel-traffic-diagnostic:\n", 1)[1]
    permissions = job.split("    permissions:\n", 1)[1].split("\n\n    steps:", 1)[0]
    assert permissions.strip().splitlines() == [
        "contents: read", "      id-token: write",
    ]


def test_workflow_pins_actions_reuses_hash_lock_and_has_no_static_key_or_cache():
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
    assert "run: python -m scripts.v3.ga4_all_channel_traffic_diagnostic" in workflow


def test_runtime_remains_fixture_backed_offline_and_adapter_disabled():
    config = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    assert config["phase_1_sources"]["analytics"] == "local_fixture"
    assert config["mode"]["network_enabled"] is False
    assert config["ga4_data_api"]["enabled"] is False
