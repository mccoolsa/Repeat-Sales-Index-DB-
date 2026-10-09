"""Place-name reference data for locality mapping (decision D-011).

Sources (both CC BY 4.0):
* Tailte Éireann Townlands, OSi National Placenames Gazetteer (points with county,
  English, Irish and alternative names).
* CSO Census 2022 Built Up Areas (BUAs): the CSO's urban geography, 867 named areas.

Raw ArcGIS response pages are stored untouched by content address. The reference version is
a hash of the parsed canonical records, so an identical re-download yields the same version.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import pandas as pd

from ireland_rsi.ingest import fetch
from ireland_rsi.storage import ObjectStore, sha256_hex

TOWNLANDS_URL = ("https://services-eu1.arcgis.com/FH5XCsx8rYXqnjF5/arcgis/rest/services/"
                 "Townlands___OSi_National_Placenames_Gazetteer/FeatureServer/0/query")
BUA_URL = ("https://services-eu1.arcgis.com/BuS9rtTsYEV5C0xh/ArcGIS/rest/services/"
           "CensusHub2022_T2_2_BUA/FeatureServer/0/query")
TOWNLAND_FIELDS = "OBJECTID_1,ID,County,English_Na,Irish_Name,Alternativ,Gaeltacht,ITM_E,ITM_N"
BUA_FIELDS = "BUA_GUID,BUA_CODE,BUA_NAME,T1_1AGETT_Norm,AREA,AREA_UOM"
LICENCE = "CC BY 4.0"


def comparison_form(text: str | None) -> str:
    """Accent-insensitive, casefolded, punctuation-free form used only for matching."""
    if not isinstance(text, str) or not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    stripped = stripped.replace("&", " and ").replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^0-9a-z]+", " ", stripped.casefold()).split())


# ---------------------------------------------------------------------------
# Acquisition
# ---------------------------------------------------------------------------


def fetch_arcgis_pages(url: str, params: dict, page_size: int = 2000,
                       fetcher=fetch) -> list[bytes]:
    """Page through an ArcGIS FeatureServer query, returning the raw response bodies."""
    pages, offset = [], 0
    while True:
        query = dict(params, f="json", resultOffset=offset, resultRecordCount=page_size)
        body = fetcher(f"{url}?{urlencode(query)}").body
        payload = json.loads(body)
        if "error" in payload:
            raise RuntimeError(f"ArcGIS error from {url}: {payload['error']}")
        pages.append(body)
        full_page = len(payload.get("features", [])) >= page_size
        if not payload.get("exceededTransferLimit") and not full_page:
            return pages
        offset += page_size


def acquire_reference(objects: ObjectStore, fetcher=fetch) -> dict:
    """Download both layers, store raw pages, and return an acquisition record."""
    retrieved = datetime.now(UTC)
    townland_pages = fetch_arcgis_pages(TOWNLANDS_URL, {
        "where": "1=1", "outFields": TOWNLAND_FIELDS, "returnGeometry": "false",
        "orderByFields": "OBJECTID_1"}, fetcher=fetcher)
    bua_pages = fetch_arcgis_pages(BUA_URL, {
        "where": "1=1", "outFields": BUA_FIELDS, "returnGeometry": "false",
        "returnCentroid": "true", "outSR": "2157", "orderByFields": "BUA_CODE"},
        fetcher=fetcher)
    record = {
        "retrieved_utc": retrieved.isoformat(),
        "licence": LICENCE,
        "townlands": {"url": TOWNLANDS_URL, "pages": [objects.put(p, ".json")[0]
                                                      for p in townland_pages]},
        "buas": {"url": BUA_URL, "pages": [objects.put(p, ".json")[0] for p in bua_pages]},
    }
    record["acquisition_id"] = "reference-" + sha256_hex(
        json.dumps([record["townlands"]["pages"], record["buas"]["pages"]]).encode())[:16]
    path = objects.root / f"acquisitions/reference/{record['acquisition_id']}.json"
    if path.exists():  # identical content seen before: keep the first-seen record
        return json.loads(path.read_text(encoding="utf-8"))
    objects.put_record(f"acquisitions/reference/{record['acquisition_id']}.json", record)
    return record


# ---------------------------------------------------------------------------
# Parsing and gazetteer construction
# ---------------------------------------------------------------------------


def _features(pages: list[bytes]) -> list[dict]:
    rows = []
    for page in pages:
        for feature in json.loads(page)["features"]:
            row = dict(feature["attributes"])
            if "centroid" in feature:
                row["_x"], row["_y"] = feature["centroid"]["x"], feature["centroid"]["y"]
            rows.append(row)
    return rows


def _clean(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_townlands(pages: list[bytes]) -> pd.DataFrame:
    rows = [{
        "townland_id": f"td:{r['ID']}",
        "county": r["County"].strip().title(),
        "name_en": _clean(r["English_Na"]),
        "name_ga": _clean(r["Irish_Name"]),
        "name_alt": _clean(r["Alternativ"]),
        "gaeltacht": r["Gaeltacht"] == "Y",
        "itm_e": float(r["ITM_E"]),
        "itm_n": float(r["ITM_N"]),
    } for r in _features(pages)]
    return pd.DataFrame(rows).sort_values("townland_id").reset_index(drop=True)


def parse_buas(pages: list[bytes]) -> pd.DataFrame:
    rows = [{
        "bua_id": f"bua:{r['BUA_CODE']}",
        "name": r["BUA_NAME"].strip(),
        "population_2022": int(r["T1_1AGETT_Norm"] or 0),
        "area": float(r["AREA"] or 0),
        "area_unit": r["AREA_UOM"],
        "itm_e": float(r["_x"]),
        "itm_n": float(r["_y"]),
    } for r in _features(pages)]
    return pd.DataFrame(rows).sort_values("bua_id").reset_index(drop=True)


def bua_counties(buas: pd.DataFrame, townlands: pd.DataFrame,
                 min_radius_m: float = 1500.0) -> dict[str, list[str]]:
    """Counties each BUA belongs to: counties of townland points near its footprint.

    The footprint is approximated by a circle of the BUA's area (at least 1.5 km), so towns
    on a county boundary (Drogheda, Athlone, Carlow) belong to both counties. The county of
    the nearest townland is always included.
    """
    tx, ty = townlands.itm_e.to_numpy(), townlands.itm_n.to_numpy()
    result = {}
    for bua in buas.itertuples(index=False):
        area_m2 = bua.area * (1e6 if str(bua.area_unit).lower().startswith(("km", "sq km"))
                              else 1.0)
        radius = max(min_radius_m, math.sqrt(max(area_m2, 0.0) / math.pi))
        d2 = (tx - bua.itm_e) ** 2 + (ty - bua.itm_n) ** 2
        near = set(townlands.county[d2 <= radius ** 2])
        near.add(townlands.county.iloc[int(d2.argmin())])
        result[bua.bua_id] = sorted(near)
    return result


BUA_SUFFIXES = (" city and suburbs", " and environs", " city")


def build_aliases(townlands: pd.DataFrame, buas: pd.DataFrame,
                  counties_by_bua: dict[str, list[str]]) -> pd.DataFrame:
    """County-scoped alias table: alias (comparison form) -> BUA or townland targets.

    `n_targets` counts distinct targets of the same type sharing an alias in a county;
    more than one means a name match is ambiguous there.
    """
    rows = []

    def add(alias, county, target_type, target_id, language, origin):
        norm = comparison_form(alias)
        if norm:
            rows.append((norm, alias, county, target_type, target_id, language, origin))

    for bua in buas.itertuples(index=False):
        for county in counties_by_bua[bua.bua_id]:
            add(bua.name, county, "bua", bua.bua_id, "official", "cso_bua_name")
            lowered = bua.name.lower()
            for suffix in BUA_SUFFIXES:
                if lowered.endswith(suffix):
                    add(bua.name[: -len(suffix)], county, "bua", bua.bua_id, "official",
                        "cso_bua_name_short")
    # Irish/English forms: a townland whose name equals a BUA name in the same county
    # lends its other-language name to the BUA (Letterkenny <-> Leitir Ceanainn).
    bua_by_name = defaultdict(list)
    for bua_id, county_list in counties_by_bua.items():
        name = buas.loc[buas.bua_id == bua_id, "name"].iloc[0]
        for county in county_list:
            bua_by_name[(comparison_form(name), county)].append(bua_id)
    for td in townlands.itertuples(index=False):
        names = [(n, lang) for n, lang in ((td.name_en, "en"), (td.name_ga, "ga"),
                                           (td.name_alt, "alt")) if isinstance(n, str) and n]
        for name, language in names:
            if name:
                add(name, td.county, "townland", td.townland_id, language, "tailte_townland")
        for name, _ in names:
            for bua_id in bua_by_name.get((comparison_form(name), td.county), []):
                for other, language in names:
                    if other != name:
                        add(other, td.county, "bua", bua_id, language, "townland_alias_of_bua")

    aliases = pd.DataFrame(rows, columns=["alias_norm", "alias", "county", "target_type",
                                          "target_id", "language", "origin"])
    aliases = aliases.drop_duplicates(["alias_norm", "county", "target_type", "target_id"])
    aliases["n_targets"] = aliases.groupby(
        ["alias_norm", "county", "target_type"]).target_id.transform("nunique")
    return aliases.sort_values(["county", "alias_norm", "target_type", "target_id"]
                               ).reset_index(drop=True)


@dataclass(frozen=True)
class Reference:
    version: str
    townlands: pd.DataFrame
    buas: pd.DataFrame
    counties_by_bua: dict[str, list[str]]
    aliases: pd.DataFrame


def build_reference(townland_pages: list[bytes], bua_pages: list[bytes]) -> Reference:
    townlands = parse_townlands(townland_pages)
    buas = parse_buas(bua_pages)
    counties = bua_counties(buas, townlands)
    aliases = build_aliases(townlands, buas, counties)
    canonical = json.dumps([_canonical_records(townlands), _canonical_records(buas), counties],
                           sort_keys=True).encode()
    return Reference("ref-" + sha256_hex(canonical)[:12], townlands, buas, counties, aliases)


def _canonical_records(df: pd.DataFrame) -> list[dict]:
    """Plain-Python records with missing values as None, independent of pandas version
    (pandas 3 uses NaN for missing strings where pandas 2 uses None)."""
    def plain(value):
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        if hasattr(value, "item"):  # numpy scalar
            value = value.item()
        if isinstance(value, float):
            return round(value, 6)
        return value

    return [{k: plain(v) for k, v in row.items()} for row in df.to_dict("records")]


def load_reference(objects: ObjectStore, acquisition: dict) -> Reference:
    townland_pages = [objects.get(h, ".json") for h in acquisition["townlands"]["pages"]]
    bua_pages = [objects.get(h, ".json") for h in acquisition["buas"]["pages"]]
    return build_reference(townland_pages, bua_pages)
