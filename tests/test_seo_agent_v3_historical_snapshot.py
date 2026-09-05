"""Synthetic contract checks only: no network, database or persistence."""

import ast
import hashlib
import json
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from scripts.v3.agent import AgentResult
from scripts.v3.config import RuntimeState, SOURCE_NAMES, load_v3_config
from scripts.v3.models import Confidence, DimensionLevel, Evidence, OpportunityScore, Recommendation, SearchSignal
from scripts.v3.source_factory import GSCRuntimeCoverage
from scripts.v3 import historical_snapshot as h


MARKERS = ("livraison colis synthétique", "person@example.invalid", "+41 79 123 45 67",
           "https://example.invalid/private", "credential-marker-zeta", "raw-response-marker-zeta")
ROOT = Path(__file__).resolve().parents[1]


def result(topics=(), *, coverage=None, gsc=False):
    scores = tuple(OpportunityScore(
        topic, *([DimensionLevel.UNKNOWN] * 7), 40, Confidence.LOW,
        MARKERS, (),
    ) for topic in topics)
    evidence = Evidence("synthetic", "2026-09-05", "synthetic", {"query": MARKERS}, Confidence.LOW)
    recs = tuple(Recommendation(topic, MARKERS[0], "weak", 40, Confidence.LOW, (evidence,), MARKERS)
                 for topic in topics)
    modes = {name: "local_fixture" for name in SOURCE_NAMES}
    counts = {name: 0 for name in SOURCE_NAMES}
    if gsc:
        modes["search_console"] = "gsc_search_analytics_api"
        counts["search_console"] = coverage.accepted_signal_count if coverage else 0
    return AgentResult(scores, recs, "\n".join(MARKERS), counts, modes, coverage)


def coverage():
    return GSCRuntimeCoverage(
        raw_row_count=4, accepted_signal_count=1, brand_row_count=1,
        unmapped_row_count=1, pii_filtered_row_count=1,
        all_rows_clicks=Decimal(8), all_rows_impressions=Decimal(80),
        accepted_clicks=Decimal(2), accepted_impressions=Decimal(20),
        brand_clicks=Decimal(2), brand_impressions=Decimal(20),
    )


def project(r=None, **kwargs):
    args = dict(result=r if r is not None else result(), execution_id="github-actions:123456",
                code_revision="a" * 40, runtime_state=RuntimeState.OFFLINE,
                observed_at="2026-09-05", period_start="2026-08-08", period_end="2026-09-02")
    args.update(kwargs)
    return h.snapshot_from_agent_result(**args)


def test_zero_topics_and_six_sources():
    s = project()
    assert s.schema_version == 1 and s.agent == "market_intelligence_v3"
    assert s.topics == () and s.recommendation_count == 0
    assert tuple(x.source for x in s.sources) == tuple(sorted(SOURCE_NAMES))
    assert all(x.metrics == () and x.signal_count == 0 for x in s.sources)
    assert s.outcome == "success" and s.safe_failure_code is None
    assert load_v3_config().runtime_state is RuntimeState.OFFLINE


def test_all_models_frozen_and_safe_fields():
    s = project(result(("parcel_delivery",)))
    values = [s, s.sources[0], s.topics[0], h.HistoricalMetric("sessions", "10", "count")]
    for value in values:
        with pytest.raises(FrozenInstanceError): setattr(value, fields(value)[0].name, None)
        assert not hasattr(value, "__dict__")
        for f in fields(value):
            h._privacy_name(f.name, context="topic_field" if type(value) is h.HistoricalTopicSnapshot else "field")


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("schema_version", True), ("schema_version", "1"),
    ("execution_id", ""), ("execution_id", "arbitrary text"), ("execution_id", "https://example.invalid"),
    ("execution_id", "x" * 129), ("execution_id", "person@example.invalid"),
    ("code_revision", "A" * 40), ("code_revision", "a" * 39), ("code_revision", "g" * 40),
    ("agent", "Human Display Name"), ("agent", "secret_value"),
    ("runtime_state", "GA4_GSC_READ_ONLY"), ("observed_at", "yesterday"),
    ("period_start", "2026-02-30"), ("period_end", "2026-9-02"),
    ("period_start", "2026-09-03"), ("period_end", "2026-09-06"),
    ("recommendation_count", True), ("recommendation_count", -1), ("recommendation_count", 1),
    ("safe_failure_code", "GSC_RESPONSE_INVALID"), ("outcome", "unknown"),
])
def test_top_level_rejects_invalid(field, value):
    with pytest.raises(h.HistoricalSnapshotError): replace(project(), **{field: value})


