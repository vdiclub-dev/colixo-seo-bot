"""Manual runtime gate tests; only synthetic responses and injected transport."""

import ast
from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.v3 import gsc_runtime_readonly_report as runner
from scripts.v3.config import RuntimeState, load_v3_config
from scripts.v3.gsc_readonly_probe import CountingGSCTransport, GSCReadOnlyProbeError

ROOT = Path(__file__).resolve().parents[1]
RAW = ("livraison colis synthétique-zeta", "colixo marque-zeta", "astronomie-zeta", "colixo test@example.invalid")


def row(query):
    return dict(keys=[query], clicks=2, impressions=20, ctr=0.1, position=8)


class FakeTransport:
    def __init__(self, rows=(), *, payload=None, status=200):
        self.payload = {"rows": rows} if payload is None else payload
        self.status = status
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        if isinstance(self.payload, Exception):
            raise self.payload
        return SimpleNamespace(status_code=self.status, json=lambda: self.payload)


def forbidden():
    raise AssertionError("network/authentication construction forbidden")


@pytest.fixture(autouse=True)
def no_default_clients(monkeypatch):
    monkeypatch.setattr("scripts.v3.source_factory.create_google_analytics_data_client", forbidden)
    monkeypatch.setattr("scripts.v3.source_factory.create_google_search_console_transport", forbidden)


def execute(t):
    output = []
    code = runner.main(["--observed-at", "2026-09-05"], transport_factory=lambda: t, emit=output.append)
    return code, "\n".join(output)


def test_safe_success_exact_coverage_and_privacy():
    t = FakeTransport([row(q) for q in RAW])
    code, output = execute(t)
    assert code == 0 and len(t.calls) == 1
    expected = dict(RUNTIME_STATE="GSC_READ_ONLY", PROPERTY="sc-domain:colixo.ch", AUTH_MODE="WIF",
                    OBSERVED_AT="2026-09-05", DATE_RANGE_START="2026-08-08", DATE_RANGE_END="2026-09-02",
                    GSC_API_CALLS="1", TOPIC_COUNT="1", RECOMMENDATION_COUNT="1", RAW_ROW_COUNT="4",
                    ACCEPTED_SIGNAL_COUNT="1", BRAND_ROW_COUNT="1", UNMAPPED_ROW_COUNT="1",
                    PII_FILTERED_ROW_COUNT="1", ALL_ROWS_CLICKS="8", ALL_ROWS_IMPRESSIONS="80",
                    ACCEPTED_CLICKS="2", ACCEPTED_IMPRESSIONS="20", BRAND_CLICKS="2", BRAND_IMPRESSIONS="20",
                    ROW_LIMIT="25000", ROW_LIMIT_REACHED="false", TOTALS_SCOPE="returned_query_rows_non_exhaustive")
    assert output.split("\n# Colixo")[0] == "\n".join(f"{k}={v}" for k,v in expected.items())
    assert output.endswith("FINAL_VERDICT=V3_GSC_READONLY_RUNTIME_PASS")
    assert t.calls[0][1] == dict(startDate="2026-08-08", endDate="2026-09-02", dimensions=["query"],
                               type="web", dataState="final", rowLimit=25000, startRow=0)
    for text in (*RAW, "commercial_query_redacted", "Evidence.fact", "keys", "example.invalid"):
        assert text not in output
    assert "search_console=gsc_search_analytics_api(read-only)" in output
    assert execute(FakeTransport([row(q) for q in reversed(RAW)]))[1] == output


@pytest.mark.parametrize("queries", [[], [RAW[1]], [RAW[2]], [RAW[3]]])
def test_zero_commercial_signals_success(queries):
    code, output = execute(FakeTransport([row(q) for q in queries]))
    assert code == 0
    for line in ("ACCEPTED_SIGNAL_COUNT=0", "TOPIC_COUNT=0", "RECOMMENDATION_COUNT=0"):
        assert line in output
    assert output.endswith("FINAL_VERDICT=" + runner.PASS_VERDICT)


