"""Manual diagnostic tests: fake transport only, no Google authentication."""

from decimal import Decimal
from pathlib import Path

import pytest

from scripts.v3 import gsc_coverage_diagnostic as diagnostic
from scripts.v3.gsc_readonly_probe import CountingGSCTransport, GSCReadOnlyProbeError
from scripts.v3.sources.search_console import GSCCollectionCoverage, GSCCollectionResult
from scripts.v3.config import RuntimeState, load_v3_config
from scripts.v3.source_factory import build_source_adapters
from scripts.v3.sources.search_console import SearchConsoleFixtureSource

ROOT = Path(__file__).resolve().parents[1]


class FakeTransport:
    def __init__(self, payload, *, fail=False, status=200):
        self.payload, self.fail, self.status = payload, fail, status
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("sensitive fake exception")
        return type("Response", (), {"status_code": self.status,
                                     "json": lambda _: self.payload})()


def row(query, clicks="0.1", impressions="10.2"):
    return dict(keys=[query], clicks=clicks, impressions=impressions, ctr=0.1, position=2)


def execute(payload, **kwargs):
    transport = FakeTransport(payload, **kwargs)
    output = []
    status = diagnostic.main(["--observed-at", "2026-09-05"],
                             transport_factory=lambda: transport, emit=output.append)
    return status, "\n".join(output), transport.calls


@pytest.mark.parametrize("arguments", [[], ["--observed-at"], ["--x", "2026-09-05"],
    ["--observed-at", "yesterday"], ["--observed-at", "2026-99-99"],
    ["--observed-at", "2026-02-30"], ["--observed-at", ""], ["--observed-at", None]])
def test_invalid_dates_do_not_create_transport(arguments):
    output = []
    def forbidden():
        pytest.fail("transport must not be constructed")
    assert diagnostic.main(arguments, transport_factory=forbidden, emit=output.append) == 1
    assert output == ["SAFE_FAILURE_CODE=OBSERVED_AT_INVALID\nGSC_API_CALLS_COMPLETED=0\n"
                      "FINAL_VERDICT=V3_GSC_COVERAGE_DIAGNOSTIC_FAILED"]


@pytest.mark.parametrize("payload", [{}, {"rows": []}, {"rows": None}])
def test_empty_success(payload):
    status, output, calls = execute(payload)
    assert status == 0 and calls == 1
    values = dict(line.split("=", 1) for line in output.splitlines())
    assert len(values) == 18
    assert values["OBSERVED_AT"] == "2026-09-05"
    assert values["DATE_RANGE_START"] == "2026-08-08"
    assert values["DATE_RANGE_END"] == "2026-09-02"
    for field in diagnostic.COUNT_FIELDS + diagnostic.METRIC_FIELDS:
        assert values[field.upper()] == "0"


@pytest.mark.parametrize("query,field", [
    ("livraison colis", "ACCEPTED_SIGNAL_COUNT"),
    ("weather forecast", "UNMAPPED_ROW_COUNT"),
    ("colixo livraison", "BRAND_ROW_COUNT"),
    ("sample@example.test", "PII_FILTERED_ROW_COUNT"),
    ("+41 79 123 45 67", "PII_FILTERED_ROW_COUNT"),
])
def test_single_categories_are_counted_without_query_output(query, field):
    status, output, calls = execute({"rows": [row(query)]})
    assert status == 0 and calls == 1
    assert f"{field}=1" in output
    assert query not in output


def test_mixed_exact_output_and_privacy():
    status, output, calls = execute({"rows": [
        row("livraison colis", "0.1", "10.1"), row("weather forecast", "0.2", "20.2"),
        row("sample@example.test", "0.3", "30.3"), row("+41 79 123 45 67", "0.4", "40.4"),
    ]})
    assert status == 0 and calls == 1
    assert output.splitlines() == [
        "PROPERTY=sc-domain:colixo.ch", "AUTH_MODE=WIF", "OBSERVED_AT=2026-09-05",
        "DATE_RANGE_START=2026-08-08", "DATE_RANGE_END=2026-09-02", "GSC_API_CALLS=1",
        "RAW_ROW_COUNT=4", "ACCEPTED_SIGNAL_COUNT=1", "BRAND_ROW_COUNT=0", "UNMAPPED_ROW_COUNT=1",
        "PII_FILTERED_ROW_COUNT=2", "ALL_ROWS_CLICKS=1", "ALL_ROWS_IMPRESSIONS=101",
        "ACCEPTED_CLICKS=0.1", "ACCEPTED_IMPRESSIONS=10.1",
        "BRAND_CLICKS=0", "BRAND_IMPRESSIONS=0",
        "FINAL_VERDICT=V3_GSC_COVERAGE_DIAGNOSTIC_PASS",
    ]