@pytest.mark.parametrize("code", [None, "", "Exception: secret text", "https://example.invalid", "A" * 65])
def test_failed_requires_safe_code(code):
    with pytest.raises(h.HistoricalSnapshotError): replace(project(), outcome="failed", safe_failure_code=code)


def test_failed_machine_outcome_supported():
    s = replace(project(), outcome="failed", safe_failure_code="GSC_RESPONSE_INVALID")
    assert h.snapshot_to_primitive(s)["safe_failure_code"] == "GSC_RESPONSE_INVALID"


@pytest.mark.parametrize("field,value", [
    ("source", "unknown"), ("mode", "unknown"), ("signal_count", True),
    ("signal_count", Decimal(0)), ("signal_count", -1), ("scope_code", "free prose"),
    ("metrics", {}), ("metrics", []), ("metrics", ("private-marker",)),
])
def test_source_validation(field, value):
    with pytest.raises(h.HistoricalSnapshotError): replace(project().sources[0], **{field: value})


@pytest.mark.parametrize("name", ["raw_query", "raw_row", "raw_response", "raw_payload", "raw_json",
    "evidence", "evidence_fact", "evidence_text", "evidence_reference", "evidence_confidence",
    "query_count", "email_count", "phone_count", "url_count", "location_count", "token_count",
    "credential_count", "secret_count", "reference_count", "markdown_count", "action_count", "reasoning_count",
    "Raw_Row_Count", "xraw_row_count", "raw_row_count_extra", "bad name", "a" * 65])
def test_metric_privacy_name_rejection(name):
    with pytest.raises(h.HistoricalSnapshotError): h.HistoricalMetric(name, 1, "count")


@pytest.mark.parametrize("value", [True, -1, 0.0, "0", Decimal(0), {"raw":"content"}, MARKERS[0]])
def test_raw_row_count_exact_integer_only(value):
    with pytest.raises(h.HistoricalSnapshotError): h.HistoricalMetric("raw_row_count", value, "count")


def test_raw_row_count_exact_exception_scope():
    m = h.HistoricalMetric("raw_row_count", 0, "count")
    assert type(m.value) is int
    with pytest.raises(h.HistoricalSnapshotError): h.HistoricalMetric("raw_row_count", True, "flag")
    with pytest.raises(h.HistoricalSnapshotError): replace(project().sources[0], metrics=(m,))
    with pytest.raises(h.HistoricalSnapshotError): h._privacy_name("raw_row_count", context="field")


@pytest.mark.parametrize("value", ["unknown", "very_low", "low", "medium", "high", DimensionLevel.HIGH])
def test_exact_evidence_confidence_values(value):
    t = project(result(("parcel_delivery",))).topics[0]
    t = replace(t, evidence_confidence=value)
    assert type(t.evidence_confidence) is DimensionLevel


@pytest.mark.parametrize("value", ["HIGH", "evidence", {}, (), MARKERS[0],
    Evidence("x", "2026-09-05", "x", {"query":MARKERS}, Confidence.HIGH)])
def test_evidence_object_and_prose_rejected(value):
    t = project(result(("parcel_delivery",))).topics[0]
    with pytest.raises(h.HistoricalSnapshotError): replace(t, evidence_confidence=value)


