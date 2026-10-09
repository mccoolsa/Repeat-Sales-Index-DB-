# Source audit: PSRA Residential Property Price Register

**Task:** 1 (source contract and viable pilot)
**Acquisition:** `ppr-all-2026-10-06`
**Audited:** 6 October 2026
**Reproduce:** `python -m ireland_rsi.source_audit data/raw/ppr/2026-10-06`. This writes
`reports/source_audit_ppr-all-2026-10-06.json`, which every number below comes from.

## 1. Acquisition

| Item | Value |
|---|---|
| Official route | PSRA home page, "DOWNLOAD ALL", which resolves to the direct URL below. The "Download Specific" form only offers a single year at a time. |
| URL | `https://www.propertypriceregister.ie/website/npsra/ppr/npsra-ppr.nsf/Downloads/PPR-ALL.zip/$FILE/PPR-ALL.zip` |
| Retrieved | 2026-10-06T13:11:27Z |
| HTTP Last-Modified | Wed, 30 Sep 2026 16:53:26 GMT |
| Page "Register Last Updated" | 30/09/2026 17:49:29 (Irish time) |
| Archive | `PPR-ALL.zip`, 19,139,142 bytes, SHA-256 `15e2811d…1b47` |
| Member | `PPR-ALL.csv`, 110,734,515 bytes, SHA-256 `7e1631bc…2d2d` |
| Storage | `data/raw/ppr/2026-10-06/`, untouched, with `manifest.json` and the raw HTTP headers |

**Snapshot behaviour:** the file is a full national snapshot that is regenerated in place. It
is not a monthly delta. PSRA states that errors are corrected by filers submitting amended
stamp duty returns, so later snapshots may change historical rows. This confirms the ingestion
design: reconcile snapshots and preserve versions, and never treat a new download as
append-only.

## 2. Physical format

| Property | Observed |
|---|---|
| Encoding | **Windows-1252 (cp1252)**. Not UTF-8: byte `0x80` is the euro sign, and strict UTF-8 decoding fails. |
| BOM | None |
| Line endings | CRLF throughout; 809,015 lines, no bare LF |
| Delimiter / quoting | Comma; every data field is double-quoted; header is unquoted |
| Records | **809,014** data rows plus 1 header; every row has exactly 9 fields |
| Embedded newlines in fields | 0 |
| Leading/trailing whitespace in fields | 0 |
| Non-ASCII content | Euro sign, and Irish fadas Á É Í Ó Ú á é í ó ú (about 2,600 bytes in total) |

Header (contract `psra_ppr_csv` v1, fingerprint `b1c01217…60c4`):

```
Date of Sale (dd/mm/yyyy),Address,County,Eircode,Price (€),Not Full Market Price,VAT Exclusive,Description of Property,Property Size Description
```

## 3. Coverage

- **Dates:** 2010-01-01 to 2026-09-25. All 809,014 parse strictly as `dd/mm/yyyy`.
- **Counties:** all 26 Republic of Ireland counties, as county names (Dublin is one value,
  not split into postal districts).
- **Volume by year:** from 18,446 (2011) up to 63,386 (2023).

| 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | 2017 | 2018 |
|---|---|---|---|---|---|---|---|---|
| 21,005 | 18,446 | 25,375 | 30,227 | 43,677 | 49,166 | 49,941 | 55,023 | 57,464 |

| 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 (Jan-Sep) |
|---|---|---|---|---|---|---|---|
| 59,067 | 49,561 | 59,608 | 62,760 | 63,386 | 61,569 | 62,098 | 40,641 |

**Filing lag is visible.** 2026 monthly counts are 3.6k-5.7k for January to August, but only
3,171 for September. The latest month, and probably the latest quarter, is incomplete. That
supports marking at least the newest quarter provisional. The actual lag profile has to be
measured by comparing successive snapshots.

- **Donegal:** 21,452 rows, of which 1,445 are dated 2025. About 5,250 addresses contain
  "Letterkenny", which is a substring match and not yet a locality assignment.

## 4. Field profile

### Price

- All 809,014 values match `€#,###.##`, so parsing to integer cents is safe.
- Minimum €5,001; maximum **€387,665,198**.
- 510 rows are ≥ €10m and 1,100 are < €10k. The high values are almost certainly the
  multi-unit or portfolio filings PSRA describes (several apartments sold for a single price).
  These are flagged for review, not deleted.

