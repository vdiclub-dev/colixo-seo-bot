from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gsc_client import SearchRow, query_search_analytics  # noqa: E402


@dataclass(frozen=True)
class Opportunity:
    query: str
    clicks: float
    impressions: float
    ctr: float
    position: float
    classification: str
    confidence: str
    score: float
    rationale: str


class PageMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.canonical = ""
        self.meta_robots = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): (value or "") for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "title":
            self._in_title = True
        elif lowered == "link" and "canonical" in values.get("rel", "").casefold().split():
            self.canonical = values.get("href", "").strip()
        elif lowered == "meta" and values.get("name", "").casefold() == "robots":
            self.meta_robots = values.get("content", "").strip()

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def load_config() -> dict[str, Any]:
    config_path = Path(os.getenv("SEO_AGENT_CONFIG", ROOT / "config" / "seo_agent_v2.json"))
    return json.loads(config_path.read_text(encoding="utf-8"))


def get_property_name(config: dict[str, Any]) -> str:
    return os.getenv("GSC_PROPERTY", "").strip() or str(config["property"])


def normalize(text: str) -> str:
    return " ".join(text.casefold().strip().split())


def contains_any(query: str, terms: list[str]) -> bool:
    q = normalize(query)
    return any(normalize(term) in q for term in terms)


def classify_query(query: str, config: dict[str, Any]) -> str:
    q = normalize(query)
    if any(normalize(term) in q for term in config["brand_terms"]):
        return "brand"
    if contains_any(q, config["low_fit_terms"]):
        return "low_fit"
    if contains_any(q, config["high_intent_terms"]):
        return "high_fit_b2b"
    if contains_any(q, config["geo_terms"]):
        return "geo_relevant"
    return "generic"


def position_value(position: float) -> float:
    if position <= 0:
        return 0.0
    if 4 <= position <= 10:
        return 32.0
    if 10 < position <= 20:
        return 50.0
    if 20 < position <= 40:
        return 38.0
    if 40 < position <= 60:
        return 20.0
    if position > 60:
        return 7.0
    return 10.0


def confidence_for_impressions(impressions: float) -> str:
    if impressions < 2:
        return "very_low"
    if impressions < 10:
        return "low"
    if impressions < 50:
        return "medium"
    return "high"


def score_query(row: SearchRow, config: dict[str, Any]) -> Opportunity:
    query = row.keys[0] if row.keys else ""
    classification = classify_query(query, config)
    score = position_value(row.position)
    score += min(22.0, math.log1p(max(row.impressions, 0.0)) * 5.2)

    rationale: list[str] = []
    if classification == "brand":
        score -= 55.0
        rationale.append("requête de marque: utile pour la réputation, pas pour l'acquisition")
    elif classification == "high_fit_b2b":
        score += 38.0
        rationale.append("intention B2B/commerciale forte")
    elif classification == "geo_relevant":
        score += 22.0
        rationale.append("intention géographique pertinente")
    elif classification == "low_fit":
        score -= 32.0
        rationale.append("intention faible marge / peu alignée avec le positionnement B2B")
    else:
        score += 5.0
        rationale.append("requête générique")

    if contains_any(query, config["geo_terms"]) and classification != "low_fit":
        score += 10.0
        rationale.append("signal local Suisse romande")
    if 10 < row.position <= 30:
        score += 12.0
        rationale.append("proche du Top 10: gain potentiellement rapide")
    if row.impressions < float(config.get("min_impressions_for_priority", 2)):
        score -= 8.0
        rationale.append("volume observé encore faible")

    return Opportunity(
        query=query,
        clicks=row.clicks,
        impressions=row.impressions,
        ctr=row.ctr,
        position=row.position,
        classification=classification,
        confidence=confidence_for_impressions(row.impressions),
        score=round(score, 1),
        rationale="; ".join(rationale),
    )


def property_totals(rows: list[SearchRow]) -> dict[str, float]:
    if len(rows) > 1:
        raise RuntimeError("Dimensionless Search Console query returned multiple rows")
    row = rows[0] if rows else SearchRow((), 0.0, 0.0, 0.0, 0.0)
    return {
        "total_clicks": row.clicks,
        "total_impressions": row.impressions,
        "ctr": row.ctr,
        "position": row.position,
    }


