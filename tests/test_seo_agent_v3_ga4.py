import builtins
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.v3.models import DimensionLevel
from scripts.v3.scoring import recommendation_for, score_opportunity
from scripts.v3.sources.analytics import (
    DEFAULT_GA4_LANDING_PAGE_TOPICS,
    GA4_METRICS,
    GA4_LANDING_PAGE_DIMENSIONS,
    GA4_PROPERTY_ID,
    GA4_RESOURCE,
    GA4_TOTAL_DIMENSION_VALUE,
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
    totals: tuple


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def run_report(self, *, request):
        self.requests.append(request)
        if len(self.requests) > 1:
            raise AssertionError("a second GA4 request is forbidden")
        response = self.response
        if isinstance(response, Exception):
            raise response
        return response


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


def fake_client(landing_rows=(), total_metrics=("20", "15", "3"), total_rows=None):
    if total_rows is None:
        total_rows = (row(
            (GA4_TOTAL_DIMENSION_VALUE, GA4_TOTAL_DIMENSION_VALUE),
            total_metrics,
        ),)
    return FakeClient(
        response(rows=landing_rows, totals=total_rows),
    )


def adapter(client, **overrides):
    arguments = {
        "property_id": GA4_PROPERTY_ID,
        "client": client,
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
        "observed_at": "2026-09-03",
    }
    arguments.update(overrides)
    return GoogleAnalyticsDataSource(**arguments)


def test_real_config_keeps_ga4_adapter_disabled_and_fixture_active():
    config = json.loads((ROOT / "config/seo_agent_v3.json").read_text())
    assert config["ga4_data_api"]["enabled"] is False
    assert config["ga4_data_api"]["property_id"] == "552715460"
    assert config["ga4_data_api"]["resource"] == "properties/552715460"
    assert "landing_page_topics" in config["ga4_data_api"]
    assert "page_topics" not in config["ga4_data_api"]
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
            observed_at="2026-09-03",
        )


def test_missing_client_or_date_range_fails_closed():
    with pytest.raises(GA4DataSourceError, match="injected"):
        adapter(None)
    with pytest.raises(GA4DataSourceError, match="date range"):
        adapter(fake_client(), start_date="")


@pytest.mark.parametrize(
    "observed_at", ["", "yesterday", "today", "28daysAgo", "2026-99-99", None]
)
def test_observed_at_requires_a_real_explicit_iso_date(observed_at):
    with pytest.raises(GA4DataSourceError, match="observed_at"):
        adapter(fake_client(), observed_at=observed_at)


def test_relative_ga4_date_range_remains_supported_without_becoming_observed_at():
    source = adapter(
        fake_client(),
        start_date="28daysAgo",
        end_date="yesterday",
        observed_at="2026-09-03",
    )
    request = source.report_request()
    assert request["date_ranges"] == [{
        "start_date": "28daysAgo",
        "end_date": "yesterday",
    }]
    assert source.observed_at == "2026-09-03"
    assert "datetime.now" not in (
        ROOT / "scripts/v3/sources/analytics.py"
    ).read_text()


