-- Human label table for the labelling app. Created up front so the app can
-- append to it; the CHECK constraint is added once by sql/labels_constraint.sql.
CREATE TABLE IF NOT EXISTS ${catalog}.silver.link_labels (
  sample_version STRING NOT NULL,
  pair_id STRING NOT NULL,
  reviewer STRING NOT NULL,
  labelled_at TIMESTAMP NOT NULL,
  label STRING NOT NULL,
  rationale STRING,
  app_version STRING
) COMMENT 'Append-only human labels; the latest label per pair and reviewer is current';
