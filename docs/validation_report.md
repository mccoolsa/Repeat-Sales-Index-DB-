# Validation and sensitivity (Donegal pilot)

**Status:** **the link-threshold gate and the validation check passed** (2026-10-07). All 260 sample pairs are labelled, and
the link threshold is frozen at 0.5 (D-029). Final-link precision is 98.0% (95% CI
95.6-99.7%), short of the 99% target; this is disclosed, not tuned away.
**Reproduce:** `python jobs/validate_index.py --target local --profile donegal --report-dir reports`.
Databricks run 91318617189323 (validate_index task, 149 s) produced the same validation
version `vv-6cce430ca4ad` as the local run.
Charts: `reports/donegal_validation_cso.png`, `reports/donegal_sensitivity.png`.

## 1. CSO benchmark

The benchmark extract is pinned by SHA-256 (`config/validation.yml`) and read from the
object store. Monthly CSO levels are averaged over complete quarters or years. The three
provisional CSO months, and our own newest (provisional) period, are excluded. Growth
rates are compared, not levels. A non-index statistic (median price, % change) is
rejected by the loader (tested).

Year-on-year growth, Donegal repeat-sales against CSO RPPI *Border - houses* (HPM09
code 21), in percentage points:

| Our series | Periods | n | Bias | Mean abs. diff. | RMSE | Correlation | Same direction |
|---|---|---:|---:|---:|---:|---:|---:|
| Donegal annual | all 2011-2025 | 15 | +1.4 | 4.5 | 5.4 | 0.90 | 93% |
| Donegal annual | published only (2014-2025) | 12 | +0.3 | 4.2 | 5.0 | 0.55 | 92% |
| Letterkenny annual | all | 15 | +0.8 | 10.2 | 13.0 | 0.68 | 73% |
| Letterkenny annual | published only | 7 | +1.5 | 5.8 | 7.0 | 0.46 | 86% |
| Donegal quarterly (not published) | all | 61 | +1.0 | 12.0 | 16.0 | 0.52 | 75% |

**Turning point:** our trough is 2014, the CSO's 2013.

**Interpretation:** over the published years, Donegal growth has almost no bias (+0.3
points a year) against the CSO regional series. The two series differ by about 4 points a
year. That is expected given the differences in population and method:
- the CSO series covers five counties, houses only and household buyers;
- it is hedonic and mix-adjusted;
- ours includes apartments and is repeat-sales.

Correlation over the published years is moderate (0.55), because once the 2011-2013 crash
is excluded, the variation left is small relative to sampling noise. The quarterly
mismatch (RMSE 16 points) confirms the uncertainty stage's decision not to publish a quarterly Donegal
series. Agreement is not evidence that the linkage is right: both series use the same
stamp-duty filings.

## 2. Linkage evidence from human labels (link-threshold gate)

**Labels:** 260 pairs, one reviewer:
- 71 same property;
- 122 different;
- 67 can't tell, mostly townland-only addresses such as "KILTOY, LETTERKENNY", which a person
  cannot resolve either.

The sample is stratified, so results are weighted by inverse sampling probability.
Intervals come from a bootstrap within strata (2,000 draws). "Can't tell" labels are
excluded.

**What is measured:** the *final* links, meaning whether a labelled pair ends up in the same
property after the clustering constraints. That is what the index uses. Raw pair scores
are kept as a diagnostic.

| Links | Labelled pairs linked | False links | Weighted precision (95% CI) | Weighted recall (95% CI) |
|---|---:|---:|---|---|
| Probabilistic, p ≥ 0.3 | 48 | 4 | 98.1% (95.7-99.7%) | 77% (58-92%) |
| **Probabilistic, p ≥ 0.5 (frozen)** | **44** | **4** | **98.0% (95.6-99.7%)** | **75% (56-90%)** |
| Probabilistic, p ≥ 0.7 | 36 | 3 | 98.3% (95.9-100%) | 72% (54-87%) |
| Probabilistic, p ≥ 0.9 | 18 | 2 | 98.4% (93.5-100%) | 45% (19-72%) |
| Rule-based only | 45 | 7 | 95.5% (91.5-98.4%) | 77% (57-92%) |

Unweighted, 40 of the 44 links at 0.5 are true matches: 90.9%, Wilson 78.8-96.4%. The
unweighted figure is lower because the rarer, harder strata were deliberately over-sampled.

**Why 0.5:** precision is flat from 0.3 to 0.9, because the remaining false links score
high. Raising the threshold only discards true links, and recall falls from 75% to 45%.
Lowering it to 0.3 adds almost nothing and shifts 2015-2025 growth by −0.05 (§3). The
model beats the rule-based baseline on precision, at similar recall.

**The four false links:**
- three are generic apartment addresses in *different towns*: "Apt 1, Bridge St,
  Carndonagh" against "Apartment 1, Bridge Street, Killybegs", and "Apt 1, Main St"
  addresses in Buncrana and Newtown Cunningham;
- the fourth is "35 Glebe Hollow, Stranorlar" against "35 The Glebe, Donegal Town".