def aggregate_visible_queries(
    rows: list[SearchRow], config: dict[str, Any]
) -> dict[str, float]:
    totals = {
        "visible_query_clicks": 0.0,
        "visible_query_impressions": 0.0,
        "brand_clicks": 0.0,
        "brand_impressions": 0.0,
        "nonbrand_clicks": 0.0,
        "nonbrand_impressions": 0.0,
    }
    for row in rows:
        totals["visible_query_clicks"] += row.clicks
        totals["visible_query_impressions"] += row.impressions
        q = row.keys[0] if row.keys else ""
        if classify_query(q, config) == "brand":
            totals["brand_clicks"] += row.clicks
            totals["brand_impressions"] += row.impressions
        else:
            totals["nonbrand_clicks"] += row.clicks
            totals["nonbrand_impressions"] += row.impressions
    totals["nonbrand_impression_share_visible_queries"] = (
        totals["nonbrand_impressions"] / totals["visible_query_impressions"]
        if totals["visible_query_impressions"]
        else 0.0
    )
    totals["nonbrand_click_share_visible_queries"] = (
        totals["nonbrand_clicks"] / totals["visible_query_clicks"]
        if totals["visible_query_clicks"]
        else 0.0
    )
    totals["brand_click_share_visible_queries"] = (
        totals["brand_clicks"] / totals["visible_query_clicks"]
        if totals["visible_query_clicks"]
        else 0.0
    )
    return totals


def unattributed_metrics(
    all_property_totals: dict[str, float], visible_totals: dict[str, float]
) -> dict[str, float]:
    return {
        "unattributed_clicks": (
            all_property_totals["total_clicks"] - visible_totals["visible_query_clicks"]
        ),
        "unattributed_impressions": (
            all_property_totals["total_impressions"]
            - visible_totals["visible_query_impressions"]
        ),
    }


def exact_brand_query(rows: list[SearchRow], query: str = "colixo") -> dict[str, Any] | None:
    expected = normalize(query)
    for row in rows:
        actual = row.keys[0] if row.keys else ""
        if normalize(actual) == expected:
            return {
                "query": actual,
                "clicks": row.clicks,
                "impressions": row.impressions,
                "ctr": row.ctr,
                "position": row.position,
            }
    return None


def select_opportunities(
    scored: list[Opportunity], top_opportunities: int, min_impressions_for_priority: float
) -> tuple[list[Opportunity], list[Opportunity], list[Opportunity]]:
    priorities = [
        item
        for item in scored
        if item.classification in {"high_fit_b2b", "geo_relevant"}
        and item.score > 0
        and item.impressions >= min_impressions_for_priority
    ]
    emerging = [
        item
        for item in scored
        if item.classification in {"high_fit_b2b", "geo_relevant"}
        and item.score > 0
        and item.impressions < min_impressions_for_priority
    ]
    watchlist = [item for item in scored if item.classification == "generic" and item.score > 0]
    sort_key = lambda item: (item.score, item.impressions)
    priorities.sort(key=sort_key, reverse=True)
    emerging.sort(key=sort_key, reverse=True)
    watchlist.sort(key=sort_key, reverse=True)
    return (
        priorities[:top_opportunities],
        watchlist[:top_opportunities],
        emerging[:top_opportunities],
    )


def normalized_html(content: str) -> str:
    without_comments = re.sub(r"<!--.*?-->", " ", content, flags=re.DOTALL)
    return " ".join(without_comments.casefold().split())


def content_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def parse_html_metadata(content: str, page_url: str) -> dict[str, str]:
    parser = PageMetadataParser()
    parser.feed(content)
    return {
        "title": " ".join(parser.title.split()),
        "canonical_url": urljoin(page_url, parser.canonical) if parser.canonical else "",
        "meta_robots": parser.meta_robots,
    }


def looks_like_html(content_type: str, content: str) -> bool:
    lowered = content.lstrip().casefold()
    return "text/html" in content_type.casefold() or lowered.startswith(("<!doctype html", "<html"))


def _redirect_is_relevant(page_url: str, location: str) -> bool:
    if not location:
        return False
    source = urlparse(page_url)
    target = urlparse(urljoin(page_url, location))
    if target.scheme not in {"http", "https"} or target.netloc != source.netloc:
        return False
    if target.geturl() == source.geturl():
        return False
    # Redirecting every retired URL to the homepage is another fallback signal,
    # not a relevant replacement for a legacy landing page.
    return target.path.rstrip("/") not in {"", "/"}


