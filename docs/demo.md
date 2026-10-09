# Demo walk-through (about 10 minutes)

The sequence follows the dashboard check: a reviewer can reproduce the pilot, inspect the measured
linkage error, follow one estimate back to source rows, understand suppression, and compare
two vintages.

## 1. The pipeline (Databricks Jobs)

Open **Jobs → `ireland-rsi refresh (donegal)`**. It has 11 serverless tasks, from
ingest through linkage (two tracks in parallel), pairs, index, uncertainty, validation and
backtest to publish. Show:
- a full run;
- the `publish_release` output (release ID, gates, metrics);
- the monthly schedule (10th of each month, 06:00);
- the failure email setting.

## 2. Unity Catalog

Open **Catalog → `ireland_rsi_donegal`**:

| Schema | What to show |
|---|---|
| `bronze` | untouched source rows and the `raw` volume of content-addressed files |
| `silver` | sales, address features, linkage candidates, properties, pairs, fits, bootstrap draws |
| `gold` | releases, vintages, lookup history, calibration, validation results and the dashboard views |

Run `SELECT * FROM ireland_rsi_donegal.gold.v_current_release` to show the pointer and the
versions that make up the release.

## 3. Dashboard

Open **Dashboards → Ireland repeat-sales index (donegal)**:

- **Market:**
  - the Donegal and Letterkenny annual indices;
  - the county band;
  - why quarterly series are not published (the support table);
  - The release state counter reads `published`.
- **Linkage:** precision and recall by threshold from 260 labels. The `final_links` rows
  (98.0% precision at 0.5) are the links the index uses; the `labels` rows are raw scores.
- **Data quality:** every row has a disposition; cells under 10 are suppressed.
- **Robustness:**
  - the threshold and method fan;
  - the Eircode-subset flag;
  - two releases on one chart, plus the revisions table (cause `method`, 0 change).
- **Validation:**
  - the CSO comparison;
  - The backtest against baselines;
  - interval coverage by holding band.

## 4. Lookup app

Open **`ireland-rsi-lookup-donegal`** and type `19 Ard Colmcille, Letterkenny`.

1. **Sale history:**
   - three sales, linked with a moderate match;
   - reported and VAT-adjusted prices;
   - which sales can anchor an estimate.
2. **Estimate:**
   - pick the 2014 sale as the anchor and 2025 as the target;
   - the label reads "Index-adjusted estimate as of 2025", with a 90% interval;
   - the note says the annual Donegal series was used because quarterly is not published.
3. **Honest failures:**
   - choose a non-market sale as the anchor: refused with its reason, not swapped;
   - search `Glencar, Letterkenny` and pick an old anchor: `UNSUPPORTED_PERIOD`
     (2011-2012 are suppressed).
4. **Chart:** the shaded band is the index's confidence interval, not the property's.

## 5. Trace one estimate to source rows

```sql
-- the anchor sale in the release, then its bronze source row
SELECT * FROM ireland_rsi_donegal.gold.lookup_history
WHERE release_id = '<current>' AND address ILIKE '19 ARD COLMCILLE%';
SELECT s.raw_row_id, r.* FROM ireland_rsi_donegal.silver.sales s
JOIN ireland_rsi_donegal.bronze.ppr_rows r ON r.raw_row_id = s.raw_row_id
WHERE s.sale_id = '<sale_id>';
-- the pairs that built the index in the anchor and target years
SELECT * FROM ireland_rsi_donegal.silver.repeat_pairs
WHERE entity_version = '<entities version>' AND decision = 'included_headline'
  AND (year(date_1) IN (2014, 2025) OR year(date_2) IN (2014, 2025));
```

## 6. Reproduce locally

```bash
python -m pytest -q
python jobs/run_pipeline.py --profile donegal --start clean
```

Local and Databricks runs produce the same version IDs:
- fit version `fv-3a7219ee3358`;
- entity version `ent-dbf7452a48d5` (frozen threshold);
- validation version `vv-94dd8f3d284f`;
- release `rel-57ed44641cde`.