@pytest.mark.parametrize("payload,kwargs,code,calls", [
    ({}, {"fail": True}, "GSC_API_REQUEST_FAILED", 0),
    ({"sensitive": "body"}, {"status": 403}, "GSC_API_REQUEST_FAILED", 1),
    ([], {}, "GSC_RESPONSE_INVALID", 1),
    ({"rows": "sensitive"}, {}, "GSC_RESPONSE_INVALID", 1),
    ({"rows": [{"keys": ["sample@example.test"]}]}, {}, "GSC_ROW_INVALID", 1),
    ({"rows": [row("private", "NaN")]}, {}, "GSC_ROW_INVALID", 1),
])
def test_failures_are_sanitized_without_retry(payload, kwargs, code, calls):
    status, output, attempts = execute(payload, **kwargs)
    assert status == 1 and attempts == 1
    assert output.splitlines() == [f"SAFE_FAILURE_CODE={code}",
        f"GSC_API_CALLS_COMPLETED={calls}", "FINAL_VERDICT=V3_GSC_COVERAGE_DIAGNOSTIC_FAILED"]


def test_transport_construction_failure():
    output = []
    def fail():
        raise RuntimeError("private credential path")
    assert diagnostic.main(["--observed-at", "2026-09-05"],
                           transport_factory=fail, emit=output.append) == 1
    assert output == ["SAFE_FAILURE_CODE=TRANSPORT_CREATION_FAILED\nGSC_API_CALLS_COMPLETED=0\n"
                      "FINAL_VERDICT=V3_GSC_COVERAGE_DIAGNOSTIC_FAILED"]


def test_mixed_brand_diagnostic_is_aggregate_only():
    status, output, calls = execute({"rows": [
        row("colixo livraison", "0.1", "10.1"), row("www colixo", "0.2", "20.2"),
        row("entreprise de livraison de colis", "0.3", "30.3"),
        row("weather forecast", "0.4", "40.4"),
        row("colixo sample@example.test", "0.5", "50.5"),
        row("colixo +41 79 123 45 67", "0.6", "60.6"),
    ]})
    assert status == 0 and calls == 1
    assert output.splitlines() == [
        "PROPERTY=sc-domain:colixo.ch", "AUTH_MODE=WIF", "OBSERVED_AT=2026-09-05",
        "DATE_RANGE_START=2026-08-08", "DATE_RANGE_END=2026-09-02", "GSC_API_CALLS=1",
        "RAW_ROW_COUNT=6", "ACCEPTED_SIGNAL_COUNT=1", "BRAND_ROW_COUNT=2",
        "UNMAPPED_ROW_COUNT=1", "PII_FILTERED_ROW_COUNT=2",
        "ALL_ROWS_CLICKS=2.1", "ALL_ROWS_IMPRESSIONS=212.1",
        "ACCEPTED_CLICKS=0.3", "ACCEPTED_IMPRESSIONS=30.3",
        "BRAND_CLICKS=0.3", "BRAND_IMPRESSIONS=30.3",
        "FINAL_VERDICT=V3_GSC_COVERAGE_DIAGNOSTIC_PASS",
    ]
    assert "CTR" not in output and "POSITION" not in output


def test_shared_guard_forbids_second_post():
    fake = FakeTransport({})
    guard = CountingGSCTransport(fake)
    guard.post("fake", json={}, timeout=1)
    with pytest.raises(GSCReadOnlyProbeError):
        guard.post("fake", json={}, timeout=1)
    assert fake.calls == 1


