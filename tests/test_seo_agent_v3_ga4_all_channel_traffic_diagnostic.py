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
    ALLOWED_FAILURE_CODES,
    AUTH_MODE,
    END_DATE,
    EXPECTED_REPORT_CALLS,
    METRIC_AGGREGATION,
    OBSERVED_AT,
    PASS_VERDICT,
    PROPERTY_ID,
    RESERVED_TOTAL,
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
    totals: tuple


class FakeClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def run_report(self, *, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def row(dimensions, metrics):
    return Row(
        dimension_values=tuple(Value(value) for value in dimensions),
        metric_values=tuple(Value(value) for value in metrics),
    )


def total_row(metrics, dimensions=(RESERVED_TOTAL,)):
    return row(dimensions, metrics)


def response(
    rows=(),
    totals=(total_row((12, 9, 3)),),
    dimensions=GA4_CHANNEL_DIMENSIONS,
    metrics=GA4_METRICS,
):
    return Response(
        dimension_headers=tuple(Header(name) for name in dimensions),
        metric_headers=tuple(Header(name) for name in metrics),
        rows=tuple(rows),
        totals=tuple(totals),
    )


def client_for(totals=(12, 9, 3), channel_rows=None):
    if channel_rows is None:
        channel_rows = (
            row(("Organic Search",), (2, 2, 1)),
            row(("Direct",), (7, 5, 1)),
        )
    total_rows = () if totals is None else (total_row(totals),)
    return FakeClient(response(rows=channel_rows, totals=total_rows))


def execute(client):
    output = []
    result = run_diagnostic(client_factory=lambda: client, emit=output.append)
    return result, output


def render_failure(monkeypatch, capsys, client_factory):
    original_run_diagnostic = diagnostic_module.run_diagnostic

    def invoke():
        return original_run_diagnostic(client_factory=client_factory)

    monkeypatch.setattr(diagnostic_module, "run_diagnostic", invoke)
    assert diagnostic_module.main() == 1
    return capsys.readouterr().out.strip().splitlines()


def assert_failure_output(lines, expected_code, expected_calls):
    assert expected_calls in (0, 1)
    assert lines == [
        "PROPERTY_ID=552715460",
        "AUTH_MODE=WIF",
        "START_DATE=2026-09-03",
        "END_DATE=2026-09-03",
        "SAFE_FAILURE_CODE={}".format(expected_code),
        "REPORT_CALLS_COMPLETED={}".format(expected_calls),
        "FINAL_VERDICT=GA4_ALL_CHANNEL_TRAFFIC_DIAGNOSTIC_FAILED",
    ]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network forbidden in tests")
        ),
    )


def test_exactly_one_report_uses_fixed_contract_and_total_aggregation():
    client = client_for()
    result, _ = execute(client)
    assert result.report_calls == EXPECTED_REPORT_CALLS == 1
    assert PROPERTY_ID == "552715460"
    assert START_DATE == END_DATE == "2026-09-03"
    assert OBSERVED_AT == "2026-09-04"
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request == {
        "property": "properties/552715460",
        "date_ranges": [{
            "start_date": "2026-09-03", "end_date": "2026-09-03",
        }],
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "metrics": [
            {"name": "sessions"},
            {"name": "engagedSessions"},
            {"name": "keyEvents"},
        ],
        "metric_aggregations": ["TOTAL"],
    }
    assert METRIC_AGGREGATION == "TOTAL"
    assert "dimension_filter" not in request
    assert "metric_filter" not in request


def test_recording_client_refuses_unexpected_or_second_request_before_forwarding():
    underlying = FakeClient(response(), response())
    recording = RecordingClient(underlying)
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
        recording.run_report(request={"property": "properties/552715460"})
    assert failure.value.safe_code == "REPORT_COUNT_FAILED"
    assert underlying.requests == []

    first = diagnostic_module._build_request()
    recording.run_report(request=first)
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as second:
        recording.run_report(request=first)
    assert second.value.safe_code == "REPORT_COUNT_FAILED"
    assert second.value.report_calls_completed == 1
    assert len(underlying.requests) == 1


def test_same_response_reserved_total_is_authoritative_and_not_a_channel():
    result, output = execute(client_for(totals=(12, 9, 3)))
    assert result.totals.sessions == 12
    assert result.totals.engaged_sessions == 9
    assert result.totals.key_events == 3
    assert all(channel.name != RESERVED_TOTAL for channel in result.channels)
    assert "TOTAL_SESSIONS=12" in output
    assert "CHANNEL_ROW_COUNT=2" in output
    assert all(RESERVED_TOTAL not in line for line in output)