### Market and VAT flags

| Field | Values |
|---|---|
| Not Full Market Price | No 767,805; **Yes 41,209** (5.1%) |
| VAT Exclusive | No 666,416; **Yes 142,598** (17.6%) |

The `**` marker described on the PSRA site does **not** appear in the CSV. The structured
"Not Full Market Price" column carries that information instead.

VAT against description:

| Description | VAT excl. = Yes | VAT excl. = No |
|---|---|---|
| New Dwelling house /Apartment | 142,594 | 2,382 |
| Second-Hand Dwelling house /Apartment | 0 | 663,989 |
| Irish-language "new" (`…Nua`) | 4 | 0 |
| Irish-language "second-hand" (`…Atháimhe`) | 0 | 45 |

No second-hand sale is VAT-exclusive, which is consistent. **2,382 new dwellings are not
VAT-exclusive**, and their price basis is ambiguous (VAT-inclusive, VAT not applicable, or a
filing error). The second-hand headline series avoids this problem.

The PSRA information note still says "exclusive of VAT at 13.5%". Revenue guidance introduced
9% for qualifying apartments from 8 October 2025. The CSV has no field identifying qualifying
apartments, so post-October-2025 new-apartment VAT adjustments cannot be resolved from this
source alone (see `VAT_RULE_UNRESOLVED`).

### Property description and size

- Description is a coarse new/second-hand split: "Dwelling house /Apartment" combined. **The
  source does not distinguish houses from apartments.** 49 rows use Irish-language
  equivalents, which `classify_source_description` must map explicitly.
- Size description is **blank for 756,169 rows (93.5%)**. It is populated almost only for new
  dwellings, in four bands, plus 3 Irish-language variants. The band "greater than 125" and
  "greater than or equal to 125" both appear, so the definition changed at some point. Size
  is not usable as a quality control for repeat sales.
- PSRA confirms "There are no further particulars (e.g. floor area, number of rooms, etc.)".

### Eircode

The Eircode **column exists** in this export, but values are effectively absent before 2021.

| Year | ≤2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|
| % populated | ≤0.3 | 0.8 | 51.4 | 76.9 | 76.3 | 75.2 | 74.1 | 76.4 |

- 251,675 populated values (31.1% of all rows). Every one is syntactically valid (routing key
  plus 4-character identifier), and all are stored without a space.
- Coverage from 2022 is broadly even by county: 59% (Kildare) to 84% (Longford).
  **Donegal 76.3%.** Donegal's lower all-years figure (28.9%) reflects the time pattern, not a
  regional gap.
- 238,652 distinct codes. **12,105 codes appear on 2 or more sales** (25,128 rows). These are
  the potential weak-label groups, and almost all fall within 2021-2026.
- Consistency warnings among repeated codes:
  - 73 span more than one county.
  - 1,173 have differing address text.
  - 490 have a differing leading house/unit number.

  Some are address variants of the same property. Others suggest wrong or reused codes, so
  Eircode agreement is not proof of identity.

## 5. Data quality observations

| Observation | Count | Handling |
|---|---|---|
| Exact duplicate rows (every field identical) | 1,078 extra occurrences in 960 groups; max multiplicity 8 | Preserve multiplicity; do not deduplicate on content alone |
| Source-side character loss (`?` replacing fadas) | 1 row (description and size text) | Record as a source defect; map by pattern in the cleaning stage |
| Price ≥ €10m | 510 | Review as possible multi-unit filings |
| New dwelling, not VAT-exclusive | 2,382 | `VAT_RULE_UNRESOLVED` unless evidence resolves it |
| Not full market price | 41,209 | Keep in silver; ineligible for the index |

## 6. Consequences for the design

1. **Eircode evaluation is only partially feasible.** A repeat-sale pair can carry an
   Eircode weak label only if both legs are from 2021 or later. Most repeat pairs in a
   2010-2026 index span longer holding periods, so Eircode labels would cover a short-hold,
   recent subset that is not representative.
   **Decision D-004:** a stratified hand-labelled sample is the primary linkage evaluation.
   Eircode labels are a secondary, explicitly limited check.
2. **No house/apartment split and no size.** Quality change cannot be controlled by property
   type from this source. Unit-number features in addresses are the only apartment signal.
3. **Encoding must be declared, not guessed.** The contract pins cp1252, and strict decoding
   fails rather than replacing characters.
