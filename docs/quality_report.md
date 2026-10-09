# Quality report: parsing and comparability

**Snapshot:** `ppr:national:7e1631bc229a5d92` (809,014 occurrences, acquired 2026-10-06)
**Rule set:** `qr-2026-10-06+fc7ea13861`. The version is a hash of `config/quality_rules.yml`,
`config/vat_rules.yml` and `clean.py`, so it changes whenever a rule changes.
**Reproduce:** `python jobs/clean_sales.py --target local --report-dir reports`

## 1. Accounting (accounting check)

Every source occurrence has exactly one primary disposition. Precedence is
quarantine > excluded > pending review > eligible. All rule hits are logged separately in
`silver.quality_events`, so rows with several flags are not double-counted.

| Primary disposition | Second-hand | New | Total |
|---|---:|---:|---:|
| Eligible (comparable market price) | 616,551 | 108,510 | **725,061** |
| Excluded | 33,070 | 23,670 | 56,740 |
| Pending review | 14,413 | 12,800 | 27,213 |
| Quarantined (unparseable) | n/a | n/a | **0** |
| **Total** | | | **809,014** |

Checks (all pass): total = parsed + quarantined; parsed = eligible + excluded + pending;
every quarantined row has a blocking reason.

**Quarantine is empty.** Every date parses strictly as `dd/mm/yyyy`, every price matches
`€#,###.##` and is positive, all flags are `Yes`/`No`, all 26 counties are recognised, and
every description maps to new or second-hand. The quarantine path is still tested (11 failure
types in `tests/test_clean.py`) for future snapshots.

## 2. Rule hits

A row can hit several rules. Primary-disposition counts are in §1.

| Rule | Rows | Effect | Meaning |
|---|---:|---|---|
| `NON_MARKET_SALE` | 41,209 | excluded | Source flags the price as not a full market price |
| `VAT_RULE_UNRESOLVED` | 14,464 | excluded | VAT-exclusive new dwelling on or after 8 Oct 2025: 9% or 13.5% cannot be determined |
| `NEW_PRICE_BASIS_UNCERTAIN` | 2,382 | excluded | New dwelling not flagged VAT-exclusive: inclusive price or filing error |
| `POSSIBLE_BULK_FILING` | 29,953 | pending review | ≥3 rows with the same date, county, price and development, or ≥10 with the same date, county and price |
| `MULTI_UNIT_ADDRESS` | 2,153 | pending review | Address lists or ranges several units ("25-40 Rathard Lawn"); also not linkage-eligible |
| `PRICE_REVIEW_LOW` | 1,100 | pending review | Below €10,000 (0.1th percentile of second-hand market prices) |
| `PRICE_REVIEW_HIGH` | 1,053 | pending review | Above €5,000,000 (about the 99.95th percentile) |
| `VAT_ADJUSTED` | 128,134 | none | Grossed up at 13.5%; before/after values logged |
| `DUPLICATE_OCCURRENCE` | 2,038 | none | Identical content appears more than once (960 groups); all copies kept |
| `HTML_ENTITY_IN_ADDRESS` | 31 | none | Source contains e.g. `&amp;`; address kept raw for the address stage |

Reconciliations with the source audit: 128,134 adjusted + 14,464 unresolved = 142,598
VAT-exclusive rows; 41,209 non-market; 2,382 new and not VAT-exclusive; 2,038 duplicate
occurrences = 1,078 extra copies + 960 originals.

## 3. Price basis for comparable prices

| `price_basis` | Rows | `analysis_price_cents` |
|---|---:|---|
| `reported` | 664,034 | Reported price. Second-hand sales are not VAT-exclusive (no VAT conflicts found) |
| `gross_from_vat_exclusive` | 128,134 | `round_half_up(net × 1.135)` under `VAT_RESIDENTIAL_13_5` (2010-01-01 to 2025-10-07) |
| `uncertain` | 16,846 | None; cannot be a price anchor or pair leg |

**Headline series (D-007):** second-hand legs use the reported price, a single consistent
basis with no VAT adjustment. New-build first legs (13.5% gross-up) are only for the
sensitivity series.

**VAT evidence.** The 13.5% rate applied throughout 2010 to 7 October 2025. The 2020-21
temporary cut affected only the standard rate (23% to 21%). From 8 October 2025, Revenue's
second reduced rate (9%) applies to *qualifying apartments in qualifying blocks* (at least
two storeys, three or more apartments, common access; scope extended 26 November 2025, runs
to 31 December 2030). Other new dwellings stay at 13.5%. The register cannot distinguish
these, so the rate is unresolved rather than guessed.

## 4. Stability over time

Share of second-hand occurrences that are index-eligible, by year:

| 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 |
|---|---|---|---|---|---|---|---|---|
| 93.5% | 93.1% | 91.2% | 92.1% | 92.5% | 91.3% | 91.9% | 91.7% | 91.3% |

| 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|
| 91.8% | 93.4% | 94.4% | 94.3% | 93.7% | 92.8% | 94.4% | 94.6% |

No year loses a disproportionate share, so exclusions should not create artificial index
movements. The validation stage will test this formally with sensitivity runs. Donegal (the pilot) has
17,685 eligible second-hand occurrences.

## 5. Known limitations

- **Bulk detection is a heuristic.** Some new-build estates sell several units at the same
  list price on the same day; these are flagged for review although the prices may be
  genuine. The validation stage compares indices with and without `POSSIBLE_BULK_FILING` rows.
- **Pending review is not yet reviewed.** These 27,213 rows are excluded from the index until
  a review policy is applied. No manual review has been done.
- **The source cannot be validated against the truth.** Valid-looking rows can still contain
  filing errors (PSRA acknowledges this). Unusual price changes are examined at the pair
  level in the property-history stage.
- **No house/apartment split** exists in the source, so the 9% VAT regime cannot be resolved
  and quality cannot be controlled by dwelling type.

## 6. Accounting check: passed on 2026-10-06

| | Local (DuckDB) | Databricks (job run 827057367655574, SUCCESS, 229 s) |
|---|---|---|
| Rule set | `qr-2026-10-06+fc7ea13861` | `qr-2026-10-06+fc7ea13861` |
| Sales / events / quarantine | 809,014 / 222,517 / 0 | 809,014 / 222,517 / 0 |
| Eligible / excluded / pending | 725,061 / 56,740 / 27,213 | identical |
| Accounting checks | all pass | all pass |
| Ingest task in the same run | n/a | `already_active`, 0 rows inserted, rebuild identical |
