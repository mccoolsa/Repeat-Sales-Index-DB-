-- Least-privilege access for a labelling app's service principal: read the frozen sample,
-- read and append labels. No access to bronze, gold, or any other silver table.
--   python -m ireland_rsi.dbx_sql sql/app_grants.sql --var catalog=ireland_rsi --var app_sp=<id>
-- National app: app_sp = cd22aa04-fa45-4ba2-9bae-7218ae3a6485 (ireland-rsi-labelling)

GRANT USE CATALOG ON CATALOG ${catalog} TO `${app_sp}`;

GRANT USE SCHEMA ON SCHEMA ${catalog}.silver TO `${app_sp}`;

GRANT SELECT ON TABLE ${catalog}.silver.label_sample TO `${app_sp}`;

GRANT SELECT, MODIFY ON TABLE ${catalog}.silver.link_labels TO `${app_sp}`;
