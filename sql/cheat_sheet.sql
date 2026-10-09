-- Ireland repeat-sales index: Databricks SQL cheat sheet
-- Every query was executed on 2026-10-06 against catalog ireland_rsi.


-- ======================================================================
-- Unity Catalog & governance (Setup)
-- ======================================================================

-- 1. Browse the project catalog
-- Schemas follow the medallion layout: bronze (untouched), silver (parsed), gold (published).
SHOW SCHEMAS IN ireland_rsi;

-- 2. Table metadata: format, size, files
-- DESCRIBE DETAIL shows format, file count and size (it cannot be used as a subquery).
DESCRIBE DETAIL ireland_rsi.bronze.ppr_rows;

-- 3. Who can do what
-- The labelling app's service principal can only read the sample and append labels.
SHOW GRANTS ON TABLE ireland_rsi.silver.link_labels;

-- 4. Files in a Unity Catalog volume
-- Untouched downloads are content-addressed; each acquisition has a JSON evidence record.
LIST '/Volumes/ireland_rsi/bronze/raw/acquisitions/psra_ppr';

-- ======================================================================
-- Ingestion & snapshots (Immutable bronze)
-- ======================================================================

-- 5. Acquisition history
-- Every download is recorded, including re-downloads of identical bytes and rejections.
SELECT retrieved_utc, status, record_count, left(member_sha256, 12) AS member_sha
FROM ireland_rsi.bronze.source_manifest
ORDER BY retrieved_utc DESC;

-- 6. Active snapshot per scope
-- Promotion is append-only; the latest 'promoted' event is the active snapshot.
SELECT scope, snapshot_id, decided_at, reason
FROM ireland_rsi.bronze.snapshot_events
WHERE decision = 'promoted'
QUALIFY ROW_NUMBER() OVER (PARTITION BY scope ORDER BY decided_at DESC) = 1;

-- 7. Duplicate occurrences are kept, not deleted
-- Identical source rows remain separate occurrences with their own sale_id.
SELECT count(*) AS duplicate_groups, sum(n - 1) AS extra_copies, max(n) AS max_copies
FROM (SELECT raw_content_fp, count(*) AS n
      FROM ireland_rsi.bronze.ppr_rows
      GROUP BY raw_content_fp HAVING count(*) > 1);

-- 8. Corrections are new versions, never overwrites
-- A price or date correction keeps the sale_id and adds a version.
SELECT change_type, count(*) AS versions
FROM ireland_rsi.silver.sale_versions
GROUP BY change_type;

-- 9. Delta history (operational audit)
-- Time travel is for recovery; explicit snapshots are the archival contract.
SELECT version, timestamp, operation, operationMetrics.numTargetRowsInserted AS inserted
FROM (DESCRIBE HISTORY ireland_rsi.bronze.ppr_rows)
ORDER BY version DESC LIMIT 5;

-- ======================================================================
-- Parsing & comparability (Silver sales)
-- ======================================================================

-- 10. Every occurrence has one disposition
-- Pin the current rule set via the stage registry, then partition the snapshot.
WITH cur AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
             WHERE stage = 'clean'
             QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1)
SELECT dwelling_status, disposition, count(*) AS n
FROM ireland_rsi.silver.sales
WHERE rule_set_version = (SELECT output_version FROM cur)
GROUP BY ALL ORDER BY dwelling_status, n DESC;

-- 11. All rule hits, one row per reason
-- reason_codes holds every rule a row triggered; explode it to count each.
WITH cur AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
             WHERE stage = 'clean'
             QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1)
SELECT reason, count(*) AS n
FROM ireland_rsi.silver.sales
LATERAL VIEW explode(split(reason_codes, ',')) AS reason
WHERE rule_set_version = (SELECT output_version FROM cur) AND reason <> ''
GROUP BY reason ORDER BY n DESC;

-- 12. Audit the VAT gross-up
-- Recompute 13.5% from the logged before/after values; mismatches should be zero.
WITH cur AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
             WHERE stage = 'clean'
             QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1)
SELECT count(*) AS adjusted,
       sum(CASE WHEN CAST(value_after AS BIGINT)
                     = round(CAST(value_before AS BIGINT) * 1.135) THEN 0 ELSE 1 END) AS mismatches
