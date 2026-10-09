"""Conservative address normalisation and locality mapping.

normalise_address() never overwrites source text. It produces comparison forms and
structured components (unit, house number, street, locality parts) and maps the address
to a CSO Built Up Area only when one target in the address's county matches. Missing
components stay None: missing is neither agreement nor disagreement.

Nothing here asserts that two addresses are the same property; that is the job of the
evaluated linkage model.
"""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from ireland_rsi.contracts import RunContext
from ireland_rsi.reference import Reference, comparison_form
from ireland_rsi.storage import sha256_hex

ADDRESS_LOGIC = "addr_v1"

ABBREVIATIONS = {
    "apt": "apartment", "apts": "apartments", "appt": "apartment", "flt": "flat",
    "rd": "road", "ave": "avenue", "av": "avenue", "dr": "drive", "cres": "crescent",
    "cresc": "crescent", "tce": "terrace", "terr": "terrace", "pk": "park", "sq": "square",
    "ct": "court", "crt": "court", "cl": "close", "gdns": "gardens", "gdn": "garden",
    "hts": "heights", "lwr": "lower", "lr": "lower", "upr": "upper", "mt": "mount",
    "pl": "place", "est": "estate", "grv": "grove", "nth": "north", "sth": "south",
    "co": "county", "nr": "number", "appartment": "apartment", "app": "apartment",
    "apartmant": "apartment",
}
# Words that number a dwelling within a building or development ("Site 5", "House 2",
# "Duplex 3"). Profiled from the leading word of 809k addresses.
UNIT_WORDS = {"apartment", "apartments", "flat", "unit", "suite", "studio", "site", "house",
              "duplex", "townhouse", "cottage", "chalet", "villa", "bungalow", "lodge",
              "penthouse"}
NUMBER_WORDS = {"number", "no"}
GENERIC_BUILDING_WORDS = {"the", "apartments", "apartment", "flats"}
GENERIC_LOCALITY_WORDS = {"town", "centre", "center", "city", "village"}
UNIT_ID = re.compile(r"^(?:[0-9]+[a-z]?|[a-z]{1,2}[0-9]{0,3})$")
HOUSE_NUMBER = re.compile(r"^([0-9]+)([a-z]?)$")
HOUSE_RANGE = re.compile(r"^[0-9]+[a-z]?-[0-9]+[a-z]?$")
POSTAL_DISTRICT = re.compile(r"^(?:dublin|d)\s?([0-9]{1,2}w?)$")
COMPONENT_SPLIT = re.compile(r",|\s{2,}")


@dataclass
class AddressFeatures:
    address_display: str
    address_norm: str
    components: list[str]
    unit_type: str | None = None
    unit_id: str | None = None
    house_number: str | None = None
    house_suffix: str | None = None
    house_range: str | None = None
    primary_number: str | None = None
    street: str | None = None
    street_core: str | None = None
    locality_parts: list[str] = field(default_factory=list)
    postal_district: str | None = None
    county: str | None = None
    locality_id: str | None = None
    locality_name: str | None = None
    locality_method: str = "county_only"
    locality_confidence: str = "none"
    ambiguity_state: str = "none"
    townland_id: str | None = None


def display_form(raw: str) -> str:
    """Readable form: HTML entities decoded, whitespace collapsed. Content unchanged."""
    text = html.unescape(raw or "")
    text = re.sub(r"\s+,", ",", text)
    return " ".join(text.split())


def _tokens(component: str) -> list[str]:
    """Comparison tokens with contextual abbreviation expansion."""
    words = comparison_form(component.replace("-", " - ")).split()
    words = [w for w in words if w]
    # Re-join number ranges split above ("18 - 21" -> "18-21").
    joined: list[str] = []
    for w in words:
        if w == "-":
            continue
        joined.append(w)
    text = component.lower()
    out: list[str] = []
    for i, w in enumerate(joined):
        nxt = joined[i + 1] if i + 1 < len(joined) else None
        prev = joined[i - 1] if i > 0 else None
        if w == "st":
            # "St Angela's", "5 St Margaret's", "Apt D16 St ..." -> Saint;
            # "Main St", "Mount St" -> Street.
            before_name = prev is None or any(ch.isdigit() for ch in prev) or (
                ABBREVIATIONS.get(prev, prev) in UNIT_WORDS | NUMBER_WORDS)
            out.append("saint" if nxt and before_name else "street")
        elif w == "no" and nxt and nxt[0].isdigit():
            out.append("number")
        else:
            out.append(ABBREVIATIONS.get(w, w))
    # Restore ranges such as 18-21 that comparison_form split into two tokens.
    for match in re.finditer(r"\b([0-9]+[a-z]?)\s*-\s*([0-9]+[a-z]?)\b", text):
        a, b = match.group(1), match.group(2)
        for i in range(len(out) - 1):
            if out[i] == a and out[i + 1] == b:
                out[i:i + 2] = [f"{a}-{b}"]
                break
    return out


