# Decision log

Each decision records what was chosen, the evidence, and the task where it takes effect.
States: **Accepted** (in force), **Proposed** (default awaiting confirmation),
**Open** (not yet decided). A decision is never edited after acceptance. To change one,
add a new entry that supersedes it.

| ID | Decision | State | Task |
|---|---|---|---|
| D-001 | National snapshot source | Accepted | 1 |
| D-002 | Source encoding cp1252 | Accepted | 1 |
| D-003 | Source contract v1 | Accepted | 1 |
| D-004 | Eircode evaluation strategy | Accepted | 1, 5 |
| D-005 | Pilot scope | Accepted | 1 |
| D-006 | Index frequency | Accepted | 1, 9 |
| D-007 | Headline population | Accepted | 3 |
| D-008 | Reuse licence and attribution | Accepted with caveat | 1 |
| D-009 | Databricks Free Edition workspace | Accepted, provisioned | 1 |
| D-010 | CSO benchmark series | Accepted | 1, 10 |
| D-011 | Locality reference | Accepted | 1, 4 |
| D-012 | Source audit module added to layout | Accepted | 1 |
| D-013 | Splink DuckDB backend inside Databricks jobs | Accepted | 1, 6 |
| D-014 | Bronze identity, reconciliation and promotion design | Accepted | 2 |
| D-015 | Package dependencies as lower bounds; exact pins in lock file | Accepted | 2 |
| D-016 | Parsing, VAT and comparability rules (rule set qr-2026-10-06) | Accepted | 3 |
| D-017 | Address normalisation and locality confidence levels | Accepted | 4 |
| D-018 | Candidate blocking and Splink model design | Accepted (thresholds pending labels) | 6 |
| D-019 | Donegal pilot as a separate profile, catalog, jobs and app | Accepted | 1-6 |
| D-020 | Constrained property histories and non-specific address rule | Accepted (threshold provisional) | 7 |
| D-021 | Repeat-sales estimator, weighting and identifiability rules | Accepted | 8 |
| D-022 | Bootstrap, calibrated publication gates and fallback | Accepted | 9 |

---

### D-001: National snapshot source (Accepted, 2026-10-06)

Use the PSRA "Download All" file `PPR-ALL.zip` as the authoritative national snapshot. The
"Download Specific" form only returns one year at a time; it is used only for
cross-checks.
**Evidence:** `docs/source_audit.md` §1. The file covers 26 counties, 2010-01-01 to
2026-09-25, and 809,014 rows.

### D-002: Source encoding (Accepted, 2026-10-06)

Decode the PSRA CSV strictly as cp1252. Never decode with replacement characters.
**Evidence:** byte `0x80` is the euro sign, and strict UTF-8 decoding fails at the header.

### D-003: Source contract v1 (Accepted, 2026-10-06)

Contract `psra_ppr_csv` v1 lives in `config/source_contract.yml`. Seven fields are required;
Eircode and Property Size Description are optional. Any header change requires a new
contract version. **Evidence:** observed header; `tests/test_contracts.py`.

### D-004: Eircode evaluation strategy (Accepted, 2026-10-06)

The primary linkage evaluation is a stratified, hand-labelled pair sample.
Eircode agreement is used only as a secondary weak-label check, restricted to pairs where
both sales carry a valid code, and reported as covering a recent, short-hold subset.
Eircodes are masked in the address-only evaluation track.
**Evidence:** Eircode population is ≤0.8% per year before 2021 and about 75% from 2022.
12,105 codes repeat. Repeated codes show 73 cross-county and 490 differing-number
conflicts. See `docs/source_audit.md` §4 and §6.

### D-005: Pilot scope (Accepted, 2026-10-06)

Donegal county pilot, with the Letterkenny lookup as the demonstration and Dublin as the
dense-urban audit sample. Both are already in the national file.
**Evidence so far:** Donegal has 21,452 rows (1,445 in 2025) and about 5,250 addresses
containing "Letterkenny". Its Eircode coverage from 2022 (76.3%) is typical.
**Confirmed by the user on 2026-10-06.**

### D-006: Index frequency (Accepted, 2026-10-06)

Quarterly, with annual or broader-geography fallback where support fails.
**Confirmed by the user on 2026-10-06.** Support evidence follows in the uncertainty stage.

### D-007: Headline population (Accepted, 2026-10-06)

