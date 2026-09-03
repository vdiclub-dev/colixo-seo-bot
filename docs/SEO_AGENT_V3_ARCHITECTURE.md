# Colixo SEO / Market Intelligence Agent V3 — Phase 1 + GA4 Phase 2C foundation

## Purpose and boundary

V3 is developed beside the production V2 agent. It does not import, modify, or
replace V2. Phase 1 is a foundation that turns local fixtures into normalized
signals, deterministic opportunity scores, and human-reviewable proposals.

The operating contract is **read-only** and **proposal-only**. There is no
workflow, schedule, credential, remote connector, publication path, price
change, or automatic commercial action in this phase.

## Data flow

1. An offline source adapter receives a local fixture.
2. The adapter validates and normalizes it into an immutable signal model.
3. Signals are grouped by topic; every observation is aggregated and counted.
4. The scoring layer evaluates each known dimension independently.
5. Unknown values remain `unknown` and are excluded from the weighted mean.
6. Evidence confidence caps recommendation strength.
7. The report labels observations as `FACT`, interpretations as `INFERENCE`,
   and proposed human actions as `RECOMMENDATION`.

The same interface can support future authorized connectors without changing
the normalized models or scoring contract. Adding a connector will require a
separate authorization, security review, and tests.

## Source adapters

Phase 1 adapters live under `scripts/v3/sources/` and accept only local fixture
objects:

- Search Console query aggregates
- analytics aggregates
- rank-tracking aggregates
- public competitor observations
- aggregated public-review observations
- aggregated Colixo business metrics

They contain no HTTP client, browser automation, Supabase client, or service
account handling. The Search Console adapter is conceptually compatible with
V2 observations but does not import or mutate the V2 implementation.

## Evidence and confidence

Every evidence item records its source, observation time, metric or fact,
confidence, and an optional public reference. Missing metrics are never
estimated. Confidence levels are `very_low`, `low`, `medium`, and `high`.
Evidence rows are averaged within each source/provenance first, then every
source receives equal weight. A verbose source therefore cannot overwhelm a
second source merely by emitting more evidence rows.

A strong recommendation requires all of the following:

- score of at least 75;
- evidence confidence of at least `medium`;
- at least four known scoring dimensions.

The weights and all three strong-recommendation thresholds are loaded from
`config/seo_agent_v3.json`; invalid or absent configuration fails closed. This
is a safety ceiling, not a claim that the proposed action is correct. Human
review remains mandatory.

## Opportunity scoring

The deterministic 0–100 score keeps seven dimensions separate:

- search demand;
- rank opportunity;
- commercial fit;
- conversion signal;
- competitive gap;
- reputation gap;
- evidence confidence.

Each known dimension maps to a fixed point value and configured weight. Unknown
dimensions receive neither points nor weight. Search volumes and business
aggregates are summed, rank observations are averaged, and competitor gap
levels are averaged. Every source count—including competitor and review
counts—is exposed in the score explanation. These operations are commutative,
so fixture order cannot affect the result.

Review reputation first applies a per-platform reliability rule. A missing or
non-positive `review_count` remains unknown. Samples below five reviews are
capped at a low gap regardless of rating. A high gap requires at least 50
reviews, multiple consistent negative themes, a rating below 3.5, and at least
medium declared confidence. Multiple platforms are then combined with bounded
square-root sample weights and a confidence factor; `observation_window`
remains attached to each normalized review signal for provenance.

## Privacy contract

Review intelligence stores only platform, competitor, average rating, review
count, observation window, short synthetic positive/negative topics, and
confidence. It must not store author names, profiles, or full review text.

Business intelligence stores only aggregates such as organic sessions,
pricing simulations, accounts created, orders started/completed, commercial
contacts, revenue, and margin. Client names, email addresses, postal addresses,
and parcel identifiers are outside the model and prohibited.

## Future public competitor intelligence

Future sources may be considered only after explicit authorization:

- public web, service, tariff, FAQ, terms, and local pages;
- public announcements and job offers;
- public, aggregated reviews;
- public search results through an authorized provider or API.

The following remain prohibited:

- bypassing authentication or accessing private areas;
- acquiring trade secrets or impersonating users;
- bypassing CAPTCHAs or other access controls;
- aggressive automation;
- collecting unnecessary personal data or individual profiles.

Any future ingestion must respect source terms, rate limits, data minimization,
retention controls, provenance, and deletion procedures.

## Report contract

The Markdown renderer always emits ten stable sections: executive summary,
search demand, rank opportunities, conversion signals, competitive gaps,
customer reputation signals, commercial value, recommended actions, unknown or
insufficient evidence, and safety/data provenance.

Reports are analytical artifacts only. They do not publish to Colixo, modify
SEO content, change pricing, contact customers, or trigger deployments.

## GA4 Data API adapter — Phase 2C, present but disabled

Phase 2C adds a read-only Google Analytics Data API adapter for property
`552715460` (`properties/552715460`). It is not wired into the V3 agent: the
active analytics source remains `local_fixture`, `network_enabled` remains
`false`, and importing V3 creates neither a Google client nor credentials.

The adapter accepts an injected client and an explicit date range. Its future
opt-in live factory uses the official `google-analytics-data` client with
Application Default Credentials. Authentication is intentionally not part of
this phase; a later gate will use OIDC / Workload Identity Federation. No
service-account key, inline credential JSON, OAuth token, or GitHub secret is
accepted or committed.

Two deterministic aggregate reports are defined:

- acquisition: dimension `sessionDefaultChannelGroup`;
- landing-page topics: dimensions `landingPage` and
  `sessionDefaultChannelGroup`;
- both: metrics `sessions`, `engagedSessions`, and `keyEvents`;
- both: exact channel filter `Organic Search`.

Known public landing pages map to explicit commercial topics. Unknown paths,
legal pages, and `(not set)` are excluded from commercial signals; no topic is
inferred from query strings or free text. `landingPagePlusQueryString`,
`pageLocation`, `pagePath`, user identifiers, client IDs, email, phone, fine
geography, device identifiers, `userPseudoId`, `transactionId`, and other
PII-bearing fields are never requested.

GA4 `sessions` grouped by `pagePath` are not additive because one session may
visit several pages. V3 therefore attributes Organic Search sessions by
`landingPage`, the first page of the session. A topic's `organic_sessions`
means Organic Search sessions whose landing page maps explicitly to that V3
topic; it is not a pageview count. The acquisition report supplies global
Organic Search totals that bound the retained commercial landing-page totals
without requiring equality.

GA4 aggregates map to the existing `TrafficSignal` model as follows:

- `sessions` → `organic_sessions`;
- `engagedSessions` → `engaged_sessions`;
- `keyEvents` → the historical V3 field `conversions`.

Here, `conversions` means GA4 key events; the model is not renamed in Phase 2C.
Evidence records `google_analytics_4`, provenance `ga4_data_api`, property ID,
date range, dimensions, metrics, channel, and public page paths only. Malformed
responses, missing metrics, unsafe paths, unexpected dimensions or channels,
missing clients, unavailable credentials, and API failures all fail closed.
