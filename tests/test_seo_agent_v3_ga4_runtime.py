import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from scripts.v3.agent import MarketIntelligenceAgent
from scripts.v3.config import RuntimeState, V3ConfigError, load_v3_config
from scripts.v3.models import DimensionLevel
from scripts.v3.source_factory import (
    SourceAuthorizationError,
    build_source_adapters,
)
from scripts.v3.sources.analytics import (
    GA4DataSourceError,
    GA4_LANDING_PAGE_DIMENSIONS,
    GA4_METRICS,
    GA4_TOTAL_DIMENSION_VALUE,
    ORGANIC_SEARCH_CHANNEL,
)


ROOT = Path(__file__).resolve().parents[1]
OFFLINE_CONFIG = ROOT / "config/seo_agent_v3.json"
GA4_CONFIG = ROOT / "config/seo_agent_v3_ga4_readonly.json"


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


def _row(dimensions, metrics):
    return Row(
        dimension_values=tuple(Value(value) for value in dimensions),
        metric_values=tuple(Value(value) for value in metrics),
    )


def _response(*, sessions="6", engaged="3", key_events="0"):
    return Response(
        dimension_headers=tuple(Header(name) for name in GA4_LANDING_PAGE_DIMENSIONS),
        metric_headers=tuple(Header(name) for name in GA4_METRICS),
        rows=(
            _row(
                ("/", ORGANIC_SEARCH_CHANNEL),
                (sessions, engaged, key_events),
            ),
        ),
        totals=(
            _row(
                (GA4_TOTAL_DIMENSION_VALUE, GA4_TOTAL_DIMENSION_VALUE),
                (sessions, engaged, key_events),
            ),
        ),
    )


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def run_report(self, *, request):
        self.requests.append(request)
        if len(self.requests) > 1:
            raise AssertionError("a second GA4 request is forbidden")
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _write_config(tmp_path, mutate):
    payload = json.loads(GA4_CONFIG.read_text(encoding="utf-8"))
    mutate(payload)
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_exactly_three_runtime_states_are_allowlisted_and_default_is_offline():
    offline = load_v3_config(OFFLINE_CONFIG)
    ga4 = load_v3_config(GA4_CONFIG)

    assert tuple(RuntimeState) == (
        RuntimeState.OFFLINE,
        RuntimeState.GA4_READ_ONLY,
        RuntimeState.GSC_READ_ONLY,
    )
    assert offline.runtime_state is RuntimeState.OFFLINE
    assert ga4.runtime_state is RuntimeState.GA4_READ_ONLY
    assert offline.mode.network_enabled is False
    assert offline.phase_1_sources.analytics == "local_fixture"
    assert offline.network_policy.analytics == "deny"
    assert offline.ga4_data_api.enabled is False


def test_ga4_profile_changes_only_the_four_authorized_runtime_values():
    offline = json.loads(OFFLINE_CONFIG.read_text(encoding="utf-8"))
    ga4 = json.loads(GA4_CONFIG.read_text(encoding="utf-8"))

    ga4["mode"]["network_enabled"] = False
    ga4["phase_1_sources"]["analytics"] = "local_fixture"
    ga4["network_policy"]["sources"]["analytics"] = "deny"
    ga4["ga4_data_api"]["enabled"] = False
    assert ga4 == offline


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value["mode"].__setitem__("network_enabled", False),
        lambda value: value["phase_1_sources"].__setitem__(
            "analytics", "local_fixture"
        ),
        lambda value: value["network_policy"]["sources"].__setitem__(
            "analytics", "deny"
        ),
        lambda value: value["ga4_data_api"].__setitem__("enabled", False),
        lambda value: value["network_policy"].__setitem__("default", "allow"),
        lambda value: value["phase_1_sources"].__setitem__(
            "search_console", "ga4_data_api"
        ),
        lambda value: value["network_policy"]["sources"].__setitem__(
            "search_console", "allow"
        ),
        lambda value: value["mode"].__setitem__("read_only", False),
        lambda value: value["mode"].__setitem__("proposal_only", False),
        lambda value: value["mode"].__setitem__(
            "site_publication_enabled", True
        ),
    ),
)
def test_partial_mixed_unknown_and_unsafe_states_fail_closed(tmp_path, mutate):
    with pytest.raises(V3ConfigError):
        load_v3_config(_write_config(tmp_path, mutate))


