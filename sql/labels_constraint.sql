-- Run once (Delta has no ADD CONSTRAINT IF NOT EXISTS).
ALTER TABLE ${catalog}.silver.link_labels
  ADD CONSTRAINT valid_label CHECK (label IN ('match', 'non_match', 'uncertain'));