4. **Snapshots are full and regenerated.** Reconciliation in the ingestion stage is required. A single
   download cannot show when a row first appeared, so first-seen dates start from this
   acquisition.

## 7. CSO benchmark (RPPI)

**Acquisition:** `cso-rppi-2026-10-06`, saved in `data/raw/cso/2026-10-06/` with a manifest.
**Contract:** `config/cso_benchmark.yml`, checked by `tests/test_cso_benchmark.py`.
**Method:** PxStat REST `ReadDataset/{table}/JSON-stat/2.0/en`. The full CSO catalogue was
searched for property-price tables.

### Tables found

| Table | Frequency | Base | Breakdown | Latest | CSO updated | Use |
|---|---|---|---|---|---|---|
| **HPM09** | Monthly | 2015=100 | 20 series: national, Dublin areas, 7 regions (houses) | 2026-07 | 2026-09-16 | **Primary** |
| **HPQ01** | Quarterly | 2015=100 | All / New / Existing | 2026Q2 | 2026-08-19 | **Primary for second-hand** |
| HPM06 | Monthly | Jan 2005=100 | Old region boundaries ("Border excluding Louth") | 2026-07 | 2026-09-16 | Rejected |
| HPA06 / HPA13 | Annual | 2005 / 2015 | As HPM06 / HPM09 | 2025 | 2026-05-13 | HPA13 possible annual check |
| HPM01 | n/a | n/a | n/a | n/a | 2020 | Stale |

All five "Residential Property Price Index" tables are genuine indices. Medians and
transaction counts are in separate tables (HPM02-HPM05, HPA02-HPA05), which were not used.

### Series selected

| Our series | CSO series | Code | Values from |
|---|---|---|---|
| National second-hand (headline) | HPQ01 Existing | `C03346V04033=02` | 2010Q1 |
| National all | HPM09 National - all residential properties | `C02803V03373=-` | 2005-01 |
| Donegal (indicative only) | HPM09 Border - houses | `C02803V03373=21` | 2010-01 |

- Index statistic codes: `HPM09C01` and `HPQ01C01`. The other statistics are percentage
  changes, not levels.
- **The Border region is Cavan, Donegal, Leitrim, Monaghan and Sligo** (CSO RPPI survey
  background notes). It is a five-county region and **houses only**. PPR cannot separate
  houses from apartments, so a Donegal comparison is indicative, not like-for-like.
- **Gaps:** none from 2010. The 11 sub-national HPM09 series start in 2010-01; the 660
  blank values are exactly 11 series × 60 months of 2005-2009.
- **Recent values:** national all 208.6 (2026-07); Border houses 265.2 (2026-07); HPQ01
  existing 208.9 (2026Q2).
- **No county-level or Letterkenny RPPI** exists in the catalogue. The town level has no
  official benchmark.

### Why CSO and our index will differ (from CSO methodology and background notes)

| | CSO RPPI | This project |
|---|---|---|
| Population | Market-based purchases **by households** only | All PPR filings; buyer type is not in the source |
| Quality adjustment | Hedonic, using stamp duty data matched to **BER** floor area and dwelling type | Repeat sales of the same property |
| Smoothing | Applied | None planned |
| Revisions | Latest **3 months provisional** | Latest quarter or more provisional |
| Sub-national reliability | CSO warns regional series "can exhibit significant volatility" | Support gates |

CSO and PPR both derive from Revenue stamp duty returns, so agreement is not independent
evidence that the linkage is right.

## 8. Reuse terms (checked 2026-10-06)

