# Linkage report: candidates and Splink model

**Status:** candidate generation, model training and scoring are complete for both tracks.
Threshold selection and the link-threshold gate evaluation wait for the human labels (in
progress).
**Reproduce:** `python jobs/train_linkage.py --target local --track address_only|production`

## 1. Tracks

| Track | Eircode | Purpose |
|---|---|---|
| `address_only` | **Absent from the input**: no column, no block, masked from address text | Evaluated against Eircode weak labels and human labels without circularity |
| `production` | Exact-match comparison and an any-county Eircode recovery block | Shows the added value of Eircodes (mainly from 2021) |

Price and sale date are never inputs. Identity is not allowed to depend on whether the
implied price growth looks plausible (tested).

## 2. Candidate generation

806,861 linkage-eligible occurrences give 325.5 billion possible pairs. The union of rules
produces **1.54M candidates**, a 211,684× reduction.

| Rule (in order) | Condition | New pairs |
|---|---|---:|
| `exact_address` | county + identical normalised address | 157,668 |
| `number_street` | county + street + primary number | 111,921 |
| `locality_number_street_key` | county + BUA + number + first street word | 138,733 |
| `rural_name` | county + distinctive name, both unnumbered | 242,812 |
| `rural_generic_name_with_place` | county + generic name ("The Avenue") + next component, both unnumbered | 6,601 |
| `townland_both_unnumbered` | county + townland, both unnumbered | 554,877 |
| `recovery_street_key_number` | county + first street word + number, a town missing on either side | 325,111 |
| `recovery_eircode_any_county` (production only) | same valid Eircode in any county | about 1,800 |

Every rule a pair satisfies is stored (`rules_hit`), not just the first.

**Oversized blocks were split, not dropped.** The first design produced 21.7M pairs. "Same
town + same number" put all 801 No. 1s in Cork city into one block (641k pairs); urban
townlands put 4,303 Dublin sales in one block; unnumbered "The Avenue" grouped 773 Dublin
sales. Fixes: add the first street word; require both sides unnumbered for townland
blocks; split generic street names by the next address component.

## 3. Model

Comparisons (match weights in bits from the address-only model):

| Comparison | Agree | Disagree | Notes |
|---|---:|---:|---|
| County | +2.9 | −7.4 | m fixed by assumption (0.995) |
| Primary number | +5.9 (term-frequency adjusted) | −5.0 | m fixed by assumption (0.97) |
| Unit | same +9.5 · one missing −1.7 | different −2.2 | conflict m fixed (0.005) |
| Street (Jaro-Winkler) | exact +11.6 · ≥0.95 +9.1 · ≥0.88 +9.5 | −2.8 | term-frequency adjusted |
| Locality (BUA) | +4.2 | −3.3 | |
| Townland | +7.5 | −4.1 | |
| Postal district | +4.0 | −2.6 | |
| Eircode (production only) | trained | trained | |

- **u-probabilities** come from 20M random pairs. **Prior:** the identical-address rule at an
  assumed 60% recall (≈4.0 × 10⁻⁷).
- **m-probabilities** come from EM on two blocks where true matches are common: same street
  and number, and same town, number and first street word.

**What went wrong and how it was fixed.** The first two training designs used blocks
dominated by neighbours (same street, different houses) and by different rural homes in
one townland. EM then learned "same street" instead of "same property": house-number m fell
to 0.012, which is chance. County and number are constant within every useful training
block, so EM cannot estimate them at all. Their m-probabilities are fixed by stated
assumption, while their u-probabilities are estimated. All assumed values are recorded in
`linkage.ASSUMED_M` and logged to MLflow, and the validation stage will vary them.

## 4. Independent check: Eircode weak labels (address-only track)

The address-only model never sees Eircodes, so pairs sharing a consistent valid Eircode are
an outside test. They cover recent sales only.

| Weak label | Pairs | In candidates | p ≥ 0.5 | p ≥ 0.9 | Baseline |
|---|---:|---:|---:|---:|---:|
| Eircode match | 12,898 | **96.1%** | 86.3% | 69.7% | 94.9% |
| Eircode conflict | 1,301 | 0.2% | 0.2% | 0.2% | 0.2% |

**Misses found and fixed upstream:** unit words such as "appartment", "site", "house",
"duplex" and "townhouse" were not recognised (the address stage fix). The placeholder Eircode `A123456`
(190 sales in 26 counties) was creating false weak matches (the cleaning stage fix).

## 5. Outputs

- `silver.link_candidates`: every scored pair, with match weight and probability, gamma
  levels, rules hit, and the deterministic baseline decision.
- `silver.link_models`: model version, input versions, blocking report, training log and
  the full Splink settings (also stored content-addressed).
- MLflow experiment `/Users/<user>/ireland-rsi-linkage` on Databricks: parameters,
  assumptions, blocking metrics and the model JSON.

## 6. Pending for the link-threshold gate (needs labels)

- Weighted accepted-link precision and candidate-conditional recall on the dev split; PR
  curves by stratum, era, county and Eircode availability.
- Blocking recall from human-labelled matches, plus the missed-match audit.
- Reject / review / accept thresholds chosen on dev data by false-link cost, then a single
  evaluation on the held-out test sets.
- Calibration of match probabilities against labels; probabilities are not called
  calibrated before this.
