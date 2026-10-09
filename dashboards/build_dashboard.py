"""Generate dashboards/ireland_rsi.lvdash.json (AI/BI dashboard) from readable definitions.

    python dashboards/build_dashboard.py

Datasets read only the governed views in dashboards/queries.sql (schema `gold`); the
catalog is set per bundle target (`dataset_catalog`), so one file serves every profile.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).with_name("ireland_rsi.lvdash.json")

DATASETS = {
    "release": ("Current release", "SELECT * FROM gold.v_release_metrics"),
    "market_annual": ("Index by area (annual)",
                      "SELECT area, period, index_value, level_lo, level_hi, growth_yoy, "
                      "endpoint_pairs, period_state FROM gold.v_market_index "
                      "WHERE frequency = 'annual' ORDER BY area, period"),
    "county_band": ("County index with 95% band",
                    "SELECT period, 'Index' AS series, index_value AS value "
                    "FROM gold.v_market_index WHERE geography_id = 'scope' AND frequency = "
                    "'annual' UNION ALL SELECT period, 'Lower 95%', level_lo "
                    "FROM gold.v_market_index WHERE geography_id = 'scope' AND frequency = "
                    "'annual' UNION ALL SELECT period, 'Upper 95%', level_hi "
                    "FROM gold.v_market_index WHERE geography_id = 'scope' AND frequency = "
                    "'annual'"),
    "support": ("Series support", "SELECT * FROM gold.v_series_support"),
    "precision": ("Link precision by threshold",
                  "SELECT threshold, metric, round(value, 3) AS value, accepted_labelled_pairs,"
                  " round(ci_lo, 3) AS ci_lo, round(ci_hi, 3) AS ci_hi "
                  "FROM gold.v_linkage_precision ORDER BY metric, threshold"),
    "score_bands": ("Candidate pairs by match probability",
                    "SELECT CAST(band AS STRING) AS band, candidate_pairs, rule_based_links "
                    "FROM gold.v_link_score_bands WHERE cell_state = 'shown' ORDER BY band"),
    "quality": ("Occurrences by year and disposition",
                "SELECT CAST(sale_year AS STRING) AS sale_year, disposition, occurrences, "
                "cell_state FROM gold.v_data_quality ORDER BY sale_year, disposition"),
    "sensitivity": ("Sensitivity experiments",
                    "SELECT experiment, geography_id, pairs, properties, growth_window, "
                    "round(log_growth, 3) AS log_growth, "
                    "round(shift_vs_headline, 3) AS shift_vs_headline, "
                    "round(max_level_gap, 3) AS max_level_gap, "
                    "example_200k_2015_sale_in_2025 FROM gold.v_sensitivity "
                    "ORDER BY geography_id, abs(shift_vs_headline) DESC"),
    "sens_series": ("Index under alternative choices (Donegal)",
                    "SELECT experiment, period, index_value FROM gold.v_sensitivity_series "
                    "WHERE geography_id = 'scope' AND frequency = 'annual' AND experiment IN "
                    "('baseline', 'deterministic_links', 'method_huber', 'threshold_0.3', "
                    "'threshold_0.9', 'with_new_first_leg', 'exclude_unusual')"),
    "cso": ("Donegal index and CSO Border houses",
            "SELECT CASE WHEN experiment = 'baseline' THEN 'Repeat-sales (Donegal)' "
            "ELSE 'CSO RPPI Border houses' END AS series, period, index_value "
            "FROM gold.v_sensitivity_series WHERE geography_id = 'scope' "
            "AND frequency = 'annual' AND experiment IN ('baseline', 'cso:border_houses')"),
    "benchmark": ("Growth agreement with the CSO",
                  "SELECT benchmark, geography_id, frequency, periods, metric, "
                  "round(value, 3) AS value, compared_periods, caveat FROM gold.v_benchmark "
                  "ORDER BY geography_id, frequency, periods, metric"),
    "backtest": ("Lookup backtest (common pairs)",
                 "SELECT eval_window, method, metric, round(value, 3) AS value, pairs "
                 "FROM gold.v_backtest WHERE segment = 'common' "
                 "ORDER BY eval_window, metric, method"),
    "calibration": ("Prediction interval coverage",
                    "SELECT horizon_band, level, round(coverage_test, 3) AS coverage_test, "
                    "n_test, round(relative_width, 2) AS relative_width, status "
                    "FROM gold.v_calibration ORDER BY horizon_band"),
    "vintages": ("County index by release",
                 "SELECT release_id, period, index_value FROM gold.v_vintages "
                 "WHERE geography_id = 'scope' AND frequency = 'annual' "
                 "AND period_state <> 'suppressed'"),
    "revisions": ("Revisions between releases",
                  "SELECT release_id, previous_release_id, geography_id, frequency, period, "
                  "round(previous_value, 2) AS previous_value, round(value, 2) AS value, "
                  "round(log_revision, 5) AS log_revision, cause FROM gold.v_revisions "
                  "ORDER BY created_at DESC, geography_id, frequency, period"),
}


def _query(dataset: str, fields: list[str]) -> list[dict]:
    return [{"name": "main_query", "query": {
        "datasetName": dataset, "disaggregated": True,
        "fields": [{"name": f, "expression": f"`{f}`"} for f in fields]}}]


def text(name: str, lines: list[str], x: int, y: int, w: int = 6, h: int = 2) -> dict:
    return {"widget": {"name": name, "multilineTextboxSpec": {"lines": lines}},
            "position": {"x": x, "y": y, "width": w, "height": h}}


def table(name: str, dataset: str, columns: list[str], title: str, x: int, y: int,
          w: int = 6, h: int = 6) -> dict:
    return {"widget": {
        "name": name, "queries": _query(dataset, columns),
        "spec": {"version": 1, "widgetType": "table",
                 "encodings": {"columns": [{"fieldName": c, "displayName": c}
                                           for c in columns]},
                 "frame": {"showTitle": True, "title": title}}},
        "position": {"x": x, "y": y, "width": w, "height": h}}


def chart(name: str, kind: str, dataset: str, x_field: str, y_field: str,
          color: str | None, title: str, x: int, y: int, w: int = 6, h: int = 6,
          y_title: str | None = None) -> dict:
    enc = {"x": {"fieldName": x_field, "scale": {"type": "categorical"},
                 "displayName": x_field},
           "y": {"fieldName": y_field, "scale": {"type": "quantitative"},
                 "displayName": y_title or y_field}}
    fields = [x_field, y_field]
    if color:
        enc["color"] = {"fieldName": color, "scale": {"type": "categorical"},
                        "displayName": color}
        fields.append(color)
    return {"widget": {
        "name": name, "queries": _query(dataset, fields),
        "spec": {"version": 3, "widgetType": kind, "encodings": enc,
                 "frame": {"showTitle": True, "title": title}}},
        "position": {"x": x, "y": y, "width": w, "height": h}}


def counter(name: str, dataset: str, field: str, title: str, x: int, y: int) -> dict:
    return {"widget": {
        "name": name, "queries": _query(dataset, [field]),
        "spec": {"version": 2, "widgetType": "counter",
                 "encodings": {"value": {"fieldName": field, "displayName": title}},
                 "frame": {"showTitle": True, "title": title}}},
        "position": {"x": x, "y": y, "width": 2, "height": 3}}


PAGES = [
    ("market", "Market", [
        text("t_market", [
            "## Repeat-sales house price index",
            "Published series only (2015 = 100). Suppressed periods are blank. Bands are 95% "
            "property-history bootstrap intervals of the **index**, not of any one home. "
            "The newest period is provisional. Release state: 'published' means every gate "
            "passed, including the frozen link threshold; 'research' means it is still "
            "provisional."], 0, 0, 6, 2),
        counter("c_props", "release", "properties", "Properties", 0, 2),
        counter("c_rows", "release", "sales_rows", "Register rows", 2, 2),
        counter("c_state", "release", "release_state", "Release state", 4, 2),
        chart("l_area", "line", "market_annual", "period", "index_value", "area",
              "Index by area (annual)", 0, 5, 3, 7, "Index (2015 = 100)"),
        chart("l_band", "line", "county_band", "period", "value", "series",
              "County index with its 95% band", 3, 5, 3, 7, "Index (2015 = 100)"),
        table("tb_support", "support", ["geography_id", "frequency", "supported", "reasons",
                                        "properties", "bootstrap_success",
                                        "periods_supported", "periods_suppressed"],
              "Which series are published, and why not", 0, 12, 6, 4),
        table("tb_market", "market_annual", ["area", "period", "index_value", "level_lo",
                                             "level_hi", "growth_yoy", "endpoint_pairs",
                                             "period_state"],
              "Annual levels, growth and support", 0, 16, 6, 7),
    ]),
    ("linkage", "Linkage", [
        text("t_link", [
            "## Linking sales into property histories",
            "Precision is measured from human labels of a stratified sample, weighted by "
            "inverse sampling probability; the interval is on unweighted counts. Denominators "
            "are the labelled pairs at or above each threshold. 'final_links' rows measure "
            "the links that survive clustering (what the index uses); 'labels' rows are raw "
            "pair scores."],
             0, 0, 6, 2),
        table("tb_prec", "precision", ["metric", "threshold", "value", "ci_lo", "ci_hi",
                                       "accepted_labelled_pairs"],
              "Label-based precision by threshold", 0, 2, 3, 8),
        chart("b_bands", "bar", "score_bands", "band", "candidate_pairs", None,
              "Candidate pairs by match probability", 3, 2, 3, 8, "Pairs"),
    ]),
    ("quality", "Data quality", [
        text("t_q", [
            "## Source and quality",
            "Every register row is kept and assigned one disposition. Cells under 10 are "
            "suppressed (small-cell rule)."], 0, 0, 6, 1),
        chart("b_quality", "bar", "quality", "sale_year", "occurrences", "disposition",
              "Register rows by year and disposition", 0, 1, 6, 8, "Rows"),
        table("tb_release", "release", ["release_id", "release_state", "snapshot_id",
                                        "sales_rows", "quarantine_rate", "eligible_rate",
                                        "properties", "linked_sale_share",
                                        "cluster_conflicts_refused", "link_threshold"],
              "Release metrics", 0, 9, 6, 3),
    ]),
    ("robustness", "Robustness", [
        text("t_r", [
            "## How much do the choices matter?",
            "Each experiment rebuilds histories, pairs and the index from the same linkage "
            "scores with one choice changed. Shifts above 0.05 log points are flagged for "
            "investigation, not used to pick a result."], 0, 0, 6, 2),
        chart("l_sens", "line", "sens_series", "period", "index_value", "experiment",
              "Donegal index under alternative choices", 0, 2, 6, 7, "Index"),
        table("tb_sens", "sensitivity", ["experiment", "geography_id", "pairs", "properties",
                                         "growth_window", "log_growth", "shift_vs_headline",
                                         "max_level_gap", "example_200k_2015_sale_in_2025"],
              "Sensitivity experiments", 0, 9, 6, 8),
        chart("l_vint", "line", "vintages", "period", "index_value", "release_id",
              "County index by release (compare vintages)", 0, 17, 3, 6, "Index"),
        table("tb_rev", "revisions", ["release_id", "previous_release_id", "geography_id",
                                      "frequency", "period", "previous_value", "value",
                                      "log_revision", "cause"],
              "Revisions between releases", 3, 17, 3, 6),
    ]),
    ("validation", "Validation", [
        text("t_v", [
            "## Validation",
            "The CSO RPPI is a benchmark, not ground truth: it covers household purchases, "
            "is hedonic and regional. Growth rates are compared. The backtest predicts each "
            "sale from an index fitted without that property and without later data "
            "(retrospective, not real-time)."], 0, 0, 6, 2),
        chart("l_cso", "line", "cso", "period", "index_value", "series",
              "Donegal repeat-sales index and CSO Border houses (2015 = 100)", 0, 2, 3, 7,
              "Index"),
        table("tb_bench", "benchmark", ["benchmark", "geography_id", "frequency", "periods",
                                        "metric", "value", "compared_periods"],
              "Year-on-year growth agreement (percentage points)", 3, 2, 3, 7),
        table("tb_bt", "backtest", ["eval_window", "method", "metric", "value", "pairs"],
              "Lookup accuracy against simple baselines", 0, 9, 3, 8),
        table("tb_cal", "calibration", ["horizon_band", "level", "coverage_test", "n_test",
                                        "relative_width", "status"],
              "90% prediction interval: test-window coverage", 3, 9, 3, 4),
    ]),
]


def build() -> dict:
    return {
        "datasets": [{"name": key, "displayName": label, "queryLines": [sql]}
                     for key, (label, sql) in DATASETS.items()],
        "pages": [{"name": key, "displayName": label, "pageType": "PAGE_TYPE_CANVAS",
                   "layout": layout} for key, label, layout in PAGES],
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