class AddressNormaliser:
    def __init__(self, reference: Reference, counties: list[str]) -> None:
        self.reference = reference
        self.version = (f"{ADDRESS_LOGIC}+{reference.version}+"
                        f"{sha256_hex(Path(__file__).read_bytes())[:8]}")
        self.county_norms = {comparison_form(c): c for c in counties}
        self.aliases: dict[tuple[str, str], list[tuple]] = defaultdict(list)
        for row in reference.aliases.itertuples(index=False):
            self.aliases[(row.county, row.alias_norm)].append(
                (row.target_type, row.target_id, row.origin, row.n_targets))
        self.bua_names = dict(zip(reference.buas.bua_id, reference.buas.name, strict=True))
        self.townland_bua = townland_bua_map(reference)
        self.dublin_bua = next((b for b, n in self.bua_names.items()
                                if n.lower().startswith("dublin city")), None)

    # -- components --------------------------------------------------------------

    def _strip_county(self, tokens: list[str], county: str) -> list[str]:
        county_norm = comparison_form(county)
        if tokens[-2:] == ["county", county_norm]:
            return tokens[:-2]
        if tokens and tokens[-1] == county_norm and len(tokens) > 1 and tokens[-2] == "county":
            return tokens[:-2]
        return tokens

    def __call__(self, address_raw: str | None, county_raw: str | None) -> AddressFeatures:
        display = display_form(address_raw or "")
        county = county_raw if county_raw in (self.county_norms.values()) else None
        pieces = [p.strip() for p in COMPONENT_SPLIT.split(html.unescape(address_raw or ""))]
        pieces = [p for p in pieces if p]
        comps = [_tokens(p) for p in pieces]
        comps = [c for c in comps if c]
        f = AddressFeatures(display, " ".join(" ".join(c) for c in comps),
                            [" ".join(c) for c in comps], county=county)
        if not comps:
            return f

        # Explicit county references ("Co. Donegal", "Letterkenny Co Donegal") are removed.
        # A bare county name ("..., Carlow") is kept: it may be the county or the town of
        # the same name, and locality mapping gives it low confidence.
        county_norm = comparison_form(county) if county else None
        while comps and county_norm:
            last = comps[-1]
            if last == ["county", county_norm] and len(comps) > 1:
                comps.pop()
                continue
            stripped = self._strip_county(last, county)
            if stripped != last and stripped:
                comps[-1] = stripped
            break
        if county_norm and len(comps) > 1 and comps[-1] == [county_norm] and not self._lookup(
                county, county_norm, "bua"):
            comps.pop()  # "..., Mayo": a county name that is not also a town

        # Dublin postal district ("Dublin 8", "D6W") - a locality clue, not a street.
        for i in range(len(comps) - 1, 0, -1):
            m = POSTAL_DISTRICT.match(" ".join(comps[i]))
            if m:
                f.postal_district = "D" + m.group(1).upper()
                comps.pop(i)
                break

        # Unit designator at the start: "Apartment 4", "Flat C", "Apt D16 St Angelas".
        first = comps[0]
        if first[0] in UNIT_WORDS and len(first) > 1 and UNIT_ID.match(first[1]):
            f.unit_type, f.unit_id = first[0], first[1]
            rest = first[2:]
            comps = ([rest] if rest else []) + comps[1:]

        # House number or building range at the start of the next component.
        if comps:
            first = comps[0]
            start = 1 if first[0] in NUMBER_WORDS and len(first) > 1 else 0
            token = first[start] if len(first) > start else ""
            m = HOUSE_NUMBER.match(token)
            if m:
                f.house_number, f.house_suffix = m.group(1), m.group(2) or None
                rest = first[start + 1:]
                comps = ([rest] if rest else []) + comps[1:]
            elif HOUSE_RANGE.match(token):
                f.house_range = token
                rest = first[start + 1:]
                comps = ([rest] if rest else []) + comps[1:]

        if comps:
            f.street = " ".join(comps[0])
            core = [t for t in comps[0] if t not in GENERIC_BUILDING_WORDS]
            f.street_core = " ".join(core) or None
            f.locality_parts = [" ".join(c) for c in comps[1:]]
        if f.unit_id:
            f.primary_number = f.unit_id
        elif f.house_number:
            f.primary_number = f.house_number + (f.house_suffix or "")

        self._map_locality(f, comps)
        return f

    # -- locality ------------------------------------------------------------------

    def _lookup(self, county: str, text: str, target_type: str) -> list[tuple]:
        return [t for t in self.aliases.get((county, text), []) if t[0] == target_type]

    def _set_bua(self, f: AddressFeatures, bua_id: str, method: str, confidence: str) -> None:
        f.locality_id, f.locality_name = bua_id, self.bua_names[bua_id]
        f.locality_method, f.locality_confidence, f.ambiguity_state = method, confidence, "unique"

    def _bua_match(self, county: str, tokens: list[str]) -> tuple[set[str], str, str] | None:
        """BUA targets for one component: exact alias, else a trailing n-gram."""
        while len(tokens) > 1 and tokens[-1] in GENERIC_LOCALITY_WORDS:
            tokens = tokens[:-1]  # "Letterkenny Town Centre" -> "Letterkenny"
        texts = [(" ".join(tokens), "bua_alias")]
        if len(tokens) > 1:
            texts += [(" ".join(tokens[-k:]), "bua_alias_suffix")
                      for k in (3, 2, 1) if k < len(tokens)]
        for text, method in texts:
            hits = self._lookup(county, text, "bua")
            if hits:
                return {h[1] for h in hits}, method, hits[0][2]
        return None

    def _map_locality(self, f: AddressFeatures, comps: list[list[str]]) -> None:
        """Assign the most specific supported locality.

        Components are read from most specific (street, then left to right) to most
        general, because rural addresses end with a post town that the property is not
        in ("Magherablade, Creeslough, Letterkenny"). An unnumbered address naming a
        townland outside every BUA footprint is rural: county level only.
        """
        if not f.county:
            f.ambiguity_state = "no_county"
            return
        bua_hit, bua_index = None, None
        for index, tokens in enumerate(comps):
            match = self._bua_match(f.county, tokens)
            if match:
                bua_hit, bua_index = match, index
                break

        unnumbered = f.primary_number is None and f.house_range is None
        townland_state = None
        for tokens in comps[: bua_index if bua_index is not None else len(comps)]:
            targets = {h[1] for h in self._lookup(f.county, " ".join(tokens), "townland")}
            if len(targets) > 1:
                townland_state = townland_state or "ambiguous_townland"
                continue
            if len(targets) == 1:
                f.townland_id = next(iter(targets))
                # A townland near a town is not assigned to it: the circular footprint
                # agreed with Eircode routing keys only 53% of the time (quality report).
                if unnumbered and self.townland_bua.get(f.townland_id) is None:
                    f.ambiguity_state = "rural_townland"
                    return
                break

        if bua_hit:
            targets, method, origin = bua_hit
            if len(targets) > 1:
                f.ambiguity_state = "ambiguous_bua"
                return
            if comps[bua_index] == [comparison_form(f.county)]:
                # Only evidence is a bare county name that is also a town name.
                self._set_bua(f, next(iter(targets)), "county_name_as_town", "low")
                return
            if origin == "townland_alias_of_bua":
                confidence = "low"  # 62% routing-key agreement
            elif method == "bua_alias":
                confidence = "high"  # official CSO name: 99% agreement
            else:
                confidence = "medium"  # trailing n-gram: 91% agreement
            self._set_bua(f, next(iter(targets)), method, confidence)
            return

        if f.postal_district and f.county == "Dublin" and self.dublin_bua:
            self._set_bua(f, self.dublin_bua, "postal_district", "high")
            return
        if f.townland_id:
            f.ambiguity_state = "unique_townland"
        elif townland_state:
            f.ambiguity_state = townland_state