Second-hand-to-second-hand pairs for the headline series. New-build first legs only as a
VAT-adjusted sensitivity series.
**Supporting evidence:** 2,382 new dwellings are not flagged VAT-exclusive, and the source
cannot identify 9%-qualifying apartments after October 2025.
**Confirmed by the user on 2026-10-06.**

### D-008: Reuse licence and attribution (Accepted with caveat, 2026-10-06)

CSO and Tailte Éireann data are CC BY 4.0 and must be attributed. The PSRA national file
states no licence. PSRA licenses its Dublin subset of the same register under CC BY 4.0, so
we proceed on an attribution basis for a non-commercial portfolio, keep address-level lookup
authenticated, and ask PSRA (info@psr.ie) to confirm. Attribution texts are in
`docs/source_audit.md` §8.
**Open item:** PSRA confirmation. Record the reply here when it arrives.

### D-009: Databricks workspace (Accepted direction, pending provisioning, 2026-10-06)

The project runs on **Databricks Free Edition**. Reasons: no cost, no expiry, and it
includes everything the design uses (Unity Catalog, serverless notebooks and jobs, a SQL
warehouse, MLflow, Databricks Apps, AI/BI dashboards). It also suits the portfolio goal of
demonstrating Databricks. The compute budget is €0 with the Free Edition
fair-use quota.
Free Edition limits that shape the design ([docs](https://docs.databricks.com/aws/en/getting-started/free-edition-limitations), updated 2026-09-29):

| Limit | Design response |
|---|---|
| Serverless only, no custom clusters or JARs | Pin the serverless environment; avoid JVM-dependent libraries (see D-013) |
| Outbound internet limited to trusted domains | Acquisition may need to run locally and upload untouched bytes to a UC Volume. Test PSRA/CSO reachability first. |
| One SQL warehouse, 2X-Small | Precompute gold tables; app and dashboard read small serving tables |
| 3 Apps, auto-stop after 24 h | Restart the app before demos; document this in the runbook |
| 5 concurrent job tasks | Keep the refresh graph mostly sequential |
| Quota exceeded → compute off for the day | Develop locally with DuckDB; run full builds on Databricks deliberately |
| No commercial use | Portfolio / non-commercial only |

Local Python 3.12 + DuckDB remains the development and test environment. The same
`ireland_rsi` package is deployed to Databricks through an Asset Bundle (`databricks.yml`).

**Provisioned 2026-10-06:**

| Item | Value |
|---|---|
| Workspace | Free Edition on AWS; the host lives only in the local CLI profile `ireland-rsi` |
| CLI profile | `ireland-rsi` (OAuth, user login) |
| Catalog | `ireland_rsi`, with schemas `bronze`, `silver`, `gold`; volume `bronze.raw`. Created by `sql/setup.sql`, because the CLI cannot create Default Storage catalogs. |
| SQL warehouse | Serverless Starter Warehouse, 2X-Small (`003cc5b18883bd14`) |
| Serverless environment | `environment_version` 4: Python 3.12.3, Spark 4.2.0 |

**Smoke test** (`jobs/smoke_test.py`, run 717900410386166, all passed): HTTPS to
propertypriceregister.ie, ws.cso.ie and pypi.org; `splink==5.0.0` + `duckdb==1.5.6`
installed from PyPI and linked a toy dataset; Delta write/read in `ireland_rsi`; and
byte-exact write/read in `/Volumes/ireland_rsi/bronze/raw`. **Consequence:** acquisition can
run inside Databricks, so the "upload from local" fallback is not needed.

### D-013: Splink backend on Databricks (Accepted, 2026-10-06)

Smoke test passed on serverless (see D-009): Splink 5.0.0 with DuckDB 1.5.6 runs inside a
job task. Note the Splink 5 API: register data first with
`Linker(DuckDBAPI().register(df), settings)`, importing `DuckDBAPI` from
`splink.backends.duckdb`. Original proposal follows.

Run Splink with its **DuckDB backend inside a serverless job task**, reading from and
writing to Delta tables, rather than the Spark backend. Splink's Spark backend registers
JVM UDF JARs for fuzzy string comparisons (e.g. Jaro-Winkler), which serverless compute
does not support, and 809k records (21k for the
Donegal pilot) fit comfortably on one node. Spark/Delta still does ingestion, cleaning,
pairs, publication and serving. The design already treats the Spark backend as an
implementation choice, not a requirement.
**Verify:** smoke test on Free Edition serverless. Install `splink==5.0.0` from PyPI, run
a toy DuckDB linkage, and round-trip a Delta table. If PyPI is not reachable, revisit.

### D-010: CSO benchmark series (Accepted, 2026-10-06)

Benchmark against CSO **HPQ01 Existing** (quarterly; matches the second-hand headline) and
**HPM09** monthly series, aggregated to complete quarters by the mean of monthly levels:
National all, and Border houses as an indicative Donegal comparison. Both are base
2015=100. Comparisons use growth rates, not levels. Reject HPM06/HPA06 (Jan 2005 base, old
region boundaries).
**Evidence:** `docs/source_audit.md` §7; `config/cso_benchmark.yml`;
`data/raw/cso/2026-10-06/manifest.json`. Border = Cavan, Donegal, Leitrim, Monaghan and
Sligo, houses only. CSO covers household purchases only and is hedonic, BER-matched and
smoothed. There is no county or town RPPI.

### D-011: Locality reference (Accepted, 2026-10-06)

"Town" = a **CSO Census 2022 Built Up Area** name (867 urban areas, including Letterkenny)
matched in the address within the correct county. **Tailte Éireann Townlands 2024**
(CC BY 4.0; English and Irish names; stable GUIDs) supplies rural disambiguation and
aliases (Letterkenny = Leitir Ceanainn). Unmatched addresses fall back to county. This is
an address-derived locality, because PPR has no coordinates. Logainm aliases are an
optional later enrichment; Eircode routing keys are diagnostic only.
**Evidence:** `docs/source_audit.md` §9.
**Confirmed by the user on 2026-10-06.**

### D-012: Source audit module (Accepted, 2026-10-06)

Added `src/ireland_rsi/source_audit.py`, which was not in the original layout, so
the source audit stage profiling is reproducible code rather than one-off commands.

### D-014: Bronze identity, reconciliation and promotion design (Accepted, 2026-10-06)

- **Storage:** untouched bytes are content-addressed (`objects/sha256/<aa>/<sha>.<ext>`) in
  `data/store` locally or `/Volumes/ireland_rsi/bronze/raw` on Databricks. Tables are
  insert-only on declared keys (`contracts.TABLE_KEYS`) through one `TableStore` interface:
  DuckDB locally, Delta + MERGE on Databricks. `storage.py` is an addition to the original
  layout.
- **Snapshot identity:** `snapshot_id = <scope>:<member sha256[:16]>`. A re-download of
  identical bytes is a new acquisition but the same snapshot, so it changes nothing.
- **Row identity:** `raw_row_id = SHA256(member_sha256 | logical record ordinal)`, plus
  `raw_content_fp` (exact raw fields) and versioned `txn_fp` (`txfp_v1`: whitespace-collapsed,
  casefolded). Sale IDs come from reconciliation, never from content alone.
- **Reconciliation:** exact one-to-one content matches first; then a unique one-to-one
  correction match on three keys in order; then new sales, with ambiguous ones queued for
  review. Corrections add versions to `silver.sale_versions` and never overwrite.
- **Scope authority:** only `ppr:national` can become active. Other scopes (form extracts)
  are `reference` snapshots mapped onto it by exact content and never create sales.
- **Promotion guard:** hold a snapshot when the record count falls by more than 0.5%, or
  when under 98% of previous occurrences carry over. Approval is an explicit, recorded
  `--force-promote`. Held snapshots write nothing to silver.
- **Append-only history:** promotion, hold, rejection and rollback are events in
  `bronze.snapshot_events`. The active snapshot is the latest `promoted` event per scope.
- **Evidence:** 21 tests in `tests/test_ingest.py`. The real 809,014-row national file
  landed locally, a repeat run inserted 0 rows, and the rebuild check was identical.

**Rebuild check: passed on 2026-10-06.**

| Check | Local (DuckDB) | Databricks (Delta, serverless) |
|---|---|---|
| Acquisition | Saved download imported | Downloaded inside Databricks; **byte-identical** to the local download (archive `15e2811d…`, member `7e1631bc…`) |
| Rows landed | 809,014 | 809,014 (run 142796109181041, 292 s) |
| Snapshot promoted | `ppr:national:7e1631bc229a5d92` | same |
| Re-run | `already_active`, 0 rows inserted | `already_active`, all counts unchanged (run 372621917735203, 103 s, SUCCESS) |
| Rebuild from saved bytes | identical | identical |
| Duplicate occurrences kept | 1,078 | 1,078 |

Two serverless quirks fixed on the way: scripts run via `exec()` without `__file__` (the job
passes `--project-root`), and any `SystemExit` (even 0) fails a task, so the entry point
exits only on failure. Run 142796109181041 did all its work but reported FAILED because of
the second quirk.

### D-015: Dependency pinning (Accepted, 2026-10-06)

`pyproject.toml` declares lower bounds, so installing the wheel on Databricks serverless
does not replace the platform's numpy/pandas/pyarrow (which its Spark client depends on).
Exact local versions live in `requirements-lock.txt`. Databricks job environments pin
`duckdb==1.5.6` and `splink==5.0.0` (the smoke-tested versions) in `databricks.yml`.

### D-016: Parsing, VAT and comparability rules (Accepted, 2026-10-06)

- **Dispositions:** quarantine > excluded > pending review > eligible, with all rule hits
  logged in `silver.quality_events`. `parse_valid`, `linkage_eligible` and
  `index_eligible` are stored separately. Non-market sales are linkage-eligible but not
  index-eligible; multi-unit addresses are neither.
- **VAT:** a gross-up is applied only to VAT-exclusive new dwellings under an
  effective-dated rule (`config/vat_rules.yml`): 13.5% from 2010-01-01 to 2025-10-07. From
  2025-10-08 the rate is unresolved, because 9% applies only to qualifying apartments in
  qualifying blocks and the register cannot identify them. Rounding is half-up to the
  cent. My initial assumption of a 2020-21 cut to 12.5% was checked and rejected: that cut
  applied to the standard rate only.
- **Review thresholds** (profiled, configurable): below €10,000 or above €5,000,000; bulk
  filing when ≥3 rows share date, county, price and development text, or ≥10 share date,
  county and price. Rows flagged for review are excluded from the index until a review
  policy exists. Their effect is tested as a sensitivity in the validation stage.
- **Grain:** `silver.sales` has one row per (snapshot, occurrence, rule-set version), so
  each snapshot's cleaned view is immutable and rule changes create new rows, never edits.
- **Evidence:** `docs/quality_report.md` (accounting check), 48 tests in `tests/test_clean.py`.

### D-017: Address normalisation and locality confidence (Accepted, 2026-10-06)

- **Reference:** the Tailte Éireann townland gazetteer (with county, Irish and English
  names) replaces the spatial-boundary approach in D-011. No spatial join is needed for
  townlands; each BUA's counties come from townland points within its area-equivalent
  radius.
- **Locality rules:** most specific place first (post towns don't capture rural homes);
  unnumbered addresses at a townland outside every BUA are rural; ambiguous names are not
  forced.
- **Confidence is calibrated against Eircode routing keys** (`docs/address_report.md` §4):
  official name and Dublin postal district are high (99.3%, 97.5%); trailing name is medium
  (90.8%); bare county name and townland-derived aliases are low.
- **Removed on evidence:** townland-within-circular-footprint assignment (52.6% agreement).
- **Town indices use high and medium only.**

### D-018: Candidate blocking and Splink model design (Accepted, thresholds pending, 2026-10-07)

- **Blocking:** a union of seven address rules (plus an Eircode recovery rule in
  production) gives 1.54M candidates from 806,861 records. Oversized blocks were split
  with extra identity evidence (first street word; both unnumbered; next component for
  generic street names) instead of being dropped (`docs/linkage_report.md` §2).
- **Tracks:** address-only (no Eircode in the input) for unbiased evaluation; production
  adds Eircode.
- **Training:** u from random sampling; prior from identical addresses at an assumed 60%
  recall; m by EM only on blocks where true matches are common. County and house-number m
  are fixed by assumption (0.995, 0.97) because EM cannot identify them from blocks that
  hold them constant; a unit conflict has m = 0.005. All assumptions are logged to MLflow
  and varied in the validation stage.
- **Rejected on evidence:** EM blocks dominated by neighbours or by different rural homes
  (house-number m collapsed to chance, 0.012).
- **Independent check:** address-only blocking captures 96.1% of Eircode weak matches and
  0.2% of conflicts.
- **Open until labels exist:** accept/review/reject thresholds, calibration, and the
  the link-threshold gate precision and recall evaluation.
- **Operations:** the labelling sample moved to its own on-demand job, so the monthly refresh
  does not create new sample versions.

### D-019: Donegal pilot as a separate pipeline profile (Accepted, 2026-10-07)

The user asked for a Donegal-only pipeline, end to end, to make labelling tractable and to
present a focused market. The national work is preserved (git tag
`national-pipeline-2026-10-07`; catalog `ireland_rsi`, its jobs, app and labels untouched).

- **One code base, two profiles** (`config/profiles/national.yml`, `donegal.yml`): scope,
  county filter, promotion policy, storage, labelling design and MLflow experiment. Every job
  script takes `--profile`.
- **Donegal lands the same untouched national download** (the bytes and their evidence
  record are shared), but only Donegal records enter bronze. Row IDs keep their ordinal in
  the full file, so every row traces to the original download. Structural validation still
  runs on the whole file.
- **Separate Unity Catalog catalog `ireland_rsi_donegal`** with its own volumes, plus a
  second bundle target (`-t donegal`) deploying its own jobs and labelling app
  (`ireland-rsi-labelling-donegal`). SQL setup files take `--var catalog=...`.
- **Placeholder Eircodes found nationally are listed explicitly**, because the
  "3+ counties" rule cannot fire inside a single county.
- **Smaller labelling sample:** 260 pairs (`config/labels_donegal.yml`).
- **Fixes found on the way:** acquisition evidence records hold download facts only (the
  scope had made one download conflict with itself across profiles).

### D-020: Constrained property histories (Accepted, threshold provisional, 2026-10-07)

- Accepted links are clustered strongest first, refusing any link that would merge
  conflicting units/numbers, Eircodes or same-day prices, or exceed 12 sales.
- **Non-specific address rule:** unnumbered addresses whose first component is only a
  townland/town name or a road/estate/block name link only on a shared valid Eircode.
  **Evidence:** the Donegal over-merge analysis (`docs/pairs_report.md` §2) and early human
  labels (0 of 10 rural unnumbered pairs judged the same property). **Cost:** pre-2021
  rural townland-only sales leave the repeat-sales sample; this must be reported as a
  selection effect.
- Pairs are consecutive within segments and never bridge non-comparable sales. The pair audit
  audit runs every time and stops the pipeline on any violation.
- The entity version hashes the config, `entities.py` and `pairs.py`. An earlier version
  omitted `pairs.py`, so one run reused a stale version ID; the local tables were rebuilt.
- The accept threshold (p ≥ 0.5) is provisional until the link-threshold gate.

### D-021: Repeat-sales estimator (Accepted, 2026-10-07)

- Headline estimator: Case-Shiller three-stage WRS with linear variance in holding years
  (non-negative coefficients), a variance floor at the 5th percentile and a weight cap of
  10× the median. OLS, Huber and 1%-trimmed WRS are sensitivity fits. Match scores are
  never used as weights.
- Identifiability: estimate the connected period set with the most pairs, with its
  earliest period as the reference. Report other components as unidentified and never
  splice them. A rank-deficient design fails the fit.
- Presentation base: mean of 2015 levels = 100, falling back to the reference period when
  2015 is not fully identified (recorded as `base_status`).
- Verified on synthetic truth (`tests/test_index.py`). Donegal fits are research outputs
  until the link-threshold gate and the publication gates are passed.

### D-022: Bootstrap, publication gates and fallback (Accepted, 2026-10-07)

- Property-history bootstrap (1,000 draws, fixed seed) refitting the full WRS each time.
  Joint draws are stored for ratio intervals.
- Gates per `config/publication.yml`. **The minimum property count was raised from 100 to
  300** on Donegal downsampling evidence; the 0.30 log-width limit was kept (≈ 0.06-0.08
  RMS error). See `docs/uncertainty_report.md` §3.
- **The Donegal pilot publishes annual series.** Quarterly fails the bootstrap-success
  gate even at full sample.
- Fallback chain with the same series for anchor and target. Unsupported paths return
  `SPARSE_GEOGRAPHY` or `UNSUPPORTED_PERIOD`, never a number.
- Replicates reuse full-design rows and solve in NumPy (identical results, ~20× faster),
  after the first serverless run took 17.5 minutes.

### D-023: CSO comparison and sensitivity design (Accepted, 2026-10-07)

- The CSO RPPI is compared by **year-on-year growth** over complete, aligned periods only:
  monthly CSO levels are averaged per complete quarter or year, excluding the three
  provisional months, and our newest (provisional) period is excluded. Levels are shown
  for orientation only.
- Benchmarks are pinned by checksum (`config/validation.yml`) and read from the object
  store, so a CSO revision cannot silently change a validation result.
- Sensitivity experiments are predeclared in `config/validation.yml`, rebuild histories,
  pairs and fits from the same linkage scores, and compare only periods with at least 20
  endpoint pairs. A variant that cannot support 2015-2025 is compared on its widest common
  window (e.g. Eircodes are only filed from 2021).
- Shifts above 0.05 log points are flagged for investigation, never used to choose a result.

### D-024: Lookup backtest design (Accepted, 2026-10-07)

- Retrospective temporal backtest with five property folds: target year Y is predicted
  from pairs whose later sale is in or before Y, excluding every pair of the evaluated
  properties. There are no historical source snapshots, so this is not a real-time
  (operational) replay.
- Windows by target year: development 2013-2018, calibration 2019-2022, test 2023-2025.
- Baselines: unchanged price, CSO national RPPI, Donegal index. The town series is tested
  as a diagnostic with a relaxed 100-property floor.

### D-025: Property estimates use the Donegal series (Accepted, 2026-10-07)

- The Letterkenny index did not beat the Donegal index for Letterkenny homes (median
  absolute error 28.8% vs 24.2% on 135 test pairs). Property estimates therefore use the
  scope chain (`publication.yml` → `lookup.estimate_chain`); the town index is still shown.
- 90% prediction intervals: empirical quantiles of calibration-window log errors in three
  holding bands; test coverage 85.6-88.0% (all within the 5-point tolerance; Wilson upper
  bounds include 90%). Intervals are wide (relative width 1.3-1.8×) because PPR resales
  differ in condition, which no index can see.

### D-026: Releases, pointer and refresh (Accepted, 2026-10-07)

- A release is immutable and keyed by a hash of every input version, the code hash and the
  config hash. Serving tables are written first; the pointer (`gold.release_events`) moves
  in a single insert afterwards. Rollback is a new pointer event. Failed gates record a
  `failed` manifest and leave the pointer unchanged.
- Hard gates: all stages present, the pair audit pairs, ≥ 1 supported series (publication gates), validation recorded
  (validation check), ≥ 1 calibrated band (backtest gate). Soft gate: link threshold frozen (link-threshold gate). A release that
  passes only the hard gates is published in state `research`.
- Each refresh recomputes derived stages from the active snapshot (no incremental scoring):
  at Donegal scale a full refresh takes minutes and cannot miss neighbours of a corrected
  sale. Incremental scoring is a national scale-up item.
- Monthly schedule (10th, 06:00 Europe/Dublin), **paused** until the link-threshold gate. Failure alerts go by
  email to the workspace user.

### D-027: Dashboard publication rule (Accepted, 2026-10-07)

- The dashboard reads only governed views (`dashboards/queries.sql`) that follow the
  current-release pointer, so app and dashboard always show the same release.
- Small-cell rule: aggregate counts below 10 are shown as blank with `cell_state =
  suppressed`. No address-level table is exposed through the dashboard.

### D-028: Lookup app access (Accepted, 2026-10-07)

- Authenticated workspace users only (Databricks Apps sign-in). The app's service
  principal has `SELECT` on the gold serving tables only, with no access to bronze or
  silver (`sql/lookup_grants.sql`).
- The app shows public register facts (address as filed, date, price) and never any
  person. Address-level anomaly flags are not shown.

### D-029: Link threshold frozen at 0.5 (Accepted, 2026-10-07)

- Evidence: 260 human labels. On final property links (after constraints), weighted by
  sampling probability: precision 98.0% (bootstrap 95% CI 95.6-99.7%), recall 75%
  (56-90%). Precision is flat from 0.3 to 0.9 and recall falls above 0.7, so 0.5 is kept.
  It beats the rule-based baseline (95.5% precision at similar recall).
- The 99% precision target is **not** met; the shortfall and its cause (generic
  apartment addresses in different towns) are disclosed in `docs/validation_report.md` §2.
  A raw-town-word constraint is proposed, to be evaluated on a fresh sample; it is not
  tuned on these labels.
- Releases built on the frozen threshold are published as `published`. The Donegal monthly
  schedule is unpaused; the national target keeps its schedule paused.
- Validation versions now include a digest of the label table, so new labels always
  produce a new validation version.
- Parity fix found while closing the link-threshold gate: the label digest was serialised with the operating
  system's line ending (`\r\n` on Windows, `\n` on Linux) and with zone-dependent timestamp
  text, so local and Databricks validation versions differed for identical labels. Both are
  now normalised (tested), and the release `rel-229d61affb29` is identical in both places.

### D-030: Scope is a complete Donegal release; users and access (Accepted, 2026-10-07)

**Decision.** The project is delivered as a complete, gated **Donegal** release (county
and Letterkenny), not as a national index. This deliberately narrows the original output 3
("national, county and eligible town indices"). The national pipeline (the ingestion to linkage stages, 809k
rows) is preserved and paused; nothing is published for it.

**Why.**
- Donegal is the hardest linkage case: a town plus a large rural hinterland of
  townland-only addresses. It is also small enough for every gate to be met with real
  evidence, including 260 hand labels.
- A national release would need tens of thousands of labels across counties and
  incremental linkage. Publishing national figures without that evidence would break the
  project's own rule: "Do not publish a local estimate merely because the software can
  calculate one."

**Who it is for.** Individuals with a Donegal property question:

| User | Question | What the release gives |
|---|---|---|
| Homeowner | How has my home's value moved since I bought it? | Index-adjusted purchase price with a 90% range |
| Buyer | Is this asking price in line with the market since its last sale? | Last sale brought forward, to compare with the asking price |
| Seller | What would the market change alone suggest? | The same, as a starting point for a conversation with an agent |
| Local reader (journalist, councillor, student) | How have Donegal and Letterkenny prices moved? | Dashboard: index, uncertainty, CSO comparison |

The estimate is never a valuation. The app says so on every result, and the 90% range
(about half to double the estimate) makes it visible.

**Access.**
- The lookup app and dashboard run on Databricks and require sign-in. That is the project's
  "authenticated first release"; there is no anonymous public access.
- To give someone access, a workspace admin invites them to the workspace, then grants
  `CAN_USE` on `ireland-rsi-lookup-donegal` and viewer access to the dashboard.
- The data is refreshed monthly (10th, 06:00). Each refresh becomes a new immutable
  release; nothing is overwritten.
- For readers without an account, the release report
  (`docs/donegal_release_<release>.pdf`) carries the published index, worked
  examples and the evidence.

**Extending later.** Another county joins only by passing the same gates on its own labels.
National scale first needs incremental linkage (D-026).

### D-031: Public repository, comment-only version change and the 7 October register (Accepted, 2026-10-09)

- The code is published at github.com/mccoolsa/Repeat-Sales-Index-DB- with a fresh history.
  Before publishing, comments, docstrings and documents were edited for readers outside the
  project: stage and gate names replace internal numbering. No logic changed.
- Stage versions hash the bytes of their source and config files, so those edits alone produced
  new versions. On the unchanged 30 September snapshot the result was `rel-6042601facc3`: the
  linkage model version was unchanged (`lm-4ed537c520de`), all 24 index periods were recorded as
  `method` revisions of at most 2e-13 index points, and every gate passed.
- The same day's Databricks refresh downloaded the register PSRA updated on 7 October 2026
  (snapshot `ppr:county=Donegal:5729e360427cc33d`, 21,484 Donegal rows) and published
  `rel-57ed44641cde` (Databricks run 358191034653175). Local Python could not verify the PSRA
  TLS chain, so the archived bytes were copied from the volume (`data/raw/ppr/2026-10-09`,
  checksum verified) and the local pipeline reproduced the same release ID.
- Effect of the new filings: 19,545 eligible sales, 1,224 headline pairs from 1,113 homes, a
  retrained model (`lm-31cc8abb732d`), Donegal 2025 at 241.5 (was 241.1) and 2026 at 255.1
  (provisional, was 255.5). Precision (98.0%), recall (75%), CSO bias (+0.3 pp) and the
  backtest (25.6% median error) are unchanged.
- The lookup app gained a local mode (`RSI_LOCAL_DUCKDB`, for offline demos) and an
  `?address=` link parameter. GitHub Actions runs lint and the test suite on every push.