def test_channels_are_sorted_deterministically_with_exact_aggregate_output():
    client = client_for(
        totals=(12, 9, 3),
        channel_rows=(
            row(("Organic Search",), (2, 2, 1)),
            row(("AI Assistant",), (1, 1, 0)),
            row(("Direct",), (7, 5, 1)),
        ),
    )
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
    result, output = execute(client_for(totals=(0, 0, 0), channel_rows=()))
    assert result.totals.sessions == 0
    assert result.channels == ()
    assert "REPORT_CALLS=1" in output
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


def test_equal_channel_sums_have_zero_residuals():
    result, output = execute(client_for(
        totals=(5, 4, 1),
        channel_rows=(row(("Direct",), (5, 4, 1)),),
    ))
    assert result.residual == diagnostic_module.Metrics()
    assert "RESIDUAL_SESSIONS_GAP=0" in output
    assert "RESIDUAL_ENGAGED_SESSIONS_GAP=0" in output
    assert "RESIDUAL_KEY_EVENTS_GAP=0" in output


def test_channel_sums_below_same_response_totals_report_positive_residuals():
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
def test_channel_sums_above_same_response_total_fail_closed(channel_metrics):
    client = client_for(
        totals=(10, 10, 10),
        channel_rows=(row(("Direct",), channel_metrics),),
    )
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
        execute(client)
    assert failure.value.safe_code == "CHANNEL_SUM_EXCEEDS_TOTAL"
    assert failure.value.report_calls_completed == 1
    assert len(client.requests) == 1


def test_missing_total_fails_closed():
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
        execute(client_for(totals=None, channel_rows=()))
    assert failure.value.safe_code == "TOTAL_RESPONSE_MISSING"
    assert failure.value.report_calls_completed == 1


def test_multiple_total_rows_fail_closed():
    client = FakeClient(response(
        totals=(total_row((1, 1, 0)), total_row((1, 1, 0))),
    ))
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
        execute(client)
    assert failure.value.safe_code == "TOTAL_RESPONSE_SCHEMA_FAILED"


@pytest.mark.parametrize("dimensions", ((), ("TOTAL",), ("Direct",)))
def test_total_row_requires_documented_reserved_total_dimension(dimensions):
    client = FakeClient(response(totals=(total_row((1, 1, 0), dimensions),)))
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
        execute(client)
    assert failure.value.safe_code == "TOTAL_RESPONSE_SCHEMA_FAILED"


@pytest.mark.parametrize("metrics", (
    (1, 1),
    ("not-a-metric", 1, 0),
    (-1, 1, 0),
    (True, 1, 0),
))
def test_malformed_total_metric_fails_closed(metrics):
    client = FakeClient(response(totals=(total_row(metrics),)))
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
        execute(client)
    assert failure.value.safe_code == "TOTAL_METRIC_FAILED"


@pytest.mark.parametrize("invalid_channel", (
    "",
    "A" * 200,
    "Direct\nInjected",
    "https://example.invalid/path",
    "Direct?campaign=private",
    "private@example.invalid",
    "Some New Free Text",
    "(other)",
    RESERVED_TOTAL,
))
def test_invalid_or_reserved_channel_row_fails_closed_without_output(
    invalid_channel,
):
    client = client_for(
        totals=(1, 1, 0),
        channel_rows=(row((invalid_channel,), (1, 1, 0)),),
    )
    output = []
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
        run_diagnostic(client_factory=lambda: client, emit=output.append)
    expected_code = (
        "CHANNEL_RESPONSE_ROW_FAILED"
        if invalid_channel == ""
        else "CHANNEL_VALUE_NOT_ALLOWLISTED"
    )
    assert failure.value.safe_code == expected_code
    assert output == []


def test_duplicate_channel_rows_fail_closed():
    client = client_for(
        totals=(2, 2, 0),
        channel_rows=(
            row(("Direct",), (1, 1, 0)),
            row(("Direct",), (1, 1, 0)),
        ),
    )
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
        execute(client)
    assert failure.value.safe_code == "CHANNEL_VALUE_DUPLICATE"


@pytest.mark.parametrize("metrics", (
    (1, 1),
    ("invalid", 1, 0),
    (-1, 1, 0),
))
def test_invalid_channel_metric_fails_closed(metrics):
    client = client_for(
        totals=(1, 1, 0),
        channel_rows=(row(("Direct",), metrics),),
    )
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as failure:
        execute(client)
    assert failure.value.safe_code == "CHANNEL_METRIC_FAILED"


def test_channel_headers_and_rows_fail_with_distinct_safe_codes():
    bad_headers = FakeClient(response(dimensions=("unexpected",)))
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as schema:
        execute(bad_headers)
    assert schema.value.safe_code == "CHANNEL_RESPONSE_SCHEMA_FAILED"

    bad_row = client_for(
        totals=(1, 1, 0),
        channel_rows=(row((), (1, 1, 0)),),
    )
    with pytest.raises(GA4AllChannelTrafficDiagnosticError) as malformed:
        execute(bad_row)
    assert malformed.value.safe_code == "CHANNEL_RESPONSE_ROW_FAILED"


