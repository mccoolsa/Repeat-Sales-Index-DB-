-- Least-privilege access for the lookup app's service principal: read the gold serving
-- tables of published releases. No access to bronze or silver.
--   python -m ireland_rsi.dbx_sql sql/lookup_grants.sql --var catalog=ireland_rsi_donegal --var app_sp=<id>

GRANT USE CATALOG ON CATALOG ${catalog} TO `${app_sp}`;

GRANT USE SCHEMA ON SCHEMA ${catalog}.gold TO `${app_sp}`;

GRANT SELECT ON TABLE ${catalog}.gold.release_events TO `${app_sp}`;

GRANT SELECT ON TABLE ${catalog}.gold.release_manifest TO `${app_sp}`;

GRANT SELECT ON TABLE ${catalog}.gold.index_vintages TO `${app_sp}`;

GRANT SELECT ON TABLE ${catalog}.gold.lookup_history TO `${app_sp}`;

GRANT SELECT ON TABLE ${catalog}.gold.release_calibration TO `${app_sp}`;