def townland_bua_map(reference: Reference, min_radius_m: float = 1500.0) -> dict[str, str]:
    """Townland -> BUA when the townland point lies within the BUA's approximate footprint
    (a circle of the BUA's area, at least 1.5 km) in a county the BUA belongs to."""
    import math

    import numpy as np

    td = reference.townlands
    best: dict[str, tuple[float, str]] = {}
    tx, ty = td.itm_e.to_numpy(), td.itm_n.to_numpy()
    for bua in reference.buas.itertuples(index=False):
        radius = max(min_radius_m, math.sqrt(max(bua.area, 0.0) / math.pi))
        d2 = (tx - bua.itm_e) ** 2 + (ty - bua.itm_n) ** 2
        inside = np.flatnonzero(
            (d2 <= radius ** 2) & td.county.isin(reference.counties_by_bua[bua.bua_id]))
        for i in inside:
            tid = td.townland_id.iat[i]
            # Normalised distance, so a townland near a small town's centre beats the edge
            # of a large town's circle.
            score = d2[i] / radius ** 2
            if tid not in best or score < best[tid][0]:
                best[tid] = (score, bua.bua_id)
    return {tid: bua for tid, (_, bua) in best.items()}


ADDRESS_COLUMNS = [
    "snapshot_id", "raw_row_id", "address_version", "sale_id", "address_display",
    "address_norm", "components_json", "unit_type", "unit_id", "house_number", "house_suffix",
    "house_range", "primary_number", "street", "street_core", "locality_parts_json",
    "postal_district", "county", "locality_id", "locality_name", "locality_method",
    "locality_confidence", "ambiguity_state", "townland_id", "run_id",
]