def test_safe_failure_code_allowlist_is_exact_for_single_report_model():
    assert ALLOWED_FAILURE_CODES == frozenset({
        "CLIENT_CREATION_FAILED",
        "CHANNEL_API_REQUEST_FAILED",
        "CHANNEL_RESPONSE_SCHEMA_FAILED",
        "CHANNEL_RESPONSE_ROW_FAILED",
        "CHANNEL_VALUE_NOT_ALLOWLISTED",
        "CHANNEL_VALUE_DUPLICATE",
        "CHANNEL_METRIC_FAILED",
        "TOTAL_RESPONSE_MISSING",
        "TOTAL_RESPONSE_SCHEMA_FAILED",
        "TOTAL_METRIC_FAILED",
        "CHANNEL_SUM_EXCEEDS_TOTAL",
        "REPORT_COUNT_FAILED",
        "UNEXPECTED_DIAGNOSTIC_FAILURE",
    })
    assert not any(code.startswith("GLOBAL_") for code in ALLOWED_FAILURE_CODES)


def test_channel_allowlist_membership_is_unchanged():
    assert ALLOWED_DEFAULT_CHANNELS == frozenset({
        "Affiliates", "AI Assistant", "Audio", "Cross-network", "Direct",
        "Display", "Email", "Mobile Push Notifications", "Organic Search",
        "Organic Shopping", "Organic Social", "Organic Video", "Paid Other",
        "Paid Search", "Paid Shopping", "Paid Social", "Paid Video",
        "Referral", "SMS", "Unassigned",
    })


def _raise_client_creation_failure():
    raise RuntimeError(
        "Bearer token JWT private@example.invalid /credential/private-key.json"
    )


@pytest.mark.parametrize(("expected_code", "expected_calls", "factory"), (
    ("CLIENT_CREATION_FAILED", 0, _raise_client_creation_failure),
    (
        "CHANNEL_API_REQUEST_FAILED",
        0,
        lambda: FakeClient(RuntimeError(
            "Authorization Bearer secret /credential/path?query=private"
        )),
    ),
    (
        "CHANNEL_RESPONSE_SCHEMA_FAILED",
        1,
        lambda: FakeClient(response(dimensions=("private@example.invalid",))),
    ),
    (
        "CHANNEL_RESPONSE_ROW_FAILED",
        1,
        lambda: client_for(
            totals=(1, 1, 0), channel_rows=(row((), (1, 1, 0)),),
        ),
    ),
    (
        "CHANNEL_VALUE_NOT_ALLOWLISTED",
        1,
        lambda: client_for(
            totals=(1, 1, 0),
            channel_rows=(row(("https://private.invalid/?token=secret",),
                              (1, 1, 0)),),
        ),
    ),
    (
        "CHANNEL_VALUE_DUPLICATE",
        1,
        lambda: client_for(
            totals=(2, 2, 0),
            channel_rows=(
                row(("Direct",), (1, 1, 0)),
                row(("Direct",), (1, 1, 0)),
            ),
        ),
    ),
    (
        "CHANNEL_METRIC_FAILED",
        1,
        lambda: client_for(
            totals=(1, 1, 0),
            channel_rows=(row(("Direct",), ("secret", 1, 0)),),
        ),
    ),
    (
        "TOTAL_RESPONSE_MISSING",
        1,
        lambda: client_for(totals=None, channel_rows=()),
    ),
    (
        "TOTAL_RESPONSE_SCHEMA_FAILED",
        1,
        lambda: FakeClient(response(totals=(
            total_row((1, 1, 0)), total_row((1, 1, 0)),
        ))),
    ),
    (
        "TOTAL_METRIC_FAILED",
        1,
        lambda: FakeClient(response(totals=(
            total_row(("private@example.invalid", 1, 0)),
        ))),
    ),
    (
        "CHANNEL_SUM_EXCEEDS_TOTAL",
        1,
        lambda: client_for(
            totals=(1, 1, 0),
            channel_rows=(row(("Direct",), (2, 1, 0)),),
        ),
    ),
))
def test_safe_failure_output_and_report_calls_completed(
    monkeypatch, capsys, expected_code, expected_calls, factory
):
    lines = render_failure(monkeypatch, capsys, factory)
    assert_failure_output(lines, expected_code, expected_calls)


