from scripts.gsc_client import SearchRow
from scripts.seo_agent_v2 import (
    aggregate,
    classify_query,
    get_property_name,
    score_query,
    select_opportunities,
)


CONFIG = {
    "brand_terms": ["colixo"],
    "high_intent_terms": ["entreprise", "coursier", "express", "e-commerce"],
    "geo_terms": ["geneve", "genève", "lausanne", "vaud", "suisse romande"],
    "low_fit_terms": ["pas cher", "gratuit"],
    "min_impressions_for_priority": 2,
}


def row(query, impressions, position, clicks=0):
    return SearchRow((query,), clicks, impressions, 0.0, position)


def test_brand_is_not_acquisition_priority():
    item = score_query(row("colixo", 135, 2.5, 30), CONFIG)
    assert item.classification == "brand"
    assert item.score < 0


def test_b2b_geo_query_is_prioritized():
    item = score_query(row("entreprise livraison geneve", 20, 18.0), CONFIG)
    assert item.classification == "high_fit_b2b"
    assert item.score > 80


def test_low_fit_pas_cher_is_deprioritized():
    item = score_query(row("colis pas cher", 32, 68.4), CONFIG)
    assert item.classification == "low_fit"
    assert item.score < 10


def test_aggregate_separates_brand_and_nonbrand():
    totals = aggregate(
        [row("colixo", 135, 2.5, 30), row("entreprise livraison geneve", 10, 20, 2)],
        CONFIG,
    )
    assert totals["brand_clicks"] == 30
    assert totals["nonbrand_clicks"] == 2
    assert totals["impressions"] == 145


def test_geo_without_b2b_is_relevant():
    assert classify_query("livraison Lausanne", CONFIG) == "geo_relevant"


def test_generic_colis_is_watchlist_not_priority():
    item = score_query(row("colis", 200, 12.0), CONFIG)
    priorities, watchlist = select_opportunities([item], 15)
    assert priorities == []
    assert [candidate.query for candidate in watchlist] == ["colis"]


def test_entreprise_livraison_geneve_remains_priority():
    item = score_query(row("entreprise livraison geneve", 20, 18.0), CONFIG)
    priorities, watchlist = select_opportunities([item], 15)
    assert [candidate.query for candidate in priorities] == ["entreprise livraison geneve"]
    assert watchlist == []


def test_colis_pas_cher_is_excluded_from_priority_and_watchlist():
    item = score_query(row("colis pas cher", 32, 18.0), CONFIG)
    priorities, watchlist = select_opportunities([item], 15)
    assert priorities == []
    assert watchlist == []


def test_gsc_property_environment_override(monkeypatch):
    monkeypatch.setenv("GSC_PROPERTY", "sc-domain:example.test")
    assert get_property_name({"property": "sc-domain:colixo.ch"}) == "sc-domain:example.test"