FROM ireland_rsi.silver.quality_events
WHERE rule_id = 'VAT_ADJUSTED' AND rule_set_version = (SELECT output_version FROM cur);

-- 13. Find placeholder Eircodes
-- A real Eircode cannot belong to several counties; A123456 was typed on 190 sales.
WITH cur AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
             WHERE stage = 'clean'
             QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1)
SELECT eircode, count(*) AS sales, count(DISTINCT county) AS counties
FROM ireland_rsi.silver.sales
WHERE rule_set_version = (SELECT output_version FROM cur) AND eircode IS NOT NULL
GROUP BY eircode HAVING count(DISTINCT county) > 2
ORDER BY counties DESC;

-- 14. Median second-hand price by year (descriptive only)
-- Medians mix property types and quality; this is why the project builds a repeat-sales index.
WITH cur AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
             WHERE stage = 'clean'
             QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1)
SELECT year(sale_date) AS yr,
       CAST(percentile_approx(analysis_price_cents / 100, 0.5) AS INT) AS median_eur,
       count(*) AS sales
FROM ireland_rsi.silver.sales
WHERE rule_set_version = (SELECT output_version FROM cur)
  AND index_eligible AND dwelling_status = 'second_hand'
GROUP BY ALL ORDER BY yr;

-- ======================================================================
-- Addresses & localities (Address features)
-- ======================================================================

-- 15. Town coverage by county and confidence
-- PIVOT turns confidence levels into columns (number of sales per county).
WITH addr AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
              WHERE stage = 'addresses'
              QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1)
SELECT * FROM (
  SELECT county, locality_confidence
  FROM ireland_rsi.silver.address_features
  WHERE address_version = (SELECT output_version FROM addr))
PIVOT (count(*) FOR locality_confidence IN ('high', 'medium', 'low', 'none'))
ORDER BY county;

-- 16. Irish and English names for the same town
-- The county-scoped alias table links Leitir Ceanainn to Letterkenny.
SELECT alias, target_id, language, origin
FROM ireland_rsi.silver.place_aliases
WHERE county = 'Donegal' AND target_type = 'bua'
  AND alias_norm IN ('letterkenny', 'leitir ceanainn', 'an bun beag', 'bunbeg');

-- 17. The post-town effect
-- Addresses that mention Letterkenny but are assigned to a more specific place.
WITH addr AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
              WHERE stage = 'addresses'
              QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1)
SELECT coalesce(locality_name, '(county level)') AS assigned_to, count(*) AS n
FROM ireland_rsi.silver.address_features
WHERE address_version = (SELECT output_version FROM addr)
  AND lower(address_display) LIKE '%letterkenny%'
GROUP BY ALL ORDER BY n DESC LIMIT 8;

-- 18. Validate towns against Eircode routing keys
-- Routing keys are independent of address text: the share of each town's sales per key.
WITH cur AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
             WHERE stage = 'clean'
             QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1),
addr AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
              WHERE stage = 'addresses'
              QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1)
SELECT a.locality_name, left(s.eircode, 3) AS routing_key, count(*) AS n,
       round(100 * count(*) / sum(count(*)) OVER (PARTITION BY a.locality_name), 1) AS pct
FROM ireland_rsi.silver.address_features a
JOIN ireland_rsi.silver.sales s USING (snapshot_id, raw_row_id)
WHERE a.address_version = (SELECT output_version FROM addr)
  AND s.rule_set_version = (SELECT output_version FROM cur)
  AND s.eircode_valid AND a.locality_confidence = 'high'
  AND a.locality_name IN ('Letterkenny', 'Bundoran')
GROUP BY ALL QUALIFY pct >= 1 ORDER BY locality_name, n DESC;

-- ======================================================================
-- Labels & evaluation design (Human review)
-- ======================================================================

-- 19. Labelling progress by stratum
-- Latest label per pair from the append-only label table, joined to the frozen sample.
WITH latest AS (
  SELECT pair_id, label FROM ireland_rsi.silver.link_labels
  QUALIFY ROW_NUMBER() OVER (PARTITION BY pair_id, reviewer ORDER BY labelled_at DESC) = 1)
