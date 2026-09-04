import json
import socket
from pathlib import Path

import pytest

from scripts.v3 import gsc_readonly_probe as probe_module
from scripts.v3.config import RuntimeState, load_v3_config
from scripts.v3.gsc_readonly_probe import (
    EXPECTED_GSC_API_CALLS,
    FAIL_VERDICT,
    PASS_VERDICT,
    SAFE_FAILURE_CODES,
    CountingGSCTransport,
    GSCReadOnlyProbeError,
    main,
    run_probe,
)
from scripts.v3.source_factory import build_source_adapters
from scripts.v3.sources.search_console import (
    GSC_ENDPOINT,
    GSC_PROPERTY,
    SearchConsoleFixtureSource,
)


ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "scripts/v3/gsc_readonly_probe.py"
WORKFLOW_PATH = ROOT / ".github/workflows/seo-v3-gsc-readonly-probe.yml"
PROVIDER = (
    "projects/270376484474/locations/global/workloadIdentityPools/"
    "github/providers/colixo-seo-bot"
)
SERVICE_ACCOUNT = "colixo-seo-gsc-reader@colixo-seo-agent.iam.gserviceaccount.com"


class FakeResponse:
    def __init__(self, payload, *, status_code=200, json_error=None):
        self.payload = payload
        self.status_code = status_code
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def row(query, clicks, impressions, ctr=0.1, position=8):
    return {
        "keys": [query],
        "clicks": clicks,
        "impressions": impressions,
        "ctr": ctr,
        "position": position,
    }


def failure_output(code, calls):
    return [
        "SAFE_FAILURE_CODE={}".format(code),
        "GSC_API_CALLS_COMPLETED={}".format(calls),
        "FINAL_VERDICT={}".format(FAIL_VERDICT),
    ]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network is forbidden in GSC probe tests")
        ),
    )


def test_runner_accepts_explicit_observed_at_and_derives_dates():
    transport = FakeTransport(FakeResponse({}))
    output = []

    execution = run_probe(
        observed_at="2026-09-04",
        transport_factory=lambda: transport,
        emit=output.append,
    )

    assert execution.api_calls == len(transport.calls) == 1
    assert "OBSERVED_AT=2026-09-04" in output
    assert "DATE_RANGE_START=2026-08-07" in output
    assert "DATE_RANGE_END=2026-09-01" in output
    assert "datetime.now" not in RUNNER_PATH.read_text()
    assert "date.today" not in RUNNER_PATH.read_text()


@pytest.mark.parametrize(
    "observed_at",
    (None, "", "today", "yesterday", "28daysAgo", "2026-99-99", "2026-9-4"),
)
def test_invalid_observed_at_fails_before_transport_creation(observed_at):
    factory_calls = []
    output = []

    assert main(
        ("--observed-at", observed_at),
        transport_factory=lambda: factory_calls.append(True),
        emit=output.append,
    ) == 1
    assert output == failure_output("OBSERVED_AT_INVALID", 0)
    assert factory_calls == []


def test_missing_cli_argument_fails_safely_before_transport_creation():
    factory_calls = []
    output = []
    assert main(
        (),
        transport_factory=lambda: factory_calls.append(True),
        emit=output.append,
    ) == 1
    assert output == failure_output("OBSERVED_AT_INVALID", 0)
    assert factory_calls == []


def test_transport_factory_failure_is_sanitized():
    sensitive = "credential path /private/adc.json token@example.invalid"
    output = []
    assert main(
        ("--observed-at", "2026-09-04"),
        transport_factory=lambda: (_ for _ in ()).throw(RuntimeError(sensitive)),
        emit=output.append,
    ) == 1
    assert output == failure_output("TRANSPORT_CREATION_FAILED", 0)
    assert sensitive not in "\n".join(output)