def analyze_http_payload(
    label: str,
    page_url: str,
    status: int,
    headers: dict[str, str],
    content: str,
    homepage_content: str = "",
) -> dict[str, Any]:
    content_type = headers.get("Content-Type", headers.get("content-type", ""))
    location = headers.get("Location", headers.get("location", ""))
    result: dict[str, Any] = {
        "label": label,
        "url": page_url,
        "status": status,
        "location": location,
        "content_type": content_type,
        "content_fingerprint": content_fingerprint(content),
    }

    if label == "robots":
        is_text = "text/plain" in content_type.casefold() and not looks_like_html(content_type, content)
        sitemap_directive = bool(re.search(r"(?im)^\s*sitemap\s*:\s*\S+", content))
        result.update(
            {
                "is_text": is_text,
                "sitemap_directive_present": sitemap_directive,
                "technical_classification": "ok" if status == 200 and is_text else "error",
            }
        )
        return result

    if label == "sitemap":
        is_html = looks_like_html(content_type, content)
        xml_valid = False
        url_count = 0
        if status == 200 and not is_html:
            try:
                root = ElementTree.fromstring(content)
                xml_valid = True
                url_count = sum(1 for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "loc")
            except ElementTree.ParseError:
                pass
        result.update(
            {
                "xml_valid": xml_valid,
                "url_count": url_count,
                "content_is_html": is_html,
                "technical_classification": (
                    "ok" if status == 200 and xml_valid and not is_html else "error"
                ),
            }
        )
        return result

    if looks_like_html(content_type, content):
        result.update(parse_html_metadata(content, page_url))
    else:
        result.update({"title": "", "canonical_url": "", "meta_robots": ""})

    normalized = normalized_html(content) if content else ""
    normalized_home = normalized_html(homepage_content) if homepage_content else ""
    similarity = (
        SequenceMatcher(None, normalized_home, normalized).ratio()
        if normalized_home and normalized
        else 0.0
    )
    result["homepage_similarity"] = round(similarity, 4)

    if label == "homepage":
        result["technical_classification"] = "ok" if status == 200 else "error"
        return result

    policy = label.split(":", 1)[1] if label.startswith("legacy:") else "review"
    fallback = status == 200 and similarity >= 0.95
    if fallback:
        result["technical_classification"] = "soft_404_or_fallback_candidate"
        result["verdict"] = "warning"
        return result

    if policy == "keep_gone":
        result["technical_classification"] = "expected_gone" if status in {404, 410} else "warning"
        result["verdict"] = "expected" if status in {404, 410} else "warning"
    elif policy == "redirect_candidate":
        if status in {301, 308} and _redirect_is_relevant(page_url, location):
            result["technical_classification"] = "expected_redirect"
            result["verdict"] = "expected"
        elif status == 200:
            result["technical_classification"] = "distinct_page_review"
            result["verdict"] = "review"
        else:
            result["technical_classification"] = "warning"
            result["verdict"] = "warning"
    elif policy == "private":
        noindex = "noindex" in result.get("meta_robots", "").casefold()
        if status == 200 and noindex:
            result["technical_classification"] = "private_noindex"
            result["verdict"] = "expected"
        elif status in {301, 302, 303, 307, 308} and location:
            result["technical_classification"] = "private_redirect"
            result["verdict"] = "expected"
        else:
            result["technical_classification"] = "private_content_review"
            result["verdict"] = "review"
    elif policy == "private_or_redirect":
        if status in {301, 302, 303, 307, 308} and location:
            result["technical_classification"] = "expected_redirect"
            result["verdict"] = "expected"
        else:
            result["technical_classification"] = "private_or_redirect_review"
            result["verdict"] = "review"
    else:
        result["technical_classification"] = "distinct_page_review" if status == 200 else "warning"
        result["verdict"] = "review" if status == 200 else "warning"
    return result


