-- Governed dashboard datasets. Every view reads the release selected by the
-- current-release pointer, so the dashboard and the lookup app always show the same release.
-- Aggregates only: no address-level rows. Small-cell rule (D-027): counts below 10 are
-- shown as NULL with cell_state = 'suppressed'.
--   python -m ireland_rsi.dbx_sql dashboards/queries.sql --var catalog=ireland_rsi_donegal

-- The release the pointer selects now.
CREATE OR REPLACE VIEW ${catalog}.gold.v_current_release AS
SELECT m.release_id, m.release_state, m.created_at, m.snapshot_id, m.versions_json,
       m.gates_json, m.metrics_json, e.action AS pointer_action, e.event_at AS pointed_at
FROM (SELECT release_id, action, event_at,
             ROW_NUMBER() OVER (ORDER BY event_at DESC, event_id DESC) AS rn
      FROM ${catalog}.gold.release_events WHERE action IN ('publish', 'rollback')) e
JOIN ${catalog}.gold.release_manifest m ON m.release_id = e.release_id
WHERE e.rn = 1;

-- Market: published index levels with year-on-year growth (same quarter a year earlier).
CREATE OR REPLACE VIEW ${catalog}.gold.v_market_index AS
SELECT v.release_id,
       CASE WHEN v.geography_id = 'scope' THEN 'County' ELSE replace(v.geography_id, 'town:', '') END
         AS area,
       v.geography_id, v.frequency, v.period,
       CASE WHEN v.period_state = 'suppressed' THEN NULL ELSE v.index_value END AS index_value,
       CASE WHEN v.period_state = 'suppressed' THEN NULL ELSE v.level_lo END AS level_lo,
       CASE WHEN v.period_state = 'suppressed' THEN NULL ELSE v.level_hi END AS level_hi,
       CASE WHEN v.period_state <> 'suppressed' AND p.period_state <> 'suppressed'
            THEN v.index_value / p.index_value - 1 END AS growth_yoy,
       v.endpoint_pairs, v.period_state, v.state_reason, v.series_properties
FROM ${catalog}.gold.index_vintages v
JOIN ${catalog}.gold.v_current_release r ON v.release_id = r.release_id
LEFT JOIN ${catalog}.gold.index_vintages p
  ON p.release_id = v.release_id AND p.geography_id = v.geography_id
 AND p.frequency = v.frequency
 AND p.period = CAST(CAST(substr(v.period, 1, 4) AS INT) - 1 AS STRING) || substr(v.period, 5)
WHERE v.series_supported;

-- Which series are published, and why the others are not.
CREATE OR REPLACE VIEW ${catalog}.gold.v_series_support AS
SELECT s.geography_id, s.frequency, s.supported, s.reasons, s.properties,
       s.effective_sample_size,
       CASE WHEN s.bootstrap_attempted > 0 THEN s.bootstrap_valid / s.bootstrap_attempted END
         AS bootstrap_success,
       s.max_property_weight_share, s.periods_supported, s.periods_suppressed
FROM ${catalog}.silver.series_support s
JOIN ${catalog}.gold.v_current_release r
  ON s.uncertainty_version = get_json_object(r.versions_json, '$.uncertainty');

-- Linkage: label-based precision by threshold (the link-threshold gate evidence; denominators shown).
CREATE OR REPLACE VIEW ${catalog}.gold.v_linkage_precision AS
SELECT CAST(replace(v.segment, 'threshold=', '') AS DOUBLE) AS threshold, v.metric, v.value,
       v.n AS accepted_labelled_pairs,
       CAST(get_json_object(v.detail, '$.lo') AS DOUBLE) AS ci_lo,
       CAST(get_json_object(v.detail, '$.hi') AS DOUBLE) AS ci_hi
FROM ${catalog}.gold.validation_results v
JOIN ${catalog}.gold.v_current_release r
  ON v.validation_version = get_json_object(r.versions_json, '$.validation')
WHERE v.category = 'linkage';

-- Linkage: candidate pairs by match-probability band for the production model.
CREATE OR REPLACE VIEW ${catalog}.gold.v_link_score_bands AS
SELECT band, candidate_pairs, rule_based_links,
       CASE WHEN candidate_pairs < 10 THEN 'suppressed' ELSE 'shown' END AS cell_state
FROM (SELECT LEAST(FLOOR(c.match_probability * 10) / 10, 0.9) AS band,
             COUNT(*) AS candidate_pairs,
             SUM(CASE WHEN c.baseline_link THEN 1 ELSE 0 END) AS rule_based_links
      FROM ${catalog}.silver.link_candidates c
      JOIN ${catalog}.gold.v_current_release r
        ON c.model_version = get_json_object(r.versions_json, '$.linkage_production')
      WHERE c.track = 'production'
      GROUP BY 1) t;

-- Data quality: occurrences by sale year and disposition, with the small-cell rule.
CREATE OR REPLACE VIEW ${catalog}.gold.v_data_quality AS
SELECT sale_year, disposition,
       CASE WHEN n < 10 THEN NULL ELSE n END AS occurrences,
       CASE WHEN n < 10 THEN 'suppressed' ELSE 'shown' END AS cell_state
FROM (SELECT YEAR(s.sale_date) AS sale_year, s.disposition, COUNT(*) AS n
      FROM ${catalog}.silver.sales s
      JOIN ${catalog}.gold.v_current_release r
        ON s.snapshot_id = r.snapshot_id
       AND s.rule_set_version = get_json_object(r.versions_json, '$.clean')
      GROUP BY 1, 2) t;

