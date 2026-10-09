"""Lookup service behaviour, with an in-memory warehouse holding gold tables."""

import json
import math
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from ireland_rsi.contracts import TABLE_SCHEMAS
from ireland_rsi.storage import DuckDBTableStore

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app" / "lookup"))
import service  # noqa: E402

CATALOG = "memory"
CHAIN = [{"geography": "town:T", "frequency": "annual"},
         {"geography": "scope", "frequency": "annual"}]
POLICY = {"estimate_chain": CHAIN, "stale_after_days": 45, "index_interval": 0.95}


class DuckBackend:
    """Same SQL and named parameters as the Databricks connector (:name -> $name)."""

    def __init__(self, tables):
        self.con = tables.con
        self.statements = []

    def query(self, sql, params):
        self.statements.append((sql, params))
        return self.con.execute(re.sub(r"(?<![:\w]):(\w+)", r"$\1", sql), params).df()


def rows(table, items):
    cols = TABLE_SCHEMAS[table].names
    return pd.DataFrame([{c: r.get(c) for c in cols} for r in items], columns=cols)


def vintage(rid, geo, values, states=None, supported=True):
    states = states or {}
    return [{"release_id": rid, "geography_id": geo, "frequency": "annual", "period": p,
             "index_value": v, "level_lo": v * 0.9, "level_hi": v * 1.1, "endpoint_pairs": 50,
             "period_state": states.get(p, "supported"), "series_supported": supported,
             "series_properties": 500} for p, v in values.items()]


def build(tables, rid="rel-a", scale=1.0, town_supported=False, cal_status="ok",
          created=None):
    t = tables
    t.insert_new("gold.release_manifest", rows("gold.release_manifest", [{
        "release_id": rid, "release_state": "research",
        "created_at": created or datetime.now(UTC), "snapshot_id": "snap",
        "lookup_policy_json": json.dumps(POLICY)}]))
    t.insert_new("gold.release_events", rows("gold.release_events", [{
        "event_id": f"evt-{rid}", "release_id": rid, "action": "publish",
        "event_at": created or datetime.now(UTC)}]))
    scope = {"2015": 100.0, "2020": 150.0 * scale, "2024": 200.0 * scale,
             "2025": 210.0 * scale}
    t.insert_new("gold.index_vintages", rows("gold.index_vintages", vintage(
        rid, "scope", scope, {"2024": "suppressed", "2025": "provisional"})
        + vintage(rid, "town:T", {"2015": 100.0, "2020": 170.0}, supported=town_supported)))
    hist = [
        {"sale_id": "s1", "property_id": "p1", "sale_date": date(2015, 6, 1),
         "address": "19 Ard Colmcille, Letterkenny", "address_search":
         "19 ard colmcille letterkenny county donegal", "county": "Donegal",
         "locality_name": "Letterkenny", "reported_price_eur": 200000.0,
         "analysis_price_eur": 200000.0, "price_basis": "reported", "anchor_eligible": True,
         "property_sales": 3, "link_strength": "strong"},
        {"sale_id": "s2", "property_id": "p1", "sale_date": date(2018, 3, 1),
         "address": "19 Ard Colmcille, Letterkenny", "address_search":
         "19 ard colmcille letterkenny county donegal", "county": "Donegal",
         "reported_price_eur": 50000.0, "analysis_price_eur": 50000.0,
         "price_basis": "reported", "anchor_eligible": False,
         "anchor_reason": "NON_MARKET_SALE", "property_sales": 3, "link_strength": "strong"},
        {"sale_id": "s3", "property_id": "p1", "sale_date": date(2020, 5, 1),
         "address": "19 Ard Colmcille, Letterkenny", "address_search":
         "19 ard colmcille letterkenny county donegal", "county": "Donegal",
         "reported_price_eur": 220000.0, "analysis_price_eur": 249700.0,
         "price_basis": "gross_from_vat_exclusive", "anchor_eligible": True,
         "property_sales": 3, "link_strength": "strong"},
        {"sale_id": "s4", "property_id": "p2", "sale_date": date(2016, 1, 1),
         "address": "Apt 2, Ard Colmcille, Letterkenny", "address_search":
         "apartment 2 ard colmcille letterkenny", "county": "Donegal",
         "reported_price_eur": 90000.0, "analysis_price_eur": None, "price_basis": "uncertain",
         "anchor_eligible": False, "anchor_reason": "VAT_RULE_UNRESOLVED",
         "property_sales": 1, "link_strength": "single_sale"},
    ]
    t.insert_new("gold.lookup_history", rows("gold.lookup_history",
                                            [{**h, "release_id": rid} for h in hist]))
    t.insert_new("gold.release_calibration", rows("gold.release_calibration", [
        {"release_id": rid, "horizon_band": "0-3y", "min_years": 0.0, "max_years": 3.0,
         "level": 0.9, "q_lo": -0.5, "q_hi": 0.6, "status": cal_status},
        {"release_id": rid, "horizon_band": "3y+", "min_years": 3.0, "max_years": 100.0,
         "level": 0.9, "q_lo": -0.4, "q_hi": 0.5, "status": "ok"}]))


