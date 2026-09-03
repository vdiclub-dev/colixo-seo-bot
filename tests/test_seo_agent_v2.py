import json
from datetime import date

from scripts import seo_agent_v2 as agent
from scripts.gsc_client import SearchRow, build_search_analytics_payload
from scripts.seo_agent_v2 import (
    aggregate_visible_queries,
    analyze_http_payload,
    classify_query,
    exact_brand_query,
    get_property_name,
    property_totals,
    render_report,
    score_query,
    select_opportunities,
    unattributed_metrics,
)


CONFIG = {
    "brand_terms": ["colixo"],
    "high_intent_terms": ["entreprise", "coursier", "express", "e-commerce"],
    "geo_terms": ["geneve", "genève", "lausanne", "vaud", "suisse romande"],
    "low_fit_terms": ["pas cher", "moins cher", "gratuit"],
    "min_impressions_for_priority": 2,
    "top_opportunities": 15,
    "site_base_url": "https://www.colixo.ch",
    "lookback_days": 90,
    "data_lag_days": 3,
    "legacy_urls": [],
    "property": "sc-domain:colixo.ch",
}


def row(query, impressions, position, clicks=0, ctr=0.0):
    return SearchRow((query,), clicks, impressions, ctr, position)


def test_real_config_has_synchronized_prod_legacy_policies():
    policies = {
        entry["path"]: entry["policy"]
        for entry in agent.load_config()["legacy_urls"]
    }
    assert policies["/simulate-price/index.html"] == "keep_gone"
    assert policies["/demande-transport.html"] == "redirect_candidate"


def test_property_totals_payload_has_no_dimensions():
    payload = build_search_analytics_payload(date(2026, 1, 1), date(2026, 1, 31), [])
    assert "dimensions" not in payload


def test_visible_query_payload_keeps_query_dimension():
    payload = build_search_analytics_payload(
        date(2026, 1, 1), date(2026, 1, 31), ["query"]
    )
    assert payload["dimensions"] == ["query"]


def test_property_and_visible_query_totals_are_distinct():
    all_totals = property_totals([SearchRow((), 40, 200, 0.2, 8.5)])
    visible = aggregate_visible_queries(
        [row("colixo", 130, 2.5, 28), row("entreprise livraison geneve", 50, 20, 2)],
        CONFIG,
    )
    assert all_totals == {
        "total_clicks": 40,
        "total_impressions": 200,
        "ctr": 0.2,
        "position": 8.5,
    }
    assert visible["visible_query_clicks"] == 30
    assert visible["visible_query_impressions"] == 180


def test_unattributed_metrics_are_not_added_to_brand_or_nonbrand():
    visible = aggregate_visible_queries(
        [row("colixo", 130, 2.5, 28), row("livraison entreprise", 50, 20, 2)],
        CONFIG,
    )
    unattributed = unattributed_metrics(
        {"total_clicks": 40, "total_impressions": 210, "ctr": 0.0, "position": 0.0},
        visible,
    )
    assert unattributed == {
        "raw_unattributed_clicks": 10,
        "raw_unattributed_impressions": 30,
        "unattributed_clicks": 10,
        "unattributed_impressions": 30,
        "aggregation_inconsistency": False,
    }
    assert visible["brand_clicks"] + visible["nonbrand_clicks"] == 30


def test_unattributed_clicks_are_clamped_when_visible_exceeds_property():
    result = unattributed_metrics(
        {"total_clicks": 5, "total_impressions": 100, "ctr": 0.0, "position": 0.0},
        {"visible_query_clicks": 7, "visible_query_impressions": 90},
    )
    assert result["raw_unattributed_clicks"] == -2
    assert result["unattributed_clicks"] == 0
    assert result["aggregation_inconsistency"] is True


def test_unattributed_impressions_are_clamped_when_visible_exceeds_property():
    result = unattributed_metrics(
        {"total_clicks": 5, "total_impressions": 80, "ctr": 0.0, "position": 0.0},
        {"visible_query_clicks": 4, "visible_query_impressions": 90},
    )
    assert result["raw_unattributed_impressions"] == -10
    assert result["unattributed_impressions"] == 0
    assert result["aggregation_inconsistency"] is True


def test_report_never_displays_negative_unattributed_values():
    visible = aggregate_visible_queries([row("colixo", 90, 2, 7)], CONFIG)
    all_totals = {"total_clicks": 5, "total_impressions": 80, "ctr": 0.1, "position": 2.0}
    unattributed = unattributed_metrics(all_totals, visible)
    report = render_report(
        date(2026, 1, 1),
        date(2026, 1, 31),
        all_totals,
        visible,
        unattributed,
        exact_brand_query([row("colixo", 90, 2, 7)]),
        [],
        [],
        [],
        [],
    )
    assert "Clics non attribuables : **0**" in report
    assert "Impressions non attribuables : **0**" in report
    assert "Les agrégations Search Console ne sont pas directement conciliables" in report
    assert "non attribuables : **-" not in report


