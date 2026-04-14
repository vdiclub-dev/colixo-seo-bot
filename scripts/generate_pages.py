from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from common import GENERATED_DIR, ROOT_DIR, bootstrap_env, generate_json_payload, load_prompt, load_settings, write_text


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <meta name="description" content="{meta_description}">
  <link rel="stylesheet" href="assets/css/style.css">
</head>
<body>
  <header class="site-header">
    <div class="container nav-wrap">
      <a class="brand" href="index.html"><img src="assets/img/logo.png" alt="Logo Colixo"><span>Colixo</span></a>
      <button class="nav-toggle" aria-label="Ouvrir le menu">Menu</button>
      <nav class="site-nav">
        <a href="index.html">Accueil</a>
        <a href="tarifs.html">Tarifs</a>
        <a href="contact.html">Contact</a>
        <a href="blog/index.html">Blog</a>
        <a class="btn btn-small" href="contact.html">Être contacté</a>
      </nav>
    </div>
  </header>

  <main class="page-main">
    <section class="page-hero">
      <div class="container">
        <p class="eyebrow">{hero_eyebrow}</p>
        <h1>{h1}</h1>
        <p class="lead">{lead}</p>
      </div>
    </section>

    <section class="section">
      <div class="container">
        {sections_html}
        <p><a class="btn" href="contact.html">{cta_text}</a></p>
      </div>
    </section>

    <section class="section section-alt">
      <div class="container">
        <h2>Pages utiles</h2>
        <div class="city-grid">
          {internal_links_html}
        </div>
      </div>
    </section>
  </main>

  <footer class="site-footer">
    <div class="container footer-grid">
      <div>
        <strong>{company_name}</strong>
        <p>Livraison de colis pour entreprises en Suisse romande.</p>
      </div>
      <div>
        <strong>Coordonnées</strong>
        <p>{company_email}<br>{company_phone}<br>{company_address_line_1}<br>{company_address_line_2}</p>
      </div>
      <div>
        <strong>Navigation</strong>
        <p><a href="tarifs.html">Tarifs</a><br><a href="contact.html">Contact</a><br><a href="blog/index.html">Blog</a></p>
      </div>
    </div>
  </footer>

  <script src="assets/js/main.js"></script>