@pytest.fixture
def db():
    tables = DuckDBTableStore()
    build(tables)
    return tables, DuckBackend(tables)


def release(backend):
    return service.current_release(backend, CATALOG)


def test_search_ranks_candidates_and_keeps_them_separate(db):
    _, b = db
    hits = service.search_properties(b, CATALOG, "19 Ard Colmcille", None, "rel-a")
    assert list(hits.property_id)[:1] == ["p1"]
    assert hits.set_index("property_id").sales["p1"] == 3
    both = service.search_properties(b, CATALOG, "ard colmcille", "Donegal", "rel-a")
    assert set(both.property_id) == {"p1", "p2"}       # ambiguity is returned, not resolved


@pytest.mark.parametrize("text", ["'; DROP TABLE gold.lookup_history; --",
                                  "19' OR '1'='1", "%", "\\"])
def test_injection_strings_are_literal_search_input(db, text):
    tables, b = db
    hits = service.search_properties(b, CATALOG, text, None, "rel-a")
    assert hits.empty or set(hits.property_id) <= {"p1", "p2"}
    assert tables.query("SELECT count(*) FROM gold.lookup_history").iloc[0, 0] == 4
    for sql, _params in b.statements:
        assert text not in sql                       # user text only ever in parameters


def test_estimate_arithmetic_interval_and_label(db):
    _, b = db
    r = service.estimate(b, CATALOG, "p1", release(b), target_period="2020")
    # Default anchor: the latest eligible sale (s3, 2020) gives ratio 1; choose s1 instead.
    assert r.anchor_sale_id == "s3" and r.index_ratio == pytest.approx(1.0)
    r = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s1",
                         target_period="2020")
    assert r.status == "ok"
    assert r.amount == pytest.approx(200000 * 150 / 100)
    assert r.horizon_band == "3y+"
    assert r.interval_lo == pytest.approx(r.amount * math.exp(-0.4))
    assert r.interval_hi == pytest.approx(r.amount * math.exp(0.5))
    assert r.label == "Index-adjusted estimate as of 2020"
    assert r.geography_id == "scope" and r.fallback_level == 1   # town series unsupported
    assert "town:T/annual: not published" in r.tried
    assert r.fallback_note.startswith("A broader area (scope)")


def test_vat_adjusted_anchor_uses_the_analysis_price_and_says_so(db):
    _, b = db
    r = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s3",
                         target_period="2025")
    assert r.status == "ok"
    assert r.anchor_price == 249700.0 and r.anchor_price_basis == "gross_from_vat_exclusive"
    assert r.provisional                                      # 2025 is provisional


