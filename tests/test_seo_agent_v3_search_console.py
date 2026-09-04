import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.v3.config import RuntimeState, load_v3_config
from scripts.v3.source_factory import build_source_adapters
from scripts.v3.sources.search_console import (
    GSC_DIMENSIONS,
    GSC_ENDPOINT,
    GSC_PROPERTY,
    GSC_READ_ONLY_SCOPE,
    GSC_REQUEST_TIMEOUT_SECONDS,
    GSC_ROW_LIMIT,
    GSCDataSourceError,
    GoogleSearchConsoleDataSource,
    SearchConsoleFixtureSource,
    classify_search_query_topic,
    create_google_search_console_transport,
)


ROOT = Path(__file__).resolve().parents[1]


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
        if len(self.calls) > 1:
            raise AssertionError("a second Search Console request is forbidden")
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def row(
    query="livraison colis",
    *,
    clicks=4,
    impressions=40,
    ctr=0.1,
    position=7.5,
):
    return {
        "keys": [query],
        "clicks": clicks,
        "impressions": impressions,
        "ctr": ctr,
        "position": position,
    }


def source(payload=None, **overrides):
    transport = FakeTransport(FakeResponse({"rows": [row()]} if payload is None else payload))
    arguments = {"transport": transport, "observed_at": "2026-09-04"}
    arguments.update(overrides)
    return GoogleSearchConsoleDataSource(**arguments), transport