@pytest.mark.parametrize("unit,value", [
    ("count", -1), ("ratio", "1.1"), ("ratio", "-0.1"), ("position", 0),
    ("score", 101), ("score", -1), ("flag", 1), ("flag", "true"),
    ("count", True), ("count", 1.2), ("unknown", 1),
    ("count", Decimal("NaN")), ("currency_chf", Decimal("Infinity")),
    ("ratio", Decimal("-Infinity")), ("score", "NaN"),
])
def test_unit_bounds(unit, value):
    with pytest.raises(h.HistoricalSnapshotError): h.HistoricalMetric("sessions", value, unit)


@pytest.mark.parametrize("unit,value,expected", [
    ("count", "10.00", Decimal(10)), ("ratio", "0.1000", Decimal("0.1")),
    ("position", 1, Decimal(1)), ("score", 100, Decimal(100)),
    ("currency_chf", "-1.50", Decimal("-1.5")), ("flag", False, False),
])
def test_numeric_normalization(unit, value, expected):
    m = h.HistoricalMetric("sessions", value, unit)
    assert m.value == expected and type(m.value) is type(expected)


def test_metrics_sorted_unique_and_canonical_decimal():
    a = h.HistoricalMetric("sessions", Decimal("10.00"), "count")
    b = h.HistoricalMetric("ctr", Decimal("0.1000"), "ratio")
    s = project()
    source = replace(s.sources[0], metrics=(a, b))
    with pytest.raises(h.HistoricalSnapshotError): replace(source, metrics=(a, a))
    snap = replace(s, sources=(source,) + s.sources[1:])
    p = h.snapshot_to_primitive(snap)
    assert p["sources"][0]["metrics"] == [dict(name="ctr", unit="ratio", value="0.1"), dict(name="sessions", unit="count", value="10")]
    reversed_snap = replace(snap, sources=(replace(source, metrics=(b, a)),) + s.sources[1:])
    assert h.snapshot_to_json(snap) == h.snapshot_to_json(reversed_snap)
    with localcontext() as ctx:
        ctx.prec = 2
        assert h._decimal_text(h._decimal(Decimal("12345.678900"))) == "12345.6789"
    assert h._decimal_text(h._decimal(Decimal("1E-7"))) == "0.0000001"
    assert h._decimal_text(h._decimal(Decimal("-0.00"))) == "0"


@pytest.mark.parametrize("field,value", [("topic", "human text"), ("topic", "query_marker"),
    ("final_score", 101), ("final_score", -1), ("confidence", "unknown"),
    ("search_demand", "invalid"), ("recommendation_strength", "urgent"),
    ("recommendation_code", "free prose")])
def test_topic_validation(field, value):
    t = project(result(("parcel_delivery",))).topics[0]
    with pytest.raises(h.HistoricalSnapshotError): replace(t, **{field: value})


@pytest.mark.parametrize("strength,code", [("strong","prioritize_reviewed_market_experiment"),
    ("moderate","validate_with_additional_evidence"), ("weak","collect_more_evidence")])
def test_recommendation_code(strength, code):
    r = result(("parcel_delivery",))
    r = replace(r, recommendations=(replace(r.recommendations[0], strength=strength),))
    assert project(r).topics[0].recommendation_code == code


@pytest.mark.parametrize("case", ["missing", "duplicate", "orphan", "duplicate_score"])
def test_inconsistent_topic_matches(case):
    r = result(("parcel_delivery",))
    if case == "missing": r = replace(r, recommendations=())
    if case == "duplicate": r = replace(r, recommendations=r.recommendations * 2)
    if case == "orphan": r = replace(r, recommendations=(replace(r.recommendations[0], topic="wine_delivery"),))
    if case == "duplicate_score": r = replace(r, scores=r.scores * 2)
    with pytest.raises(h.HistoricalSnapshotError): project(r)