def test_an_ineligible_anchor_is_refused_not_swapped(db):
    _, b = db
    r = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s2")
    assert r.status == "unavailable" and r.reason_code == "NO_ELIGIBLE_ANCHOR"
    assert r.amount is None and r.anchor_sale_id == "s2"
    assert "NON_MARKET_SALE" in r.message
    v = service.estimate(b, CATALOG, "p2", release(b), anchor_sale_id="s4")
    assert v.reason_code == "UNRESOLVED_VAT" and v.amount is None
    none = service.estimate(b, CATALOG, "p2", release(b))
    assert none.reason_code == "NO_ELIGIBLE_ANCHOR"


def test_suppressed_target_period_is_unsupported(db):
    _, b = db
    r = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s1",
                         target_period="2024")
    assert r.reason_code == "UNSUPPORTED_PERIOD" and r.amount is None
    early = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s3",
                             target_period="2015")
    assert early.reason_code == "TARGET_BEFORE_ANCHOR"


def test_no_published_series_is_sparse_geography():
    tables = DuckDBTableStore()
    build(tables)
    tables.con.execute("UPDATE gold.index_vintages SET series_supported = false")
    b = DuckBackend(tables)
    r = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s1")
    assert r.reason_code == "SPARSE_GEOGRAPHY" and r.amount is None


def test_town_series_is_used_first_when_published():
    tables = DuckDBTableStore()
    build(tables, town_supported=True)
    b = DuckBackend(tables)
    r = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s1",
                         target_period="2020")
    assert r.geography_id == "town:T" and r.fallback_level == 0
    assert r.amount == pytest.approx(200000 * 1.7)


def test_failed_calibration_withholds_the_amount():
    tables = DuckDBTableStore()
    build(tables, cal_status="coverage_failed")
    b = DuckBackend(tables)
    r = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s3",
                         target_period="2020")       # 0-year horizon: the failed band
    assert r.reason_code == "UNCALIBRATED_INTERVAL" and r.amount is None
    assert r.index_ratio is not None                 # the index facts are still shown


def test_a_pinned_release_stays_consistent_after_a_new_publication():
    tables = DuckDBTableStore()
    build(tables, "rel-a")
    b = DuckBackend(tables)
    pinned = release(b)
    build(tables, "rel-b", scale=2.0, created=datetime.now(UTC) + timedelta(seconds=1))
    assert release(b).release_id == "rel-b"
    old = service.estimate(b, CATALOG, "p1", pinned, anchor_sale_id="s1", target_period="2020")
    new = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s1",
                           target_period="2020")
    assert old.release_id == "rel-a" and old.amount == pytest.approx(300000)
    assert new.release_id == "rel-b" and new.amount == pytest.approx(600000)


def test_stale_release_is_flagged():
    tables = DuckDBTableStore()
    build(tables, created=datetime.now(UTC) - timedelta(days=60))
    b = DuckBackend(tables)
    r = service.estimate(b, CATALOG, "p1", release(b), anchor_sale_id="s1",
                         target_period="2020")
    assert "SOURCE_STALE" in r.warnings and r.status == "ok"


def test_unknown_property_is_no_match(db):
    _, b = db
    assert service.estimate(b, CATALOG, "nope", release(b)).reason_code == "NO_MATCH"


def test_warehouse_failure_surfaces_to_the_caller():
    class Broken:
        def query(self, sql, params):
            raise ConnectionError("warehouse stopped")

    with pytest.raises(ConnectionError):
        service.current_release(Broken(), CATALOG)
    assert "unavailable" in service.REASONS["SERVICE_UNAVAILABLE"]


def test_every_reason_code_has_a_message():
    for code in ("NO_MATCH", "AMBIGUOUS_PROPERTY", "NO_ELIGIBLE_ANCHOR", "UNRESOLVED_VAT",
                 "UNSUPPORTED_PERIOD", "DISCONNECTED_SERIES", "SPARSE_GEOGRAPHY",
                 "UNCALIBRATED_INTERVAL", "SOURCE_STALE", "SERVICE_UNAVAILABLE"):
        assert service.REASONS[code]
