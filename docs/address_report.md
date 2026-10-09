# Address normalisation and locality report

**Snapshot:** `ppr:national:7e1631bc229a5d92` · **Address version:**
`addr_v1+ref-d81f89a66c28+f8b49a51` (normaliser logic + reference version + code hash)
**Reproduce:** `python jobs/build_addresses.py --target local --report-dir reports`

## 1. Reference data (D-011)

| Source | Records | Use |
|---|---:|---|
| Tailte Éireann Townlands, OSi National Placenames Gazetteer (CC BY 4.0) | 50,380 townlands in 26 counties | English, Irish and alternative names; county; ITM point |
| CSO Census 2022 Built Up Areas (CC BY 4.0) | 867 BUAs | Definition of "town"; names, populations, centroids |
| Derived alias table `silver.place_aliases` | 86,806 county-scoped aliases | Name → BUA or townland, with an ambiguity count |
| Derived `silver.localities` | 907 BUA-county rows | 39 BUAs span two counties (e.g. Drogheda: Louth and Meath) |

Raw ArcGIS response pages are stored untouched, content-addressed
(`acquisitions/reference/reference-3da8bfa751ef67df.json`). The reference version is a hash
of the parsed records, so re-downloading identical data changes nothing. The hash uses
plain-Python canonical records: an earlier version hashed pandas output directly and gave
different versions on Databricks (pandas 2) and locally (pandas 3) for byte-identical data,
because missing values are represented differently. This was fixed and tested.

Bilingual aliases come from townland records whose name equals a BUA name in the same
county (Letterkenny ↔ Leitir Ceanainn; An Bun Beag ↔ Bunbeg). Only 2 BUA aliases are
ambiguous within a county.

## 2. Definition of "town"

**A town is a CSO Census 2022 Built Up Area named in the address within the sale's county.**
It is an *address-derived* locality, not a boundary-verified one, because the register has
no coordinates. Rules, most specific place first:

1. Components are read from the street onwards. Rural addresses end with a post town the
   property is not in ("Magherablade, Creeslough, Letterkenny" → Creeslough).
2. An unnumbered address naming a townland outside every BUA is **rural**: county level only.
3. Explicit "Co. X" is removed. A bare county name that is also a town ("…, Carlow") is kept,
   but at **low** confidence.
4. A name shared by several BUAs in the county is **ambiguous** and is not forced.
5. Otherwise the sale is county level only.

## 3. Coverage

| Confidence | Occurrences | Share | Methods |
|---|---:|---:|---|
| High | 440,062 | 54.4% | Exact official BUA name; Dublin postal district |
| Medium | 6,533 | 0.8% | BUA name at the end of a component ("Main Street Letterkenny") |
| Low | 106,506 | 13.2% | Bare county name as town; townland-derived alias |
| None (county level) | 255,913 | 31.6% | Includes 56,239 confirmed rural and 15,416 ambiguous townland names |

The share at high or medium confidence is lowest in Carlow (24.6%), Limerick, Kilkenny,
Sligo, Longford and Galway (38.9%). These are counties whose main town shares the county's
name, so many addresses only give that name (low confidence).

Structured components: 77.6% have a house or unit number, 5.1% a unit (apartment, flat,
unit), 0.12% a building number range, and 22.5% a uniquely matched townland.

**Donegal pilot:** 13,253 high and 202 medium assignments out of 21,452. **Letterkenny:**
4,326 high/medium occurrences, of which **3,345 are index-eligible second-hand sales**.
Of the 5,260 addresses containing "Letterkenny", about 900 were assigned elsewhere
(Ramelton, Falcarragh/An Fál Carrach, Bunbeg/An Bun Beag, rural townlands), because
Letterkenny was only their post town.

## 4. Measured accuracy (Eircode routing keys)

Routing keys are independent of address text. For each BUA, valid keys are those covering
at least 5% of its exact-name assignments. Each assignment with a valid Eircode (251k sales,
mainly from 2021 on) is scored on whether its key is valid for the assigned BUA.

| Method | Confidence | Sales checked | Agreement |
|---|---|---:|---:|
| Exact official BUA name (outside Dublin) | high | 106,377 | **99.3%** |
| Dublin postal district ↔ routing key (exact, e.g. D8 ↔ D08) | high | 39,535 | **97.5%** |
| Trailing BUA name in component | medium | 2,358 | 90.8% |
| Bare county name as town (outside Dublin) | low | 29,957 | 89.1% |
| Bare "Dublin" as Dublin city | low | 9,571 | 32.1%* |
| Townland-derived alias | low | 932 | 62.4% |
| ~~Townland within BUA circular footprint~~ | removed | 5,103 | 52.6% |

\* The Dublin city valid-key set comes from only 175 exact-name assignments, so the general
check under-states Dublin; the postal-district check is the reliable Dublin measure.

**Method removed on evidence:** assigning a townland to a BUA by a circular approximation of
the BUA's footprint agreed only 52.6% of the time outside Dublin. It is no longer used for
town assignment (townland IDs are kept for linkage).

**Recommendation for the uncertainty stage:** use only **high and medium** assignments for town indices.
Low-confidence sales still count towards their county.

## 5. Normalisation guarantees (tests)

`tests/test_addresses.py` (37 tests) covers golden and adversarial cases:

- "Apt 4, The Grove" / "4 The Grove Apartments, Co. Donegal" share `primary_number = 4`
  and `street_core = grove`. These are shared features, not an identity claim.
- Apartment 4 ≠ 14; 12A ≠ 12; a unit number is not a street number ("Apt 4, 12 Main St").
- A repeated estate name in different towns gives different localities.
- Rural unnumbered addresses, punctuation and case changes, Irish accents and Irish town
  names, missing locality, ambiguous names, border towns, and town names inside street
  names ("Letterkenny Road, Ramelton").
- "St" is Saint before a name ("APT D16 ST ANGELAS") and Street after one ("MOUNT ST").
- Deterministic and idempotent output, and source text is never modified.

## 6. Limitations

- Precision is measured only where Eircodes exist (mostly sales from 2021 on). Older address
  styles may behave differently.
- Low-confidence assignments (13%) are kept for transparency, not for town indices.
- No geocoding: a town is matched by name, not by location. Renumbered or renamed estates
  cannot be detected from text.

## 7. Address-confidence check: passed on 2026-10-06

| | Local (DuckDB) | Databricks (job run 200310425130751, SUCCESS, 300 s) |
|---|---|---|
| Reference acquisition | `reference-3da8bfa751ef67df` | same (byte-identical pages) |
| Reference / address version | `ref-d81f89a66c28` / `addr_v1+ref-d81f89a66c28+f8b49a51` | identical |
| Features | 809,014 | 809,014 |
| Mapped to a BUA | 68.37% | 68.37% |
| Postal district ↔ routing key | 97.5% | 97.5% |

Golden fixtures pass (38 tests), normalisation is versioned, and address-derived geography
has a stated definition (§2) with measured coverage (§3) and accuracy (§4).