def test_no_request_cannot_report_success(monkeypatch):
    empty = GSCCollectionCoverage(0, 0, 0, 0, *(Decimal(0) for _ in range(4)))
    monkeypatch.setattr(diagnostic.GoogleSearchConsoleDataSource, "collect_with_coverage",
                        lambda self: GSCCollectionResult((), empty))
    status, output, calls = execute({})
    assert status == 1 and calls == 0
    assert output.startswith("SAFE_FAILURE_CODE=REPORT_COUNT_INVALID\nGSC_API_CALLS_COMPLETED=0\n")


def test_render_failure_keeps_completed_count():
    fake = FakeTransport({})
    def fail_emit(_):
        raise RuntimeError("sensitive rendering exception")
    with pytest.raises(diagnostic.GSCCoverageDiagnosticError) as raised:
        diagnostic.run_diagnostic(observed_at="2026-09-05",
                                  transport_factory=lambda: fake, emit=fail_emit)
    assert raised.value.safe_code == "REPORT_RENDER_FAILED"
    assert raised.value.calls == 1
    assert str(raised.value) == "REPORT_RENDER_FAILED"


def test_unknown_failure_code_is_never_exposed():
    error = diagnostic.GSCCoverageDiagnosticError("sensitive value", 22)
    assert error.safe_code == "UNEXPECTED_DIAGNOSTIC_FAILURE"
    assert error.calls == 0


@pytest.mark.parametrize("mutation,code", [
    (("raw_row_count", 1), "COVERAGE_INVARIANT_INVALID"),
    (("raw_row_count", True), "COVERAGE_INVARIANT_INVALID"),
    (("brand_row_count", 1), "COVERAGE_INVARIANT_INVALID"),
    (("all_rows_clicks", Decimal("NaN")), "REPORT_RENDER_FAILED"),
    (("all_rows_impressions", Decimal("Infinity")), "REPORT_RENDER_FAILED"),
    (("accepted_clicks", Decimal(-1)), "REPORT_RENDER_FAILED"),
])
def test_coverage_is_revalidated_before_emitting(monkeypatch, mutation, code):
    original = diagnostic.GoogleSearchConsoleDataSource.collect_with_coverage
    def corrupt(self):
        result = original(self)
        object.__setattr__(result.coverage, *mutation)
        return result
    monkeypatch.setattr(diagnostic.GoogleSearchConsoleDataSource, "collect_with_coverage", corrupt)
    status, output, calls = execute({})
    assert status == 1 and calls == 1
    assert output.startswith(f"SAFE_FAILURE_CODE={code}\n")
    assert "PROPERTY=" not in output


def test_workflow_security_contract():
    workflow = (ROOT / ".github/workflows/seo-v3-gsc-coverage-diagnostic.yml").read_text()
    assert "on:\n  workflow_dispatch:\n\npermissions: {}" in workflow
    assert "permissions:\n      contents: read\n      id-token: write" in workflow
    for pin in (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093",
    ):
        assert pin in workflow
    for value in (
        "persist-credentials: false", 'python-version: "3.11"',
        "projects/270376484474/locations/global/workloadIdentityPools/github/providers/colixo-seo-bot",
        "colixo-seo-gsc-reader@colixo-seo-agent.iam.gserviceaccount.com",
        "project_id: colixo-seo-agent", "create_credentials_file: true",
        "export_environment_variables: true", "--require-hashes", "--only-binary=:all:",
        "requirements-ga4-probe.lock", "TZ=Europe/Zurich date +%F",
    ):
        assert value in workflow
    assert workflow.count("python -m scripts.v3.gsc_coverage_diagnostic") == 1
    for forbidden in ("schedule:", "push:", "pull_request:", "workflow_call:",
                      "GSC_SERVICE_ACCOUNT_JSON", "credentials_json", "artifact", "cache:"):
        assert forbidden not in workflow


def test_default_runtime_stays_offline_and_diagnostic_is_not_a_runtime_adapter():
    assert tuple(s.value for s in RuntimeState) == ("OFFLINE", "GA4_READ_ONLY", "GSC_READ_ONLY")
    config = load_v3_config()
    assert config.runtime_state is RuntimeState.OFFLINE
    assert isinstance(build_source_adapters(config)["search_console"], SearchConsoleFixtureSource)
    factory = (ROOT / "scripts/v3/source_factory.py").read_text()
    assert "gsc_coverage_diagnostic" not in factory