def test_exact_colixo_brand_row_is_exportable_without_extrapolation():
    result = exact_brand_query(
        [row("colixo transport", 20, 4, 3), row("Colixo", 133, 2.5, 29, 0.218)]
    )
    assert result == {
        "query": "Colixo",
        "clicks": 29,
        "impressions": 133,
        "ctr": 0.218,
        "position": 2.5,
    }
    assert exact_brand_query([row("colixo transport", 20, 4, 3)]) is None


def test_brand_share_is_explicitly_limited_to_visible_queries():
    totals = aggregate_visible_queries(
        [row("colixo", 135, 2.5, 30), row("entreprise livraison geneve", 10, 20, 2)],
        CONFIG,
    )
    assert totals["brand_click_share_visible_queries"] == 30 / 32
    assert totals["nonbrand_click_share_visible_queries"] == 2 / 32


def test_brand_is_not_acquisition_priority():
    item = score_query(row("colixo", 135, 2.5, 30), CONFIG)
    assert item.classification == "brand"
    assert item.score < 0


def test_one_impression_high_fit_query_becomes_emerging_signal():
    item = score_query(row("entreprise de livraison de colis autour de moi", 1, 11), CONFIG)
    priorities, watchlist, emerging = select_opportunities([item], 15, 2)
    assert priorities == []
    assert watchlist == []
    assert [candidate.query for candidate in emerging] == [item.query]
    assert emerging[0].confidence == "very_low"


def test_two_impression_geneva_query_is_low_confidence_priority():
    item = score_query(row("entreprise livraison geneve", 2, 38.5), CONFIG)
    priorities, watchlist, emerging = select_opportunities([item], 15, 2)
    assert [candidate.query for candidate in priorities] == ["entreprise livraison geneve"]
    assert priorities[0].confidence == "low"
    assert watchlist == []
    assert emerging == []


def test_b2b_geo_query_is_prioritized():
    item = score_query(row("entreprise livraison geneve", 20, 18.0), CONFIG)
    priorities, _, _ = select_opportunities([item], 15, 2)
    assert item.classification == "high_fit_b2b"
    assert item.score > 80
    assert item.confidence == "medium"
    assert priorities == [item]


def test_low_fit_pas_cher_is_deprioritized():
    item = score_query(row("colis pas cher", 32, 68.4), CONFIG)
    assert item.classification == "low_fit"
    assert item.score < 10


def test_low_fit_moins_cher_is_not_generic():
    item = score_query(row("envoi moins cher", 32, 18.0), CONFIG)
    priorities, watchlist, emerging = select_opportunities([item], 15, 2)
    assert item.classification == "low_fit"
    assert priorities == []
    assert watchlist == []
    assert emerging == []


def test_geo_without_b2b_is_relevant():
    assert classify_query("livraison Lausanne", CONFIG) == "geo_relevant"


def test_generic_colis_is_watchlist_not_priority():
    item = score_query(row("colis", 200, 12.0), CONFIG)
    priorities, watchlist, emerging = select_opportunities([item], 15, 2)
    assert priorities == []
    assert [candidate.query for candidate in watchlist] == ["colis"]
    assert emerging == []


def test_http_200_identical_to_homepage_is_soft_404_candidate():
    homepage = "<html><head><title>Colixo</title></head><body>Transport suisse</body></html>"
    result = analyze_http_payload(
        "legacy:redirect_candidate",
        "https://www.colixo.ch/ancienne-page",
        200,
        {"Content-Type": "text/html; charset=utf-8"},
        homepage,
        homepage,
    )
    assert result["technical_classification"] == "soft_404_or_fallback_candidate"
    assert result["verdict"] == "warning"


def test_keep_gone_http_200_is_warning_even_when_distinct():
    result = analyze_http_payload(
        "legacy:keep_gone",
        "https://www.colixo.ch/retired",
        200,
        {"Content-Type": "text/html"},
        "<html><title>Still here</title><body>Distinct page</body></html>",
        "<html><title>Home</title><body>Home</body></html>",
    )
    assert result["technical_classification"] == "warning"
    assert result["verdict"] == "warning"


def test_keep_gone_http_404_is_expected_gone():
    result = analyze_http_payload(
        "legacy:keep_gone",
        "https://www.colixo.ch/simulate-price/index.html",
        404,
        {"Content-Type": "text/html"},
        "<html><title>Page introuvable</title></html>",
        "<html><title>Home</title></html>",
    )
    assert result["technical_classification"] == "expected_gone"
    assert result["verdict"] == "expected"


def test_redirect_candidate_to_homepage_is_not_accepted_as_relevant():
    result = analyze_http_payload(
        "legacy:redirect_candidate",
        "https://www.colixo.ch/ancienne-page",
        301,
        {"Content-Type": "text/html", "Location": "https://www.colixo.ch/"},
        "",
    )
    assert result["technical_classification"] == "warning"
    assert result["verdict"] == "warning"