def technical_checks(config: dict[str, Any]) -> list[dict[str, Any]]:
    base = config["site_base_url"].rstrip("/")
    targets = [
        {"label": "homepage", "url": base + "/"},
        {"label": "robots", "url": base + "/robots.txt"},
        {"label": "sitemap", "url": base + "/sitemap.xml"},
    ]
    for legacy in config.get("legacy_urls", []):
        targets.append(
            {
                "label": f"legacy:{legacy['policy']}",
                "url": base + legacy["path"],
            }
        )

    results: list[dict[str, Any]] = []
    homepage_content = ""
    for target in targets:
        try:
            resp = requests.get(
                target["url"],
                timeout=15,
                allow_redirects=False,
                headers={"User-Agent": "ColixoSEOAgent/2.0 (+https://www.colixo.ch)"},
            )
            content = resp.text
            result = analyze_http_payload(
                target["label"],
                target["url"],
                resp.status_code,
                dict(resp.headers),
                content,
                homepage_content,
            )
            results.append(result)
            if target["label"] == "homepage" and resp.status_code == 200:
                homepage_content = content
        except requests.RequestException as exc:
            results.append(
                {
                    "label": target["label"],
                    "url": target["url"],
                    "status": 0,
                    "location": "",
                    "content_type": "",
                    "content_fingerprint": "",
                    "technical_classification": "error",
                    "error": type(exc).__name__,
                }
            )
    return results


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_report(
    start: date,
    end: date,
    all_property_totals: dict[str, float],
    visible_totals: dict[str, float],
    unattributed: dict[str, float],
    brand_query: dict[str, Any] | None,
    opportunities: list[Opportunity],
    emerging_signals: list[Opportunity],
    watchlist: list[Opportunity],
    tech: list[dict[str, Any]],
) -> str:
    lines = [
        "# Colixo SEO Agent v2 — rapport hebdomadaire",
        "",
        f"Période Search Console : **{start.isoformat()} → {end.isoformat()}**",
        "",
        "## Totaux de la propriété",
        "",
        "Requête Search Analytics sans dimension :",
        "",
        f"- Clics totaux : **{all_property_totals['total_clicks']:.0f}**",
        f"- Impressions totales : **{all_property_totals['total_impressions']:.0f}**",
        f"- CTR : **{pct(all_property_totals['ctr'])}**",
        f"- Position moyenne : **{all_property_totals['position']:.1f}**",
        "",
        "## Lignes de requêtes visibles",
        "",
        f"- Clics attribués aux requêtes visibles : **{visible_totals['visible_query_clicks']:.0f}**",
        f"- Impressions attribuées aux requêtes visibles : **{visible_totals['visible_query_impressions']:.0f}**",
        f"- Clics non attribuables : **{unattributed['unattributed_clicks']:.0f}**",
        f"- Impressions non attribuables : **{unattributed['unattributed_impressions']:.0f}**",
        "- Les données non attribuables ne sont classées ni comme marque ni comme hors marque.",
        f"- Part des impressions hors marque parmi les requêtes visibles : **{pct(visible_totals['nonbrand_impression_share_visible_queries'])}**",
        f"- Part des clics hors marque parmi les requêtes visibles : **{pct(visible_totals['nonbrand_click_share_visible_queries'])}**",
        f"- Part des clics marque parmi les requêtes visibles : **{pct(visible_totals['brand_click_share_visible_queries'])}**",
        "",
        "## Performance marque",
        "",
    ]

    if brand_query is None:
        lines += ["La requête exacte `colixo` n'est pas présente dans les lignes visibles.", ""]
    else:
        lines += [
            "| Requête | Clics | Impressions | CTR | Position moyenne |",
            "|---|---:|---:|---:|---:|",
            f"| {brand_query['query']} | {brand_query['clicks']:.0f} | "
            f"{brand_query['impressions']:.0f} | {pct(brand_query['ctr'])} | "
            f"{brand_query['position']:.1f} |",
            "",
        ]

    if visible_totals["nonbrand_click_share_visible_queries"] < 0.35:
        lines += [
            "> ⚠️ **Diagnostic des requêtes visibles :** Colixo dépend encore fortement des recherches de marque. "
            "La priorité reste la découverte par des prospects qui ne connaissent pas encore Colixo.",
            "",
        ]

    lines += ["## Opportunités commerciales prioritaires", ""]
    if not opportunities:
        lines.append("Aucune opportunité exploitable détectée sur la période.")
    else:
        lines += [
            "| Priorité | Requête | Position | Impressions | Clics | Classe | Confiance | Pourquoi |",
            "|---:|---|---:|---:|---:|---|---|---|",
        ]
        for idx, item in enumerate(opportunities, 1):
            rationale = item.rationale.replace("|", "/")
            lines.append(
                f"| {idx} | {item.query} | {item.position:.1f} | {item.impressions:.0f} | "
                f"{item.clicks:.0f} | {item.classification} | {item.confidence} | {rationale} |"
            )

    lines += ["", "## Signaux émergents", ""]
    if not emerging_signals:
        lines.append("Aucun signal émergent détecté sous le seuil d'impressions.")
    else:
        lines += [
            "| Requête | Position | Impressions | Clics | Classe | Confiance | Pourquoi |",
            "|---|---:|---:|---:|---|---|---|",
        ]
        for item in emerging_signals:
            rationale = item.rationale.replace("|", "/")
            lines.append(
                f"| {item.query} | {item.position:.1f} | {item.impressions:.0f} | "
                f"{item.clicks:.0f} | {item.classification} | {item.confidence} | {rationale} |"
            )

    lines += ["", "## Requêtes génériques à surveiller", ""]
    if not watchlist:
        lines.append("Aucune requête générique à surveiller détectée sur la période.")
    else:
        lines += [
            "| Requête | Position | Impressions | Clics | Score | Confiance | Pourquoi |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
        for item in watchlist:
            rationale = item.rationale.replace("|", "/")
            lines.append(
                f"| {item.query} | {item.position:.1f} | {item.impressions:.0f} | "
                f"{item.clicks:.0f} | {item.score:.1f} | {item.confidence} | {rationale} |"
            )

    lines += ["", "## Contrôles techniques", ""]
    lines += [
        "| URL | HTTP | Type | Location | Title | Canonical | Meta robots | Fingerprint | Classification | Détails |",
        "|---|---:|---|---|---|---|---|---|---|---|",
    ]
    for item in tech:
        details: list[str] = []
        if item["label"] == "robots":
            details = [
                f"text={str(item.get('is_text', False)).lower()}",
                f"sitemap_directive={str(item.get('sitemap_directive_present', False)).lower()}",
            ]
        elif item["label"] == "sitemap":
            details = [
                f"xml_valid={str(item.get('xml_valid', False)).lower()}",
                f"url_count={item.get('url_count', 0)}",
                f"html={str(item.get('content_is_html', False)).lower()}",
            ]
        elif "homepage_similarity" in item:
            details = [f"homepage_similarity={item['homepage_similarity']:.4f}"]
        cells = [
            item["url"],
            str(item["status"]),
            str(item.get("content_type", "")),
            str(item.get("location", "")) or "—",
            str(item.get("title", "")) or "—",
            str(item.get("canonical_url", "")) or "—",
            str(item.get("meta_robots", "")) or "—",
            str(item.get("content_fingerprint", "")) or "—",
            str(item.get("technical_classification", "")),
            "; ".join(details) or "—",
        ]
        lines.append("| " + " | ".join(cell.replace("|", "%7C") for cell in cells) + " |")

    lines += [
        "",
        "## Garde-fous",
        "",
        "- **Aucune modification du site n'est publiée par cet agent.**",
        "- Aucun backlink n'est créé automatiquement.",
        "- Les requêtes de type « pas cher » sont volontairement dépriorisées.",
        "- Toute future modification de contenu doit passer par une branche, des tests et une PR avant PROD.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    config = load_config()
    lag = int(config.get("data_lag_days", 3))
    lookback = int(config.get("lookback_days", 90))
    end = date.today() - timedelta(days=lag)
    start = end - timedelta(days=lookback - 1)

    property_name = get_property_name(config)
    property_rows = query_search_analytics(property_name, start, end, [])
    query_rows = query_search_analytics(property_name, start, end, ["query"])
    all_property_totals = property_totals(property_rows)
    visible_totals = aggregate_visible_queries(query_rows, config)
    unattributed = unattributed_metrics(all_property_totals, visible_totals)
    brand_query = exact_brand_query(query_rows)
    scored = [score_query(row, config) for row in query_rows]
    opportunities, watchlist, emerging_signals = select_opportunities(
        scored,
        int(config.get("top_opportunities", 15)),
        float(config.get("min_impressions_for_priority", 2)),
    )

    tech = technical_checks(config)
    report = render_report(
        start,
        end,
        all_property_totals,
        visible_totals,
        unattributed,
        brand_query,
        opportunities,
        emerging_signals,
        watchlist,
        tech,
    )

    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "latest.md").write_text(report, encoding="utf-8")
    payload = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "property_totals": all_property_totals,
        "visible_query_totals": visible_totals,
        "unattributed": unattributed,
        "brand_query": brand_query,
        "opportunities": [asdict(item) for item in opportunities],
        "emerging_signals": [asdict(item) for item in emerging_signals],
        "watchlist": [asdict(item) for item in watchlist],
        "technical_checks": tech,
    }
    (reports_dir / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(report)


if __name__ == "__main__":
    main()
