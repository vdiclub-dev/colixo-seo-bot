from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable
from urllib.parse import quote

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


@dataclass(frozen=True)
class SearchRow:
    keys: tuple[str, ...]
    clicks: float
    impressions: float
    ctr: float
    position: float


def build_search_analytics_payload(
    start_date: date,
    end_date: date,
    dimensions: Iterable[str],
    row_limit: int = 25000,
) -> dict[str, Any]:
    dims = list(dimensions)
    payload: dict[str, Any] = {
        "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(),
        "type": "web",
        "dataState": "final",
        "rowLimit": row_limit,
        "startRow": 0,
    }
    if dims:
        payload["dimensions"] = dims
    return payload


def _credentials():
    from google.oauth2 import service_account
    raw = os.getenv("GSC_SERVICE_ACCOUNT_JSON", "").strip()
    filename = os.getenv("GSC_SERVICE_ACCOUNT_FILE", "").strip()
    if raw:
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("GSC_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
        return service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    if filename:
        return service_account.Credentials.from_service_account_file(filename, scopes=[SCOPE])
    raise RuntimeError(
        "Missing Search Console credentials. Set GSC_SERVICE_ACCOUNT_JSON or GSC_SERVICE_ACCOUNT_FILE."
    )


def query_search_analytics(
    property_name: str,
    start_date: date,
    end_date: date,
    dimensions: Iterable[str],
    row_limit: int = 25000,
) -> list[SearchRow]:
    if not property_name:
        raise ValueError("property_name is required")
    endpoint = (
        "https://www.googleapis.com/webmasters/v3/sites/"
        f"{quote(property_name, safe='')}/searchAnalytics/query"
    )
    # Search Console property totals require a genuinely dimensionless query.
    # Sending an empty dimensions array is not equivalent on every API version.
    payload = build_search_analytics_payload(start_date, end_date, dimensions, row_limit)
    from google.auth.transport.requests import AuthorizedSession

    session = AuthorizedSession(_credentials())
    response = session.post(endpoint, json=payload, timeout=45)
    response.raise_for_status()
    data = response.json()
    rows: list[SearchRow] = []
    for item in data.get("rows", []):
        rows.append(
            SearchRow(
                keys=tuple(str(value) for value in item.get("keys", [])),
                clicks=float(item.get("clicks", 0.0)),
                impressions=float(item.get("impressions", 0.0)),
                ctr=float(item.get("ctr", 0.0)),
                position=float(item.get("position", 0.0)),
            )
        )
    return rows
