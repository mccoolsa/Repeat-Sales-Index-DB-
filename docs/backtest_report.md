# Lookup backtest and prediction intervals (Donegal pilot)

**Status:** the backtest gate passed for the Donegal series. Town-level estimates are not used,
because they did not beat Donegal. Results are conditional on the provisional link threshold.
**Reproduce:** `python jobs/backtest_lookup.py --target local --profile donegal --report-dir reports`.
Databricks run 91318617189323 (backtest_lookup task, 52 s) produced the same version
`bv-d84ffd5003f7`. Chart: `reports/donegal_backtest.png`.

## 1. Design

- **Test case:** a headline repeat pair. The first sale is the anchor and the second sale
  is the truth; the estimate is anchor × index[target year] / index[anchor year].
- **No future data:** for target year Y, the index uses only pairs whose later sale is in
  or before Y.
- **No self-evidence:** properties are split into 5 folds by a hash of their ID. Each fold
  is predicted from an index fitted without any of its pairs.
- **Support:** a fold index may adjust a price only with ≥ 300 properties and ≥ 20 endpoint
  pairs in both years (the publication thresholds).
- **Windows by target year:**
  - development: 2013-2018;
  - calibration: 2019-2022 (interval quantiles only);
  - untouched test: 2023-2025.
- **Retrospective, not operational:** there is one present-day source snapshot, so filing
  delays cannot be replayed. This does not measure real-time performance.

Tests (`tests/test_backtest.py`):
- tripling every price after 2016 does not change any 2016-or-earlier prediction;
- held-out properties never enter their fold's fit;
- noise-free data is predicted exactly;
- unsupported cases return no amount, never zero or the nominal price;
- interval quantiles ignore test-window errors.

## 2. Accuracy against simple baselines (pairs every method could predict)

| Method | Test MdAPE | Test bias (log) | Test RMSE (log) | Calibration MdAPE |
|---|---:|---:|---:|---:|
| Unchanged price | 37.8% | +0.515 | 0.751 | 38.3% |
| CSO national RPPI ¹ | 26.7% | +0.151 | 0.548 | 23.3% |
| **Donegal index (lookup)** | **25.6%** | **−0.007** | **0.534** | **22.7%** |

¹ The CSO national series is final, published data, so it has the benefit of hindsight.

n = 446 test pairs. Index adjustment removes almost all of the bias of an unadjusted price.
It beats the national index on bias (−0.007 against +0.151) and slightly on typical error.
The remaining error of about 25% is property-specific (condition, extensions, the
particular buyer), which no area index can see.

**Development window:** no fold index had enough support before 2019. Fewer than 300
properties had resold by then, so 2013-2018 targets get no estimate (support rate 0%;
62% in the calibration window, 100% in the test window). This is the publication gate
working as intended, not a gap in the code.

## 3. Does a town index help? (backtest gate)

Letterkenny's fold indices never reach 300 properties once a fold is held out. As a
diagnostic only, the town series was re-run with a 100-property floor. It was compared
with Donegal on the Letterkenny homes both could predict:

| Window | n | Letterkenny index MdAPE | Donegal index MdAPE |
|---|---:|---:|---:|
| Calibration | 53 | 23.9% | **19.0%** |
| Test | 135 | 28.8% | **24.2%** |

The town index is worse in both windows: the extra local signal is outweighed by its
noise. **Decision D-025:** property estimates use the Donegal series everywhere
(`publication.yml → lookup.estimate_chain: scope`). The Letterkenny index is still
published and shown as an index.

## 4. Error by holding period

| Holding | Test MdAPE (index) | Test MdAPE (unchanged) | Index bias |
|---|---:|---:|---:|
| 0-3 years | 17.8% | 24.4% | **+0.151** (estimates too low) |
| 3-6 years | 22.5% | 31.8% | −0.009 |
| 6+ years | 35.8% | 47.4% | **−0.113** (estimates too high) |

This is the classic repeat-sales pattern:
- **Short-hold resales gain more than the market**, consistent with buy-renovate-sell.
- **Long holds gain less than the market**, consistent with depreciation and
  under-maintenance.

The index cannot correct either effect; the prediction intervals absorb them. These are
proxies, not claims about individual homes.

## 5. Property prediction intervals (90%)

Empirical 5th and 95th percentiles of calibration-window log errors in each band, then
tested on 2023-2025:

| Band | Calibration n | Interval (× estimate) | Test n | Test coverage | 95% CI | Status |
|---|---:|---|---:|---:|---|---|
| 0-3 y | 63 | 0.56 to 2.32 | 125 | 88.0% | 81-93% | ok |
| 3-6 y | 101 | 0.62 to 1.92 | 139 | 85.6% | 79-90% | ok |
| 6 y+ | 89 | 0.42 to 1.82 | 182 | 85.7% | 80-90% | ok |

**Assessment:**
- **Coverage:** all bands are within the 5-point tolerance, and every confidence interval
  includes 90%. However, all three point estimates are below 90%. The intervals slightly
  under-cover, and the next recalibration should widen them.
- **Width:** the intervals are wide, from roughly half to double the estimate. That is an
  honest picture of what an index adjustment can say about one home.
- **Horizon:** dispersion does not rise steadily with holding time. Short holds are the
  noisiest relative to their bias, so a monotone horizon model was not imposed.
- **Index uncertainty is not added again:** these are out-of-sample errors, so they already
  include it.

## 6. What the lookup does with this

| Situation | Result |
|---|---|
| Supported series covers both years; band calibrated | Estimate, 90% interval, "Index-adjusted estimate as of [period]" |
| Band failed calibration | `UNCALIBRATED_INTERVAL`, no amount |
| Anchor not eligible (non-market, VAT unresolved, bulk) | `NO_ELIGIBLE_ANCHOR` / `UNRESOLVED_VAT`, the user's choice is not swapped |
| Period suppressed or outside the series | `UNSUPPORTED_PERIOD` |
| No published series | `SPARSE_GEOGRAPHY` |
