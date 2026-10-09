# Methodology

How the Ireland repeat-sales index is built, what each step assumes, and where it can
fail. Figures are for the Donegal pilot, release `rel-229d61affb29` (published). Each
section points to the task report that holds the evidence.

## 1. Source

The PSRA Residential Property Price Register lists every residential sale filed for stamp
duty since 2010. Each sale has a date, an address as typed by the filer, a price,
VAT and market flags, a description and an optional Eircode. There is no property
identifier, no floor area and no buyer type.

- **Acquisition:** the national file is downloaded and its untouched bytes are stored in a
  content-addressed object store. Rows land in bronze with occurrence-aware IDs.
  Snapshots are reconciled and promoted only if they pass checks (`source_audit.md`, D-014).
- **Cleaning:**
  - each row gets exactly one disposition (eligible, non-market, bulk filing, VAT
    unresolved, extreme price, …);
  - new-dwelling prices are converted to VAT-inclusive with effective-dated rates;
  - the 2025 9% and 13.5% VAT regime is left unresolved without evidence
    (`quality_report.md`, D-016).
- **Donegal:** 21,452 rows, of which 19,520 are index-eligible (91%).

## 2. From addresses to properties

- **Address normalisation:** unit, number, street, townland and town are extracted. Towns
  come from CSO built-up areas and townlands from Tailte Éireann, and locality confidence
  is calibrated against Eircode routing keys (`address_report.md`, D-017).
- **Candidate pairs:** blocking rules on address components, with recall checked against
  Eircode weak matches: 96% nationally (`linkage_report.md`, D-018).
- **Probabilistic linkage (Splink):**
  - m-probabilities for county and number are fixed by assumption;
  - EM runs only on match-rich blocks, because unrestricted EM learned "same street"
    instead of "same home";
  - prices are never used, so linkage cannot be tuned to the index.
- **Constrained clustering:** links are added strongest first, and a merge is refused if it
  would join different units or numbers, different Eircodes, two same-day prices, or more
  than 12 sales. Townland-only and estate-only addresses link only on a shared Eircode.
  2,984 accepted links were refused (`pairs_report.md`, D-020).
- **Threshold:** accept p ≥ 0.5, **frozen at the link-threshold gate** after 260 human labels. Final-link
  precision is 98.0% (95% CI 95.6-99.7%) and recall 75%, weighted by sampling
  probability. Precision is flat across thresholds 0.3-0.9, so a higher threshold would
  only lose true links. The 99% aspiration is not met; the remaining errors are generic
  apartment addresses in different towns (`validation_report.md` §2, D-029).

## 3. Repeat pairs

- Only consecutive comparable sales within a property history form pairs. A non-market
  or unresolved sale breaks the history.
- **Headline population:** second-hand to second-hand (D-007).
- Short holds and unusual changes are flagged as proxies, never as renovations.
- **Donegal:** 1,220 headline pairs from 1,109 properties, with zero audit violations (pair audit).

## 4. Index

**Estimator.** Case-Shiller weighted repeat sales:
1. y = log(p₂/p₁) is regressed on period dummies (−1 at the first sale's period, +1 at the
   second's).
2. The squared residuals are modelled as a + b·years, with non-negative least squares.
3. WLS is fitted with weights 1/σ², floored at the 5th percentile and capped at 10× the
   median.

**Rebasing and identifiability.** The index is rebased so the 2015 mean is 100. Only the
connected period set is estimated, and a rank-deficient design fails rather than produce
numbers (`index_report.md`, D-021). OLS, Huber and trimmed fits are sensitivity checks.

**Uncertainty.** A property-history bootstrap (1,000 draws) resamples whole histories,
because a property's pairs are dependent. Replicates that lose a period are counted as
failures. Joint draws give intervals for any ratio of two periods (`uncertainty_report.md`,
D-022).

**Publication gates** (calibrated by downsampling Donegal):
- a series needs at least 300 properties, an effective sample size of at least 50,
  bootstrap success of at least 95%, no property carrying more than 5% of the weight, and
  an identified 2015 base;
- a period needs at least 20 endpoint pairs and a 95% log width of at most 0.30;
- the newest period is provisional.

Donegal publishes annual series only; quarterly fails the bootstrap gate. Letterkenny's
annual series is published for 9 years.

## 5. Validation (`validation_report.md`)

- **CSO:** year-on-year growth over complete aligned years, against Border houses: bias
  +0.3 pp a year and mean absolute difference 4.2 pp over the published years. The CSO is
  a benchmark, not ground truth: it covers household purchases, is hedonic and is regional.
- **Sensitivity:** 2015-2025 growth moves by at most 0.04 log points across link thresholds
  from 0.5 to 0.97, and by at most 0.015 across estimators. Loose links (0.3) dampen growth
  by 0.05. Eircode-confirmed pairs grew 0.12 faster in 2021-2025: this is the main open
  question for the link-threshold gate.
- **Selection:** rural homes are 56% of sales but 38% of repeat legs. The index describes
  resold, identifiable homes.

## 6. Property estimate (`backtest_report.md`)

The estimate is the anchor price × index[target] / index[anchor], using the Donegal series.
The town index did not beat Donegal for Letterkenny homes (D-025).

The backtest is temporal and cross-fitted: no future pairs and no pairs from the same
property. On the 2023-2025 test window:
- the median absolute error is 25.6%, against 37.8% for an unadjusted price and 26.7% for
  the CSO national index;
- bias is −0.007 log.

The 90% prediction interval comes from calibration-window errors by holding band. Test
coverage is 85.6-88.0%, and the interval runs from about 0.5× to 2× the estimate. The
lookup returns a reason code instead of a number when support or calibration is missing.

## 7. Publication (`runbook.md`)

- **Releases:** immutable and keyed by a hash of every input version, the code and the
  config. The current-release pointer is an append-only event table.
- **Failures and rollback:** a failed release cannot become visible, and a rollback is a
  new event.
- **Revisions:** tagged as source, method or both.
- **Refresh:** monthly, recomputing every derived stage (about 14 minutes of serverless
  time for Donegal).

## 8. Known limitations

1. **Link precision is 98%, not 99%.** About 2% of links join different homes, mostly
   generic apartment addresses in different towns. A fix is proposed but needs a fresh
   labelled sample to evaluate honestly.
2. **The Eircode-subset growth gap** (+0.12 in 2021-2025) is not explained. At a 2%
   false-link rate, linkage error is an unlikely cause, which points to segment
   differences. One reviewer labelled the sample, so there is no inter-rater agreement
   figure.
3. **Residual dispersion is high** (SD 0.47 in log returns). It reflects quality change,
   condition, false links and multi-unit filings that the register cannot distinguish.
4. **Retrospective backtest only:** the project has no historical snapshots, so filing lags
   cannot be replayed.
5. **Rural under-representation** and no stock reweighting.
6. **No houses/apartments split:** the PPR has no dwelling type, so the CSO comparison is
   approximate.
7. **No incremental linkage:** each refresh is a full rebuild. That is fine for one county
   and must be revisited before national scale.
