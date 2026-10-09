"""Dashboard views follow the current release, reconcile to gold tables, apply the
small-cell rule and compute growth correctly. The same SQL file runs on Databricks; here it
runs on DuckDB with a get_json_object shim."""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from ireland_rsi.contracts import TABLE_SCHEMAS
from ireland_rsi.dbx_sql import split_statements, substitute
from ireland_rsi.storage import DuckDBTableStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def rows(table, items):
    cols = TABLE_SCHEMAS[table].names
    return pd.DataFrame([{c: r.get(c) for c in cols} for r in items], columns=cols)


def release(t, rid, state, at, values, versions):
    t.insert_new("gold.release_manifest", rows("gold.release_manifest", [{
        "release_id": rid, "release_state": state, "created_at": at, "snapshot_id": "snap",
        "versions_json": json.dumps(versions),
        "gates_json": json.dumps({"g6_threshold_frozen": {"detail": "0.5 (provisional)"}}),
        "metrics_json": json.dumps({"sales_rows": 25, "properties": 20})}]))
    vint = []
    for (geo, freq, period), (value, state_) in values.items():
        vint.append({"release_id": rid, "geography_id": geo, "frequency": freq,
                     "period": period, "index_value": value, "level_lo": value * 0.9,
                     "level_hi": value * 1.1, "endpoint_pairs": 30, "period_state": state_,
                     "series_supported": True, "series_properties": 400})
    t.insert_new("gold.index_vintages", rows("gold.index_vintages", vint))
    if state != "failed":
        t.insert_new("gold.release_events", rows("gold.release_events", [{
            "event_id": f"e-{rid}", "release_id": rid, "action": "publish", "event_at": at}]))


@pytest.fixture
def t():
    tables = DuckDBTableStore()
    con = tables.con
    con.execute("CREATE MACRO get_json_object(j, p) AS json_extract_string(j, p)")
    versions = {"uncertainty": "uv-1", "validation": "vv-1", "backtest": "bv-1", "clean": "qr-1",
                "linkage_production": "lm-1"}
    release(tables, "rel-a", "research", NOW - timedelta(days=30), {
        ("scope", "annual", "2015"): (100.0, "supported"),
        ("scope", "annual", "2016"): (110.0, "supported"),
        ("scope", "annual", "2017"): (121.0, "suppressed"),
        ("scope", "quarterly", "2015Q1"): (100.0, "supported"),
        ("scope", "quarterly", "2016Q1"): (105.0, "supported"),
    }, versions)
    release(tables, "rel-b", "research", NOW, {
        ("scope", "annual", "2015"): (100.0, "supported"),
        ("scope", "annual", "2016"): (120.0, "provisional"),
    }, versions)
    release(tables, "rel-x", "failed", NOW + timedelta(days=1), {
        ("scope", "annual", "2015"): (999.0, "supported")}, versions)
    tables.insert_new("silver.series_support", rows("silver.series_support", [
        {"uncertainty_version": "uv-1", "fit_id": "f1", "geography_id": "scope",
         "frequency": "annual", "supported": True, "properties": 400,
         "bootstrap_attempted": 0, "bootstrap_valid": 0},
        {"uncertainty_version": "uv-0", "fit_id": "f0", "geography_id": "scope",
         "frequency": "annual", "supported": False, "bootstrap_attempted": 10,
         "bootstrap_valid": 5}]))
    sales = [{"snapshot_id": "snap", "raw_row_id": f"r{i}", "rule_set_version": "qr-1",
              "sale_id": f"s{i}", "sale_date": date(2015 if i < 20 else 2016, 1, 1),
              "disposition": "eligible" if i < 20 else "non_market"} for i in range(25)]
    tables.insert_new("silver.sales", rows("silver.sales", sales))
    tables.insert_new("gold.validation_results", rows("gold.validation_results", [
        {"validation_version": "vv-1", "category": "sensitivity", "experiment": "baseline",
         "geography_id": "scope", "frequency": "annual", "metric": m, "segment": seg,
         "value": v, "n": 100}
        for m, seg, v in [("pairs", "all", 100), ("log_growth", "2015-2025", 0.88),
                          ("growth_shift_vs_baseline", "2015-2025", 0.0)]] + [
        {"validation_version": "bv-1", "category": "backtest", "experiment": "local",
         "geography_id": "scope", "frequency": "annual", "metric": "median_abs_pct_error",
         "segment": "test|common", "value": 25.6, "n": 446},
        {"validation_version": "vv-1", "category": "linkage", "experiment": "labels",
         "geography_id": "scope", "metric": "precision", "segment": "threshold=0.5",
         "value": 0.9, "n": 30, "detail": json.dumps({"lo": 0.74, "hi": 0.97})}]))
    for empty in ("silver.link_candidates", "gold.sensitivity_series",
                  "gold.release_calibration", "gold.revisions"):
        tables.insert_new(empty, rows(empty, []))
    for sql in split_statements(substitute(
            (ROOT / "dashboards" / "queries.sql").read_text(encoding="utf-8"),
            {"catalog": "memory"})):
        con.execute(sql)
    return tables