@pytest.mark.parametrize("value", [None, "", "yesterday", "2026-99-99", "2026-02-30", "2026-9-05", "0001-01-01"])
def test_invalid_date_before_factory(value):
    output = []
    assert runner.main(["--observed-at", value], transport_factory=forbidden, emit=output.append) == 1
    assert output == ["SAFE_FAILURE_CODE=OBSERVED_AT_INVALID\nGSC_API_CALLS_COMPLETED=0\nFINAL_VERDICT=" + runner.FAIL_VERDICT]


@pytest.mark.parametrize("args", [[], ["--observed-at"], ["--date", "2026-09-05"], ["--observed-at", "2026-09-05", "extra"]])
def test_bad_cli(args):
    out = []
    assert runner.main(args, transport_factory=forbidden, emit=out.append) == 1
    assert "SAFE_FAILURE_CODE=OBSERVED_AT_INVALID" in out[0]


def test_exact_config_state_and_no_ga4(monkeypatch):
    actual = runner.MarketIntelligenceAgent
    def spy(**kwargs):
        assert kwargs["config_path"] == ROOT / "config/seo_agent_v3_gsc_readonly.json"
        a = actual(**kwargs)
        assert a.config.runtime_state is RuntimeState.GSC_READ_ONLY
        assert a.config.phase_1_sources.analytics == "local_fixture"
        assert a.config.network_policy.analytics == "deny"
        return a
    monkeypatch.setattr(runner, "MarketIntelligenceAgent", spy)
    assert execute(FakeTransport())[0] == 0


@pytest.mark.parametrize("state", [RuntimeState.OFFLINE, RuntimeState.GA4_READ_ONLY])
def test_wrong_runtime_refused(monkeypatch, state):
    monkeypatch.setattr(runner, "MarketIntelligenceAgent", lambda **kwargs: SimpleNamespace(config=SimpleNamespace(runtime_state=state)))
    code, output = execute(FakeTransport())
    assert code == 1 and "SAFE_FAILURE_CODE=RUNTIME_CONSTRUCTION_FAILED" in output


def test_second_post_guard():
    t = FakeTransport()
    guard = CountingGSCTransport(t)
    guard.post("synthetic", json={}, timeout=1)
    with pytest.raises(GSCReadOnlyProbeError):
        guard.post("synthetic", json={}, timeout=1)
    assert len(t.calls) == 1


def test_second_agent_collection_refused(monkeypatch):
    actual = runner.MarketIntelligenceAgent
    class Twice(actual):
        def run(self, fixtures):
            super().run(fixtures)
            return super().run(fixtures)
    monkeypatch.setattr(runner, "MarketIntelligenceAgent", Twice)
    t = FakeTransport()
    code, output = execute(t)
    assert code == 1 and len(t.calls) == 1
    assert output == "SAFE_FAILURE_CODE=REPORT_COUNT_INVALID\nGSC_API_CALLS_COMPLETED=1\nFINAL_VERDICT=" + runner.FAIL_VERDICT


@pytest.mark.parametrize("t,expected,calls", [
    (FakeTransport(payload=[]), "GSC_RESPONSE_INVALID", 1),
    (FakeTransport(payload={"rows":"private-marker"}), "GSC_RESPONSE_INVALID", 1),
    (FakeTransport([{"keys":["private-marker"]}]), "GSC_ROW_INVALID", 1),
    (FakeTransport(status=403), "GSC_API_REQUEST_FAILED", 1),
    (FakeTransport(payload=RuntimeError("private-marker")), "GSC_API_REQUEST_FAILED", 0),
])
def test_sanitized_failure_no_retry(t, expected, calls):
    code, output = execute(t)
    assert code == 1 and len(t.calls) == 1
    assert output == f"SAFE_FAILURE_CODE={expected}\nGSC_API_CALLS_COMPLETED={calls}\nFINAL_VERDICT={runner.FAIL_VERDICT}"