def test_gsc_exact_projection_and_no_fabricated_metrics():
    c = coverage()
    s = project(result(("parcel_delivery",), coverage=c, gsc=True), runtime_state=RuntimeState.GSC_READ_ONLY)
    source = next(x for x in s.sources if x.source == "search_console")
    assert source.scope_code == "returned_query_rows_non_exhaustive"
    values = {m.name:m.value for m in source.metrics}
    assert values == dict(raw_row_count=4, accepted_signal_count=1, brand_row_count=1, unmapped_row_count=1,
                         pii_filtered_row_count=1, all_rows_clicks=8, all_rows_impressions=80,
                         accepted_clicks=2, accepted_impressions=20, brand_clicks=2, brand_impressions=20,
                         row_limit=25000, row_limit_reached=False)
    assert all(x.metrics == () for x in s.sources if x.source != "search_console")
    assert type(values["raw_row_count"]) is int
    c = replace(c, raw_row_count=25000, unmapped_row_count=24997, row_limit_reached=True)
    s = project(result(coverage=c, gsc=True), runtime_state=RuntimeState.GSC_READ_ONLY)
    assert next(m.value for src in s.sources for m in src.metrics if m.name == "row_limit_reached") is True


@pytest.mark.parametrize("change", [{"raw_row_count":True}, {"accepted_signal_count":2},
    {"totals_scope":"complete"}, {"row_limit_reached":True}, {"row_limit":3},
    {"brand_clicks":Decimal(100)}])
def test_bad_gsc_coverage(change):
    with pytest.raises(h.HistoricalSnapshotError):
        project(result(coverage=replace(coverage(), **change), gsc=True), runtime_state=RuntimeState.GSC_READ_ONLY)


def test_zero_topic_gsc_shape():
    c = replace(coverage(), raw_row_count=2, accepted_signal_count=0, pii_filtered_row_count=0,
                accepted_clicks=Decimal(0), accepted_impressions=Decimal(0))
    s = project(result(coverage=c, gsc=True), runtime_state=RuntimeState.GSC_READ_ONLY)
    assert s.topics == () and s.recommendation_count == 0 and s.outcome == "success"


def test_malicious_markers_ignored_not_escaped():
    class Poison:
        def __repr__(self): raise AssertionError("ignored field was traversed")
        def __str__(self): raise AssertionError("ignored field was traversed")
    r = result(("parcel_delivery",))
    signal = SearchSignal("parcel_delivery", MARKERS[0])
    rec = replace(r.recommendations[0], action="\n".join(MARKERS), reasoning=MARKERS,
                  evidence=(Evidence("synthetic", "2026-09-05", "x", {"query": signal.query, "payload": MARKERS}, Confidence.HIGH),))
    r = replace(r, recommendations=(rec,))
    s = project(r)
    outputs = (repr(s), str(s), repr(h.snapshot_to_primitive(s)), h.snapshot_to_json(s))
    for output in outputs:
        assert all(m not in output for m in MARKERS)
        assert "commercial_query_redacted" not in output
    poison_rec = replace(rec, action=Poison(), reasoning=Poison(), evidence=Poison())
    poisoned = replace(r, markdown=Poison(), recommendations=(poison_rec,),
                       scores=(replace(r.scores[0], explanation=Poison()),))
    assert h.snapshot_to_json(project(poisoned)) == h.snapshot_to_json(s)
    assert h.snapshot_fingerprint(s) == hashlib.sha256(h.snapshot_to_json(s).encode("utf-8")).hexdigest()


def test_input_order_and_fingerprint():
    r = result(("wine_delivery", "parcel_delivery"))
    ordered = replace(r, scores=r.scores[::-1], recommendations=r.recommendations[::-1],
                      source_counts=dict(reversed(list(r.source_counts.items()))),
                      source_modes=dict(reversed(list(r.source_modes.items()))))
    a, b = project(r), project(ordered)
    assert a == b and h.snapshot_to_json(a) == h.snapshot_to_json(b)
    assert h.snapshot_fingerprint(a) == h.snapshot_fingerprint(b)
    assert len(h.snapshot_fingerprint(a)) == 64
    assert h.snapshot_fingerprint(replace(a, execution_id="github-actions:654321")) != h.snapshot_fingerprint(a)
    assert h.snapshot_to_primitive(a) == json.loads(h.snapshot_to_json(a))


