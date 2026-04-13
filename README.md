# Colixo SEO Bot

Bot Python d'automatisation SEO pour le site statique `colixo-site`.

## Ce que fait le bot

- génère des pages SEO locales pour la Suisse romande
- génère des articles de blog SEO
- met à jour `sitemap.xml`
- clone le repo `colixo-site`
- copie les fichiers générés
- commit et push automatiquement

## Stack

- Python 3.11
- OpenAI API
- GitHub Actions
- python-dotenv

## Installation locale

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Variables d'environnement

- `OPENAI_API_KEY`
- `OPENAI_MODEL`
- `SITE_REPO_URL`
- `SITE_REPO_PAT`
- `GIT_AUTHOR_NAME`
- `GIT_AUTHOR_EMAIL`

## Lancer la pipeline

```bash
python scripts/run_seo_pipeline.py
```

## Workflow GitHub Actions

Le workflow d'exemple `.github/workflows/seo.yml.example` :

- tourne chaque semaine
- peut aussi être lancé à la main
- génère les pages et articles
- met à jour le sitemap
- pousse automatiquement les changements dans `colixo-site`

Quand votre token GitHub dispose du scope `workflow`, renommez ce fichier en `.github/workflows/seo.yml`.
