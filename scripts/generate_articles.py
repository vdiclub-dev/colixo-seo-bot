from __future__ import annotations

import argparse

from common import GENERATED_DIR, bootstrap_env, generate_json_payload, load_prompt, load_settings, write_text


ARTICLE_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <meta name="description" content="{meta_description}">
  <link rel="stylesheet" href="../assets/css/style.css">
</head>
<body>
  <header class="site-header">
    <div class="container nav-wrap">
      <a class="brand" href="../index.html"><img src="../assets/img/logo.png" alt="Logo Colixo"><span>Colixo</span></a>
      <button class="nav-toggle" aria-label="Ouvrir le menu">Menu</button>
      <nav class="site-nav">
        <a href="../index.html">Accueil</a>
        <a href="../tarifs.html">Tarifs</a>
        <a href="../contact.html">Contact</a>
        <a href="index.html">Blog</a>
        <a class="btn btn-small" href="../contact.html">Être contacté</a>
      </nav>
    </div>
  </header>

  <main class="page-main">
    <article class="section article">
      <div class="container narrow">
        <p class="eyebrow">{hero_eyebrow}</p>
        <h1>{h1}</h1>
        <p>{intro}</p>
        {sections_html}
        <p><a class="btn" href="../contact.html">{cta_text}</a></p>
      </div>
    </article>
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
        <p><a href="index.html">Blog</a><br><a href="../contact.html">Contact</a><br><a href="../tarifs.html">Tarifs</a></p>
      </div>
    </div>
  </footer>

  <script src="../assets/js/main.js"></script>
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


def build_prompt(base_prompt: str, article: dict[str, str]) -> str:
    prompt = base_prompt
    replacements = {
        "{article_title}": article["title_hint"],
        "{article_slug}": article["slug"],
        "{search_angle}": article["search_angle"],
        "{cta}": article["cta_text"],
    }
    for placeholder, value in replacements.items():
        prompt = prompt.replace(placeholder, value)
    return prompt


def generate_articles() -> None:
    bootstrap_env()
    settings = load_settings()
    company = settings.get("company", {})
    prompt_template = load_prompt("blog_article.txt")
    output_dir = GENERATED_DIR / "blog"
    output_dir.mkdir(parents=True, exist_ok=True)

    for article in settings["articles"]:
        payload = generate_json_payload(build_prompt(prompt_template, article), settings)
        html = ARTICLE_TEMPLATE.format(
            title=payload["title"],
            meta_description=payload["meta_description"],
            hero_eyebrow=payload["hero_eyebrow"],
            h1=payload["h1"],
            intro=payload["intro"],
            sections_html=render_sections(payload["sections"]),
            cta_text=payload["cta_text"],
            company_name=company.get("name", "Colixo"),
            company_email=company.get("email", "contact@colixo.ch"),
            company_phone=company.get("phone", "+41 22 000 00 00"),
            company_address_line_1=company.get("address_line_1", "Suisse romande"),
            company_address_line_2=company.get("address_line_2", ""),
        )
        write_text(output_dir / f"{article['slug']}.html", html)
        print(f"Generated article: {article['slug']}.html")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SEO blog articles for colixo-site.")
    parser.parse_args()
    generate_articles()