def test_no_io_or_generic_agent_serialization():
    tree = ast.parse((ROOT / "scripts/v3/historical_snapshot.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(n.name in {"hashlib", "json", "re"} for n in node.names)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"now", "today", "post", "write_text", "write_bytes", "collect", "run"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "asdict", "vars", "print"}
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"markdown", "query", "action", "reasoning", "explanation", "evidence", "fact"}


@pytest.mark.parametrize("state,source,mode,scope", [
    (RuntimeState.GA4_READ_ONLY, "analytics", "ga4_data_api", "consented_traffic_aggregate"),
    (RuntimeState.GSC_READ_ONLY, "search_console", "gsc_search_analytics_api", "returned_query_rows_non_exhaustive"),
])
def test_other_supported_provenance_without_invented_metrics(state, source, mode, scope):
    r = result()
    r.source_modes[source] = mode
    s = project(r, runtime_state=state)
    value = next(x for x in s.sources if x.source == source)
    assert value.scope_code == scope and value.metrics == ()


@pytest.mark.parametrize("case", ["missing_source", "duplicate_source", "missing_count", "unknown_mode", "runtime_mismatch", "coverage_mismatch"])
def test_inconsistent_source_inventory(case):
    r = result()
    with pytest.raises(h.HistoricalSnapshotError):
        if case == "missing_source": replace(project(), sources=project().sources[:-1])
        elif case == "duplicate_source": replace(project(), sources=(project().sources[0],) * 6)
        elif case == "missing_count":
            del r.source_counts["analytics"]
            project(r)
        elif case == "unknown_mode":
            r.source_modes["analytics"] = "unknown"
            project(r)
        elif case == "runtime_mismatch": project(r, runtime_state=RuntimeState.GSC_READ_ONLY)
        elif case == "coverage_mismatch": project(replace(r, gsc_coverage=coverage()))


def test_all_safe_changes_affect_fingerprint():
    s = project(result(("parcel_delivery",)))
    changed = [replace(s, agent="another_agent"), replace(s, execution_id="manual:789"),
               replace(s, code_revision="b" * 40), replace(s, observed_at="2026-09-06"),
               replace(s, period_start="2026-08-09"), replace(s, period_end="2026-09-03"),
               replace(s, outcome="failed", safe_failure_code="GSC_ROW_INVALID")]
    for field, value in [("final_score", 41), ("confidence", Confidence.HIGH),
                         ("search_demand", DimensionLevel.HIGH), ("evidence_confidence", DimensionLevel.LOW)]:
        changed.append(replace(s, topics=(replace(s.topics[0], **{field:value}),)))
    changed.append(replace(s, sources=(replace(s.sources[0], signal_count=1),) + s.sources[1:]))
    changed.append(replace(s, sources=(replace(s.sources[0], metrics=(h.HistoricalMetric("sessions", 1, "count"),)),) + s.sources[1:]))
    assert all(h.snapshot_fingerprint(x) != h.snapshot_fingerprint(s) for x in changed)


def test_serialization_rechecks_nested_confidence_not_evidence():
    s = project(result(("parcel_delivery",)))
    object.__setattr__(s.topics[0], "evidence_confidence", Evidence("x", "2026-09-05", "x", MARKERS, Confidence.HIGH))
    with pytest.raises(h.HistoricalSnapshotError): h.snapshot_to_json(s)


def test_decimal_spellings_are_identical_and_json_has_no_exponent():
    s = project()
    variants = []
    for value in (10, "10.0", Decimal("10.000"), "1e1"):
        source = replace(s.sources[0], metrics=(h.HistoricalMetric("sessions", value, "count"),))
        variants.append(replace(s, sources=(source,) + s.sources[1:]))
    assert len({h.snapshot_to_json(v) for v in variants}) == 1
    assert len({h.snapshot_fingerprint(v) for v in variants}) == 1


def test_projection_has_no_network(monkeypatch):
    import socket
    def deny(*args, **kwargs): raise AssertionError("network forbidden")
    monkeypatch.setattr(socket, "socket", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    assert h.snapshot_to_json(project()).startswith("{")
