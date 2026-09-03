import builtins
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.v3.sources.analytics import (
    DEFAULT_GA4_PAGE_TOPICS,
    GA4_CHANNEL_DIMENSIONS,
    GA4_METRICS,
    GA4_PAGE_DIMENSIONS,
    GA4_PROPERTY_ID,
    GA4_RESOURCE,
    ORGANIC_SEARCH_CHANNEL,
    AnalyticsFixtureSource,
    GA4DataSourceError,
    GoogleAnalyticsDataSource,
    create_google_analytics_data_client,
)


ROOT = Path(__file__).resolve().parents[1]


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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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


def fake_client(page_rows=(), channel_rows=None):
    if channel_rows is None:
        channel_rows = (row((ORGANIC_SEARCH_CHANNEL,), ("20", "15", "3")),)
    return FakeClient(
        response(GA4_CHANNEL_DIMENSIONS, channel_rows),
        response(GA4_PAGE_DIMENSIONS, page_rows),
    )


def adapter(client, **overrides):
    arguments = {
        "property_id": GA4_PROPERTY_ID,
        "client": client,
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
    }
    arguments.update(overrides)
    return GoogleAnalyticsDataSource(**arguments)


def test_real_config_keeps_ga4_adapter_disabled_and_fixture_active():
    config = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    assert config["ga4_data_api"]["enabled"] is False
    assert config["ga4_data_api"]["property_id"] == "552715460"
    assert config["ga4_data_api"]["resource"] == "properties/552715460"
    assert config["phase_1_sources"]["analytics"] == "local_fixture"
    assert config["mode"]["network_enabled"] is False


def test_property_and_resource_are_exact_and_public_identifiers():
    source = adapter(fake_client())
    assert GA4_PROPERTY_ID == "552715460"
    assert GA4_RESOURCE == "properties/552715460"
    assert source.property_id == GA4_PROPERTY_ID
    assert source.property_resource == GA4_RESOURCE


@pytest.mark.parametrize("property_id", ["", " ", "not-a-property", "-1", "0", None])
def test_missing_or_invalid_property_id_fails_closed(property_id):
    with pytest.raises(GA4DataSourceError, match="property_id"):
        GoogleAnalyticsDataSource(
            property_id=property_id,
            client=fake_client(),
            start_date="2026-08-01",
            end_date="2026-08-31",
        )


def test_missing_client_or_date_range_fails_closed():
    with pytest.raises(GA4DataSourceError, match="injected"):
        adapter(None)
    with pytest.raises(GA4DataSourceError, match="date range"):
        adapter(fake_client(), start_date="")


def test_channel_request_uses_only_the_approved_dimension_metrics_and_filter():
    request = adapter(fake_client()).channel_request()
    assert request["property"] == "properties/552715460"
    assert tuple(item["name"] for item in request["dimensions"]) == (
        "sessionDefaultChannelGroup",
    )
    assert tuple(item["name"] for item in request["metrics"]) == (
        "sessions", "engagedSessions", "keyEvents",
    )
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


def test_page_request_uses_page_path_and_never_pii_or_query_dimensions():
    request = adapter(fake_client()).page_request()
    assert tuple(item["name"] for item in request["dimensions"]) == (
        "pagePath", "sessionDefaultChannelGroup",
    )
    serialized = json.dumps(request)
    for prohibited in (
        "pageLocation", "userPseudoId", "transactionId", "clientId",
        "email", "phone", "city", "deviceId",
    ):
        assert prohibited not in serialized