-- Data quality headline numbers recorded with the release.
CREATE OR REPLACE VIEW ${catalog}.gold.v_release_metrics AS
SELECT r.release_id, r.release_state, r.created_at, r.snapshot_id,
       CAST(get_json_object(r.metrics_json, '$.sales_rows') AS BIGINT) AS sales_rows,
       CAST(get_json_object(r.metrics_json, '$.quarantine_rate') AS DOUBLE) AS quarantine_rate,
       CAST(get_json_object(r.metrics_json, '$.eligible_rate') AS DOUBLE) AS eligible_rate,
       CAST(get_json_object(r.metrics_json, '$.properties') AS BIGINT) AS properties,
       CAST(get_json_object(r.metrics_json, '$.linked_sale_share') AS DOUBLE)
         AS linked_sale_share,
       CAST(get_json_object(r.metrics_json, '$.cluster_conflicts_refused') AS BIGINT)
         AS cluster_conflicts_refused,
       get_json_object(r.gates_json, '$.g6_threshold_frozen.detail') AS link_threshold
FROM ${catalog}.gold.v_current_release r;

-- Robustness: every sensitivity experiment against the headline.
CREATE OR REPLACE VIEW ${catalog}.gold.v_sensitivity AS
SELECT v.experiment, v.geography_id,
       MAX(CASE WHEN v.metric = 'pairs' THEN v.value END) AS pairs,
       MAX(CASE WHEN v.metric = 'properties' THEN v.value END) AS properties,
       MAX(CASE WHEN v.metric = 'log_growth' THEN v.segment END) AS growth_window,
       MAX(CASE WHEN v.metric = 'log_growth' THEN v.value END) AS log_growth,
       MAX(CASE WHEN v.metric = 'growth_shift_vs_baseline' THEN v.value END) AS shift_vs_headline,
       MAX(CASE WHEN v.metric = 'max_log_gap_vs_baseline' THEN v.value END) AS max_level_gap,
       MAX(CASE WHEN v.metric = 'example_estimate_eur' THEN v.value END)
         AS example_200k_2015_sale_in_2025
FROM ${catalog}.gold.validation_results v
JOIN ${catalog}.gold.v_current_release r
  ON v.validation_version = get_json_object(r.versions_json, '$.validation')
WHERE v.category = 'sensitivity'
GROUP BY v.experiment, v.geography_id;

-- Robustness and validation: index levels per experiment and the aligned CSO series.
CREATE OR REPLACE VIEW ${catalog}.gold.v_sensitivity_series AS
SELECT s.experiment, s.geography_id, s.frequency, s.period, s.index_value
FROM ${catalog}.gold.sensitivity_series s
JOIN ${catalog}.gold.v_current_release r
  ON s.validation_version = get_json_object(r.versions_json, '$.validation');

-- Validation: growth agreement with the CSO benchmark.
CREATE OR REPLACE VIEW ${catalog}.gold.v_benchmark AS
SELECT v.experiment AS benchmark, v.geography_id, v.frequency, v.segment AS periods,
       v.metric, v.value, v.n AS compared_periods,
       get_json_object(v.detail, '$.caveat') AS caveat
FROM ${catalog}.gold.validation_results v
JOIN ${catalog}.gold.v_current_release r
  ON v.validation_version = get_json_object(r.versions_json, '$.validation')
WHERE v.category = 'benchmark';

-- Validation: lookup backtest accuracy by window, method and segment.
CREATE OR REPLACE VIEW ${catalog}.gold.v_backtest AS
SELECT substr(v.segment, 1, instr(v.segment, '|') - 1) AS eval_window,
       v.experiment AS method,
       substr(v.segment, instr(v.segment, '|') + 1) AS segment,
       v.metric, v.value, v.n AS pairs
FROM ${catalog}.gold.validation_results v
JOIN ${catalog}.gold.v_current_release r
  ON v.validation_version = get_json_object(r.versions_json, '$.backtest')
WHERE v.category = 'backtest';

-- Validation: property prediction interval coverage on the untouched test window.
CREATE OR REPLACE VIEW ${catalog}.gold.v_calibration AS
SELECT c.horizon_band, c.level, c.q_lo, c.q_hi, c.coverage_test, c.n_test, c.status,
       exp(c.q_hi) - exp(c.q_lo) AS relative_width
FROM ${catalog}.gold.release_calibration c
JOIN ${catalog}.gold.v_current_release r ON c.release_id = r.release_id;

-- Vintages: every complete release, for comparing two releases and their revisions.
CREATE OR REPLACE VIEW ${catalog}.gold.v_vintages AS
SELECT v.release_id, m.created_at, m.release_state, v.geography_id, v.frequency, v.period,
       v.index_value, v.period_state
FROM ${catalog}.gold.index_vintages v
JOIN ${catalog}.gold.release_manifest m ON m.release_id = v.release_id
WHERE m.release_state <> 'failed' AND v.series_supported;

CREATE OR REPLACE VIEW ${catalog}.gold.v_revisions AS
SELECT v.release_id, m.created_at, v.previous_release_id, v.geography_id, v.frequency,
       v.period, v.previous_value, v.value, v.log_revision, v.cause
FROM ${catalog}.gold.revisions v
JOIN ${catalog}.gold.release_manifest m ON m.release_id = v.release_id;
