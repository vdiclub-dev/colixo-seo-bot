from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

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
    score: float
    rationale: str


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
        score=round(score, 1),
        rationale="; ".join(rationale),
    )


def aggregate(rows: list[SearchRow], config: dict[str, Any]) -> dict[str, float]:
    totals = {
        "clicks": 0.0,
        "impressions": 0.0,
        "brand_clicks": 0.0,
        "brand_impressions": 0.0,
        "nonbrand_clicks": 0.0,
        "nonbrand_impressions": 0.0,
    }
    for row in rows:
        totals["clicks"] += row.clicks
        totals["impressions"] += row.impressions
        q = row.keys[0] if row.keys else ""
        if classify_query(q, config) == "brand":
            totals["brand_clicks"] += row.clicks
            totals["brand_impressions"] += row.impressions
        else:
            totals["nonbrand_clicks"] += row.clicks
            totals["nonbrand_impressions"] += row.impressions
    totals["nonbrand_impression_share"] = (
        totals["nonbrand_impressions"] / totals["impressions"] if totals["impressions"] else 0.0
    )
    totals["nonbrand_click_share"] = (
        totals["nonbrand_clicks"] / totals["clicks"] if totals["clicks"] else 0.0
    )
    return totals


def select_opportunities(
    scored: list[Opportunity], top_opportunities: int
) -> tuple[list[Opportunity], list[Opportunity]]:
    priorities = [
        item
        for item in scored
        if item.classification in {"high_fit_b2b", "geo_relevant"} and item.score > 0
    ]
    watchlist = [item for item in scored if item.classification == "generic" and item.score > 0]
    sort_key = lambda item: (item.score, item.impressions)
    priorities.sort(key=sort_key, reverse=True)
    watchlist.sort(key=sort_key, reverse=True)
    return priorities[:top_opportunities], watchlist[:top_opportunities]


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
    for target in targets:
        try:
            resp = requests.get(
                target["url"],
                timeout=15,
                allow_redirects=False,
                headers={"User-Agent": "ColixoSEOAgent/2.0 (+https://www.colixo.ch)"},
            )
            results.append(
                {
                    "label": target["label"],
                    "url": target["url"],
                    "status": resp.status_code,
                    "location": resp.headers.get("Location", ""),
                }
            )
        except requests.RequestException as exc:
            results.append(
                {
                    "label": target["label"],
                    "url": target["url"],
                    "status": 0,
                    "location": "",
                    "error": str(exc),
                }
            )
    return results


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_report(
    start: date,
    end: date,
    totals: dict[str, float],
    opportunities: list[Opportunity],
    watchlist: list[Opportunity],
    tech: list[dict[str, Any]],
) -> str:
    lines = [
        "# Colixo SEO Agent v2 — rapport hebdomadaire",
        "",
        f"Période Search Console : **{start.isoformat()} → {end.isoformat()}**",
        "",
        "## Acquisition organique",
        "",
        f"- Clics : **{totals['clicks']:.0f}**",
        f"- Impressions : **{totals['impressions']:.0f}**",
        f"- Part des impressions hors marque : **{pct(totals['nonbrand_impression_share'])}**",
        f"- Part des clics hors marque : **{pct(totals['nonbrand_click_share'])}**",
        "",
    ]

    if totals["nonbrand_click_share"] < 0.35:
        lines += [
            "> ⚠️ **Diagnostic acquisition :** Colixo dépend encore fortement des recherches de marque. "
            "La priorité reste la découverte par des prospects qui ne connaissent pas encore Colixo.",
            "",
        ]

    lines += ["## Opportunités commerciales prioritaires", ""]
    if not opportunities:
        lines.append("Aucune opportunité exploitable détectée sur la période.")
    else:
        lines += [
            "| Priorité | Requête | Position | Impressions | Clics | Classe | Pourquoi |",
            "|---:|---|---:|---:|---:|---|---|",
        ]
        for idx, item in enumerate(opportunities, 1):
            rationale = item.rationale.replace("|", "/")
            lines.append(
                f"| {idx} | {item.query} | {item.position:.1f} | {item.impressions:.0f} | "
                f"{item.clicks:.0f} | {item.classification} | {rationale} |"
            )

    lines += ["", "## Requêtes génériques à surveiller", ""]
    if not watchlist:
        lines.append("Aucune requête générique à surveiller détectée sur la période.")
    else:
        lines += [
            "| Requête | Position | Impressions | Clics | Score | Pourquoi |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for item in watchlist:
            rationale = item.rationale.replace("|", "/")
            lines.append(
                f"| {item.query} | {item.position:.1f} | {item.impressions:.0f} | "
                f"{item.clicks:.0f} | {item.score:.1f} | {rationale} |"
            )

    lines += ["", "## Contrôles techniques", ""]
    lines += ["| URL | Statut | Redirection | Politique |", "|---|---:|---|---|"]
    for item in tech:
        policy = item["label"].split(":", 1)[1] if item["label"].startswith("legacy:") else item["label"]
        location = str(item.get("location", "")).replace("|", "%7C")
        lines.append(f"| {item['url']} | {item['status']} | {location or '—'} | {policy} |")

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
    query_rows = query_search_analytics(property_name, start, end, ["query"])
    totals = aggregate(query_rows, config)
    scored = [score_query(row, config) for row in query_rows]
    opportunities, watchlist = select_opportunities(
        scored, int(config.get("top_opportunities", 15))
    )

    tech = technical_checks(config)
    report = render_report(start, end, totals, opportunities, watchlist, tech)

    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "latest.md").write_text(report, encoding="utf-8")
    payload = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "totals": totals,
        "opportunities": [asdict(item) for item in opportunities],
        "watchlist": [asdict(item) for item in watchlist],
        "technical_checks": tech,
    }
    (reports_dir / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(report)


if __name__ == "__main__":
    main()