def test_counting_transport_allows_exactly_one_post_and_blocks_second():
    delegate = FakeTransport(FakeResponse({}))
    counting = CountingGSCTransport(delegate)
    assert counting.post(GSC_ENDPOINT, json={}, timeout=30).status_code == 200
    assert counting.post_attempts == counting.completed_requests == 1
    with pytest.raises(GSCReadOnlyProbeError) as raised:
        counting.post(GSC_ENDPOINT, json={}, timeout=30)
    assert raised.value.safe_code == "REPORT_COUNT_INVALID"
    assert raised.value.api_calls_completed == 1
    assert len(delegate.calls) == 1
    assert not hasattr(counting, "transport")
    assert not hasattr(counting, "response")
    assert not hasattr(counting, "credentials")


def test_success_with_zero_signals_is_valid_and_safe():
    transport = FakeTransport(FakeResponse({}))
    output = []
    execution = run_probe(
        observed_at="2026-09-04",
        transport_factory=lambda: transport,
        emit=output.append,
    )

    assert execution.signals == ()
    assert execution.api_calls == 1
    assert output == [
        "PROPERTY=sc-domain:colixo.ch",
        "AUTH_MODE=WIF",
        "OBSERVED_AT=2026-09-04",
        "DATE_RANGE_START=2026-08-07",
        "DATE_RANGE_END=2026-09-01",
        "GSC_API_CALLS=1",
        "SIGNAL_COUNT=0",
        "TOTAL_CLICKS=0",
        "TOTAL_IMPRESSIONS=0",
        "GENERAL_DELIVERY_SIGNAL_COUNT=0",
        "BUSINESS_DELIVERY_SIGNAL_COUNT=0",
        "PARCEL_DELIVERY_SIGNAL_COUNT=0",
        "WINE_DELIVERY_SIGNAL_COUNT=0",
        "SECURE_WATCH_DELIVERY_SIGNAL_COUNT=0",
        "FINAL_VERDICT={}".format(PASS_VERDICT),
    ]


def test_commercial_signals_are_aggregated_without_queries_or_derived_metrics():
    queries = (
        "livraison",
        "livraison entreprise",
        "livraison colis",
        "livraison de vins",
        "envoi sécurisé horlogerie",
        "unknown confidential phrase",
        "livraison colis secret@example.invalid",
        "livraison colis +41 79 123 45 67",
    )
    payload = {"rows": [
        row(queries[0], "1.5", "10"),
        row(queries[1], "2", "20"),
        row(queries[2], "3.25", "30"),
        row(queries[3], "4", "40"),
        row(queries[4], "5", "50"),
        row(queries[5], "100", "1000"),
        row(queries[6], "100", "1000"),
        row(queries[7], "100", "1000"),
    ]}
    transport = FakeTransport(FakeResponse(payload))
    output = []

    execution = run_probe(
        observed_at="2026-09-04",
        transport_factory=lambda: transport,
        emit=output.append,
    )

    rendered = "\n".join(output)
    assert execution.api_calls == len(transport.calls) == 1
    assert len(execution.signals) == 5
    assert "SIGNAL_COUNT=5" in output
    assert "TOTAL_CLICKS=15.75" in output
    assert "TOTAL_IMPRESSIONS=150" in output
    for topic in (
        "GENERAL_DELIVERY",
        "BUSINESS_DELIVERY",
        "PARCEL_DELIVERY",
        "WINE_DELIVERY",
        "SECURE_WATCH_DELIVERY",
    ):
        assert "{}_SIGNAL_COUNT=1".format(topic) in output
    assert "CTR" not in rendered
    assert "POSITION" not in rendered
    for query in queries:
        assert query not in rendered
    assert "keys" not in rendered
    assert "evidence" not in rendered.lower()


