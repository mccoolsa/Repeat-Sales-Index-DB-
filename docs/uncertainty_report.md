# Uncertainty and publication rules (Donegal pilot)

**Status:** bootstrap, gates and fallback implemented; guardrails calibrated on Donegal.
Results are conditional on the provisional link threshold (the link-threshold gate pending).
**Reproduce:** `python jobs/index_uncertainty.py --target local --profile donegal --report-dir reports`. Databricks run 403577573524793 (uncertainty task, 163 s) produced the same version `uv-06090cd842b3` and identical decisions.

## 1. Method

- **Property-history bootstrap:** properties are resampled with replacement and each brings
  all of its pairs, because a 3-sale property's two pairs are dependent. The full estimator
  (OLS → variance model → WLS) is refitted in every replicate. 1,000 draws for release,
  fixed seed 20261007.
- **Valid replicates** must identify exactly the full-sample periods with the same rebasing.
  Failures are counted by reason (`period_set_changed`, `unidentifiable`, ...).
- **Intervals** are 95% percentile intervals of levels and growth. The ratio between any
  two periods (needed for the property estimate) is computed *inside each draw* from the
  stored joint draws (`silver.index_draws`), preserving their covariance. The 2015 base year
  has zero width by construction, since every replicate is rebased to 100 there.
- **Scope of the intervals:** sampling uncertainty given the linkage and filtering
  decisions. Systematic false matching is assessed by threshold and disputed-link
  sensitivity in the validation stage.

- **Performance:** a valid replicate identifies exactly the full-sample periods, so its
  design is a resampling of the full design's rows. Replicates therefore reuse those rows and
  solve OLS → variance → WLS in NumPy, instead of rebuilding the design from dates through
  statsmodels. Results match the rebuild-and-refit reference to 10⁻⁹ (tested); the Donegal
  step fell from 63 s to 18 s locally. The first Databricks run took 17.5 minutes on
  serverless before this change.

## 2. Publication gates (`config/publication.yml`)

| Gate | Level | Value | Basis |
|---|---|---|---|
| Distinct properties | series | ≥ 300 | Calibrated (§3); starting value 100 |
| Effective sample size | series | ≥ 50 | Default |
| Bootstrap success | series | ≥ 95% | Default |
| Largest property weight share | series | ≤ 5% | "No single property dominating" |
| 2015 base identified | series | required | Comparable levels |
| Endpoint pairs | period | ≥ 20 | Default |
| 95% log interval width | period | ≤ 0.30 (≈ ±15%) | Calibrated (§3) |
| Newest period | period | provisional | The release stage refines from filing lags |

**Fallback** (same series for anchor and target; no cross-geography division, no
interpolation): Letterkenny quarterly → Letterkenny annual → Donegal quarterly → Donegal
annual → no estimate (`SPARSE_GEOGRAPHY` or `UNSUPPORTED_PERIOD`).

## 3. Calibration by downsampling Donegal

Random property subsets, 5 replicates each, 200 draws; error = RMS log difference from the
full-sample fit.

**Annual (17 years)**

| Properties | Bootstrap success | Median log width | RMS error vs full |
|---:|---:|---:|---:|
| 111 | 70% | 0.57 | 0.171 |
| 222 | 93% | 0.39 | 0.105 |
| 366 | 100% | 0.33 | 0.084 |
| 554 | 100% | 0.27 | 0.054 |
| 832 | 100% | 0.22 | 0.023 |
| 1,109 | 100% | 0.19 | - |

**Quarterly (67 quarters):** success reaches only 94% even with all 1,109 properties
(width 0.32); 54% at 554 properties.

**Conclusions:** the default 100-property floor was not binding (series that small fail
anyway), so it was raised to 300. The 0.30 width limit corresponds to an RMS error of about
0.06-0.08 log points. **Donegal does not support a quarterly series:** the pilot publishes
annually.

## 4. Donegal results (uncertainty version `uv-06090cd842b3`, 1,000 draws)

| Series | Published | Properties | Bootstrap success | Years shown | Median width |
|---|---|---:|---:|---|---:|
| **Donegal annual** | ✅ | 1,081 | 100% | 15 of 17 (2011-2012 suppressed; 2026 provisional) | 0.20 |
| Donegal quarterly | ❌ success 92.6% | 1,109 | 92.6% | 0 | 0.33 |
| **Letterkenny annual** | ✅ | 350 | 100% | 9 of 17 (2014-2019, 2021-2023) | 0.28 |
| Letterkenny quarterly | ❌ success 38.4% | 358 | 38.4% | 0 | 0.55 |

Selected Donegal annual values (2015 = 100, 95% interval): 2020 145.1 (131.4-160.3);
2023 201.0 (185.0-221.7); 2025 241.1 (219.3-267.8); 2026 255.5 (229.8-288.8, provisional).
The CSO Border houses series lies within the Donegal interval in most years (chart:
`reports/donegal_index_intervals.png`).

**Fallback examples:**
- Letterkenny 2015 → 2025: Letterkenny 2025 is suppressed (width 0.33), so the estimate
  uses **Donegal annual** (fallback level 3). The lookup must show this.
- 2012 → 2026: no series shows both years, so the result is **no estimate**
  (`UNSUPPORTED_PERIOD`).

## 5. Not done (by design)

Hierarchical town-within-county shrinkage is a separate, later model. It is adopted only
if it beats this fallback on held-out error and coverage.