| Source | Licence | Evidence | Attribution |
|---|---|---|---|
| CSO RPPI (HPM09, HPQ01) | **CC BY 4.0** | [CSO copyright policy](https://www.cso.ie/en/aboutus/whoweare/copyrightpolicy/): "licensed under Creative Commons Attribution (CC BY 4.0)". CSO logo excluded. | "Source: Central Statistics Office, Ireland (CC BY 4.0)" |
| PSRA national register (`PPR-ALL.zip`) | **Not stated** on the download page | The PSRA site has no reuse statement. PSRA's only data.gov.ie listings ([organisation page](https://data.gov.ie/organization/property-services-regulatory-authority)) are the *Dublin* register subset and the Dublin commercial leases register, both **CC BY 4.0**. | "Contains Residential Property Price Register data, Property Services Regulatory Authority" |
| Tailte Éireann townlands 2024 | **CC BY 4.0** (data.gov.ie listing) | 50,580 features; fields include `GUID`, `ENG_NAME_VALUE`, `GLE_NAME_VALUE`, `GAELTACHT_AREA`; **no county field** | "Contains Tailte Éireann data (CC BY 4.0)" |
| Logainm placenames | **CC BY 4.0** (API key required; no bulk download; do not scrape) | [Logainm open data](https://www.logainm.ie/en/about/open-data) | "Irish-language placename data by Logainm © Government of Ireland and licensed under CC BY 4.0." |

**PSRA conclusion.** The same publisher licenses a subset of the same register under
CC BY 4.0, which strongly suggests attribution-based reuse is intended. However, no licence
is stated for the national file. For a non-commercial portfolio project: attribute as
above, keep address-level lookup behind authentication, and ask PSRA
(info@psr.ie) to confirm the national file's terms. Record the reply under D-008.

Address-level data: the register is published under statute (s.86 Property Services
(Regulation) Act 2011), but an app that assembles a property's sale history is a new
presentation of it. The authenticated-access default stays.

## 9. Locality reference options

PPR has **no coordinates**, only free-text addresses, so boundary polygons only help if
addresses are geocoded. Geocoding about 809k addresses needs a licensed product (ECAD or
GeoDirectory) or a rate-limited open service, which is impractical. The locality
reference therefore has to work mainly as a **name gazetteer**: county-scoped English and
Irish names matched against address tokens.

| Option | Licence | Strengths | Weaknesses | Verdict |
|---|---|---|---|---|
| **A. Tailte Éireann Townlands 2024** | CC BY 4.0 | Official, about 50.6k names, English **and** Irish name per record, stable GUID, Gaeltacht flag, bulk download | No county attribute (needs one spatial join to county boundaries); townland ≠ town | **Recommended gazetteer** |
| **B. CSO Census 2022 Built Up Areas** | Tailte Éireann / CSO open data (licence to confirm on download) | 867 named urban areas, the CSO's current official "town" geography, includes Letterkenny | Polygons are unusable without geocoding; used here as a list of town names | **Recommended "town" definition** |
| C. Logainm API | CC BY 4.0 | Richest bilingual aliases and variant spellings | API key, rate limits, no bulk download, monthly refresh expected | Optional alias enrichment later |
| D. Eircode routing keys (139) | Unofficial boundaries (Autoaddress) | CSO uses routing keys as a reference area | Eircodes absent before 2021 in PPR; boundaries not official | Reject as primary; diagnostic only |
| E. OpenStreetMap / Nominatim | ODbL (share-alike) | Coordinates | Usage policy (about 1 request/s) makes 809k geocodes impractical; share-alike obligations | Reject |

Recommended definition (D-011): **"town" = a CSO Census 2022 Built Up Area name matched
in the address within the correct county**, with townland names (A) used to disambiguate
rural addresses and to supply Irish/English aliases. Addresses matching no BUA fall back
to county. This is an address-derived locality, not a boundary-verified one, and the
documentation must say so.

## 10. Source-contract check status

| The source-contract check requirement | Status |
|---|---|
| Source access evidence | **Done**: official URL, manifest, checksums |
| Real sample schema | **Done**: contract v1, tested |
| Eircode availability decision | **Done**: D-004 |
| Licence / reuse terms | **Done with caveat**: CSO and Tailte Éireann are CC BY 4.0; the PSRA national file has no stated licence (§8, D-008). Email PSRA to confirm. |
| Pilot scope | **Done**: Donegal / Letterkenny (D-005) |
| Compatible runtime | **Done**: local Python 3.12 pinned; Databricks Free Edition provisioned and serverless smoke test passed (D-009, D-013) |
| CSO benchmark contract | **Done**: HPM09 + HPQ01 archived, series codes in `config/cso_benchmark.yml` (D-010) |
| Locality reference | **Done**: BUA names plus Tailte Éireann townlands (§9, D-011) |
| Cross-check against form download | **Open**: Donegal / 2025 / all months should contain 1,445 rows |
| Dense urban audit sample | Covered by the national file (Dublin, 253,544 rows); no separate extract needed |

**Source-contract check: passed on 2026-10-06.** Open follow-ups that do not block the ingestion stage: PSRA licence
confirmation (D-008) and the optional Donegal 2025 form cross-check.