SELECT s.stratum, count(*) AS pairs, count(l.label) AS labelled,
       count_if(l.label = 'match') AS same, count_if(l.label = 'non_match') AS different
FROM ireland_rsi.silver.label_sample s
LEFT JOIN latest l USING (pair_id)
WHERE s.sample_version = 'ls-2026-10-06+42e09aca'
GROUP BY s.stratum ORDER BY pairs DESC;

-- 20. No property group leaks across splits
-- Every connected group of sales sits in exactly one of dev / test / test_temporal.
SELECT split, count(*) AS pairs, count(DISTINCT group_id) AS groups,
       (SELECT count(*) FROM (SELECT group_id FROM ireland_rsi.silver.label_sample
          WHERE sample_version = 'ls-2026-10-06+42e09aca'
          GROUP BY group_id HAVING count(DISTINCT split) > 1)) AS leaking_groups
FROM ireland_rsi.silver.label_sample
WHERE sample_version = 'ls-2026-10-06+42e09aca'
GROUP BY split;

-- 21. Eircode weak labels and their conflicts
-- A shared Eircode is weak positive evidence; conflicts flag block-level or reused codes.
WITH cur AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
             WHERE stage = 'clean'
             QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1)
SELECT weak_label, coalesce(conflict_reason, '-') AS conflict, count(*) AS pairs
FROM ireland_rsi.silver.weak_labels
WHERE rule_set_version = (SELECT output_version FROM cur)
GROUP BY ALL ORDER BY pairs DESC;

-- ======================================================================
-- Preview: repeat-sale pairs (Exploration ahead of index estimation)
-- ======================================================================

-- 22. Consecutive sales of an identical address (LAG)
-- Exploratory only: exact address text is not the evaluated linkage model.
WITH cur AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
             WHERE stage = 'clean'
             QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1),
addr AS (SELECT output_version FROM ireland_rsi.silver.stage_runs
              WHERE stage = 'addresses'
              QUALIFY ROW_NUMBER() OVER (ORDER BY completed_at DESC) = 1),
s AS (
  SELECT a.county, a.address_norm, s.sale_date, s.analysis_price_cents AS price
  FROM ireland_rsi.silver.sales s
  JOIN ireland_rsi.silver.address_features a USING (snapshot_id, raw_row_id)
  WHERE s.rule_set_version = (SELECT output_version FROM cur)
    AND a.address_version = (SELECT output_version FROM addr)
    AND s.index_eligible AND s.dwelling_status = 'second_hand'
    AND a.primary_number IS NOT NULL AND a.locality_confidence = 'high'),
p AS (
  SELECT *, LAG(sale_date) OVER w AS prev_date, LAG(price) OVER w AS prev_price
  FROM s WINDOW w AS (PARTITION BY county, address_norm ORDER BY sale_date))
SELECT count(*) AS pairs,
       round(avg(datediff(sale_date, prev_date) / 365.25), 1) AS avg_hold_years,
       round(percentile_approx(ln(price / prev_price), 0.5), 3) AS median_log_return
FROM p WHERE prev_date IS NOT NULL AND sale_date > prev_date;

-- ======================================================================
-- Operations (Lineage & versions)
-- ======================================================================

-- 23. Which version did each stage produce?
-- The stage registry pins downstream work to exact upstream versions.
SELECT stage, output_version, completed_at
FROM ireland_rsi.silver.stage_runs
QUALIFY ROW_NUMBER() OVER (PARTITION BY stage ORDER BY completed_at DESC) = 1
ORDER BY completed_at;

-- 24. Row counts across the pipeline
-- Silver tables keep every rule-set and address version (insert-only), so they hold a multiple of the 809,014 source occurrences; filter by version for current data.
SELECT 'bronze.ppr_rows' AS table_name, count(*) AS n FROM ireland_rsi.bronze.ppr_rows
UNION ALL SELECT 'silver.sales', count(*) FROM ireland_rsi.silver.sales
UNION ALL SELECT 'silver.address_features', count(*) FROM ireland_rsi.silver.address_features
UNION ALL SELECT 'silver.label_sample', count(*) FROM ireland_rsi.silver.label_sample
UNION ALL SELECT 'silver.link_labels', count(*) FROM ireland_rsi.silver.link_labels;