def test_transport_creation_sanitized():
    out = []
    assert runner.main(["--observed-at", "2026-09-05"], transport_factory=forbidden, emit=out.append) == 1
    assert out == ["SAFE_FAILURE_CODE=TRANSPORT_CREATION_FAILED\nGSC_API_CALLS_COMPLETED=0\nFINAL_VERDICT=" + runner.FAIL_VERDICT]


def test_construction_failure_sanitized(monkeypatch):
    def broken(**kwargs): raise RuntimeError("private-marker")
    monkeypatch.setattr(runner, "MarketIntelligenceAgent", broken)
    code, output = execute(FakeTransport())
    assert code == 1
    assert output == "SAFE_FAILURE_CODE=RUNTIME_CONSTRUCTION_FAILED\nGSC_API_CALLS_COMPLETED=0\nFINAL_VERDICT=" + runner.FAIL_VERDICT


def test_zero_posts_fail_closed(monkeypatch):
    actual = runner.MarketIntelligenceAgent
    class NoCollection(actual):
        def run(self, fixtures): return None
    monkeypatch.setattr(runner, "MarketIntelligenceAgent", NoCollection)
    code, output = execute(FakeTransport())
    assert code == 1
    assert output == "SAFE_FAILURE_CODE=REPORT_COUNT_INVALID\nGSC_API_CALLS_COMPLETED=0\nFINAL_VERDICT=" + runner.FAIL_VERDICT


def test_unexpected_failure_sanitized(monkeypatch):
    actual = runner.MarketIntelligenceAgent
    class Broken(actual):
        def run(self, fixtures):
            super().run(fixtures)
            raise RuntimeError("private-marker")
    monkeypatch.setattr(runner, "MarketIntelligenceAgent", Broken)
    code, output = execute(FakeTransport())
    assert code == 1
    assert output == "SAFE_FAILURE_CODE=UNEXPECTED_RUNTIME_FAILURE\nGSC_API_CALLS_COMPLETED=1\nFINAL_VERDICT=" + runner.FAIL_VERDICT


def test_emit_failure_safe_code():
    def broken_emit(text): raise RuntimeError("private-marker")
    with pytest.raises(runner.GSCRuntimeReportError) as e:
        runner.run_runtime_report(observed_at="2026-09-05", transport_factory=FakeTransport, emit=broken_emit)
    assert e.value.safe_code == "REPORT_RENDER_FAILED"
    assert e.value.calls == 1 and "private-marker" not in str(e.value)


@pytest.mark.parametrize("change", [
    {"raw_row_count": 1}, {"brand_row_count": -1}, {"row_limit": 10},
    {"row_limit_reached": True}, {"totals_scope": "complete"},
    {"all_rows_clicks": Decimal("NaN")}, {"all_rows_impressions": "private-marker"},
])
def test_bad_coverage_no_partial_output(monkeypatch, change):
    actual = runner.MarketIntelligenceAgent
    class Altered(actual):
        def run(self, fixtures):
            r = super().run(fixtures)
            return replace(r, gsc_coverage=replace(r.gsc_coverage, **change))
    monkeypatch.setattr(runner, "MarketIntelligenceAgent", Altered)
    code, output = execute(FakeTransport())
    assert code == 1
    assert output == "SAFE_FAILURE_CODE=COVERAGE_INVALID\nGSC_API_CALLS_COMPLETED=1\nFINAL_VERDICT=" + runner.FAIL_VERDICT