def normalise_addresses(sales: pd.DataFrame, normaliser: AddressNormaliser,
                        run: RunContext) -> pd.DataFrame:
    """Address features for every parsed sale occurrence (one row per occurrence)."""
    cache: dict[tuple, dict] = {}
    rows = []
    for rec in sales[["snapshot_id", "raw_row_id", "sale_id", "address_raw",
                      "county"]].itertuples(index=False):
        key = (rec.address_raw, rec.county)
        if key not in cache:
            cache[key] = asdict(normaliser(rec.address_raw, rec.county))
        feat = cache[key]
        rows.append({
            "snapshot_id": rec.snapshot_id, "raw_row_id": rec.raw_row_id,
            "address_version": normaliser.version, "sale_id": rec.sale_id,
            **{k: v for k, v in feat.items() if k not in ("components", "locality_parts")},
            "components_json": json.dumps(feat["components"], ensure_ascii=False),
            "locality_parts_json": json.dumps(feat["locality_parts"], ensure_ascii=False),
            "run_id": run.run_id,
        })
    return pd.DataFrame(rows)[ADDRESS_COLUMNS]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def reference_tables(reference: Reference) -> tuple[pd.DataFrame, pd.DataFrame]:
    from ireland_rsi.reference import LICENCE

    aliases = reference.aliases.assign(reference_version=reference.version, licence=LICENCE)
    localities = pd.DataFrame([
        (reference.version, bua.bua_id, county, bua.name, bua.population_2022, bua.itm_e,
         bua.itm_n, len(reference.counties_by_bua[bua.bua_id]), LICENCE)
        for bua in reference.buas.itertuples(index=False)
        for county in reference.counties_by_bua[bua.bua_id]
    ], columns=["reference_version", "bua_id", "county", "name", "population_2022", "itm_e",
                "itm_n", "n_counties", "licence"])
    return aliases[["reference_version", "county", "alias_norm", "alias", "target_type",
                    "target_id", "language", "origin", "n_targets", "licence"]], localities


def coverage_summary(features: pd.DataFrame) -> dict:
    mapped = features.locality_id.notna()
    by_county = features.assign(mapped=mapped).groupby("county").mapped.mean()
    return {
        "occurrences": len(features),
        "mapped_to_bua": int(mapped.sum()),
        "mapped_share": round(float(mapped.mean()), 4),
        "by_method": features.locality_method.value_counts().to_dict(),
        "by_state": features.ambiguity_state.value_counts().to_dict(),
        "mapped_share_by_county": {k: round(float(v), 3) for k, v in by_county.items()},
        "with_unit": int(features.unit_id.notna().sum()),
        "with_primary_number": int(features.primary_number.notna().sum()),
    }