def test_redirect_candidate_internal_non_homepage_301_is_expected():
    result = analyze_http_payload(
        "legacy:redirect_candidate",
        "https://www.colixo.ch/demande-transport.html",
        301,
        {
            "Content-Type": "text/plain",
            "Location": "/portail-client/livraison-colis-suisse-romande",
        },
        "",
    )
    assert result["technical_classification"] == "expected_redirect"
    assert result["verdict"] == "expected"


def test_private_http_200_without_noindex_requires_review():
    result = analyze_http_payload(
        "legacy:private",
        "https://www.colixo.ch/private",
        200,
        {"Content-Type": "text/html"},
        "<html><title>Private page</title></html>",
        "<html><title>Home</title></html>",
    )
    assert result["technical_classification"] == "private_content_review"
    assert result["verdict"] == "review"


def test_private_http_200_with_noindex_still_requires_review():
    result = analyze_http_payload(
        "legacy:private",
        "https://www.colixo.ch/private",
        200,
        {"Content-Type": "text/html"},
        '<html><head><meta name="robots" content="noindex,nofollow"></head></html>',
        "<html><title>Home</title></html>",
    )
    assert result["technical_classification"] == "private_noindex_review"
    assert result["verdict"] == "review"
    assert result["verdict"] != "expected"


def test_sitemap_http_200_with_html_is_error():
    result = analyze_http_payload(
        "sitemap",
        "https://www.colixo.ch/sitemap.xml",
        200,
        {"Content-Type": "text/html"},
        "<html><title>Colixo</title></html>",
    )
    assert result["content_is_html"] is True
    assert result["xml_valid"] is False
    assert result["technical_classification"] == "error"


def test_valid_sitemap_counts_urls():
    content = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.colixo.ch/</loc></url><url><loc>https://www.colixo.ch/a</loc></url></urlset>"""
    result = analyze_http_payload(
        "sitemap",
        "https://www.colixo.ch/sitemap.xml",
        200,
        {"Content-Type": "application/xml"},
        content,
    )
    assert result["xml_valid"] is True
    assert result["url_count"] == 2
    assert result["technical_classification"] == "ok"


def test_robots_requires_text_and_detects_sitemap_directive():
    result = analyze_http_payload(
        "robots",
        "https://www.colixo.ch/robots.txt",
        200,
        {"Content-Type": "text/plain; charset=utf-8"},
        "User-agent: *\nDisallow:\nSitemap: https://www.colixo.ch/sitemap.xml\n",
    )
    assert result["is_text"] is True
    assert result["sitemap_directive_present"] is True
    assert result["technical_classification"] == "ok"


def test_html_metadata_and_fingerprint_are_exported():
    result = analyze_http_payload(
        "legacy:review",
        "https://www.colixo.ch/page",
        200,
        {"Content-Type": "text/html"},
        '<html><head><title> Page test </title><link rel="canonical" href="/canon"><meta name="robots" content="noindex"></head></html>',
        "<html><title>Home</title></html>",
    )
    assert result["title"] == "Page test"
    assert result["canonical_url"] == "https://www.colixo.ch/canon"
    assert result["meta_robots"] == "noindex"
    assert len(result["content_fingerprint"]) == 64


def test_main_makes_two_gsc_queries_and_exports_accuracy_fields(monkeypatch, tmp_path):
    calls = []

    def fake_query(property_name, start, end, dimensions):
        calls.append((property_name, list(dimensions)))
        if not dimensions:
            return [SearchRow((), 40, 200, 0.2, 8.5)]
        return [
            row("colixo", 130, 2.5, 28, 28 / 130),
            row("livraison entreprise", 50, 20, 2, 0.04),
        ]

    monkeypatch.setattr(agent, "ROOT", tmp_path)
    monkeypatch.setattr(agent, "load_config", lambda: CONFIG)
    monkeypatch.setattr(agent, "query_search_analytics", fake_query)
    monkeypatch.setattr(agent, "technical_checks", lambda config: [])
    agent.main()

    assert calls == [
        ("sc-domain:colixo.ch", []),
        ("sc-domain:colixo.ch", ["query"]),
    ]
    payload = json.loads((tmp_path / "reports" / "latest.json").read_text())
    assert payload["property_totals"]["total_clicks"] == 40
    assert payload["visible_query_totals"]["visible_query_clicks"] == 30
    assert payload["unattributed"]["unattributed_clicks"] == 10
    assert payload["unattributed"]["raw_unattributed_clicks"] == 10
    assert payload["unattributed"]["aggregation_inconsistency"] is False
    assert payload["brand_query"]["query"] == "colixo"
    assert "brand_click_share_visible_queries" in payload["visible_query_totals"]


def test_gsc_property_environment_override(monkeypatch):
    monkeypatch.setenv("GSC_PROPERTY", "sc-domain:example.test")
    assert get_property_name({"property": "sc-domain:colixo.ch"}) == "sc-domain:example.test"
