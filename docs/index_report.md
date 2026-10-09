# Repeat-sales index estimation (Donegal pilot)

**Status:** estimator implemented and verified on synthetic data; Donegal research fits
produced. These are **research fits**: the link threshold behind the pairs is provisional
(the link-threshold gate pending), intervals come in the uncertainty stage, and the formal CSO validation is the validation stage.
**Reproduce:** `python jobs/fit_index.py --target local --profile donegal --report-dir reports`. Databricks job run 935351075266298 (SUCCESS, 374 s) produced the same fit version `fv-c4bed5c7ca78` and identical levels.

## 1. Estimator

For each pair sold in periods s < t: y = log(p_t / p_s), X has −1 in column s and +1 in
column t, the reference period is fixed at 0, and there is no intercept. The index is
100 · exp(β_t) rebased so the mean of the 2015 levels is 100. Rebasing changes levels, not
growth (tested).

| Method | Use |
|---|---|
| `ols` | Unweighted baseline |
| `wrs` | **Headline:** Case-Shiller three-stage. OLS, then non-negative least squares of squared residuals on holding years (σ² = a + b·years), then WLS with weights 1/σ². The variance has a floor at its 5th percentile; weights are capped at 10× the median. |
| `huber` | Robust M-estimate (sensitivity) |
| `trimmed` | WRS after trimming the 1% tails of log returns (sensitivity) |

**Identifiability:** periods are linked through pairs. The connected set holding the most
pairs is estimated, and its earliest period is the reference. Pairs in other components are
excluded and their periods reported as unidentified; they are never spliced in. A
rank-deficient design fails the fit. Same-period pairs are excluded and counted.

**Weighting convention:** `statsmodels.WLS(weights = 1/σ²)` and OLS on rows scaled by 1/σ are
tested to agree to 10⁻¹⁰. Match scores are not multiplied into the weights: linkage
uncertainty and holding-period noise are different mechanisms.

## 2. Synthetic verification (the identifiability check, part 1)

`tests/test_index.py` (12 tests): a known random-walk index over 24 quarters, with noise
rising with holding time, is recovered by all four methods with maximum error < 0.02 log
points. Uniform prices give exactly 100. Rebasing preserves ratios. Disconnected periods are
reported, not spliced. Same-period-only data raises an error. Variance and weights are
finite, positive, floored and capped. Annual fits work.

## 3. Donegal research fits (the identifiability check, part 2)

Entity version `ent-15cb2e0a501e`, headline population (second-hand to second-hand), 2015 = 100.

| Geography | Frequency | Pairs | Periods | Reference | Latest level | Periods with < 20 endpoint pairs |
|---|---|---:|---:|---|---|---:|
| Donegal | annual | 1,182 | 17 | 2010 | 255.5 (2026) | 0 |
| Donegal | quarterly | 1,220 | 67 | 2010Q1 | 271.1 (2026Q3) | 15 |
| Letterkenny | annual | 381 | 17 | 2010 | 293.9 (2026) | 3 |
| Letterkenny | quarterly | 391 | 65 | 2010Q1 | 284.2 (2026Q3) | **58** |

All 20 configured fits are identifiable (4 methods × 2 frequencies × 2 geographies, plus
the new-build-first-leg sensitivity). Across methods, Donegal annual 2026 ranges from
255.5 (WRS) to 270.3 (Huber); Letterkenny annual from 293.9 to 308.8. **Quarterly
Letterkenny is unsupported** by the publication guardrails (the uncertainty stage decides annual or a county
fallback).

**Variance model (Donegal annual WRS):** σ² = 0.202 + 0.0049 · years, residual SD 0.47.
The large intercept means dispersion barely grows with holding time, so weights are nearly
uniform (effective sample size 1,177 of 1,182) and WRS ≈ OLS. 24 pairs exceed 4 robust
standard deviations. Such dispersion points to remaining false links, multi-unit filings
and quality change; the residual anomaly review and human labels address it.

## 4. Sense check against the CSO (not a validation)

Donegal annual WRS against CSO RPPI **Border houses** (HPM09, mean of monthly levels per
year):

| | Ours | CSO Border houses |
|---|---:|---:|
| Cumulative log growth 2015→2025 | 88.0 | 87.5 |
| 2026 level (2015 = 100) | 255.5 | 258.6 |
| Year-on-year growth correlation (2011-2026) | 0.895 | |
| Mean absolute YoY difference | 4.3 pp | |

The CSO series covers five counties and houses only, is hedonic and smoothed, and draws
on the same stamp-duty source. Agreement is encouraging but is not evidence that the
linkage is correct. Chart: `reports/donegal_index_vs_cso.png`.

## 5. Outputs

`silver.index_fits` (one row per fit: inputs, status, exclusions, variance model,
diagnostics), `silver.index_points` (β, conventional SE, level, endpoint pairs per period),
`silver.index_pair_residuals` (headline WRS residuals and weights, for anomaly review and
the uncertainty stage bootstrap).