def routing_key_validation(features: pd.DataFrame, eircodes: pd.DataFrame,
                           min_key_share: float = 0.05) -> dict:
    """Independent check of locality assignment against Eircode routing keys.

    For each BUA, valid routing keys are those covering at least `min_key_share` of its
    exact-official-name (high confidence) assignments. Every assignment with a valid
    Eircode is scored by whether its key is in that set, by method and confidence. Dublin
    postal districts are checked exactly ("D8" <-> "D08"). Eircodes exist mainly from
    2021, so this measures recent sales only.
    """
    j = features.merge(eircodes, on=["snapshot_id", "raw_row_id"])
    j = j[j.eircode.notna()].assign(rk=lambda d: d.eircode.str[:3])
    exact = j[(j.locality_method == "bua_alias") & (j.locality_confidence == "high")]
    counts = exact.groupby(["locality_id", "rk"]).size()
    shares = counts / counts.groupby(level=0).transform("sum")
    valid = set(shares[shares >= min_key_share].index)
    mapped = j[j.locality_id.notna()].copy()
    mapped["agrees"] = [(loc, rk) in valid for loc, rk in zip(mapped.locality_id, mapped.rk,
                                                               strict=True)]
    mapped["dublin"] = mapped.locality_name.str.startswith("Dublin city")
    by_method = (mapped.groupby(["locality_method", "locality_confidence", "dublin"])
                 .agrees.agg(["size", "mean"]).reset_index())
    pd_rows = j[j.postal_district.notna()]
    expected = "D" + pd_rows.postal_district.str[1:].str.zfill(2).where(
        ~pd_rows.postal_district.str.endswith("W"), pd_rows.postal_district.str[1:])
    return {
        "rows_with_eircode": len(j),
        "agreement_by_method": [
            {"method": r.locality_method, "confidence": r.locality_confidence,
             "dublin_city": bool(r.dublin), "rows": int(r["size"]),
             "agreement": round(float(r["mean"]), 3)}
            for _, r in by_method.iterrows()],
        "postal_district_exact": {"rows": len(pd_rows),
                                  "agreement": round(float((pd_rows.rk == expected).mean()), 3)
                                  if len(pd_rows) else None},
    }


def run_addresses(tables, objects, scope: str, counties: list[str], run: RunContext,
                  fetcher=None) -> dict:
    """Acquire reference data, store gazetteer tables, and featurise the active snapshot."""
    from ireland_rsi.ingest import active_snapshot, fetch
    from ireland_rsi.reference import acquire_reference, load_reference

    acquisition = acquire_reference(objects, fetcher=fetcher or fetch)
    reference = load_reference(objects, acquisition)
    aliases, localities = reference_tables(reference)
    counts = {
        "aliases_inserted": tables.insert_new("silver.place_aliases", aliases),
        "localities_inserted": tables.insert_new("silver.localities", localities),
    }
    snapshot = active_snapshot(tables, scope)
    sales = tables.query(
        "SELECT DISTINCT snapshot_id, raw_row_id, sale_id, address_raw, county "
        f"FROM {tables.qualify('silver.sales')} WHERE snapshot_id = '{snapshot}'")
    normaliser = AddressNormaliser(reference, counties)
    features = normalise_addresses(sales, normaliser, run)
    counts["features_inserted"] = tables.insert_new("silver.address_features", features)
    from datetime import UTC, datetime

    from ireland_rsi.storage import record_stage

    record_stage(tables, "addresses", snapshot, normaliser.version, run.run_id,
                 datetime.now(UTC))
    eircodes = tables.query(
        "SELECT DISTINCT snapshot_id, raw_row_id, eircode "
        f"FROM {tables.qualify('silver.sales')} "
        f"WHERE snapshot_id = '{snapshot}' AND eircode_valid")
    return {"snapshot_id": snapshot, "reference_acquisition": acquisition["acquisition_id"],
            "reference_version": reference.version, "address_version": normaliser.version,
            "row_counts": counts, "coverage": coverage_summary(features),
            "validation": routing_key_validation(features, eircodes)}