def test_report_count_invariant_has_safe_failure_code(monkeypatch, capsys):
    class MiscountingRecordingClient:
        def __init__(self, client):
            self.client = client
            self.requests = []
            self.responses = []

        def run_report(self, *, request):
            self.requests.append(request)
            return self.client.run_report(request=request)

    monkeypatch.setattr(
        diagnostic_module, "RecordingClient", MiscountingRecordingClient
    )
    lines = render_failure(monkeypatch, capsys, lambda: client_for())
    assert_failure_output(lines, "REPORT_COUNT_FAILED", 0)


def test_unexpected_internal_failure_is_sanitized_after_one_report(
    monkeypatch, capsys
):
    def fail_with_private_data(_response):
        raise RuntimeError(
            "JWT Authorization private@example.invalid "
            "https://example.invalid/?token=secret /credential/private-key.json"
        )

    monkeypatch.setattr(diagnostic_module, "_build_result", fail_with_private_data)
    lines = render_failure(monkeypatch, capsys, lambda: client_for())
    assert_failure_output(lines, "UNEXPECTED_DIAGNOSTIC_FAILURE", 1)


def test_report_calls_completed_two_is_impossible_and_safely_normalized():
    failure = GA4AllChannelTrafficDiagnosticError(
        "CHANNEL_RESPONSE_ROW_FAILED", 2
    )
    assert failure.safe_code == "REPORT_COUNT_FAILED"
    assert failure.report_calls_completed == 0


@pytest.mark.parametrize("factory", (
    lambda: FakeClient(RuntimeError(
        "Authorization Bearer jwt.payload.signature /credential/private-key.json "
        "https://example.invalid/?token=secret private@example.invalid"
    )),
    lambda: client_for(
        totals=(1, 1, 0),
        channel_rows=(row((
            "https://example.invalid/private?token=secret&email="
            "private@example.invalid",
        ), (1, 1, 0)),),
    ),
))
def test_failure_output_never_contains_raw_or_sensitive_values(
    monkeypatch, capsys, factory
):
    rendered = "\n".join(render_failure(monkeypatch, capsys, factory))
    lowered = rendered.lower()
    assert len(rendered.splitlines()) == 7
    assert all(value not in lowered for value in (
        "private@example.invalid",
        "https://",
        "?token=",
        "/credential/",
        "bearer",
        "authorization",
        "jwt.payload",
        "private-key",
        "secret",
    ))


def test_untyped_main_failure_uses_only_safe_fallback(monkeypatch, capsys):
    monkeypatch.setattr(
        diagnostic_module,
        "run_diagnostic",
        lambda: (_ for _ in ()).throw(RuntimeError(
            "Authorization token private@example.invalid"
        )),
    )
    assert diagnostic_module.main() == 1
    assert_failure_output(
        capsys.readouterr().out.strip().splitlines(),
        "UNEXPECTED_DIAGNOSTIC_FAILURE",
        0,
    )


def test_success_output_uses_only_existing_aggregate_keys():
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
    assert "REPORT_CALLS=1" in output


def test_runner_has_one_forward_call_and_no_disallowed_data_dimensions():
    source = RUNNER_PATH.read_text()
    assert source.count("response = client.run_report(request=") == 1
    assert source.count("self.client.run_report(") == 1
    assert "metric_aggregations" in source
    assert "GLOBAL_DIMENSIONS" not in source
    assert "dimension_filter" not in source
    assert "metric_filter" not in source
    assert all(value not in source for value in (
        "landingPage", "pagePath", "pageLocation", "queryString",
        "userPseudoId", "clientId", "transactionId", "country", "device",
    ))
    assert AUTH_MODE == "WIF"


def test_workflow_is_unchanged_manual_only_with_scoped_oidc_permissions():
    workflow = WORKFLOW_PATH.read_text()
    trigger = workflow.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    assert trigger.strip() == "workflow_dispatch:"
    assert all(item not in trigger for item in (
        "push:", "pull_request:", "schedule:", "workflow_call:",
    ))
    assert workflow.split("permissions:", 1)[1].split(
        "\njobs:", 1
    )[0].strip() == "{}"
    assert workflow.count("id-token: write") == 1
    job = workflow.split("  ga4-all-channel-traffic-diagnostic:\n", 1)[1]
    permissions = job.split("    permissions:\n", 1)[1].split(
        "\n\n    steps:", 1
    )[0]
    assert permissions.strip().splitlines() == [
        "contents: read", "      id-token: write",
    ]
    assert "run: python -m scripts.v3.ga4_all_channel_traffic_diagnostic" in workflow


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
    assert "workload_identity_provider: {}".format(PROVIDER) in workflow
    assert "service_account: {}".format(SERVICE_ACCOUNT) in workflow
    assert "secrets." not in workflow


def test_runtime_remains_fixture_backed_offline_and_adapter_disabled():
    config = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    assert config["phase_1_sources"]["analytics"] == "local_fixture"
    assert config["mode"]["network_enabled"] is False
    assert config["ga4_data_api"]["enabled"] is False
