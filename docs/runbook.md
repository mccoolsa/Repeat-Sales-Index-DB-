# Runbook

Operational procedures for the Ireland repeat-sales index. Sections are added as tasks land.

## 1. Environments

| | Local (development) | Databricks (Free Edition, serverless) |
|---|---|---|
| Objects (untouched bytes) | `data/store/` | `/Volumes/ireland_rsi/bronze/raw/` |
| Tables | DuckDB `data/warehouse/ireland_rsi.duckdb` | Delta in catalog `ireland_rsi` |
| Staging | n/a | `/Volumes/ireland_rsi/bronze/staging/` (transient; safe to empty) |
| Entry point | `python jobs/ingest_ppr.py --target local` | job `ingest_ppr` (bundle) |

Both run the same `ireland_rsi` code. `config/sources.yml` holds URLs, scopes, the
promotion policy and storage locations.

One-time Databricks setup: see the README. Deploy code changes with:

```bash
databricks bundle deploy
```

## 2. Ingesting the PSRA register

```bash
databricks bundle run ingest_ppr                                        # download in Databricks
python jobs/ingest_ppr.py --target local                                # download locally
python jobs/ingest_ppr.py --target local --saved data/raw/ppr/2026-10-06   # import a saved download
```

Each run:

1. Downloads with up to 3 attempts (2 s and 4 s backoff) for network errors and 5xx. A 4xx
   fails immediately.
2. Rejects HTML pages, short downloads (Content-Length mismatch), invalid zips, CRC
   failures and unexpected members. A rejected acquisition is still recorded in
   `bronze.source_manifest`.
3. Stores the zip and the CSV under `objects/sha256/<aa>/<sha256>.<ext>`, plus an evidence
   record in `acquisitions/psra_ppr/<acquisition_id>.json`. Identical bytes are stored once.
4. Parses under the source contract. Wrong field counts, a missing final line terminator,
   undecodable bytes or an unknown header → **rejected**.
5. Inserts every record occurrence into `bronze.ppr_rows`, keyed by
   `raw_row_id = SHA256(member_sha256 | record ordinal)`.
6. Reconciles against the active snapshot and decides:

| Decision | Meaning | Active data changes? |
|---|---|---|
| `promoted` | New snapshot became active | Yes |
| `already_active` | Same bytes as the active snapshot | No |
| `held` | Record count fell more than 0.5%, or under 98% of previous occurrences carried over | No |
| `rejected` | Acquisition or structure invalid | No |
| `reference` | Non-national scope (e.g. one county-year); mapped onto national for cross-checks | No |

7. For a promoted or already-active snapshot, re-parses the stored bytes and confirms they
   reproduce `bronze.ppr_rows` exactly (rebuild check). Exit code 2 if not.

The job exits non-zero for `held` and `rejected`, so Databricks marks the run failed.

### Reading results

```sql
-- What happened to each acquisition
SELECT acquisition_id, retrieved_utc, status, status_reason, record_count
FROM ireland_rsi.bronze.source_manifest ORDER BY retrieved_utc DESC;

-- Promotion history (latest promoted row per scope is the active snapshot)
SELECT decided_at, scope, snapshot_id, decision, reason
FROM ireland_rsi.bronze.snapshot_events ORDER BY decided_at DESC;

-- Corrections and items waiting for review
SELECT * FROM ireland_rsi.silver.sale_versions WHERE change_type = 'correction';
SELECT * FROM ireland_rsi.silver.reconciliation_reviews;
```

### A snapshot was held

1. Read the reason in `bronze.snapshot_events`.
2. Compare the record counts by year and county with the previous snapshot. Download the
   matching form extracts if needed.
3. If the drop is genuine (for example PSRA removed erroneous filings), approve it
   explicitly. The approval is recorded in the event reason:

   - Databricks: in the Jobs UI, use **Run now with different parameters** and add
     `--force-promote` to the task parameters.
   - Locally: `python jobs/ingest_ppr.py --target local --saved <folder> --force-promote`

4. If the file is faulty, do nothing. The previous snapshot stays active.

### Rolling back

Promotion is append-only. To reactivate an earlier snapshot, append a `promoted` event for
it (with a reason) to `bronze.snapshot_events`. Never delete events or rows.

## 3. Reconciliation rules

- **Exact match:** an occurrence with the same raw content as one in the active snapshot
  keeps that sale and version. Pairing is one-to-one, so identical rows keep their
  multiplicity.
- **Correction:** among the leftovers, a unique one-to-one match on (address, county, date),
  then (address, county, price), then (Eircode, county, date, price) keeps the sale ID and
  adds a version. The old version is never changed.
- **Review:** a leftover with several candidates becomes a new sale and is listed in
  `silver.reconciliation_reviews`.
- **Absent:** a previous occurrence missing from the new snapshot is not deleted. It stays
  in earlier snapshots' membership.

`txn_fp` canonicalisation is versioned (`txfp_v1`). Changing it requires a new version, not
an edit.

## 4. Releases and the current-release pointer

A release is published by the last job task (`publish_release`) after validation and the
backtest. Readers (lookup app, dashboard) follow the pointer: the latest `publish` or
`rollback` event in `gold.release_events` (view `gold.v_current_release`).

```bash
# Which release is current, and what it was built from
SELECT * FROM ireland_rsi_donegal.gold.v_current_release;

# Roll back to an earlier complete release (a new pointer event; nothing is deleted)
databricks bundle run ingest_ppr -t donegal --only publish_release --python-params \
  "--target,databricks,--project-root,/Workspace/Users/<you>/.bundle/ireland-rsi/donegal/files,--profile,donegal,--rollback-to,<release_id>,--reason,<why>"

# Locally
python jobs/publish_release.py --target local --profile donegal --rollback-to <release_id>
```

