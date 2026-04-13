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

- `OPENAI_API_KEY` : clé API OpenAI utilisée pour générer les pages et articles
- `OPENAI_MODEL` : modèle OpenAI à utiliser, par exemple `gpt-5.4-mini`
- `SITE_REPO_URL` : URL HTTPS du dépôt `colixo-site`
- `SITE_REPO_PAT` : token GitHub avec accès en écriture au repo `colixo-site`
- `GIT_AUTHOR_NAME` : nom utilisé pour les commits automatiques
- `GIT_AUTHOR_EMAIL` : email utilisé pour les commits automatiques

## Liaison avec le dépôt colixo-site

Le bot est prévu pour travailler avec le dépôt :

```text
https://github.com/vdiclub-dev/colixo-site.git
```

À chaque exécution de la pipeline :

1. le repo `colixo-site` est cloné automatiquement dans `tmp/colixo-site`
2. les pages générées sont copiées à la racine du site
3. les articles générés sont copiés dans le dossier `blog/`
4. `sitemap.xml` est recalculé
5. un `git add`, `commit` et `push` sont lancés automatiquement

Le token GitHub n'est jamais écrit en dur dans le code. Il est lu via `SITE_REPO_PAT`, utilisé pour le clonage, puis le remote git est immédiatement réécrit sans credentials.

Si aucun changement n'est détecté après copie et mise à jour du sitemap, le bot n'effectue aucun commit ni push.

## Exemple de configuration locale

Crée un fichier `.env` à partir de `.env.example` puis remplis :

```env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.4-mini
SITE_REPO_URL=https://github.com/vdiclub-dev/colixo-site.git
SITE_REPO_PAT=github_pat_...
GIT_AUTHOR_NAME=Colixo SEO Bot
GIT_AUTHOR_EMAIL=seo-bot@colixo.ch
```

## Accès GitHub recommandé

Pour `SITE_REPO_PAT`, utilise un token GitHub dédié avec :

- accès au dépôt `colixo-site`
- permission `Contents: Read and write`

Si tu veux activer GitHub Actions plus tard dans `colixo-seo-bot`, le token utilisé pour pousser des fichiers dans `.github/workflows/` devra aussi avoir le scope `workflow`.

## Lancer la pipeline

```bash
python scripts/run_seo_pipeline.py
```

Tu peux aussi lancer les étapes séparément :

```bash
python scripts/generate_pages.py
python scripts/generate_articles.py
python scripts/push_to_site_repo.py
```

## Workflow GitHub Actions

Le workflow d'exemple `workflow-examples/seo.yml.example` :

- tourne chaque semaine
- peut aussi être lancé à la main
- génère les pages et articles
- met à jour le sitemap
- pousse automatiquement les changements dans `colixo-site`

Quand votre token GitHub dispose du scope `workflow`, copiez ce fichier vers `.github/workflows/seo.yml`.
