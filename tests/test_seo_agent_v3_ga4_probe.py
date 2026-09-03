import json
import re
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.v3.ga4_readonly_probe import (
    AUTH_MODE,
    CHANNEL,
    DIMENSIONS,
    METRICS,
    PROPERTY_ID,
    PROPERTY_RESOURCE,
    GA4ReadOnlyProbeError,
    build_request,
    run_probe,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/seo-v3-ga4-readonly-probe.yml"
PROBE_PATH = ROOT / "scripts/v3/ga4_readonly_probe.py"
PROVIDER = (
    "projects/270376484474/locations/global/workloadIdentityPools/"
    "github/providers/colixo-seo-bot"
)
SERVICE_ACCOUNT = (
    "colixo-seo-ga4-reader@colixo-seo-agent.iam.gserviceaccount.com"
)


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
    def __init__(self, response):
        self.response = response
        self.requests = []

    def run_report(self, *, request):
        self.requests.append(request)
        return self.response


def response(rows=()):
    return Response(
        dimension_headers=tuple(Header(name) for name in DIMENSIONS),
        metric_headers=tuple(Header(name) for name in METRICS),
        rows=tuple(rows),
    )


def row(metrics):
    return Row(
        dimension_values=(Value(CHANNEL),),
        metric_values=tuple(Value(value) for value in metrics),
    )


def workflow_text():
    return WORKFLOW_PATH.read_text()


def test_workflow_is_manual_only_with_exact_permissions():
    workflow = workflow_text()
    trigger_block = workflow.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    permission_block = workflow.split("permissions:\n", 1)[1].split("\njobs:", 1)[0]
    assert trigger_block.strip() == "workflow_dispatch:"
    assert permission_block.strip().splitlines() == [
        "contents: read",
        "  id-token: write",
    ]
    assert all(trigger not in trigger_block for trigger in (
        "push:", "pull_request:", "schedule:", "workflow_call:",
    ))


def test_workflow_uses_exact_wif_identity_and_no_static_secret():
    workflow = workflow_text()
    assert "workload_identity_provider: {}".format(PROVIDER) in workflow
    assert "service_account: {}".format(SERVICE_ACCOUNT) in workflow
    assert "project_id: colixo-seo-agent" in workflow
    assert "secrets." not in workflow
    assert "service_account_key" not in workflow
    assert "credentials_json" not in workflow


def test_workflow_actions_are_immutable_and_checkout_does_not_persist_credentials():
    workflow = workflow_text()
    expected = {
        "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
        "google-github-actions/auth": "7c6bc770dae815cd3e89ee6cdf493a5fab2cc093",
    }
    for action, sha in expected.items():
        assert "uses: {}@{}".format(action, sha) in workflow
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)
    assert set(uses) == {
        "{}@{}".format(action, sha) for action, sha in expected.items()
    }
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", value) for value in uses)
    assert "persist-credentials: false" in workflow
    assert "run: python -m scripts.v3.ga4_readonly_probe" in workflow


def test_request_is_one_fixed_aggregate_organic_search_report():
    request = build_request()
    assert PROPERTY_ID == "552715460"
    assert request["property"] == "properties/552715460" == PROPERTY_RESOURCE
    assert request["date_ranges"] == [{"start_date": "today", "end_date": "today"}]
    assert tuple(item["name"] for item in request["dimensions"]) == DIMENSIONS
    assert tuple(item["name"] for item in request["metrics"]) == METRICS
    assert request["dimension_filter"] == {
        "filter": {
            "field_name": "sessionDefaultChannelGroup",
            "string_filter": {
                "match_type": "EXACT",
                "value": "Organic Search",
                "case_sensitive": True,
            },
        }
    }


def test_request_contains_no_pii_or_query_string_dimensions():
    serialized = json.dumps(build_request())
    prohibited = (
        "landingPage", "pagePath", "pageLocation", "queryString",
        "userPseudoId", "transactionId", "clientId", "email", "phone",
        "city", "region", "country", "deviceCategory", "deviceId",
    )
    assert all(field not in serialized for field in prohibited)


def test_probe_makes_exactly_one_call_and_emits_only_safe_aggregate_output(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network forbidden in tests")
        ),
    )
    client = FakeClient(response((row(("12", "9", "2.5")),)))
    output = []
    result = run_probe(client=client, emit=output.append)
    assert len(client.requests) == 1
    assert result.row_count == 1
    assert output == [
        "PROPERTY_ID=552715460",
        "AUTH_MODE=WIF",
        "REPORT_CALLS=1",
        "DIMENSIONS=sessionDefaultChannelGroup",
        "METRICS=sessions,engagedSessions,keyEvents",
        "CHANNEL=Organic Search",
        "ROW_COUNT=1",
        "ORGANIC_SESSIONS_TOTAL=12",
        "ENGAGED_SESSIONS_TOTAL=9",
        "KEY_EVENTS_TOTAL=2.5",
        "FINAL_VERDICT=GA4_READONLY_PROBE_PASS",
    ]


def test_zero_rows_is_a_successful_read_only_probe():
    client = FakeClient(response())
    output = []
    result = run_probe(client=client, emit=output.append)
    assert len(client.requests) == 1
    assert result.row_count == 0
    assert "ORGANIC_SESSIONS_TOTAL=0" in output
    assert output[-1] == "FINAL_VERDICT=GA4_READONLY_PROBE_PASS"


@pytest.mark.parametrize("metrics", [("bad", "1", "0"), ("1", "-1", "0")])
def test_invalid_metrics_fail_closed_without_logging_credentials(metrics):
    client = FakeClient(response((row(metrics),)))
    output = []
    with pytest.raises(GA4ReadOnlyProbeError, match="metric"):
        run_probe(client=client, emit=output.append)
    assert len(client.requests) == 1
    assert output == []


def test_api_failure_is_sanitized_and_emits_nothing():
    class FailingClient:
        def __init__(self):
            self.calls = 0

        def run_report(self, *, request):
            self.calls += 1
            raise RuntimeError("credential and token internals")

    client = FailingClient()
    output = []
    with pytest.raises(GA4ReadOnlyProbeError, match="read-only report failed") as failure:
        run_probe(client=client, emit=output.append)
    assert client.calls == 1
    assert "credential" not in str(failure.value)
    assert "token" not in str(failure.value)
    assert output == []


def test_probe_source_has_one_run_report_and_no_credential_logging():
    source = PROBE_PATH.read_text()
    assert source.count(".run_report(") == 1
    assert AUTH_MODE == "WIF"
    prohibited = (
        "GOOGLE_APPLICATION_CREDENTIALS", "access_token", "refresh_token",
        "Authorization", "credential file", "private_key", "jwt",
    )
    assert all(value not in source for value in prohibited)


def test_v3_runtime_remains_offline_and_fixture_backed():
    config = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    assert config["phase_1_sources"]["analytics"] == "local_fixture"
    assert config["mode"]["network_enabled"] is False
    assert config["ga4_data_api"]["enabled"] is False