def test_fake_client_only_and_no_socket_access(monkeypatch):
    def refuse_network(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", refuse_network)
    client = fake_client((row(("/", ORGANIC_SEARCH_CHANNEL), ("10", "7", "2")),))
    signals = adapter(client).collect()
    assert len(client.requests) == 2
    assert signals[0].organic_sessions == 10


def test_ga4_metrics_map_to_existing_traffic_signal_and_evidence():
    page_rows = (
        row(("/business-plus", ORGANIC_SEARCH_CHANNEL), ("12", "9", "2.5")),
    )
    signal = adapter(fake_client(page_rows)).collect()[0]
    assert signal.topic == "business_delivery"
    assert signal.organic_sessions == 12
    assert signal.engaged_sessions == 9
    assert signal.conversions == 2.5
    proof = signal.evidence[0]
    assert proof.source == "google_analytics_4"
    assert proof.reference == "properties/552715460"
    assert proof.fact["provenance"] == "ga4_data_api"
    assert proof.fact["property"] == "552715460"
    assert proof.fact["date_range"] == {
        "start_date": "2026-08-01", "end_date": "2026-08-31",
    }
    assert proof.fact["dimensions"] == GA4_PAGE_DIMENSIONS
    assert proof.fact["metrics"] == GA4_METRICS


def test_multiple_rows_per_topic_are_aggregated_deterministically():
    page_topics = {
        "/parcel-a": "parcel_delivery",
        "/parcel-b": "parcel_delivery",
        "/wine": "wine_delivery",
    }
    rows = (
        row(("/parcel-b", ORGANIC_SEARCH_CHANNEL), ("4.2", "3", "1")),
        row(("/wine", ORGANIC_SEARCH_CHANNEL), ("5", "4", "0")),
        row(("/parcel-a", ORGANIC_SEARCH_CHANNEL), ("6.8", "5", "2")),
    )
    first = adapter(fake_client(rows), page_topics=page_topics).collect()
    second = adapter(fake_client(tuple(reversed(rows))), page_topics=page_topics).collect()
    assert first == second
    assert tuple(item.topic for item in first) == ("parcel_delivery", "wine_delivery")
    assert first[0].organic_sessions == 11
    assert first[0].engaged_sessions == 8
    assert first[0].conversions == 3


def test_unknown_and_legal_pages_are_explicitly_excluded():
    rows = (
        row(("/unknown-public-page", ORGANIC_SEARCH_CHANNEL), ("100", "90", "20")),
        row(("/protection-donnees", ORGANIC_SEARCH_CHANNEL), ("100", "90", "20")),
    )
    assert adapter(fake_client(rows)).collect() == ()


def test_unknown_pages_cannot_create_a_commercial_topic():
    custom_mapping = dict(DEFAULT_GA4_PAGE_TOPICS)
    assert "/unknown-public-page" not in custom_mapping
    result = adapter(
        fake_client((row(("/unknown-public-page", ORGANIC_SEARCH_CHANNEL), ("1", "1", "1")),)),
        page_topics=custom_mapping,
    ).collect()
    assert result == ()


@pytest.mark.parametrize("unsafe_path", ["/parcel?email=x", "/parcel#phone", "not-a-path", ""])
def test_query_strings_fragments_and_malformed_paths_fail_closed(unsafe_path):
    client = fake_client((row((unsafe_path, ORGANIC_SEARCH_CHANNEL), ("1", "1", "0")),))
    with pytest.raises(GA4DataSourceError, match="unsafe pagePath|row dimensions"):
        adapter(client).collect()


def test_unexpected_channel_or_dimensions_fail_closed():
    wrong_channel = fake_client(
        (row(("/", "Direct"), ("1", "1", "0")),),
    )
    with pytest.raises(GA4DataSourceError, match="unexpected channel"):
        adapter(wrong_channel).collect()

    wrong_headers = FakeClient(
        response(("pageLocation",)),
        response(GA4_PAGE_DIMENSIONS),
    )
    with pytest.raises(GA4DataSourceError, match="dimensions"):
        adapter(wrong_headers).collect()


@pytest.mark.parametrize("invalid", [None, "", "NaN", "Infinity", "-0.1", True])
def test_invalid_numeric_values_fail_closed(invalid):
    rows = (row(("/", ORGANIC_SEARCH_CHANNEL), (invalid, "1", "0")),)
    with pytest.raises(GA4DataSourceError, match="metric"):
        adapter(fake_client(rows)).collect()


def test_missing_or_changed_metric_fails_closed():
    missing_value = (row(("/", ORGANIC_SEARCH_CHANNEL), ("1", "1")),)
    with pytest.raises(GA4DataSourceError, match="row metrics"):
        adapter(fake_client(missing_value)).collect()

    wrong_metric_headers = FakeClient(
        response(GA4_CHANNEL_DIMENSIONS, metrics=("sessions", "engagedSessions", "conversions")),
        response(GA4_PAGE_DIMENSIONS),
    )
    with pytest.raises(GA4DataSourceError, match="metrics"):
        adapter(wrong_metric_headers).collect()


def test_api_failure_is_sanitized_and_fails_closed():
    client = FakeClient(RuntimeError("remote details must not escape"))
    with pytest.raises(GA4DataSourceError, match="GA4 Data API request failed") as failure:
        adapter(client).collect()
    assert "remote details" not in str(failure.value)


def test_future_live_factory_fails_closed_when_adc_is_unavailable(monkeypatch):
    real_import = builtins.__import__

    class MissingCredentialsClient:
        def __init__(self):
            raise RuntimeError("credential internals must not escape")

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "google.analytics.data_v1beta":
            return SimpleNamespace(BetaAnalyticsDataClient=MissingCredentialsClient)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(GA4DataSourceError, match="Credentials are unavailable") as failure:
        create_google_analytics_data_client()
    assert "credential internals" not in str(failure.value)


def test_fixture_adapter_remains_unchanged_and_usable():
    signal = AnalyticsFixtureSource().collect(({
        "topic": "fixture-topic",
        "organic_sessions": 3,
        "engaged_sessions": 2,
        "conversions": 1,
    },))[0]
    assert (signal.topic, signal.organic_sessions, signal.engaged_sessions, signal.conversions) == (
        "fixture-topic", 3, 2, 1,
    )


def test_dependency_and_auth_contract_are_keyless_and_workflow_free():
    requirements = (ROOT / "requirements.txt").read_text()
    source = (ROOT / "scripts/v3/sources/analytics.py").read_text()
    assert "google-analytics-data>=0.22.0,<0.23.0" in requirements
    assert "BetaAnalyticsDataClient()" in source
    assert "Application Default Credentials" in source
    prohibited = (
        "GA4_SERVICE_ACCOUNT_JSON", "credentials_json", "private_key",
        "oauth_token", "service_account_info",
    )
    repository_text = "\n".join(
        path.read_text(errors="ignore")
        for root in (ROOT / "scripts/v3", ROOT / "config", ROOT / "docs")
        for path in root.rglob("*")
        if path.is_file()
    )
    assert all(value not in repository_text for value in prohibited)


def test_current_agent_does_not_construct_or_activate_ga4_adapter():
    agent_source = (ROOT / "scripts/v3/agent.py").read_text()
    assert "GoogleAnalyticsDataSource" not in agent_source
    assert '"analytics": AnalyticsFixtureSource()' in agent_source
