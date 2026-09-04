import json
import re
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.v3 import ga4_adapter_readonly_test as runner_module
from scripts.v3.ga4_adapter_readonly_test import (
    AUTH_MODE,
    END_DATE,
    EXPECTED_REPORT_CALLS,
    OBSERVED_AT,
    PASS_VERDICT,
    PROPERTY_ID,
    START_DATE,
    GA4AdapterReadOnlyTestError,
    run_adapter_test,
)
from scripts.v3.models import Confidence, Evidence, TrafficSignal
from scripts.v3.sources.analytics import (
    DEFAULT_GA4_LANDING_PAGE_TOPICS,
    GA4_LANDING_PAGE_DIMENSIONS,
    GA4_METRICS,
    GA4_TOTAL_DIMENSION_VALUE,
    ORGANIC_SEARCH_CHANNEL,
    GoogleAnalyticsDataSource,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/v3/ga4_adapter_readonly_test.py"
WORKFLOW_PATH = ROOT / ".github/workflows/seo-v3-ga4-adapter-readonly-test.yml"
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


class FakeSource:
    signals = ()
    instances = []

    def __init__(self, **arguments):
        self.arguments = arguments
        self.collect_calls = 0
        type(self).instances.append(self)

    def collect(self):
        self.collect_calls += 1
        return self.signals


def response(dimensions, rows=(), totals=()):
    return Response(
        dimension_headers=tuple(Header(name) for name in dimensions),
        metric_headers=tuple(Header(name) for name in GA4_METRICS),
        rows=tuple(rows),
        totals=tuple(totals),
    )


def row(dimensions, metrics):
    return Row(
        dimension_values=tuple(Value(value) for value in dimensions),
        metric_values=tuple(Value(value) for value in metrics),
    )


def traffic_signal(topic, sessions, engaged, conversions, raw_reference=None):
    evidence = ()
    if raw_reference is not None:
        evidence = (Evidence(
            source="fake",
            observed_at=OBSERVED_AT,
            metric="aggregate",
            fact={"landing_page": raw_reference},
            confidence=Confidence.HIGH,
        ),)
    return TrafficSignal(
        topic=topic,
        organic_sessions=sessions,
        engaged_sessions=engaged,
        conversions=conversions,
        evidence=evidence,
    )


@pytest.fixture(autouse=True)
def reset_and_forbid_network(monkeypatch):
    FakeSource.instances = []
    FakeSource.signals = ()
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network forbidden in tests")
        ),
    )


def test_runner_uses_existing_adapter_contract_and_collects_exactly_once():
    output = []
    returned = run_adapter_test(
        client_factory=lambda: "fake-client",
        data_source_class=FakeSource,
        emit=output.append,
    )
    assert returned == ()
    assert len(FakeSource.instances) == 1
    source = FakeSource.instances[0]
    assert source.collect_calls == 1
    assert source.arguments == {
        "property_id": "552715460",
        "client": "fake-client",
        "start_date": "2026-09-03",
        "end_date": "2026-09-03",
        "observed_at": "2026-09-04",
        "landing_page_topics": dict(DEFAULT_GA4_LANDING_PAGE_TOPICS),
    }


def test_existing_adapter_performs_exactly_one_fixed_read_only_report():
    total = row(
        (GA4_TOTAL_DIMENSION_VALUE, GA4_TOTAL_DIMENSION_VALUE),
        ("4", "3", "1"),
    )
    client = FakeClient(
        response(
            GA4_LANDING_PAGE_DIMENSIONS,
            (row(("/", ORGANIC_SEARCH_CHANNEL), ("4", "3", "1")),),
            (total,),
        ),
    )
    source = GoogleAnalyticsDataSource(
        property_id=PROPERTY_ID,
        client=client,
        start_date=START_DATE,
        end_date=END_DATE,
        observed_at=OBSERVED_AT,
        landing_page_topics=dict(DEFAULT_GA4_LANDING_PAGE_TOPICS),
    )
    assert len(source.collect()) == 1
    assert len(client.requests) == EXPECTED_REPORT_CALLS == 1
    assert all(request["property"] == "properties/552715460" for request in client.requests)
    assert all(request["date_ranges"] == [{
        "start_date": "2026-09-03", "end_date": "2026-09-03",
    }] for request in client.requests)
    assert tuple(item["name"] for item in client.requests[0]["dimensions"]) == (
        "landingPage", "sessionDefaultChannelGroup",
    )
    assert client.requests[0]["metric_aggregations"] == ["TOTAL"]
    for request in client.requests:
        assert tuple(item["name"] for item in request["metrics"]) == (
            "sessions", "engagedSessions", "keyEvents",
        )
        assert request["dimension_filter"]["filter"]["string_filter"]["value"] == (
            "Organic Search"
        )


def test_zero_signals_is_successful_and_uses_allowlisted_output_only():
    output = []
    run_adapter_test(
        client_factory=lambda: object(),
        data_source_class=FakeSource,
        emit=output.append,
    )
    assert output == [
        "PROPERTY_ID=552715460",
        "AUTH_MODE=WIF",
        "START_DATE=2026-09-03",
        "END_DATE=2026-09-03",
        "OBSERVED_AT=2026-09-04",
        "EXPECTED_REPORT_CALLS=1",
        "SIGNAL_COUNT=0",
        "TOPICS=",
        "FINAL_VERDICT=GA4_ADAPTER_READONLY_TEST_PASS",
    ]