def test_probe_uses_existing_adapter_static_request_contract_once():
    transport = FakeTransport(FakeResponse({}))
    run_probe(
        observed_at="2026-09-04",
        transport_factory=lambda: transport,
        emit=lambda _line: None,
    )
    assert EXPECTED_GSC_API_CALLS == 1
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"] == GSC_ENDPOINT
    assert transport.calls[0]["json"] == {
        "startDate": "2026-08-07",
        "endDate": "2026-09-01",
        "dimensions": ["query"],
        "type": "web",
        "dataState": "final",
        "rowLimit": 25000,
        "startRow": 0,
    }
    assert GSC_PROPERTY == "sc-domain:colixo.ch"


def test_api_failure_is_sanitized_without_retry():
    sensitive = "raw response token query=private@example.invalid"
    transport = FakeTransport(RuntimeError(sensitive))
    output = []

    assert main(
        ("--observed-at", "2026-09-04"),
        transport_factory=lambda: transport,
        emit=output.append,
    ) == 1
    assert output == failure_output("GSC_API_REQUEST_FAILED", 0)
    assert sensitive not in "\n".join(output)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("payload", "code"),
    (
        ([], "GSC_RESPONSE_INVALID"),
        ({"rows": "invalid"}, "GSC_RESPONSE_INVALID"),
        ({"rows": [{"keys": ["livraison"]}]}, "GSC_ROW_INVALID"),
        ({"rows": [row("livraison", "NaN", 10)]}, "GSC_ROW_INVALID"),
    ),
)
def test_malformed_response_or_row_is_sanitized(payload, code):
    transport = FakeTransport(FakeResponse(payload))
    output = []
    assert main(
        ("--observed-at", "2026-09-04"),
        transport_factory=lambda: transport,
        emit=output.append,
    ) == 1
    assert output == failure_output(code, 1)
    assert len(transport.calls) == 1


def test_http_failure_body_is_never_output_and_is_not_retried():
    sensitive = "private unknown query secret@example.invalid"
    transport = FakeTransport(FakeResponse({"error": sensitive}, status_code=403))
    output = []
    assert main(
        ("--observed-at", "2026-09-04"),
        transport_factory=lambda: transport,
        emit=output.append,
    ) == 1
    assert output == failure_output("GSC_API_REQUEST_FAILED", 1)
    assert sensitive not in "\n".join(output)
    assert len(transport.calls) == 1


def test_json_failure_is_sanitized_as_response_invalid():
    sensitive = "raw malformed body query=private"
    transport = FakeTransport(FakeResponse(None, json_error=ValueError(sensitive)))
    output = []
    assert main(
        ("--observed-at", "2026-09-04"),
        transport_factory=lambda: transport,
        emit=output.append,
    ) == 1
    assert output == failure_output("GSC_RESPONSE_INVALID", 1)
    assert sensitive not in "\n".join(output)


def test_failure_codes_are_exact_and_output_is_three_safe_lines():
    assert SAFE_FAILURE_CODES == (
        "OBSERVED_AT_INVALID",
        "TRANSPORT_CREATION_FAILED",
        "GSC_API_REQUEST_FAILED",
        "GSC_RESPONSE_INVALID",
        "GSC_ROW_INVALID",
        "REPORT_COUNT_INVALID",
        "REPORT_RENDER_FAILED",
        "UNEXPECTED_PROBE_FAILURE",
    )
    error = GSCReadOnlyProbeError("not-allowlisted", 99)
    assert error.safe_code == "REPORT_COUNT_INVALID"
    assert error.api_calls_completed == 1


def test_render_failure_is_sanitized_after_one_call():
    transport = FakeTransport(FakeResponse({}))

    def failing_emit(_line):
        raise RuntimeError("private output failure")

    with pytest.raises(GSCReadOnlyProbeError) as raised:
        run_probe(
            observed_at="2026-09-04",
            transport_factory=lambda: transport,
            emit=failing_emit,
        )
    assert raised.value.safe_code == "REPORT_RENDER_FAILED"
    assert raised.value.api_calls_completed == 1