def q(t, sql):
    return t.query(sql)


def test_views_follow_the_current_release_pointer(t):
    assert q(t, "SELECT release_id FROM gold.v_current_release").iloc[0, 0] == "rel-b"
    market = q(t, "SELECT DISTINCT release_id FROM gold.v_market_index")
    assert list(market.release_id) == ["rel-b"]
    t.insert_new("gold.release_events", rows("gold.release_events", [{
        "event_id": "e-rollback", "release_id": "rel-a", "action": "rollback",
        "event_at": NOW + timedelta(hours=1)}]))
    assert q(t, "SELECT release_id FROM gold.v_current_release").iloc[0, 0] == "rel-a"
    assert set(q(t, "SELECT release_id FROM gold.v_market_index").release_id) == {"rel-a"}


def test_failed_releases_never_appear(t):
    v = q(t, "SELECT DISTINCT release_id FROM gold.v_vintages ORDER BY 1")
    assert list(v.release_id) == ["rel-a", "rel-b"]


def test_suppressed_periods_have_no_value(t):
    t.insert_new("gold.release_events", rows("gold.release_events", [{
        "event_id": "e-rb", "release_id": "rel-a", "action": "rollback",
        "event_at": NOW + timedelta(hours=1)}]))
    m = q(t, "SELECT * FROM gold.v_market_index WHERE frequency = 'annual' ORDER BY period")
    row = m.set_index("period").loc["2017"]
    assert pd.isna(row.index_value) and row.period_state == "suppressed"
    assert pd.isna(m.set_index("period").loc["2017", "growth_yoy"])


def test_growth_uses_the_same_period_a_year_earlier(t):
    t.insert_new("gold.release_events", rows("gold.release_events", [{
        "event_id": "e-rb", "release_id": "rel-a", "action": "rollback",
        "event_at": NOW + timedelta(hours=1)}]))
    m = q(t, "SELECT frequency, period, growth_yoy FROM gold.v_market_index")
    g = m.set_index(["frequency", "period"]).growth_yoy
    assert g["annual", "2016"] == pytest.approx(0.10)
    assert g["quarterly", "2016Q1"] == pytest.approx(0.05)
    assert pd.isna(g["annual", "2015"])


def test_small_cells_are_suppressed_and_totals_reconcile(t):
    dq = q(t, "SELECT * FROM gold.v_data_quality ORDER BY sale_year")
    shown = dq[dq.cell_state == "shown"]
    hidden = dq[dq.cell_state == "suppressed"]
    assert list(shown.occurrences) == [20]
    assert len(hidden) == 1 and hidden.occurrences.isna().all()      # 5 rows < 10
    total = q(t, "SELECT count(*) FROM silver.sales").iloc[0, 0]
    assert shown.occurrences.sum() + 5 == total


def test_zero_bootstrap_attempts_do_not_divide_by_zero(t):
    s = q(t, "SELECT * FROM gold.v_series_support")
    assert len(s) == 1                          # only the current release's version
    assert pd.isna(s.bootstrap_success.iloc[0])


def test_metric_views_pivot_and_split_correctly(t):
    sens = q(t, "SELECT * FROM gold.v_sensitivity").iloc[0]
    assert sens.pairs == 100 and sens.log_growth == pytest.approx(0.88)
    assert sens.growth_window == "2015-2025"
    bt = q(t, "SELECT * FROM gold.v_backtest").iloc[0]
    assert (bt.eval_window, bt.method, bt.segment) == ("test", "local", "common")
    lp = q(t, "SELECT * FROM gold.v_linkage_precision").iloc[0]
    assert lp.threshold == 0.5 and lp.ci_lo == pytest.approx(0.74)
    rm = q(t, "SELECT * FROM gold.v_release_metrics").iloc[0]
    assert rm.sales_rows == 25 and rm.link_threshold == "0.5 (provisional)"