</body>
</html>
"""

OVERRIDES_DIR = ROOT_DIR / "page_overrides" / "pages"
PRIORITY_PAGE_SLUGS = {
    "livraison-geneve",
    "livraison-lausanne",
    "livraison-fribourg",
    "livraison-neuchatel",
    "livraison-valais",
    "livraison-ecommerce",
    "livraison-pharmacies",
    "livraison-garages",
    "livraison-entreprise",
    "livraison-express",
    "transport-colis",
}


def render_sections(sections: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for section in sections:
        heading = str(section["h2"])
        paragraphs = "".join(f"<p>{paragraph}</p>" for paragraph in section["paragraphs"])
        parts.append(f"<h2>{heading}</h2>{paragraphs}")
    return "".join(parts)


def build_prompt(base_prompt: str, replacements: dict[str, str]) -> str:
    prompt = base_prompt
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)
    return prompt


def format_focus_points(values: list[str]) -> str:
    if not values:
        return "service fiable, réactivité, simplicité, accompagnement"
    return ", ".join(values)


def sector_page_slug(sector: dict[str, Any]) -> str:
    raw_slug = str(sector["slug"])
    return raw_slug if raw_slug.startswith("livraison-") else f"livraison-{raw_slug}"


def mark_secondary_page(html: str, slug: str) -> str:
    if slug in PRIORITY_PAGE_SLUGS:
        return html
    return html.replace(
        "<body>\n",
        "<body>\n  <!-- Secondary SEO page kept out of primary navigation to limit duplication risk. -->\n",
        1,
    )


def page_link(href: str, label: str) -> str:
    return f'<a class="city-link" href="{href}">{label}</a>'


def build_internal_links(page: dict[str, str]) -> str:
    slug = page["page_slug"]
    if page["page_type"] == "city":
        links = [
            page_link("livraison-lausanne.html" if slug != "livraison-lausanne" else "livraison-geneve.html", "Autre ville: Lausanne" if slug != "livraison-lausanne" else "Autre ville: Genève"),
            page_link("livraison-ecommerce.html", "Métier: e-commerce"),
            page_link("livraison-entreprise.html", "Service: livraison entreprise"),
        ]
    elif page["page_type"] == "sector":
        links = [
            page_link("livraison-geneve.html", "Ville: Genève"),
            page_link("livraison-pharmacies.html" if slug != "livraison-pharmacies" else "livraison-garages.html", "Métier: pharmacies" if slug != "livraison-pharmacies" else "Métier: garages"),
            page_link("livraison-express.html", "Service: livraison express"),
        ]
    else:
        links = [
            page_link("livraison-geneve.html", "Ville: Genève"),
            page_link("livraison-ecommerce.html", "Métier: e-commerce"),
            page_link("livraison-entreprise.html" if slug != "livraison-entreprise" else "transport-colis.html", "Service: livraison entreprise" if slug != "livraison-entreprise" else "Service: transport de colis"),
        ]
    return "\n          ".join(links)


def build_page_specs(settings: dict[str, Any]) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = []

    for city in settings.get("cities", []):
        specs.append(
            {
                "page_type": "city",
                "target_name": city["name"],
                "page_slug": city["slug"],
                "target_region": settings.get("target_region", "Suisse romande"),
                "positioning": settings.get("positioning", ""),
                "specific_angle": city.get("local_angle", ""),
                "focus_points": format_focus_points(city.get("local_focus", [])),
                "cta": city.get("cta_text", settings.get("main_cta", "Demander un devis")),
            }
        )

    for sector in settings.get("sectors", []):
        specs.append(
            {
                "page_type": "sector",
                "target_name": sector["name"],
                "page_slug": sector_page_slug(sector),
                "target_region": settings.get("target_region", "Suisse romande"),
                "positioning": settings.get("positioning", ""),
                "specific_angle": f"solution de livraison adaptée au secteur {sector['name']}",
                "focus_points": format_focus_points(sector.get("needs", [])),
                "cta": settings.get("main_cta", "Demander un devis"),
            }
        )

    for service_page in settings.get("service_pages", []):
        specs.append(
            {
                "page_type": "service",
                "target_name": service_page["name"],
                "page_slug": service_page["slug"],
                "target_region": settings.get("target_region", "Suisse romande"),
                "positioning": settings.get("positioning", ""),
                "specific_angle": f"page de service dédiée à {service_page['name']}",
                "focus_points": format_focus_points(service_page.get("focus", [])),
                "cta": settings.get("secondary_cta", settings.get("main_cta", "Être contacté")),
            }
        )

    return specs


def generate_pages() -> None:
    bootstrap_env()
    settings = load_settings()
    company = settings.get("company", {})
    prompt_template = load_prompt("landing_page.txt")
    output_dir = GENERATED_DIR / "pages"
    output_dir.mkdir(parents=True, exist_ok=True)

    for page in build_page_specs(settings):
        override_path = OVERRIDES_DIR / f"{page['page_slug']}.html"
        if override_path.exists():
            write_text(output_dir / override_path.name, override_path.read_text(encoding="utf-8"))
            print(f"Copied override page: {override_path.name}")
            continue

        payload = generate_json_payload(
            build_prompt(
                prompt_template,
                {
                    "{page_type}": page["page_type"],
                    "{target_name}": page["target_name"],
                    "{page_slug}": page["page_slug"],
                    "{target_region}": page["target_region"],
                    "{positioning}": page["positioning"],
                    "{specific_angle}": page["specific_angle"],
                    "{focus_points}": page["focus_points"],
                    "{cta}": page["cta"],
                    "{brand_name}": settings.get("brand_name", "Colixo"),
                },
            ),
            settings,
        )
        html = PAGE_TEMPLATE.format(
            title=payload["title"],
            meta_description=payload["meta_description"],
            hero_eyebrow=payload["hero_eyebrow"],
            h1=payload["h1"],
            lead=payload["lead"],
            sections_html=render_sections(payload["sections"]),
            internal_links_html=build_internal_links(page),
            cta_text=payload["cta_text"],
            company_name=company.get("name", "Colixo"),
            company_email=company.get("email", "contact@colixo.ch"),
            company_phone=company.get("phone", "+41 22 000 00 00"),
            company_address_line_1=company.get("address_line_1", "Suisse romande"),
            company_address_line_2=company.get("address_line_2", ""),
        )
        html = mark_secondary_page(html, page["page_slug"])
        write_text(output_dir / f"{page['page_slug']}.html", html)
        print(f"Generated {page['page_type']} page: {page['page_slug']}.html")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SEO pages for cities, sectors, and services.")
    parser.parse_args()
    generate_pages()
