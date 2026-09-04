import json
import socket
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import scripts.v3.agent as agent_module
from scripts.v3.config import (
    SOURCE_NAMES,
    V3ConfigError,
    load_v3_config,
)
from scripts.v3.source_factory import (
    LIVE_GA4_RUNTIME_ALLOWED,
    SourceAuthorizationError,
    authorize_source,
    build_source_adapters,
)
from scripts.v3.sources.analytics import AnalyticsFixtureSource
from scripts.v3.sources.business_metrics import BusinessMetricsFixtureSource
from scripts.v3.sources.competitors import CompetitorFixtureSource
from scripts.v3.sources.rank_tracker import RankTrackerFixtureSource
from scripts.v3.sources.reviews import ReviewsFixtureSource
from scripts.v3.sources.search_console import SearchConsoleFixtureSource


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config/seo_agent_v3.json"


def _payload():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_invalid_config(tmp_path, case):
    payload = _payload()
    if case == "network_enabled":
        payload["mode"]["network_enabled"] = True
    elif case == "analytics_ga4":
        payload["phase_1_sources"]["analytics"] = "ga4_data_api"
    elif case == "ga4_enabled":
        payload["ga4_data_api"]["enabled"] = True
    elif case == "analytics_allow":
        payload["network_policy"]["sources"]["analytics"] = "allow"
    elif case == "default_allow":
        payload["network_policy"]["default"] = "allow"
    elif case == "missing_network_policy":
        del payload["network_policy"]
    elif case == "missing_source_policy":
        del payload["network_policy"]["sources"]["reviews"]
    elif case == "unknown_phase_source":
        payload["phase_1_sources"]["future_source"] = "local_fixture"
    elif case == "unknown_network_source":
        payload["network_policy"]["sources"]["future_source"] = "deny"
    elif case == "invalid_policy":
        payload["network_policy"]["sources"]["analytics"] = "sometimes"
    elif case == "renamed_fixture":
        payload["phase_1_sources"]["rank_tracker"] = "fixture"
    elif case == "read_only_false":
        payload["mode"]["read_only"] = False
    elif case == "proposal_only_false":
        payload["mode"]["proposal_only"] = False
    elif case == "publication_enabled":
        payload["mode"]["site_publication_enabled"] = True
    else:
        raise AssertionError("unknown test case")
    path = tmp_path / "invalid-v3-config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_current_config_is_typed_immutable_and_exactly_offline():
    config = load_v3_config()

    assert config.mode.read_only is True
    assert config.mode.proposal_only is True
    assert config.mode.network_enabled is False
    assert config.mode.site_publication_enabled is False
    assert tuple(config.phase_1_sources.value_for(name) for name in SOURCE_NAMES) == (
        "local_fixture",
    ) * 6
    assert all(
        "://" not in config.phase_1_sources.value_for(name)
        for name in SOURCE_NAMES
    )
    assert config.network_policy.default == "deny"
    assert tuple(config.network_policy.value_for(name) for name in SOURCE_NAMES) == (
        "deny",
    ) * 6
    assert config.ga4_data_api.enabled is False
    assert config.ga4_data_api.property_id == "552715460"
    assert config.ga4_data_api.resource == "properties/552715460"

    with pytest.raises(FrozenInstanceError):
        config.mode.network_enabled = True
    with pytest.raises(TypeError):
        config.score_weights["search_demand"] = 99


@pytest.mark.parametrize(
    "case",
    (
        "network_enabled",
        "analytics_ga4",
        "ga4_enabled",
        "analytics_allow",
        "default_allow",
        "missing_network_policy",
        "missing_source_policy",
        "unknown_phase_source",
        "unknown_network_source",
        "invalid_policy",
        "renamed_fixture",
        "read_only_false",
        "proposal_only_false",
        "publication_enabled",
    ),
)
def test_every_non_offline_state_fails_before_source_construction(
    tmp_path, monkeypatch, case
):
    path = _write_invalid_config(tmp_path, case)
    source_factory_called = False

    def unexpected_source_construction(_config):
        nonlocal source_factory_called
        source_factory_called = True
        raise AssertionError("source construction must not be reached")

    monkeypatch.setattr(
        agent_module,
        "build_source_adapters",
        unexpected_source_construction,
    )
    with pytest.raises(V3ConfigError):
        agent_module.MarketIntelligenceAgent(path)
    assert source_factory_called is False


def test_factory_returns_exactly_the_six_static_fixture_sources():
    adapters = build_source_adapters(load_v3_config())

    assert tuple(adapters) == SOURCE_NAMES
    assert tuple(type(adapters[name]) for name in SOURCE_NAMES) == (
        SearchConsoleFixtureSource,
        AnalyticsFixtureSource,
        RankTrackerFixtureSource,
        CompetitorFixtureSource,
        ReviewsFixtureSource,
        BusinessMetricsFixtureSource,
    )