@pytest.mark.parametrize("field,value,expected", [("gsc_coverage",None,"COVERAGE_INVALID"), ("markdown",{},"REPORT_RENDER_FAILED")])
def test_missing_coverage_or_invalid_markdown(monkeypatch, field, value, expected):
    actual = runner.MarketIntelligenceAgent
    class Altered(actual):
        def run(self, fixtures): return replace(super().run(fixtures), **{field:value})
    monkeypatch.setattr(runner, "MarketIntelligenceAgent", Altered)
    code, output = execute(FakeTransport())
    assert code == 1 and output.startswith("SAFE_FAILURE_CODE=" + expected)
    assert "RUNTIME_STATE=" not in output


@pytest.mark.parametrize("count", [24999, 25000])
def test_row_limit_metadata(count):
    t = FakeTransport([row(RAW[1])] * count)
    code, output = execute(t)
    assert code == 0 and len(t.calls) == 1
    assert f"RAW_ROW_COUNT={count}" in output
    assert f"ROW_LIMIT_REACHED={str(count == 25000).lower()}" in output
    assert "TOTALS_SCOPE=returned_query_rows_non_exhaustive" in output


def test_runner_has_no_generic_serialization_or_query_access():
    source = (ROOT / "scripts/v3/gsc_runtime_readonly_report.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute): assert node.attr not in {"query", "fact"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"repr", "asdict"}
    for forbidden_text in ("commercial_query_redacted", "datetime.now", "date.today", "json.dumps"):
        assert forbidden_text not in source


def test_manual_workflow_security():
    path = ROOT / ".github/workflows/seo-v3-gsc-runtime-readonly-report.yml"
    source = path.read_text()
    assert source.startswith("name: Colixo SEO Agent V3 GSC Runtime Read-only Report\n\non:\n  workflow_dispatch:\n\npermissions: {}\n\njobs:\n  gsc-runtime-readonly-report:\n")
    assert source.count("workflow_dispatch:") == 1
    assert "    permissions:\n      contents: read\n      id-token: write\n\n    steps:" in source
    assert source.count("permissions:") == 2
    assert [line.strip().removeprefix("uses: ").split()[0] for line in source.splitlines() if "uses: " in line] == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093"]
    assert "          persist-credentials: false" in source
    assert '          python-version: "3.11"' in source
    for key, value in dict(
        workload_identity_provider="projects/270376484474/locations/global/workloadIdentityPools/github/providers/colixo-seo-bot",
        service_account="colixo-seo-gsc-reader@colixo-seo-agent.iam.gserviceaccount.com",
        project_id="colixo-seo-agent", create_credentials_file="true", export_environment_variables="true").items():
        assert f"          {key}: {value}" in source
    for required in ("--require-hashes", "--only-binary=:all:", "requirements-ga4-probe.lock",
                     "TZ=Europe/Zurich date +%F", '--observed-at "$OBSERVED_AT"', "$GITHUB_STEP_SUMMARY"):
        assert required in source
    assert source.count("python -m scripts.v3.gsc_runtime_readonly_report") == 1
    for forbidden_text in ("upload-artifact", "schedule:", "workflow_call:", "push:", "pull_request:", "credentials_json",
                           "secrets.", "colixo-seo-ga4-reader", "cache:"):
        assert forbidden_text not in source


def test_default_runtime_remains_offline():
    assert load_v3_config().runtime_state is RuntimeState.OFFLINE


@pytest.mark.parametrize("path,digest", [
    (".github/workflows/seo-v3-ga4-runtime-readonly-report.yml", "e8de0086377591b1251a70335cee88d09b2d0d30b410f8bd99bfd97ade5f315c"),
    ("config/seo_agent_v3.json", "4310d0cd6881ecc0cf5155c702f892ef9ba8196a43febb93f94567ef2be12ecb"),
    ("scripts/v3/sources/search_console.py", "eb16bdfa9b7fca30c9196238ab908115cc53eab5b74f2634e54a02be23b0b6c5"),
    ("requirements-ga4-probe.lock", "07a1283869f7a8fbc12d58410761747a5ca5236e3fa22813b7e56f458bb1af9d"),
])
def test_foundation_contract_fingerprints(path, digest):
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest
