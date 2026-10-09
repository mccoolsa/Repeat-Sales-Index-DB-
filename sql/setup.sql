-- Unity Catalog objects for the project. Idempotent; run with:
--   python -m ireland_rsi.dbx_sql sql/setup.sql --var catalog=ireland_rsi_donegal
-- On Free Edition the catalog must be created by SQL (or the UI) so it uses Default Storage.

CREATE CATALOG IF NOT EXISTS ${catalog}
  COMMENT 'Ireland repeat-sales house price index (portfolio project): ${catalog}';

CREATE SCHEMA IF NOT EXISTS ${catalog}.bronze
  COMMENT 'Immutable source bytes, manifests and raw source rows';

CREATE SCHEMA IF NOT EXISTS ${catalog}.silver
  COMMENT 'Parsed sales, quality events, addresses, linkage and repeat pairs';

CREATE SCHEMA IF NOT EXISTS ${catalog}.gold
  COMMENT 'Published index vintages, validation, lookup serving tables and release manifests';

CREATE VOLUME IF NOT EXISTS ${catalog}.bronze.raw
  COMMENT 'Untouched source downloads (PSRA, CSO, reference data) with manifests';

CREATE VOLUME IF NOT EXISTS ${catalog}.bronze.staging
  COMMENT 'Transient Parquet files used to merge rows into Delta tables; safe to empty';
