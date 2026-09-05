"""Offline fixture and disabled live Search Console adapters for V3."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Tuple
from urllib.parse import quote

from ..models import Confidence, Evidence, SearchSignal
from .base import FixtureSource, fixture_evidence


GSC_PROPERTY = "sc-domain:colixo.ch"
GSC_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GSC_ENDPOINT = (
    "https://www.googleapis.com/webmasters/v3/sites/{}/searchAnalytics/query"
    .format(quote(GSC_PROPERTY, safe=""))
)
GSC_DIMENSIONS = ("query",)
GSC_ROW_LIMIT = 25000
GSC_REQUEST_TIMEOUT_SECONDS = 30


class GSCDataSourceError(ValueError):
    """Raised when Search Console input or output cannot be trusted."""


@dataclass(frozen=True)
class GSCCollectionCoverage:
    """Aggregate valid-row coverage; no excluded query content is retained."""

    raw_row_count: int
    accepted_signal_count: int
    unmapped_row_count: int
    pii_filtered_row_count: int
    all_rows_clicks: Decimal
    all_rows_impressions: Decimal
    accepted_clicks: Decimal
    accepted_impressions: Decimal
    brand_row_count: int = 0
    brand_clicks: Decimal = Decimal(0)
    brand_impressions: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.raw_row_count != (
            self.accepted_signal_count
            + self.brand_row_count
            + self.unmapped_row_count
            + self.pii_filtered_row_count
        ):
            raise GSCDataSourceError("Search Console coverage invariant is invalid")


@dataclass(frozen=True)
class GSCCollectionResult:
    signals: Tuple[SearchSignal, ...]
    coverage: GSCCollectionCoverage


_EMAIL_QUERY = re.compile(
    r"(?i)(?<![\w.+-])[a-z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-z0-9-]+(?:\.[a-z0-9-]+)+(?![\w.-])"
)
_PHONE_QUERY = re.compile(
    r"(?<!\w)(?:(?:\+|00)\s*)?\d(?:[\s()./-]*\d){7,}(?!\w)"
)

# Ordered from the most specific commercial intent to the general service.
_TOPIC_PHRASES = (
    (
        "secure_watch_delivery",
        (
            "envoi securise",
            "livraison securisee",
            "transport securise",
            "livraison horlogerie",
            "transport horlogerie",
            "livraison montre",
            "transport montre",
        ),
    ),
    (
        "wine_delivery",
        (
            "livraison vin",
            "livraison vins",
            "livraison de vin",
            "livraison de vins",
            "transport vin",
            "transport vins",
            "envoi vin",
            "envoi vins",
        ),
    ),
    (
        "business_delivery",
        (
            "livraison entreprise",
            "livraison pour entreprise",
            "transport entreprise",
            "service livraison entreprise",
            "livraison b2b",
        ),
    ),
    (
        "parcel_delivery",
        (
            "livraison colis",
            "transport colis",
            "envoi colis",
        ),
    ),
)
_GENERAL_DELIVERY_QUERIES = frozenset({
    "livraison",
    "service livraison",
    "service de livraison",
    "livraison suisse",
    "livraison suisse romande",
    "livraison rapide",
    "livraison express",
})


class SearchConsoleFixtureSource(FixtureSource[SearchSignal]):
    def collect(self, fixture: Iterable[Mapping[str, Any]]) -> Tuple[SearchSignal, ...]:
        return tuple(
            SearchSignal(
                topic=str(item["topic"]),
                query=str(item["query"]),
                clicks=item.get("clicks"),
                impressions=item.get("impressions"),
                ctr=item.get("ctr"),
                average_position=item.get("average_position"),
                evidence=fixture_evidence(item.get("evidence", ())),
            )
            for item in fixture
        )


def classify_search_query_topic(query: str) -> Optional[str]:
    """Return a reviewable commercial topic, or ``None`` for unknown intent."""

    normalized = _normalize_query(query)
    if not normalized or _contains_obvious_pii(query) or is_brand_query(query):
        return None
    tokens = set(normalized.split())
    # Hiring intent is not a request for transport services.
    if tokens & {"emploi", "emplois", "recrutement", "stage", "stages"}:
        return None
    intent_rules = {
        "secure_watch_delivery": (
            {"horlogerie", "montre", "montres"},
            {"livraison", "transport", "envoi", "securise", "securisee"},
        ),
        "wine_delivery": ({"vin", "vins"}, {"livraison", "transport", "envoi"}),
        "business_delivery": (
            {"entreprise", "entreprises", "professionnel", "professionnels", "b2b"},
            {"livraison", "livrer", "transport", "colis", "coursier"},
        ),
        "parcel_delivery": ({"colis"}, {"livraison", "livrer", "transport", "envoi"}),
    }
    for topic, phrases in _TOPIC_PHRASES:
        subjects, actions = intent_rules[topic]
        if (tokens & subjects and tokens & actions) or any(
            _contains_phrase(normalized, phrase) for phrase in phrases
        ):
            return topic
    if normalized in _GENERAL_DELIVERY_QUERIES:
        return "general_delivery"
    return None


def is_brand_query(query: str) -> bool:
    """Detect standalone brand navigation independently of word order."""
    return "colixo" in _normalize_query(query).split()


def create_google_search_console_transport() -> Any:
    """Explicitly construct an ADC AuthorizedSession for a future live gate.

    This factory is unreachable from current V3 runtimes. Google modules,
    authentication, and transport construction therefore do not occur on import.
    """

    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
    except ImportError:
        raise GSCDataSourceError("Google authentication support is unavailable") from None
    try:
        credentials, _ = google.auth.default(scopes=(GSC_READ_ONLY_SCOPE,))
        return AuthorizedSession(credentials)
    except Exception:
        raise GSCDataSourceError(
            "Search Console Application Default Credentials are unavailable"
        ) from None


class GoogleSearchConsoleDataSource:
    """Read aggregate query metrics through an injected HTTP transport.

    The source is deliberately absent from ``build_source_adapters`` and cannot
    be selected by the currently authorized OFFLINE and GA4_READ_ONLY runtimes.
    """

    def __init__(self, *, transport: Any, observed_at: str) -> None:
        if transport is None or not callable(getattr(transport, "post", None)):
            raise GSCDataSourceError("an injected Search Console transport is required")
        self.observed_at = _validate_observed_at(observed_at)
        observed_date = date.fromisoformat(self.observed_at)
        self.start_date = (observed_date - timedelta(days=28)).isoformat()
        self.end_date = (observed_date - timedelta(days=3)).isoformat()
        self.transport = transport
        self.property = GSC_PROPERTY
        self.endpoint = GSC_ENDPOINT

    def query_payload(self) -> Mapping[str, Any]:
        return {
            "startDate": self.start_date,
            "endDate": self.end_date,
            "dimensions": list(GSC_DIMENSIONS),
            "type": "web",
            "dataState": "final",
            "rowLimit": GSC_ROW_LIMIT,
            "startRow": 0,
        }

    def collect(self) -> Tuple[SearchSignal, ...]:
        return self.collect_with_coverage().signals

    def collect_with_coverage(self) -> GSCCollectionResult:
        """Collect once using the same strict parser and commercial filters."""
        response = self._request_once()
        payload = self._response_payload(response)
        rows = payload.get("rows", ())
        if rows is None:
            rows = ()
        if not isinstance(rows, (list, tuple)):
            raise GSCDataSourceError("Search Console response schema is invalid")

        signals = []
        raw_row_count = unmapped_row_count = pii_filtered_row_count = 0
        all_rows_clicks = all_rows_impressions = Decimal(0)
        accepted_clicks = accepted_impressions = Decimal(0)
        brand_row_count = 0
        brand_clicks = brand_impressions = Decimal(0)
        for row in rows:
            query, clicks, impressions, ctr, position = self._parse_row(row)
            raw_row_count += 1
            all_rows_clicks += clicks
            all_rows_impressions += impressions
            if _contains_obvious_pii(query):
                pii_filtered_row_count += 1
                continue
            if is_brand_query(query):
                brand_row_count += 1
                brand_clicks += clicks
                brand_impressions += impressions
                continue
            topic = classify_search_query_topic(query)
            if topic is None:
                unmapped_row_count += 1
                continue
            accepted_clicks += clicks
            accepted_impressions += impressions
            fact = {
                "query": query,
                "clicks": float(clicks),
                "impressions": float(impressions),
                "ctr": float(ctr),
                "average_position": float(position),
                "date_range": {
                    "start_date": self.start_date,
                    "end_date": self.end_date,
                },
                "property": self.property,
                "provenance": "gsc_search_analytics_api",
            }
            signals.append(SearchSignal(
                topic=topic,
                query=query,
                clicks=float(clicks),
                impressions=float(impressions),
                ctr=float(ctr),
                average_position=float(position),
                evidence=(Evidence(
                    source="google_search_console",
                    observed_at=self.observed_at,
                    metric="search_query_aggregate",
                    fact=fact,
                    confidence=Confidence.HIGH,
                ),),
            ))
        return GSCCollectionResult(
            signals=tuple(sorted(signals, key=lambda signal: (signal.topic, signal.query))),
            coverage=GSCCollectionCoverage(
                raw_row_count=raw_row_count,
                accepted_signal_count=len(signals),
                unmapped_row_count=unmapped_row_count,
                pii_filtered_row_count=pii_filtered_row_count,
                all_rows_clicks=all_rows_clicks,
                all_rows_impressions=all_rows_impressions,
                accepted_clicks=accepted_clicks,
                accepted_impressions=accepted_impressions,
                brand_row_count=brand_row_count,
                brand_clicks=brand_clicks,
                brand_impressions=brand_impressions,
            ),
        )

    def _request_once(self) -> Any:
        try:
            return self.transport.post(
                self.endpoint,
                json=self.query_payload(),
                timeout=GSC_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception:
            raise GSCDataSourceError("Search Console API request failed") from None

    @staticmethod
    def _response_payload(response: Any) -> Mapping[str, Any]:
        if type(getattr(response, "status_code", None)) is not int:
            raise GSCDataSourceError("Search Console API response is invalid")
        if response.status_code != 200:
            raise GSCDataSourceError("Search Console API request failed")
        try:
            payload = response.json()
        except Exception:
            raise GSCDataSourceError("Search Console response JSON is invalid") from None
        if not isinstance(payload, dict):
            raise GSCDataSourceError("Search Console response schema is invalid")
        return payload

    @staticmethod
    def _parse_row(
        row: Any,
    ) -> Tuple[str, Decimal, Decimal, Decimal, Decimal]:
        if not isinstance(row, dict) or set(row) != {
            "keys", "clicks", "impressions", "ctr", "position"
        }:
            raise GSCDataSourceError("Search Console row schema is invalid")
        keys = row["keys"]
        if not isinstance(keys, (list, tuple)) or len(keys) != 1:
            raise GSCDataSourceError("Search Console row query key is invalid")
        query = keys[0]
        if not isinstance(query, str) or not query.strip():
            raise GSCDataSourceError("Search Console row query key is invalid")
        query = query.strip()

        clicks = _parse_metric(row["clicks"], "clicks", allow_zero=True)
        impressions = _parse_metric(
            row["impressions"], "impressions", allow_zero=True
        )
        ctr = _parse_metric(row["ctr"], "ctr", allow_zero=True)
        if ctr > Decimal(1):
            raise GSCDataSourceError("Search Console row metric is invalid")
        position = _parse_metric(row["position"], "position", allow_zero=False)
        return query, clicks, impressions, ctr, position


def _validate_observed_at(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise GSCDataSourceError("observed_at must be a real ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise GSCDataSourceError(
            "observed_at must be a real ISO date (YYYY-MM-DD)"
        ) from None
    return value


def _parse_metric(value: Any, name: str, *, allow_zero: bool) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise GSCDataSourceError("Search Console row metric is invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise GSCDataSourceError("Search Console row metric is invalid") from None
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise GSCDataSourceError("Search Console row metric is invalid")
    return parsed


def _normalize_query(query: Any) -> str:
    if not isinstance(query, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", query.casefold())
    without_accents = "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", without_accents).split())


def _contains_phrase(query: str, phrase: str) -> bool:
    return " {} ".format(phrase) in " {} ".format(query)


def _contains_obvious_pii(query: Any) -> bool:
    if not isinstance(query, str):
        return False
    return bool(_EMAIL_QUERY.search(query) or _PHONE_QUERY.search(query))
