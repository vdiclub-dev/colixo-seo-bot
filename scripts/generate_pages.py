from __future__ import annotations

import argparse

from common import GENERATED_DIR, bootstrap_env, generate_json_payload, load_prompt, load_settings, write_text


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
  </main>

  <footer class="site-footer">
    <div class="container footer-grid">
      <div>
        <strong>Colixo</strong>
        <p>Livraison de colis pour entreprises en Suisse romande.</p>
      </div>
      <div>
        <strong>Coordonnées</strong>
        <p>contact@colixo.ch<br>+41 22 000 00 00<br>Suisse romande</p>
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


def render_sections(sections: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for section in sections:
        heading = str(section["h2"])
        paragraphs = "".join(f"<p>{paragraph}</p>" for paragraph in section["paragraphs"])
        parts.append(f"<h2>{heading}</h2>{paragraphs}")
    return "".join(parts)


def build_prompt(base_prompt: str, city: dict[str, str]) -> str:
    return base_prompt.format(
        city_name=city["name"],
        city_slug=city["slug"],
        local_angle=city["local_angle"],
        cta=city["cta_text"],
    )


def generate_pages() -> None:
    bootstrap_env()
    settings = load_settings()
    prompt_template = load_prompt("seo_page.txt")
    output_dir = GENERATED_DIR / "pages"
    output_dir.mkdir(parents=True, exist_ok=True)

    for city in settings["cities"]:
        payload = generate_json_payload(build_prompt(prompt_template, city), settings)
        html = PAGE_TEMPLATE.format(
            title=payload["title"],
            meta_description=payload["meta_description"],
            hero_eyebrow=payload["hero_eyebrow"],
            h1=payload["h1"],
            lead=payload["lead"],
            sections_html=render_sections(payload["sections"]),
            cta_text=payload["cta_text"],
        )
        write_text(output_dir / f"{city['slug']}.html", html)
        print(f"Generated page: {city['slug']}.html")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SEO city pages for colixo-site.")
    parser.parse_args()
    generate_pages()