def test_ga4_authorization_and_observed_at_validation_precede_client_creation():
    config = load_v3_config(GA4_CONFIG)
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return FakeClient(_response())

    with pytest.raises(SourceAuthorizationError, match="observed_at"):
        build_source_adapters(config, ga4_client_factory=factory)
    with pytest.raises(SourceAuthorizationError, match="observed_at"):
        build_source_adapters(
            config,
            observed_at="yesterday",
            ga4_client_factory=factory,
        )
    assert factory_calls == []

    stale_marker = replace(config, runtime_state=RuntimeState.OFFLINE)
    with pytest.raises(V3ConfigError, match="marker"):
        build_source_adapters(
            stale_marker,
            observed_at="2026-09-04",
            ga4_client_factory=factory,
        )
    assert factory_calls == []


def test_offline_runtime_never_invokes_an_injected_ga4_factory():
    def forbidden_factory():
        raise AssertionError("offline runtime must not create a GA4 client")

    agent = MarketIntelligenceAgent(
        OFFLINE_CONFIG,
        ga4_client_factory=forbidden_factory,
    )
    assert agent.source_modes["analytics"] == "local_fixture"


def test_ga4_read_only_runtime_uses_one_fake_request_and_exposes_provenance():
    client = FakeClient(_response())
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return client

    result = MarketIntelligenceAgent(
        GA4_CONFIG,
        observed_at="2026-09-04",
        ga4_client_factory=factory,
    ).run({})

    assert factory_calls == [True]
    assert len(client.requests) == 1
    assert result.source_modes == {
        "search_console": "local_fixture",
        "analytics": "ga4_data_api",
        "rank_tracker": "local_fixture",
        "competitors": "local_fixture",
        "reviews": "local_fixture",
        "business_metrics": "local_fixture",
    }
    assert result.source_counts["analytics"] == 1
    assert result.scores[0].topic == "general_delivery"
    assert result.scores[0].conversion_signal is DimensionLevel.UNKNOWN
    assert result.scores[0].final_score == 0
    assert result.recommendations[0].strength == "weak"
    evidence = result.recommendations[0].evidence[0]
    assert evidence.source == "google_analytics_4"
    assert evidence.observed_at == "2026-09-04"
    assert evidence.fact["organic_sessions"] == 6.0
    assert evidence.fact["engaged_sessions"] == 3.0
    assert evidence.fact["key_events"] == 0.0
    assert "analytics=ga4_data_api(read-only)" in result.markdown
    assert "Sources are local fixtures only" not in result.markdown


def test_ga4_runtime_combines_live_analytics_with_fixture_search_console():
    client = FakeClient(_response())
    result = MarketIntelligenceAgent(
        GA4_CONFIG,
        observed_at="2026-09-04",
        ga4_client_factory=lambda: client,
    ).run({
        "search_console": ({
            "topic": "general_delivery",
            "query": "livraison suisse",
            "impressions": 120,
            "average_position": 12,
        },),
    })

    assert len(client.requests) == 1
    assert result.source_counts["search_console"] == 1
    assert result.source_counts["analytics"] == 1
    assert result.scores[0].search_demand is DimensionLevel.MEDIUM
    assert result.scores[0].conversion_signal is DimensionLevel.UNKNOWN
    assert "search_console=local_fixture" in result.markdown


@pytest.mark.parametrize(
    "analytics_fixture",
    (
        ({"topic": "forbidden"},),
        None,
        {},
        "",
    ),
)
def test_ga4_runtime_rejects_analytics_fixture_without_calling_api(
    analytics_fixture,
):
    client = FakeClient(_response())
    agent = MarketIntelligenceAgent(
        GA4_CONFIG,
        observed_at="2026-09-04",
        ga4_client_factory=lambda: client,
    )

    with pytest.raises(SourceAuthorizationError, match="empty fixture"):
        agent.run({"analytics": analytics_fixture})
    assert client.requests == []


def test_ga4_api_failure_fails_closed_without_fixture_fallback_or_retry():
    client = FakeClient(RuntimeError("sanitized upstream failure"))
    agent = MarketIntelligenceAgent(
        GA4_CONFIG,
        observed_at="2026-09-04",
        ga4_client_factory=lambda: client,
    )

    with pytest.raises(GA4DataSourceError, match="request failed"):
        agent.run({})
    assert len(client.requests) == 1
