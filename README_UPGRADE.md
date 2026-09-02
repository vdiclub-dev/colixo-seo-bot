# Colixo SEO Agent v2 — safe upgrade

## Goal

Replace the old "generate and push pages" behavior with a safer SEO loop:

**MONITOR → ANALYSE → PROPOSE → HUMAN APPROVAL → PR → PROD**

Version 2 does **not** modify the Colixo production site. It reads Google Search Console, checks public technical URLs, scores commercial opportunities, and posts a weekly report in a GitHub issue.

## Why this upgrade is necessary

The current SEO repository still contains a legacy publishing path aimed at an older site repository. The production homepage currently lives in the actively maintained Colixo application repository, while the old SEO generator was designed around legacy/static SEO pages. V2 therefore starts read-only and separates SEO intelligence from production publishing.

## Security gate — do this before enabling the workflow

1. Rotate any credentials that have ever been committed to the repository history or to a public `.env.example`.
2. Replace `.env.example` with the sanitized file in this package.
3. Store credentials only as GitHub Actions secrets.
4. Do not reuse a compromised token, even after deleting it from the latest commit: Git history may still contain it.

## Google Search Console one-time setup

1. In Google Cloud, create a project and enable the Search Console API.
2. Create a service account and a JSON key.
3. In Search Console, grant that service-account email access to the `colixo.ch` Domain property.
4. Add the complete JSON key as the GitHub Actions secret `GSC_SERVICE_ACCOUNT_JSON`.
5. The agent uses the read-only scope `https://www.googleapis.com/auth/webmasters.readonly` and property `sc-domain:colixo.ch`.

## Files to install

- `scripts/gsc_client.py`
- `scripts/seo_agent_v2.py`
- `config/seo_agent_v2.json`
- `tests/test_seo_agent_v2.py`
- `requirements-v2.txt`
- `.github/workflows/seo.yml` (replace the old scheduled workflow)
- `.env.example` (replace and sanitize)

## What the report contains

- branded vs non-branded clicks/impressions;
- B2B and geographic query classification;
- opportunity scoring biased toward positions 10–30 and real commercial intent;
- deliberate de-prioritization of low-fit "pas cher" traffic;
- homepage / robots / sitemap checks;
- status checks for the 13 legacy URLs already seen in Search Console;
- no automatic content publishing and no automatic backlinking.

## Controlled activation

The scheduled run is intentionally disabled in this version. After merge, configure the
read-only `GSC_SERVICE_ACCOUNT_JSON` secret, run `workflow_dispatch` manually, and validate
the real report. Enable the weekly schedule only through a separate reviewed pull request.

## Acceptance gate

Before merge:

```bash
python -m pip install -r requirements-v2.txt
pytest -q tests/test_seo_agent_v2.py
```

Then run the workflow manually once. It must:

- authenticate to `sc-domain:colixo.ch`;
- generate `reports/latest.md` and `reports/latest.json`;
- post a GitHub issue/comment;
- make **zero writes** to the production site repository;
- expose no credentials in logs.

## Phase 2 (only after 2–4 weeks of clean reports)

The agent may prepare a PR for one high-value SEO change at a time. It should never merge or deploy to production by itself. Content claims, prices, geographic coverage, delivery SLAs and customer proof must be validated against real Colixo data before publication.