def test_report_request_is_exact_single_report_contract():
    request = adapter(fake_client()).report_request()
    assert request["property"] == "properties/552715460"
    assert request["date_ranges"] == [{
        "start_date": "2026-08-01",
        "end_date": "2026-08-31",
    }]
    assert tuple(item["name"] for item in request["dimensions"]) == (
        "landingPage", "sessionDefaultChannelGroup",
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
    assert request["metric_aggregations"] == ["TOTAL"]
    serialized = json.dumps(request)
    for prohibited in (
        "pagePath", "landingPagePlusQueryString", "pageLocation",
        "pagePathPlusQueryString", "userPseudoId", "transactionId",
        "clientId", "email", "phone", "city", "deviceId",
    ):
        assert prohibited not in serialized


def test_obsolete_two_report_paths_are_removed():
    source = (ROOT / "scripts/v3/sources/analytics.py").read_text()
    assert "channel_request" not in source
    assert "_parse_channel_response" not in source
    assert GA4_TOTAL_DIMENSION_VALUE == "RESERVED_TOTAL"


def test_fake_client_only_and_no_socket_access(monkeypatch):
    def refuse_network(*_args, **_kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", refuse_network)
    client = fake_client((row(("/", ORGANIC_SEARCH_CHANNEL), ("10", "7", "2")),))
    signals = adapter(client).collect()
    assert len(client.requests) == 1
    assert signals[0].organic_sessions == 10


def test_zero_traffic_uses_one_same_response_zero_total():
    client = fake_client(total_metrics=("0", "0", "0"))

    assert adapter(client).collect() == ()
    assert len(client.requests) == 1


def test_ga4_metrics_map_to_existing_traffic_signal_and_evidence():
    landing_rows = (
        row(("/business-plus", ORGANIC_SEARCH_CHANNEL), ("12", "9", "2.5")),
    )
    signal = adapter(fake_client(landing_rows)).collect()[0]
    assert signal.topic == "business_delivery"
    assert signal.organic_sessions == 12
    assert signal.engaged_sessions == 9
    assert signal.conversions is None
    proof = signal.evidence[0]
    assert proof.source == "google_analytics_4"
    assert proof.observed_at == "2026-09-03"
    assert proof.reference == "properties/552715460"
    assert proof.fact["provenance"] == "ga4_data_api"
    assert proof.fact["property"] == "552715460"
    assert proof.fact["date_range"] == {
        "start_date": "2026-08-01", "end_date": "2026-08-31",
    }
    assert proof.fact["dimensions"] == GA4_LANDING_PAGE_DIMENSIONS
    assert proof.fact["metrics"] == GA4_METRICS
    assert proof.fact["organic_channel_totals"] == {
        "sessions": 20.0,
        "engaged_sessions": 15.0,
        "key_events": 3.0,
    }
    assert proof.fact["landing_pages"] == ("/business-plus",)
    assert proof.fact["key_events"] == 2.5
    assert "page_paths" not in proof.fact


def test_ga4_small_zero_key_event_sample_is_telemetry_only_and_scores_weak():
    client = fake_client(
        (row(("/", ORGANIC_SEARCH_CHANNEL), ("6", "4", "0")),),
        total_metrics=("6", "4", "0"),
    )
    signal = adapter(client).collect()[0]
    score = score_opportunity(signal.topic, traffic=signal)

    assert signal.organic_sessions == 6
    assert signal.conversions is None
    assert signal.evidence[0].confidence.value == "high"
    assert signal.evidence[0].fact["key_events"] == 0
    assert score.conversion_signal is DimensionLevel.UNKNOWN
    assert score.final_score == 0
    assert recommendation_for(score).strength == "weak"


def test_ga4_positive_key_events_are_retained_but_not_commercial_conversions():
    client = fake_client(
        (row(("/", ORGANIC_SEARCH_CHANNEL), ("100", "70", "7")),),
        total_metrics=("100", "70", "7"),
    )
    signal = adapter(client).collect()[0]

    assert signal.organic_sessions == 100
    assert signal.conversions is None
    assert signal.evidence[0].fact["key_events"] == 7
    assert score_opportunity(
        signal.topic, traffic=signal
    ).conversion_signal is DimensionLevel.UNKNOWN


def test_known_landing_pages_map_to_their_explicit_topics():
    landing_rows = (
        row(("/", ORGANIC_SEARCH_CHANNEL), ("8", "6", "1")),
        row(("/business-plus", ORGANIC_SEARCH_CHANNEL), ("12", "9", "2")),
    )
    signals = adapter(fake_client(landing_rows)).collect()
    assert tuple(signal.topic for signal in signals) == (
        "business_delivery",
        "general_delivery",
    )


@pytest.mark.parametrize(
    ("channel_metrics", "landing_metrics"),
    [
        (("20", "15", "3"), ("12", "9", "2")),
        (("12", "9", "2"), ("12", "9", "2")),
    ],
)
def test_same_response_totals_bound_commercial_landing_totals(
    channel_metrics, landing_metrics
):
    client = fake_client(
        (row(("/", ORGANIC_SEARCH_CHANNEL), landing_metrics),),
        total_metrics=channel_metrics,
    )
    assert adapter(client).collect()[0].organic_sessions == float(landing_metrics[0])


def test_same_response_total_allows_unrepresented_residual_traffic():
    client = fake_client(
        (row(("/", ORGANIC_SEARCH_CHANNEL), ("4", "3", "1")),),
        total_metrics=("100", "80", "10"),
    )
    signal = adapter(client).collect()[0]

    assert signal.organic_sessions == 4
    assert signal.evidence[0].fact["organic_channel_totals"] == {
        "sessions": 100.0,
        "engaged_sessions": 80.0,
        "key_events": 10.0,
    }
    assert len(client.requests) == 1


@pytest.mark.parametrize(
    ("channel_metrics", "landing_metrics"),
    [
        (("10", "20", "20"), ("11", "5", "1")),
        (("20", "10", "20"), ("5", "11", "1")),
        (("20", "20", "10"), ("5", "5", "11")),
    ],
)
def test_commercial_landing_metric_above_channel_total_fails_closed(
    channel_metrics, landing_metrics
):
    client = fake_client(
        (row(("/", ORGANIC_SEARCH_CHANNEL), landing_metrics),),
        total_metrics=channel_metrics,
    )
    with pytest.raises(GA4DataSourceError, match="exceed Organic Search"):
        adapter(client).collect()


def test_missing_or_multiple_total_rows_fail_closed():
    missing = fake_client(total_rows=())
    with pytest.raises(GA4DataSourceError, match="exactly one total row"):
        adapter(missing).collect()

    total = row(
        (GA4_TOTAL_DIMENSION_VALUE, GA4_TOTAL_DIMENSION_VALUE),
        ("1", "1", "0"),
    )
    multiple = fake_client(total_rows=(total, total))
    with pytest.raises(GA4DataSourceError, match="exactly one total row"):
        adapter(multiple).collect()


@pytest.mark.parametrize(
    "dimensions",
    [
        (GA4_TOTAL_DIMENSION_VALUE,),
        (GA4_TOTAL_DIMENSION_VALUE, ORGANIC_SEARCH_CHANNEL),
        ("TOTAL", "TOTAL"),
    ],
)
def test_malformed_total_dimensions_fail_closed(dimensions):
    client = fake_client(total_rows=(row(dimensions, ("1", "1", "0")),))
    with pytest.raises(GA4DataSourceError, match="dimensions"):
        adapter(client).collect()


@pytest.mark.parametrize(
    "metrics",
    [
        ("1", "1"),
        ("1", "1", None),
        ("1", "1", "NaN"),
    ],
)
def test_malformed_total_metrics_fail_closed(metrics):
    total = row(
        (GA4_TOTAL_DIMENSION_VALUE, GA4_TOTAL_DIMENSION_VALUE), metrics
    )
    client = fake_client(total_rows=(total,))
    with pytest.raises(GA4DataSourceError, match="metric"):
        adapter(client).collect()


def test_multiple_rows_per_topic_are_aggregated_deterministically():
    landing_page_topics = {
        "/parcel-a": "parcel_delivery",
        "/parcel-b": "parcel_delivery",
        "/wine": "wine_delivery",
    }
    rows = (
        row(("/parcel-b", ORGANIC_SEARCH_CHANNEL), ("4.2", "3", "1")),
        row(("/wine", ORGANIC_SEARCH_CHANNEL), ("5", "4", "0")),
        row(("/parcel-a", ORGANIC_SEARCH_CHANNEL), ("6.8", "5", "2")),
    )
    first = adapter(
        fake_client(rows), landing_page_topics=landing_page_topics
    ).collect()
    second = adapter(
        fake_client(tuple(reversed(rows))), landing_page_topics=landing_page_topics
    ).collect()
    assert first == second
    assert tuple(item.topic for item in first) == ("parcel_delivery", "wine_delivery")
    assert first[0].organic_sessions == 11
    assert first[0].engaged_sessions == 8
    assert first[0].conversions is None
    assert first[0].evidence[0].fact["key_events"] == 3


def test_unknown_and_legal_landing_pages_are_explicitly_excluded():
    rows = (
        row(("/unknown-public-page", ORGANIC_SEARCH_CHANNEL), ("100", "90", "20")),
        row(("/protection-donnees", ORGANIC_SEARCH_CHANNEL), ("100", "90", "20")),
    )
    assert adapter(fake_client(rows)).collect() == ()


def test_raw_unknown_landing_page_never_leaks_into_mapped_evidence():
    unknown_path = "/unknown-sensitive-looking-value"
    rows = (
        row((unknown_path, ORGANIC_SEARCH_CHANNEL), ("5", "4", "1")),
        row(("/", ORGANIC_SEARCH_CHANNEL), ("5", "4", "1")),
    )
    signal = adapter(fake_client(rows)).collect()[0]

    assert signal.evidence[0].fact["landing_pages"] == ("/",)
    assert unknown_path not in json.dumps(signal.evidence[0].fact)


def test_not_set_landing_page_is_explicitly_excluded():
    rows = (row(("(not set)", ORGANIC_SEARCH_CHANNEL), ("100", "90", "20")),)
    assert adapter(fake_client(rows)).collect() == ()


def test_unknown_landing_pages_cannot_create_a_commercial_topic():
    custom_mapping = dict(DEFAULT_GA4_LANDING_PAGE_TOPICS)
    assert "/unknown-public-page" not in custom_mapping
    result = adapter(
        fake_client((row(("/unknown-public-page", ORGANIC_SEARCH_CHANNEL), ("1", "1", "1")),)),
        landing_page_topics=custom_mapping,
    ).collect()
    assert result == ()


@pytest.mark.parametrize("unsafe_path", ["/parcel?email=x", "/parcel#phone", "not-a-path", ""])
def test_query_strings_fragments_and_malformed_paths_fail_closed(unsafe_path):
    client = fake_client((row((unsafe_path, ORGANIC_SEARCH_CHANNEL), ("1", "1", "0")),))
    with pytest.raises(GA4DataSourceError, match="unsafe landingPage|row dimensions"):
        adapter(client).collect()


def test_unexpected_channel_or_dimensions_fail_closed():
    wrong_channel = fake_client(
        (row(("/", "Direct"), ("1", "1", "0")),),
    )
    with pytest.raises(GA4DataSourceError, match="unexpected channel"):
        adapter(wrong_channel).collect()

    wrong_headers = FakeClient(
        response(("pageLocation",)),
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
        response(metrics=("sessions", "engagedSessions", "conversions")),
    )
    with pytest.raises(GA4DataSourceError, match="metrics"):
        adapter(wrong_metric_headers).collect()


def test_api_failure_is_sanitized_and_fails_closed():
    client = FakeClient(RuntimeError("remote details must not escape"))
    with pytest.raises(GA4DataSourceError, match="GA4 Data API request failed") as failure:
        adapter(client).collect()
    assert "remote details" not in str(failure.value)
    assert len(client.requests) == 1


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


def test_current_agent_default_remains_offline_while_factory_supports_ga4():
    agent_source = (ROOT / "scripts/v3/agent.py").read_text()
    factory_source = (ROOT / "scripts/v3/source_factory.py").read_text()
    assert "GoogleAnalyticsDataSource" not in agent_source
    assert "build_source_adapters(" in agent_source
    assert "AnalyticsFixtureSource" in factory_source
    assert "GoogleAnalyticsDataSource" in factory_source
    assert "create_google_analytics_data_client" in factory_source
    assert "GA4_READ_ONLY_RUNTIME_SUPPORTED = True" in factory_source
