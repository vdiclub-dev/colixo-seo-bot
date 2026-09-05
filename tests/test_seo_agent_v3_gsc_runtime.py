"""GSC runtime boundary: fake transports only, no authentication or network."""

import json
from itertools import product
from dataclasses import asdict, FrozenInstanceError, replace
from pathlib import Path

import pytest

from scripts.v3.agent import MarketIntelligenceAgent
from scripts.v3.config import GSC_CONTRACT, RuntimeState, V3ConfigError, load_v3_config
from scripts.v3.source_factory import build_source_adapters, authorize_source, SourceAuthorizationError
from scripts.v3.scoring import score_opportunity

ROOT = Path(__file__).resolve().parents[1]
GSC = ROOT / "config/seo_agent_v3_gsc_readonly.json"
GA4 = ROOT / "config/seo_agent_v3_ga4_readonly.json"
OFFLINE = ROOT / "config/seo_agent_v3.json"
RAW = ("livraison colis zeta", "colixo marque zeta", "astronomie zeta", "colixo test@example.invalid")


def row(query):
    return dict(keys=[query], clicks=2, impressions=20, ctr=0.1, position=8)


class FakeTransport:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        if isinstance(self.rows, Exception):
            raise self.rows
        payload = {"rows": self.rows}
        class Response:
            status_code = 200

            def json(self):
                return payload
        return Response()


def forbidden():
    raise AssertionError("real client construction forbidden")


@pytest.fixture(autouse=True)
def block_default_clients(monkeypatch):
    monkeypatch.setattr("scripts.v3.source_factory.create_google_search_console_transport", forbidden)
    monkeypatch.setattr("scripts.v3.source_factory.create_google_analytics_data_client", forbidden)


def agent(transport):
    return MarketIntelligenceAgent(GSC, observed_at="2026-09-05",
                                   gsc_transport_factory=lambda: transport,
                                   ga4_client_factory=forbidden)


def test_privacy_projection_and_one_request(capsys):
    t = FakeTransport([row(q) for q in RAW])
    a = agent(t)
    result = a.run({})
    assert len(t.calls) == 1
    assert t.calls[0][1] == dict(startDate="2026-08-08", endDate="2026-09-02",
                               dimensions=["query"], type="web", dataState="final",
                               rowLimit=25000, startRow=0)
    assert result.source_counts["search_console"] == 1
    assert result.source_modes["search_console"] == "gsc_search_analytics_api"
    assert all(v == "local_fixture" for k, v in result.source_modes.items() if k != "search_console")
    c = result.gsc_coverage
    assert (c.raw_row_count, c.accepted_signal_count, c.brand_row_count,
            c.unmapped_row_count, c.pii_filtered_row_count) == (4, 1, 1, 1, 1)
    assert (c.all_rows_clicks, c.all_rows_impressions, c.accepted_clicks,
            c.accepted_impressions, c.brand_clicks, c.brand_impressions) == (8, 80, 2, 20, 2, 20)
    assert c.row_limit == 25000 and c.row_limit_reached is False
    assert c.totals_scope == "returned_query_rows_non_exhaustive"
    with pytest.raises(FrozenInstanceError):
        c.brand_row_count = 7
    projections = [repr(result), str(result), result.markdown, repr(result.recommendations),
                   repr(result.scores), repr(c), repr(asdict(result))]
    for text in projections:
        assert all(q not in text for q in RAW)
        assert "example.invalid" not in text
    evidence = result.recommendations[0].evidence
    assert len(evidence) == 1
    assert set(evidence[0].fact) == {"property", "date_range", "provenance", "commercial_topic",
                                    "clicks", "impressions", "ctr", "average_position"}
    assert evidence[0].observed_at == "2026-09-05"
    assert evidence[0].confidence.value == "high"
    assert "search_console=gsc_search_analytics_api(read-only)" in result.markdown
    assert capsys.readouterr().out == ""
    # Independent runs collect again; no cached raw response or stale metadata.
    t.rows = []
    again = a.run({})
    assert len(t.calls) == 2 and again.gsc_coverage.raw_row_count == 0


@pytest.mark.parametrize("queries", [[], [RAW[1]], [RAW[2]], [RAW[3]], list(RAW[1:])])
def test_excluded_rows_never_score(queries):
    result = agent(FakeTransport([row(q) for q in queries])).run({})
    assert result.scores == result.recommendations == ()
    assert result.source_counts["search_console"] == 0
    score = score_opportunity("parcel_delivery", search=())
    assert score.search_demand.value == "unknown"
    assert score.final_score == 0
    assert score.evidence_confidence.value == "unknown"


@pytest.mark.parametrize("fixture", [None, {}, "", [row(RAW[0])], (row(RAW[0]),)])
def test_nonempty_or_invalid_fixture_refused(fixture):
    t = FakeTransport([])
    a = agent(t)
    with pytest.raises(SourceAuthorizationError):
        a.run({"search_console": fixture})
    assert t.calls == []


@pytest.mark.parametrize("date", [None, "", "yesterday", "2026-99-99", "2026-02-30", "2026-9-05"])
def test_date_before_transport_creation(date):
    with pytest.raises(SourceAuthorizationError):
        MarketIntelligenceAgent(GSC, observed_at=date, gsc_transport_factory=forbidden)