def test_import_has_no_google_auth_client_or_network_side_effects():
    script = """
import builtins
import socket

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'google' or name.startswith('google.'):
        raise AssertionError('Google import is forbidden during module import')
    return original_import(name, *args, **kwargs)

def forbidden_socket(*args, **kwargs):
    raise AssertionError('network access is forbidden during module import')

builtins.__import__ = guarded_import
socket.create_connection = forbidden_socket
import scripts.v3.sources.search_console
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={"PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_fixture_source_behavior_is_preserved():
    fixture = [{
        "topic": "parcel_delivery",
        "query": "livraison colis",
        "clicks": 2,
        "impressions": 20,
        "ctr": 0.1,
        "average_position": 8,
        "evidence": (),
    }]
    signal = SearchConsoleFixtureSource().collect(fixture)[0]
    assert signal.topic == "parcel_delivery"
    assert signal.query == "livraison colis"
    assert signal.clicks == 2


def test_property_scope_and_endpoint_are_exact_and_non_configurable():
    adapter, _ = source()
    assert GSC_PROPERTY == "sc-domain:colixo.ch"
    assert GSC_READ_ONLY_SCOPE == "https://www.googleapis.com/auth/webmasters.readonly"
    assert GSC_ENDPOINT == (
        "https://www.googleapis.com/webmasters/v3/sites/"
        "sc-domain%3Acolixo.ch/searchAnalytics/query"
    )
    assert adapter.property == GSC_PROPERTY
    assert adapter.endpoint == GSC_ENDPOINT


def test_explicit_transport_factory_uses_adc_with_read_only_scope(monkeypatch):
    import google.auth
    import google.auth.transport.requests

    sentinel_credentials = object()
    sentinel_session = object()
    calls = []

    def fake_default(*, scopes):
        calls.append(("default", scopes))
        return sentinel_credentials, "unused-project"

    def fake_session(credentials):
        calls.append(("session", credentials))
        return sentinel_session

    monkeypatch.setattr(google.auth, "default", fake_default)
    monkeypatch.setattr(
        google.auth.transport.requests,
        "AuthorizedSession",
        fake_session,
    )

    assert create_google_search_console_transport() is sentinel_session
    assert calls == [
        ("default", (GSC_READ_ONLY_SCOPE,)),
        ("session", sentinel_credentials),
    ]


@pytest.mark.parametrize(
    "observed_at",
    (None, "", "today", "yesterday", "28daysAgo", "2026-99-99", "2026-9-4"),
)
def test_observed_at_requires_a_real_explicit_iso_date(observed_at):
    with pytest.raises(GSCDataSourceError, match="observed_at"):
        GoogleSearchConsoleDataSource(
            transport=FakeTransport(FakeResponse({})),
            observed_at=observed_at,
        )


def test_dates_are_deterministic_from_observed_at_without_current_time():
    adapter, _ = source()
    assert adapter.start_date == "2026-08-07"
    assert adapter.end_date == "2026-09-01"
    module = (ROOT / "scripts/v3/sources/search_console.py").read_text()
    assert "datetime.now" not in module
    assert "date.today" not in module


def test_collect_makes_one_exact_read_only_query_request():
    adapter, transport = source()
    adapter.collect()

    assert len(transport.calls) == 1
    assert transport.calls[0] == {
        "url": GSC_ENDPOINT,
        "json": {
            "startDate": "2026-08-07",
            "endDate": "2026-09-01",
            "dimensions": ["query"],
            "type": "web",
            "dataState": "final",
            "rowLimit": 25000,
            "startRow": 0,
        },
        "timeout": GSC_REQUEST_TIMEOUT_SECONDS,
    }
    assert GSC_DIMENSIONS == ("query",)
    assert GSC_ROW_LIMIT == 25000


def test_valid_row_maps_metrics_and_high_confidence_evidence():
    adapter, _ = source({"rows": [row(
        "livraison colis suisse",
        clicks="4.5",
        impressions="40",
        ctr="0.1125",
        position="7.25",
    )]})
    signal = adapter.collect()[0]

    assert signal.topic == "parcel_delivery"
    assert signal.query == "livraison colis suisse"
    assert signal.clicks == 4.5
    assert signal.impressions == 40
    assert signal.ctr == 0.1125
    assert signal.average_position == 7.25
    evidence = signal.evidence[0]
    assert evidence.source == "google_search_console"
    assert evidence.observed_at == "2026-09-04"
    assert evidence.metric == "search_query_aggregate"
    assert evidence.confidence.value == "high"
    assert set(evidence.fact) == {
        "query",
        "clicks",
        "impressions",
        "ctr",
        "average_position",
        "date_range",
        "property",
        "provenance",
    }
    assert evidence.fact["date_range"] == {
        "start_date": "2026-08-07",
        "end_date": "2026-09-01",
    }
    assert evidence.fact["property"] == "sc-domain:colixo.ch"


@pytest.mark.parametrize(
    ("query", "topic"),
    (
        ("livraison", "general_delivery"),
        ("Livraison Suisse romande", "general_delivery"),
        ("livraison entreprise", "business_delivery"),
        ("livraison colis", "parcel_delivery"),
        ("transport colis Fribourg", "parcel_delivery"),
        ("livraison de vins", "wine_delivery"),
        ("envoi sécurisé horlogerie", "secure_watch_delivery"),
    ),
)
def test_topic_classification_is_deterministic_and_reviewable(query, topic):
    assert classify_search_query_topic(query) == topic
    assert classify_search_query_topic(query) == topic


def test_unknown_query_cannot_create_a_commercial_signal():
    adapter, _ = source({"rows": [row("météo demain") ]})
    assert classify_search_query_topic("météo demain") is None
    assert adapter.collect() == ()


@pytest.mark.parametrize(
    "query",
    (
        "livraison colis jean.dupont@example.com",
        "livraison colis +41 79 123 45 67",
        "transport colis 0041 26 555 12 34",
    ),
)
def test_obvious_email_and_phone_queries_are_rejected_without_signal(query):
    adapter, _ = source({"rows": [row(query)]})
    assert classify_search_query_topic(query) is None
    assert adapter.collect() == ()


@pytest.mark.parametrize("keys", ([], ["one", "two"], "query", None))
def test_malformed_query_key_count_fails_closed(keys):
    malformed = row()
    malformed["keys"] = keys
    adapter, _ = source({"rows": [malformed]})
    with pytest.raises(GSCDataSourceError, match="query key"):
        adapter.collect()


@pytest.mark.parametrize(
    ("metric", "value"),
    (
        ("clicks", -1),
        ("clicks", True),
        ("impressions", None),
        ("ctr", "NaN"),
        ("ctr", "Infinity"),
        ("ctr", 1.01),
        ("position", 0),
        ("position", "-Infinity"),
    ),
)
def test_malformed_nonfinite_or_invalid_metrics_fail_closed(metric, value):
    malformed = row()
    malformed[metric] = value
    adapter, _ = source({"rows": [malformed]})
    with pytest.raises(GSCDataSourceError, match="metric"):
        adapter.collect()


def test_missing_metric_and_unexpected_row_field_fail_closed():
    missing = row()
    del missing["clicks"]
    adapter, _ = source({"rows": [missing]})
    with pytest.raises(GSCDataSourceError, match="schema"):
        adapter.collect()

    extra = row()
    extra["country"] = "CH"
    adapter, _ = source({"rows": [extra]})
    with pytest.raises(GSCDataSourceError, match="schema"):
        adapter.collect()


@pytest.mark.parametrize("payload", ([], "invalid", {"rows": "invalid"}))
def test_malformed_response_schema_fails_closed(payload):
    adapter, _ = source(payload)
    with pytest.raises(GSCDataSourceError, match="schema"):
        adapter.collect()


def test_empty_search_console_response_is_a_valid_empty_result():
    adapter, transport = source({})
    assert adapter.collect() == ()
    assert len(transport.calls) == 1


def test_api_and_json_failures_are_sanitized_and_never_retried():
    secret_text = "raw-private-api-detail"
    transport = FakeTransport(RuntimeError(secret_text))
    adapter = GoogleSearchConsoleDataSource(
        transport=transport,
        observed_at="2026-09-04",
    )
    with pytest.raises(GSCDataSourceError) as raised:
        adapter.collect()
    assert secret_text not in str(raised.value)
    assert len(transport.calls) == 1

    transport = FakeTransport(FakeResponse(None, json_error=ValueError(secret_text)))
    adapter = GoogleSearchConsoleDataSource(
        transport=transport,
        observed_at="2026-09-04",
    )
    with pytest.raises(GSCDataSourceError) as raised:
        adapter.collect()
    assert secret_text not in str(raised.value)
    assert len(transport.calls) == 1


def test_non_success_http_is_sanitized_without_retry():
    transport = FakeTransport(FakeResponse({"error": "private"}, status_code=403))
    adapter = GoogleSearchConsoleDataSource(
        transport=transport,
        observed_at="2026-09-04",
    )
    with pytest.raises(GSCDataSourceError, match="API request failed") as raised:
        adapter.collect()
    assert "403" not in str(raised.value)
    assert "private" not in str(raised.value)
    assert len(transport.calls) == 1


def test_current_runtimes_and_search_console_factory_remain_unchanged(tmp_path):
    offline = load_v3_config()
    assert offline.runtime_state is RuntimeState.OFFLINE
    assert offline.phase_1_sources.search_console == "local_fixture"
    assert offline.network_policy.search_console == "deny"
    assert isinstance(build_source_adapters(offline)["search_console"], SearchConsoleFixtureSource)

    payload = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    payload["mode"]["network_enabled"] = True
    payload["phase_1_sources"]["analytics"] = "ga4_data_api"
    payload["network_policy"]["sources"]["analytics"] = "allow"
    payload["ga4_data_api"]["enabled"] = True
    path = tmp_path / "ga4-read-only.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    ga4_read_only = load_v3_config(path)

    assert ga4_read_only.runtime_state is RuntimeState.GA4_READ_ONLY
    assert ga4_read_only.phase_1_sources.search_console == "local_fixture"
    assert ga4_read_only.network_policy.search_console == "deny"
    adapters = build_source_adapters(
        ga4_read_only,
        observed_at="2026-09-04",
        ga4_client_factory=lambda: object(),
    )
    assert isinstance(adapters["search_console"], SearchConsoleFixtureSource)


def test_live_search_console_source_is_unreachable_from_runtime_factory():
    factory_source = (ROOT / "scripts/v3/source_factory.py").read_text()
    config_source = (ROOT / "scripts/v3/config.py").read_text()
    assert "GoogleSearchConsoleDataSource" not in factory_source
    assert "create_google_search_console_transport" not in factory_source
    assert "GSC_READ_ONLY" not in config_source
    assert tuple(state.value for state in RuntimeState) == ("OFFLINE", "GA4_READ_ONLY")


def test_no_workflow_or_legacy_v2_auth_path_is_changed_by_foundation():
    changed = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert all(not path.startswith(".github/workflows/") for path in changed)
    assert "scripts/gsc_client.py" not in changed