def test_factory_is_default_deny_and_never_constructs_google_client(
    monkeypatch,
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network or Google client construction is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(
        "scripts.v3.sources.analytics.create_google_analytics_data_client",
        forbidden,
    )
    config = load_v3_config()
    adapters = build_source_adapters(config)

    assert len(adapters) == 6
    with pytest.raises(SourceAuthorizationError, match="unknown"):
        authorize_source(config, "future_source", requires_network=False)
    with pytest.raises(SourceAuthorizationError, match="disabled"):
        authorize_source(config, "analytics", requires_network=True)


def test_factory_revalidates_manually_constructed_config_before_construction():
    config = load_v3_config()
    unsafe = replace(
        config,
        mode=replace(config.mode, network_enabled=True),
    )

    with pytest.raises(V3ConfigError, match="offline mode"):
        build_source_adapters(unsafe)


def test_offline_agent_import_and_construction_need_no_google_package_or_socket():
    script = """
import builtins
import socket

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'google' or name.startswith('google.'):
        raise AssertionError('Google package import is forbidden offline')
    return original_import(name, *args, **kwargs)

def forbidden_socket(*args, **kwargs):
    raise AssertionError('socket access is forbidden offline')

builtins.__import__ = guarded_import
socket.create_connection = forbidden_socket
from scripts.v3.agent import MarketIntelligenceAgent
agent = MarketIntelligenceAgent()
assert tuple(agent.adapters) == (
    'search_console', 'analytics', 'rank_tracker',
    'competitors', 'reviews', 'business_metrics',
)
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


def test_factory_has_no_dynamic_import_or_live_ga4_path():
    source = (ROOT / "scripts/v3/source_factory.py").read_text(encoding="utf-8")
    config_source = (ROOT / "scripts/v3/config.py").read_text(encoding="utf-8")

    for prohibited in (
        "importlib",
        "eval(",
        "exec(",
        "private_key",
        "client_secret",
        "service_account_info",
    ):
        assert prohibited not in source
        assert prohibited not in config_source
    assert "GoogleAnalyticsDataSource" not in source
    assert "create_google_analytics_data_client" not in source
    assert "credential" not in source.lower()
    assert LIVE_GA4_RUNTIME_ALLOWED is False


def test_agent_uses_central_factory_only():
    source = (ROOT / "scripts/v3/agent.py").read_text(encoding="utf-8")

    assert "build_source_adapters(self.config)" in source
    for class_name in (
        "SearchConsoleFixtureSource",
        "AnalyticsFixtureSource",
        "RankTrackerFixtureSource",
        "CompetitorFixtureSource",
        "ReviewsFixtureSource",
        "BusinessMetricsFixtureSource",
        "GoogleAnalyticsDataSource",
    ):
        assert class_name not in source


def test_agent_output_matches_pre_refactor_snapshot():
    fixture = {
        "search_console": [{
            "topic": "parcel_delivery",
            "query": "livraison colis",
            "impressions": 350,
            "average_position": 12,
        }],
        "analytics": [{
            "topic": "parcel_delivery",
            "organic_sessions": 20,
            "engaged_sessions": 15,
            "conversions": 2,
        }],
        "rank_tracker": [{
            "topic": "parcel_delivery",
            "query": "livraison colis",
            "position": 12,
        }],
        "competitors": [{
            "topic": "parcel_delivery",
            "competitor": "aggregate",
            "gap_level": "medium",
        }],
        "reviews": [{
            "topic": "parcel_delivery",
            "source_platform": "aggregate",
            "competitor": "aggregate",
            "rating_average": 4.0,
            "review_count": 20,
            "observation_window": "90d",
            "negative_topics": ["tracking"],
            "confidence": "medium",
        }],
        "business_metrics": [{
            "topic": "parcel_delivery",
            "pricing_simulations": 12,
            "orders_started": 5,
            "commercial_contacts": 4,
            "revenue": 1000,
        }],
    }

    result = agent_module.MarketIntelligenceAgent().run(fixture)

    assert tuple(score.topic for score in result.scores) == ("parcel_delivery",)
    assert result.source_counts == {name: 1 for name in SOURCE_NAMES}
    assert result.scores[0].final_score == 66
    assert result.scores[0].explanation == (
        "search_demand=medium",
        "rank_opportunity=high",
        "commercial_fit=high",
        "conversion_signal=low",
        "competitive_gap=medium",
        "reputation_gap=low",
        "evidence_confidence=unknown",
        "search_signals=1",
        "traffic_signals=1",
        "rank_signals=1",
        "competitor_signals=1",
        "review_signals=1",
        "business_signals=1",
        "evidence_sources=0",
        "confidence_aggregation=equal_weight_per_evidence_source",
    )
    assert result.recommendations[0].strength == "weak"
    assert result.markdown == """# Colixo SEO / Market Intelligence Agent V3

## 1. Executive summary

- FACT: 1 opportunity topic(s) evaluated from local fixtures.
- INFERENCE: Scores summarize known evidence only; unknown metrics are excluded.
- RECOMMENDATION: Human review is required before any action.

## 2. Search demand

- FACT: parcel_delivery: medium

## 3. Rank opportunities

- FACT: parcel_delivery: high

## 4. Conversion signals

- FACT: parcel_delivery: low

## 5. Competitive gaps

- FACT: parcel_delivery: medium

## 6. Customer reputation signals

- FACT: parcel_delivery: low

## 7. Commercial value

- FACT: parcel_delivery: high

## 8. Recommended actions

- INFERENCE: parcel_delivery scores 66/100 with very_low confidence.
- RECOMMENDATION: [weak] Collect more evidence; do not make a strong recommendation.

## 9. Unknown / insufficient evidence

- FACT: parcel_delivery: evidence_confidence

## 10. Safety / data provenance

- FACT: Sources are local fixtures only: analytics=1, business_metrics=1, competitors=1, rank_tracker=1, reviews=1, search_console=1.
- FACT: Models contain aggregated metrics and no intentional personal data fields.
- INFERENCE: Evidence confidence limits recommendation strength.
- RECOMMENDATION: Keep V3 read-only and proposal-only until separately authorized.
"""


def test_key_event_and_scoring_files_remain_outside_this_change():
    changed = subprocess.run(
        ["git", "diff", "--name-only", "origin/main"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    assert "scripts/v3/sources/analytics.py" not in changed
    assert "scripts/v3/scoring.py" not in changed
    assert LIVE_GA4_RUNTIME_ALLOWED is False
