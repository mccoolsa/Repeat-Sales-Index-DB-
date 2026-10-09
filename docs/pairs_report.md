# Property histories and repeat-sale pairs (Donegal pilot)

**Status:** built and audited. The link acceptance threshold (p ≥ 0.5, production track) is
**provisional until the link-threshold gate** selects it from labelled data. Rerunning after the link-threshold gate is a config
change (`config/entities.yml`).
**Reproduce:** `python jobs/build_pairs.py --target local --profile donegal --report-dir reports`

## 1. Method

1. **Constrained clustering.** Accepted links are added strongest first. A link is refused
   when it would put conflicting facts into one property: different unit or primary
   numbers, different valid Eircodes, two different prices on one day, more than 12 sales,
   or (see §2) a non-specific address without a shared Eircode. Refused links are stored in
   `silver.entity_rejected_edges` for review. This prevents A-B, B-C chains from merging
   apartment 4 with apartment 14.
2. **Property IDs** are generated (not official) and named after the property's earliest
   sale, so an ID survives as later sales join. `silver.entity_crosswalk` records
   merges and splits between entity versions.
3. **Pairs** join consecutive comparable sales within a property: three sales give two
   pairs. A non-comparable sale (non-market, unresolved VAT, flagged bulk filing or extreme
   price) ends a segment; pairs never bridge across it. Identical filings on the same day
   count as one transaction.
4. **Decisions:** `included_headline` (second-hand to second-hand), `included_new_first_leg`
   (sensitivity only), `diagnostic_same_quarter`, `excluded_zero_holding`,
   `excluded_new_second_leg`.
5. **Pair audit** on every run: both legs eligible, same property, no eligible sale
   skipped, no non-comparable sale bridged. A failed audit stops the run.

## 2. Over-merging found and fixed: non-specific addresses

The first Donegal run produced "properties" with up to 12 sales, and 28% of included pairs
were held under two years. Inspection showed the large clusters were **unnumbered addresses
that name only a townland**, for example "BALLYNESS, FALCARRAGH": 9 sales from €30k to €390k,
clearly different homes. The share of numbered addresses fell from 76% in 2-sale
properties to about 0% in 5+-sale properties.

**Rule (D-020):** an address with no house number, unit or range is **non-specific** when its
first component is only a townland or town name, or a road, estate or block name ("Trusk
Road", "Oak Grove", "Summerhill Apts"). Non-specific sales link only when both carry the
same valid Eircode. Named houses ("Rose Cottage", "The Old Rectory") are unaffected.

| | Before | After |
|---|---:|---:|
| Properties with 6+ sales | 42 | 1 (a portfolio-split building whose rows never form pairs) |
| Accepted links refused for Eircode conflict | 388 | 24 |
| Unusual price changes (share of included pairs) | 17.3% | 9.0% |
| Short-hold resales | 28.3% | 22.6% |
| Headline pairs | 2,069 | 1,220 |

Early human labels agree: of the first 10 rural unnumbered pairs reviewed, 0 were judged the
same property, 7 different and 3 undecidable.

**Cost:** older rural sales described only by a townland cannot enter the repeat-sales
sample (Eircodes exist mainly from 2021). Rural Donegal is therefore under-represented
before 2021. The validation stage must report this selection effect, and the methodology must state it.

## 3. Donegal results (entity version `ent-15cb2e0a501e`, model `lm-4ed537c520de`)

| Measure | Value |
|---|---:|
| Linkage-eligible sales | 21,404 |
| Properties | 19,790 (1,455 with repeat sales) |
| Links refused | 2,984: 2,935 non-specific address, 24 Eircode conflict, 17 same-day price, 8 unit |
| Pairs | 1,394: **1,220 headline**, 140 new-build first leg, 22 new second leg (excluded), 11 same quarter, 1 zero hold |
| Median hold (headline) | 3.85 years (10th-90th percentile 1.0-9.4) |
| Median log price change (headline) | 0.37 (≈ +45%) |
| The pair audit | 1,394 pairs checked, **0 violations** |

**Proxy rates** (included pairs, denominator 1,360): short-hold resales (< 2 years) 307
(22.6%); unusual price changes 123 (9.0%). These are proxies, not evidence of renovation.
Possible explanations include false links, multi-unit filings, filing errors and genuine
quick resales. Unnumbered pairs still have about twice the short-hold rate of numbered
ones, which is why the residual-return anomaly review after the index estimation stage matters.

**Reproducibility:** Databricks job run 236736068542960 (SUCCESS, 381 s) and a local run
produced the same model version, the same entity version and identical counts, and the pair audit
audit passed on both.

## 4. Open items

- **Threshold:** set at the link-threshold gate from labels, then rebuild (new entity version + crosswalk).
- **Residual anomalies:** after the first index fit, flag pairs whose return
  departs strongly from the market path, using a frozen training-only policy.
- **Multi-unit buildings without unit numbers** (e.g. "84 Lower Main Street": the same
  €84,375 on 7 dates) are excluded through the bulk-filing flag. A dedicated rule for
  repeated identical prices at one address may be needed nationally.