In each case the town extractor assigned no town, or the same weak one, to one side, so
neither the model nor the constraints saw the conflict. **Proposed fix (not applied):**
compare the raw town words when both addresses name a known town. It is not applied here,
because tuning rules on the same labels used to measure them would make the 98% figure
optimistic. The fix should be evaluated on a fresh labelled sample.

**Missed matches and label quality.** Of the 71 labelled matches, 31 are not linked. Most
are near-variants the blocking or model scores low, such as "Rann Mor Close" against
"Rann Mor Meadow", or "Whitehill" against "Whitehill Road". A few labels deserve a second
look, because they join homes in different towns ("7 St Bridgets Terrace, Kerrykeel"
against "… Bundoran") or different roads in one estate (Rann Mor Close / Meadow / Walk).
If those are not matches, the recall estimate rises; precision is unaffected.

## 3. Sensitivity grid

Each experiment rebuilds histories, pairs and fits from the same linkage scores with one
choice changed. Only periods with at least 20 endpoint pairs are compared. The table
shows log growth 2015-2025 for Donegal; the headline is 0.880, a factor of 2.41.

| Experiment | Pairs | Growth | Shift | Note |
|---|---:|---:|---:|---|
| Headline (p ≥ 0.5, WRS, second-hand) | 1,220 | 0.880 | - | |
| Rule-based links only | 1,862 | 0.872 | −0.008 | 52% more pairs, same growth |
| Threshold 0.3 | 1,576 | 0.828 | **−0.052** | flagged: looser links dampen growth |
| Threshold 0.7 | 1,065 | 0.877 | −0.003 | |
| Threshold 0.9 | 677 | 0.844 | −0.036 | |
| Threshold 0.97 / 0.99 | 491 / 409 | 0.919 / 0.922 | +0.039 / +0.042 | thin early years |
| With new-build first legs | 1,360 | 0.851 | −0.029 | new-build premium lowers growth |
| OLS / Huber / trimmed WRS | 1,220 | 0.876 / 0.894 / 0.871 | −0.004 / +0.014 / −0.009 | estimator barely matters |
| Excluding unusual changes | 1,098 | 0.865 | −0.015 | |
| Excluding resales within 2 years | 928 | 0.885 | +0.005 | |
| First sale from 2015 | 902 | 0.895 | +0.015 | |
| Quarterly fit, averaged to years | 1,220 | 0.881 | +0.001 | |
| Same valid Eircode on both legs | 230 | 0.535 (2021-25) | **+0.120** | flagged, see below |

**Findings:**

- **The threshold matters less than feared.** Between 0.5 and 0.97, Donegal 2015-2025
  growth moves by at most 0.04 log points (about 4%). Only the loosest setting (0.3) moves
  it by more than the 0.05 flag. In Letterkenny, 0.99 leaves too few early pairs to cover
  2015 at all.
- **The estimator choice is immaterial.** OLS, WRS, Huber and trimmed WRS agree within
  0.015 for Donegal.
- **Eircode-confirmed pairs grew faster in 2021-2025** (+0.12 against the headline over the
  same window). Eircodes are only filed from 2021, so this subset is small (230 pairs) and
  recent. There are two candidate explanations, not yet resolved:
  1. the headline contains false links that dampen measured growth, as the threshold-0.3
     result also suggests;
  2. homes filed with an Eircode are a different, faster-rising segment.

  Missed-match and accepted-link audits at the link-threshold gate should separate these. Until
  then, this is the main open risk to the headline.
- Letterkenny is more sensitive. Excluding unusual changes shifts it by −0.073, because a
  few large-return pairs carry weight in a 391-pair series.

## 4. Who is in the index (selection)

Shares of index-eligible sales, compared with shares of repeat-sale legs (headline pairs):

| Segment | All sales | Repeat legs | Gap |
|---|---:|---:|---:|
| Letterkenny | 19.2% | 32.2% | +13.0 pp |
| Other / rural | 55.6% | 37.6% | **−18.0 pp** |
| Under €150k | 58.9% | 67.5% | +8.6 pp |
| €350k+ | 5.0% | 3.3% | −1.7 pp |
| New dwellings | 9.4% | 0% | by design (D-007) |

Rural homes are under-represented, because townland-only addresses cannot be linked safely
(D-020). Cheaper homes are over-represented. No reweighting is applied: there is no
suitable reference stock to weight to. The index describes **resold, identifiable homes**,
which skew urban and lower-priced.

## 5. Frozen settings

These are now frozen:
- the WRS estimator;
- the headline population (second-hand to second-hand);
- anomaly flags only, with no exclusions;
- the publication policy (`publication.yml`);
- **the link threshold, 0.5** (`entities.yml`, `threshold_status: frozen`).

Freezing changed the entity version and therefore every downstream version. Index levels
were unchanged (24 revisions of 0, cause `method`), and the release state became
`published`.

The Eircode-subset question in §3 is still open. It is now bounded by the measured
precision: 2% false links cannot explain a 0.12 log-point growth gap on their own, which
points to segment differences rather than linkage error.