def test_workflow_is_manual_only_and_permissions_are_exact():
    workflow = WORKFLOW_PATH.read_text()
    assert workflow.startswith("name: Colixo SEO Agent V3 GSC Read-only Probe\n")
    assert "\non:\n  workflow_dispatch:\n\npermissions: {}\n" in workflow
    assert workflow.count("workflow_dispatch:") == 1
    for forbidden in ("push:", "pull_request:", "schedule:", "workflow_call:"):
        assert forbidden not in workflow
    assert "permissions: {}" in workflow
    assert "    permissions:\n      contents: read\n      id-token: write\n" in workflow


def test_workflow_uses_exact_pins_wif_identity_and_hardened_install():
    workflow = WORKFLOW_PATH.read_text()
    assert "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093" in workflow
    assert "persist-credentials: false" in workflow
    assert 'python-version: "3.11"' in workflow
    assert "workload_identity_provider: {}".format(PROVIDER) in workflow
    assert "service_account: {}".format(SERVICE_ACCOUNT) in workflow
    assert "project_id: colixo-seo-agent" in workflow
    assert "create_credentials_file: true" in workflow
    assert "export_environment_variables: true" in workflow
    assert "--require-hashes" in workflow
    assert "--only-binary=:all:" in workflow
    assert "requirements-ga4-probe.lock" in workflow


def test_workflow_runs_probe_once_without_secret_artifact_or_other_api():
    workflow = WORKFLOW_PATH.read_text()
    assert workflow.count("python -m scripts.v3.gsc_readonly_probe") == 1
    assert 'OBSERVED_AT="$(TZ=Europe/Zurich date +%F)"' in workflow
    assert '| tee -a "$GITHUB_STEP_SUMMARY"' in workflow
    for forbidden in (
        "GSC_SERVICE_ACCOUNT_JSON",
        "GSC_SERVICE_ACCOUNT_FILE",
        "upload-artifact",
        "artifact",
        "analyticsdata.googleapis.com",
        "colixo-seo-ga4-reader",
    ):
        assert forbidden not in workflow


def test_existing_runtime_states_and_live_gsc_reachability_are_unchanged(tmp_path):
    offline = load_v3_config()
    assert offline.runtime_state is RuntimeState.OFFLINE
    assert isinstance(build_source_adapters(offline)["search_console"], SearchConsoleFixtureSource)

    payload = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    payload["mode"]["network_enabled"] = True
    payload["phase_1_sources"]["analytics"] = "ga4_data_api"
    payload["network_policy"]["sources"]["analytics"] = "allow"
    payload["ga4_data_api"]["enabled"] = True
    config_path = tmp_path / "ga4-read-only.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    ga4_read_only = load_v3_config(config_path)
    assert ga4_read_only.runtime_state is RuntimeState.GA4_READ_ONLY
    adapters = build_source_adapters(
        ga4_read_only,
        observed_at="2026-09-04",
        ga4_client_factory=lambda: object(),
    )
    assert isinstance(adapters["search_console"], SearchConsoleFixtureSource)
    assert tuple(state.value for state in RuntimeState) == ("OFFLINE", "GA4_READ_ONLY")

    factory_source = (ROOT / "scripts/v3/source_factory.py").read_text()
    assert "GoogleSearchConsoleDataSource" not in factory_source
    assert "create_google_search_console_transport" not in factory_source


def test_probe_pr_scope_leaves_configs_legacy_auth_and_dependency_lock_unchanged():
    changed = {
        ".github/workflows/seo-v3-gsc-readonly-probe.yml",
        "scripts/v3/gsc_readonly_probe.py",
        "tests/test_seo_agent_v3_gsc_readonly_probe.py",
    }
    protected = (
        "scripts/v3/config.py",
        "scripts/v3/source_factory.py",
        "scripts/v3/agent.py",
        "scripts/v3/scoring.py",
        "scripts/gsc_client.py",
        ".github/workflows/seo.yml",
        "requirements-ga4-probe.lock",
    )
    assert changed.isdisjoint(protected)
    for path in protected:
        assert (ROOT / path).exists()
