"""Coverage contracts exercised exclusively through fake transports."""

from dataclasses import FrozenInstanceError, asdict, fields, replace
from decimal import Decimal

import pytest

from scripts.v3.sources.search_console import (
    GSCCollectionCoverage,
    GSCCollectionResult,
    GSCDataSourceError,
    GSC_ENDPOINT,
    GSC_REQUEST_TIMEOUT_SECONDS,
    GoogleSearchConsoleDataSource,
)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeTransport:
    def __init__(self, payload, fail=False):
        self.payload = payload
        self.fail = fail
        self.calls = []

    def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        if self.fail:
            raise RuntimeError("fake transport failure")
        return FakeResponse(self.payload)


def row(query="livraison colis", clicks="0.1", impressions="10.2"):
    return dict(keys=[query], clicks=clicks, impressions=impressions,
                ctr="0.01", position="3.5")


def source(payload, *, fail=False):
    transport = FakeTransport(payload, fail=fail)
    return GoogleSearchConsoleDataSource(
        transport=transport, observed_at="2026-09-04"
    ), transport


@pytest.mark.parametrize("payload", [{}, {"rows": []}, {"rows": None}])
def test_empty_coverage_is_valid_and_zero(payload):
    adapter, transport = source(payload)
    result = adapter.collect_with_coverage()
    assert result.signals == ()
    assert result.coverage == GSCCollectionCoverage(
        0, 0, 0, 0, Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    )
    assert len(transport.calls) == 1


@pytest.mark.parametrize("query,counts", [
    ("livraison colis", (1, 0, 0)),
    ("weather forecast", (0, 1, 0)),
    ("livraison colis sample@example.test", (0, 0, 1)),
    ("livraison colis +41 79 123 45 67", (0, 0, 1)),
])
def test_single_row_partition_and_metrics(query, counts):
    adapter, _ = source({"rows": [row(query)]})
    result = adapter.collect_with_coverage()
    coverage = result.coverage
    assert coverage.raw_row_count == 1
    assert (coverage.accepted_signal_count, coverage.unmapped_row_count,
            coverage.pii_filtered_row_count) == counts
    assert len(result.signals) == counts[0]
    assert coverage.all_rows_clicks == Decimal("0.1")
    assert coverage.all_rows_impressions == Decimal("10.2")
    assert coverage.accepted_clicks == (Decimal("0.1") if counts[0] else 0)
    assert coverage.accepted_impressions == (Decimal("10.2") if counts[0] else 0)


def mixed_rows():
    return [row("livraison vin", "0.1", "10.1"),
            row("livraison colis", "0.2", "20.2"),
            row("weather forecast", "0.3", "30.3"),
            row("sample@example.test", "0.4", "40.4"),
            row("+41 79 123 45 67", "0.5", "50.5")]


def test_mixed_rows_have_exact_partition_and_decimal_totals():
    adapter, transport = source({"rows": mixed_rows()})
    result = adapter.collect_with_coverage()
    assert result.coverage == GSCCollectionCoverage(
        5, 2, 1, 2, Decimal("1.5"), Decimal("151.5"),
        Decimal("0.3"), Decimal("30.3"),
    )
    assert result.coverage.raw_row_count == (
        result.coverage.accepted_signal_count + result.coverage.unmapped_row_count
        + result.coverage.pii_filtered_row_count
    )
    assert len(transport.calls) == 1
    assert transport.calls[0] == (GSC_ENDPOINT, {
        "startDate": "2026-08-07", "endDate": "2026-09-01",
        "dimensions": ["query"], "type": "web", "dataState": "final",
        "rowLimit": 25000, "startRow": 0,
    }, GSC_REQUEST_TIMEOUT_SECONDS)


def test_coverage_and_result_are_frozen_and_only_have_permitted_fields():
    adapter, _ = source({"rows": mixed_rows()})
    result = adapter.collect_with_coverage()
    assert {f.name for f in fields(result)} == {"signals", "coverage"}
    assert {f.name for f in fields(result.coverage)} == {
        "raw_row_count", "accepted_signal_count", "unmapped_row_count",
        "pii_filtered_row_count", "all_rows_clicks", "all_rows_impressions",
        "accepted_clicks", "accepted_impressions",
    }
    assert all(type(value) in (int, Decimal)
               for value in asdict(result.coverage).values())
    with pytest.raises(FrozenInstanceError):
        result.coverage.raw_row_count = 99
    with pytest.raises(FrozenInstanceError):
        result.signals = ()
    for excluded in ("sample@example.test", "+41 79 123 45 67", "weather forecast"):
        assert excluded not in repr(result)


def test_invalid_partition_fails_closed():
    adapter, _ = source({})
    with pytest.raises(GSCDataSourceError, match="coverage invariant"):
        replace(adapter.collect_with_coverage().coverage, raw_row_count=1)


def test_collect_preserves_signals_evidence_and_shares_coverage_semantics(monkeypatch):
    adapter, transport = source({"rows": mixed_rows()})
    result = adapter.collect_with_coverage()
    legacy, legacy_transport = source({"rows": mixed_rows()})
    assert legacy.collect() == result.signals
    assert len(legacy_transport.calls) == len(transport.calls) == 1
    assert [signal.topic for signal in result.signals] == ["parcel_delivery", "wine_delivery"]
    parcel = result.signals[0]
    assert parcel.query == "livraison colis"
    assert (parcel.clicks, parcel.impressions, parcel.ctr, parcel.average_position) == (
        0.2, 20.2, 0.01, 3.5
    )
    assert parcel.evidence[0].fact["query"] == "livraison colis"
    assert parcel.evidence[0].observed_at == "2026-09-04"
    monkeypatch.setattr(adapter, "collect_with_coverage", lambda: result)
    assert adapter.collect() is result.signals
    assert len(transport.calls) == 1


def test_row_order_does_not_change_result():
    forward, _ = source({"rows": mixed_rows()})
    reverse, _ = source({"rows": list(reversed(mixed_rows()))})
    assert forward.collect_with_coverage() == reverse.collect_with_coverage()


@pytest.mark.parametrize("method", ["collect", "collect_with_coverage"])
@pytest.mark.parametrize("bad_row", [None, {}, {"keys": []}, "invalid"])
def test_malformed_rows_never_skipped(method, bad_row):
    adapter, transport = source({"rows": [row(), bad_row]})
    with pytest.raises(GSCDataSourceError):
        getattr(adapter, method)()
    assert len(transport.calls) == 1


@pytest.mark.parametrize("metric", ["clicks", "impressions", "ctr", "position"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", -1, True, None, "bad"])
def test_invalid_metrics_fail_before_any_coverage_is_returned(metric, value):
    invalid = row("sample@example.test")
    invalid[metric] = value
    adapter, transport = source({"rows": [row(), invalid]})
    with pytest.raises(GSCDataSourceError, match="row metric"):
        adapter.collect_with_coverage()
    assert len(transport.calls) == 1


@pytest.mark.parametrize("method", ["collect", "collect_with_coverage"])
def test_transport_failure_is_not_retried(method):
    adapter, transport = source({}, fail=True)
    with pytest.raises(GSCDataSourceError, match="API request failed"):
        getattr(adapter, method)()
    assert len(transport.calls) == 1