def test_factories_isolated():
    offline = MarketIntelligenceAgent(OFFLINE, ga4_client_factory=forbidden, gsc_transport_factory=forbidden)
    assert offline.run({}).gsc_coverage is None
    built = []
    adapters = build_source_adapters(load_v3_config(GA4), observed_at="2026-09-05",
                                    ga4_client_factory=lambda: built.append(1) or object(),
                                    gsc_transport_factory=forbidden)
    assert built == [1] and adapters["search_console"].collect(()) == ()
    for path, denied in [(GSC, "analytics"), (GA4, "search_console")]:
        with pytest.raises(SourceAuthorizationError):
            authorize_source(load_v3_config(path), denied, requires_network=True)
    with pytest.raises(SourceAuthorizationError):
        authorize_source(load_v3_config(GSC), "search_console", requires_network=False)


@pytest.mark.parametrize("key", list(GSC_CONTRACT) + ["enabled"])
def test_config_contract_immutable(tmp_path, key):
    payload = json.loads(GSC.read_text())
    payload["gsc_search_analytics"][key] = "invalid"
    p = tmp_path / "config.json"
    p.write_text(json.dumps(payload))
    with pytest.raises(V3ConfigError):
        load_v3_config(p)


@pytest.mark.parametrize("case", ["missing", "partial", "mixed", "enabled_off", "network_off", "wrong_adapter", "extra_network"])
def test_partial_and_mixed_configs_refused(tmp_path, case):
    p = json.loads(GSC.read_text())
    if case == "missing": del p["gsc_search_analytics"]
    if case == "partial": del p["gsc_search_analytics"]["row_limit"]
    if case == "mixed":
        p["ga4_data_api"]["enabled"] = True
        p["phase_1_sources"]["analytics"] = "ga4_data_api"
        p["network_policy"]["sources"]["analytics"] = "allow"
    if case == "enabled_off": p["gsc_search_analytics"]["enabled"] = False
    if case == "network_off": p["mode"]["network_enabled"] = False
    if case == "wrong_adapter": p["phase_1_sources"]["search_console"] = "local_fixture"
    if case == "extra_network": p["network_policy"]["sources"]["reviews"] = "allow"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(p))
    with pytest.raises(V3ConfigError): load_v3_config(path)


def test_forged_config_refused_before_construction():
    c = load_v3_config(GSC)
    c = replace(c, gsc_search_analytics=replace(c.gsc_search_analytics, contract=()))
    with pytest.raises(V3ConfigError):
        build_source_adapters(c, gsc_transport_factory=forbidden)


@pytest.mark.parametrize("network,adapter,allow,enabled", list(product([False, True], repeat=4)))
def test_exact_gsc_state_matrix(tmp_path, network, adapter, allow, enabled):
    payload = json.loads(OFFLINE.read_text())
    payload["mode"]["network_enabled"] = network
    payload["phase_1_sources"]["search_console"] = "gsc_search_analytics_api" if adapter else "local_fixture"
    payload["network_policy"]["sources"]["search_console"] = "allow" if allow else "deny"
    payload["gsc_search_analytics"]["enabled"] = enabled
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload))
    if all((network, adapter, allow, enabled)):
        assert load_v3_config(path).runtime_state is RuntimeState.GSC_READ_ONLY
    elif not any((network, adapter, allow, enabled)):
        assert load_v3_config(path).runtime_state is RuntimeState.OFFLINE
    else:
        with pytest.raises(V3ConfigError): load_v3_config(path)


def test_wrapper_itself_returns_no_query():
    a = agent(FakeTransport([row(RAW[0])]))
    signals = a.adapters["search_console"].collect([])
    assert signals[0].query == "commercial_query_redacted"
    assert RAW[0] not in repr(asdict(signals[0]))
    assert "query" not in signals[0].evidence[0].fact


def test_non_callable_transport_factory_refused():
    with pytest.raises(SourceAuthorizationError):
        MarketIntelligenceAgent(GSC, observed_at="2026-09-05", gsc_transport_factory=17)


@pytest.mark.parametrize("count", [24999, 25000])
def test_returned_rows_never_claim_completeness(count):
    t = FakeTransport([row(RAW[1])] * count)
    result = agent(t).run({})
    assert len(t.calls) == 1
    assert result.gsc_coverage.row_limit_reached is (count == 25000)
    assert result.gsc_coverage.totals_scope == "returned_query_rows_non_exhaustive"
    assert result.scores == ()


def test_failure_no_retry_or_stale_coverage():
    t = FakeTransport([])
    a = agent(t)
    a.run({})
    t.rows = RuntimeError("private-response-marker")
    with pytest.raises(ValueError) as error:
        a.run({})
    assert "private-response-marker" not in str(error.value)
    assert len(t.calls) == 2
    assert a.adapters["search_console"].coverage is None


def test_row_order_deterministic_after_redaction():
    rows = [row(RAW[0]), row("livraison colis alpha")]
    rows[1]["impressions"] = 40
    rows[1]["ctr"] = 0.05
    assert agent(FakeTransport(rows)).run({}) == agent(FakeTransport(rows[::-1])).run({})