| Situation | What happens |
|---|---|
| Same inputs published again | No-op (`unchanged`), no new event |
| A hard gate fails | Manifest recorded as `failed`, `fail` event, pointer unchanged, task fails, email alert |
| Failure while writing serving tables | No manifest, no pointer move; the half-written release is never readable |
| Rollback target incomplete or failed | Refused; pointer unchanged |
| Link threshold not frozen (link-threshold gate) | Published as `research`; app and dashboard show a banner |

Revisions to every published period are written to `gold.revisions`, tagged by cause:
- `source`: new snapshot, same code and config;
- `method`: same snapshot, new code or config;
- `source+method`: both changed.

### Release drill evidence (2026-10-07)

| Drill | Where | Run | Result |
|---|---|---|---|
| Pilot refresh, validation → publish | Databricks | 91318617189323 | `rel-aebf4353627d` (identical locally) |
| Method change republish | Databricks | 311920815471052 | `rel-ab2bca6dd4e8`, 24 periods revised, cause `method`, max \|revision\| 0 |
| Forced gate failure | Databricks | 670090834968514 | `failed` manifest, pointer stayed on `rel-ab2bca6dd4e8` |
| Rollback, then roll forward | Databricks | 340552084390510, 958168556559520 | pointer → `rel-aebf4353627d` → `rel-ab2bca6dd4e8` |
| Full scheduled-style refresh, all 11 tasks | Databricks | 182473369106343 | 620 s wall, 634 s task time; register unchanged (`already_active`, 0 rows inserted in every data stage). Uncertainty, validation and backtest re-versioned because `publication.yml` had changed since they last ran, giving `rel-349b53e28e73` (identical locally), with 24 revisions, all exactly 0 |
| Mid-write failure | local | - | lookup rows written, no manifest, pointer unchanged, rollback to it refused |
| Idempotent republish | local | - | `unchanged` |
| Late old sale revises old periods | tests | `test_publish.py` | 2011 level moves after a late 2011 filing |

## 5. Refresh schedule, alerts and cost

- The job runs at 06:00 Europe/Dublin on the 10th of each month (unpaused after the link-threshold gate;
  the national target stays paused).
- A failed task emails the workspace user (`email_notifications.on_failure`).
- Each refresh recomputes every derived stage from the active snapshot (D-026). Unchanged
  inputs produce the same versions and no new rows.
- **Measured compute (serverless task time, Donegal):**

  | Stage | Time |
  |---|---|
  | ingest | 93-175 s |
  | clean | ~46 s |
  | addresses | ~67 s |
  | linkage, two tracks in parallel | ~57 s |
  | entities and pairs | ~47 s |
  | index | ~55 s |
  | uncertainty | 159 s |
  | validation | 149 s |
  | backtest | 52 s |
  | publish | 32-81 s |
  | **Total** | **about 14 minutes** |

  Free Edition exposes no billing system tables, so task time is the recorded cost measure.

## 6. Apps

| App | Purpose | Access |
|---|---|---|
| `ireland-rsi-labelling-donegal` | Human labels for the link-threshold gate | `SELECT` sample, `SELECT, MODIFY` labels (`sql/app_grants.sql`) |
| `ireland-rsi-lookup-donegal` | Sale history and index-adjusted estimate | `SELECT` on five gold tables only (`sql/lookup_grants.sql`) |

```bash
databricks bundle run lookup -t donegal                     # deploy and start the lookup app
python -m ireland_rsi.dbx_sql sql/lookup_grants.sql --var catalog=ireland_rsi_donegal --var app_sp=<client id>
python jobs/lookup_latency.py --catalog ireland_rsi_donegal  # the lookup check latency measurement
```

Free Edition apps stop after 24 hours; start them again from the Apps page or with
`databricks apps start <name>`. Measured latency on 2026-10-07: full lookup workflow
(about 7 queries), from this laptop: cold 14.6 s, warm p50 2.27 s, **warm p95 2.98 s**.

## 7. Dashboard

```bash
python -m ireland_rsi.dbx_sql dashboards/queries.sql --var catalog=ireland_rsi_donegal  # views
python dashboards/build_dashboard.py && databricks bundle deploy -t donegal              # layout
```

The views must exist before the dashboard is opened. The datasets use two-part names
(`gold.v_*`), and the bundle sets the catalog per target (`dataset_catalog`).

## 8. Link threshold (link-threshold gate): done on 2026-10-07

The threshold was frozen at 0.5 after 260 labels (D-029, `docs/validation_report.md` §2).
To relabel or extend the sample later:

1. Label in the app, then pull the labels locally: `python jobs/pull_labels.py --profile donegal`.
2. Run `validate_index`. Its version includes a label digest, so new labels always give a
   new version, with final-link precision and recall by threshold.
3. If the evidence changes, edit `accept_threshold` in `config/entities.yml` and run the
   full job. Every downstream version changes, and revisions are tagged `method`.

The Donegal schedule is **unpaused** (`schedule_pause_status: UNPAUSED` on the `donegal`
target). To stop it, set the variable to `PAUSED` and deploy.

## 9. After each monthly release

```bash
python jobs/pull_labels.py --profile donegal               # only if labels changed
python jobs/run_pipeline.py --profile donegal --start clean # local copy of the release
python jobs/report_charts.py --profile donegal
python docs/build_release_report.py                        # docs/donegal_release_<release_id>.pdf
```

Share the release report with readers who have no workspace account (D-030).