def test_multiple_signals_are_reported_in_deterministic_aggregate_order():
    FakeSource.signals = (
        traffic_signal("wine_delivery", 2.5, 2, 1),
        traffic_signal("business_delivery", 7, 5, 0),
    )
    output = []
    returned = run_adapter_test(
        client_factory=lambda: object(),
        data_source_class=FakeSource,
        emit=output.append,
    )
    assert tuple(signal.topic for signal in returned) == (
        "business_delivery", "wine_delivery",
    )
    assert "TOPICS=business_delivery,wine_delivery" in output
    assert output[8:16] == [
        "SIGNAL_1_TOPIC=business_delivery",
        "SIGNAL_1_ORGANIC_SESSIONS=7",
        "SIGNAL_1_ENGAGED_SESSIONS=5",
        "SIGNAL_1_CONVERSIONS=0",
        "SIGNAL_2_TOPIC=wine_delivery",
        "SIGNAL_2_ORGANIC_SESSIONS=2.5",
        "SIGNAL_2_ENGAGED_SESSIONS=2",
        "SIGNAL_2_CONVERSIONS=1",
    ]
    assert output[-1] == "FINAL_VERDICT={}".format(PASS_VERDICT)


def test_unverified_ga4_conversion_semantics_are_reported_as_unknown():
    FakeSource.signals = (
        traffic_signal("business_delivery", 7, 5, None),
    )
    output = []

    run_adapter_test(
        client_factory=lambda: object(),
        data_source_class=FakeSource,
        emit=output.append,
    )

    assert "SIGNAL_1_CONVERSIONS=UNKNOWN" in output
    assert output[-1] == "FINAL_VERDICT={}".format(PASS_VERDICT)


def test_runner_never_logs_raw_landing_pages_queries_or_evidence():
    private_value = "/unknown?email=private@example.invalid"
    FakeSource.signals = (
        traffic_signal("general_delivery", 1, 1, 0, private_value),
    )
    output = []
    run_adapter_test(
        client_factory=lambda: object(),
        data_source_class=FakeSource,
        emit=output.append,
    )
    rendered = "\n".join(output)
    assert private_value not in rendered
    assert "email" not in rendered
    allowed = re.compile(
        r"^(PROPERTY_ID|AUTH_MODE|START_DATE|END_DATE|OBSERVED_AT|"
        r"EXPECTED_REPORT_CALLS|SIGNAL_COUNT|TOPICS|"
        r"SIGNAL_\d+_(?:TOPIC|ORGANIC_SESSIONS|ENGAGED_SESSIONS|CONVERSIONS)|"
        r"FINAL_VERDICT)="
    )
    assert all(allowed.match(line) for line in output)


def test_unknown_or_malformed_topic_fails_closed_without_output():
    FakeSource.signals = (
        traffic_signal("unknown?email=private@example.invalid", 1, 1, 0),
    )
    output = []
    with pytest.raises(GA4AdapterReadOnlyTestError, match="read-only test failed"):
        run_adapter_test(
            client_factory=lambda: object(),
            data_source_class=FakeSource,
            emit=output.append,
        )
    assert output == []


def test_api_or_adapter_failure_is_sanitized_without_output():
    class FailingSource(FakeSource):
        def collect(self):
            self.collect_calls += 1
            raise RuntimeError("token credential /private/path?email=secret")

    output = []
    with pytest.raises(
        GA4AdapterReadOnlyTestError, match="GA4 adapter read-only test failed"
    ) as failure:
        run_adapter_test(
            client_factory=lambda: object(),
            data_source_class=FailingSource,
            emit=output.append,
        )
    assert output == []
    rendered = str(failure.value)
    assert all(value not in rendered for value in (
        "token", "credential", "/private", "email", "secret",
    ))


def test_main_exits_nonzero_with_only_the_sanitized_failure_verdict(
    monkeypatch, capsys
):
    def fail_closed():
        raise RuntimeError("token credential /private/path?email=secret")

    monkeypatch.setattr(runner_module, "run_adapter_test", fail_closed)
    assert runner_module.main() == 1
    assert capsys.readouterr().out == (
        "FINAL_VERDICT=GA4_ADAPTER_READONLY_TEST_FAILED\n"
    )


def test_runner_source_has_no_direct_report_call_or_credential_logging():
    source = RUNNER_PATH.read_text()
    assert ".run_report(" not in source
    assert source.count(".collect()") == 1
    assert AUTH_MODE == "WIF"
    assert all(value not in source for value in (
        "GOOGLE_APPLICATION_CREDENTIALS", "access_token", "refresh_token",
        "Authorization", "private_key", "jwt", "environ",
    ))


def test_workflow_is_manual_only_with_job_scoped_oidc_and_exact_pins():
    workflow = WORKFLOW_PATH.read_text()
    trigger = workflow.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    assert trigger.strip() == "workflow_dispatch:"
    assert all(item not in trigger for item in (
        "push:", "pull_request:", "schedule:", "workflow_call:",
    ))
    assert workflow.split("permissions:", 1)[1].split("\njobs:", 1)[0].strip() == "{}"
    assert workflow.count("id-token: write") == 1
    job = workflow.split("  ga4-adapter-readonly-test:\n", 1)[1]
    permissions = job.split("    permissions:\n", 1)[1].split("\n\n    steps:", 1)[0]
    assert permissions.strip().splitlines() == [
        "contents: read", "      id-token: write",
    ]
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


def test_workflow_reuses_hash_lock_and_exact_wif_without_static_key():
    workflow = WORKFLOW_PATH.read_text()
    assert "python-version: \"3.11\"" in workflow
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
    assert "run: python -m scripts.v3.ga4_adapter_readonly_test" in workflow


def test_v3_runtime_remains_disabled_and_fixture_backed():
    config = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    assert config["phase_1_sources"]["analytics"] == "local_fixture"
    assert config["mode"]["network_enabled"] is False
    assert config["ga4_data_api"]["enabled"] is False
