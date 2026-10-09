"""Address normalisation, gazetteer construction and locality mapping.

Uses a synthetic gazetteer (fake ArcGIS pages, invented coordinates) so results are exact
and need no network access.
"""

import json
import math
from types import SimpleNamespace

import pandas as pd
import pytest

from ireland_rsi.addresses import AddressNormaliser, display_form, normalise_addresses
from ireland_rsi.contracts import RunContext
from ireland_rsi.reference import (
    acquire_reference,
    build_reference,
    comparison_form,
    fetch_arcgis_pages,
)
from ireland_rsi.storage import ObjectStore

COUNTIES = ["Donegal", "Dublin", "Wexford", "Louth", "Meath"]


def townland(i, county, en, ga=" ", x=0.0, y=0.0):
    return {"attributes": {"OBJECTID_1": i, "ID": i, "County": county.upper(),
                           "English_Na": en, "Irish_Name": ga, "Alternativ": " ",
                           "Gaeltacht": "N", "ITM_E": x, "ITM_N": y}}


def bua(code, name, x, y, radius_m=1500.0, pop=1000):
    return {"attributes": {"BUA_GUID": f"g{code}", "BUA_CODE": code, "BUA_NAME": name,
                           "T1_1AGETT_Norm": pop, "AREA": math.pi * radius_m ** 2,
                           "AREA_UOM": "Sq Metres"},
            "centroid": {"x": x, "y": y}}


TOWNLANDS = [
    townland(1, "Donegal", "LETTERKENNY", "Leitir Ceanainn", 600_100, 900_100),
    townland(2, "Donegal", "WOODHILL", x=600_500, y=900_500),            # inside Letterkenny
    townland(3, "Donegal", "BOMANY", x=606_000, y=906_000),              # rural
    townland(4, "Donegal", "GLEBE", x=640_000, y=940_000),               # same name twice
    townland(5, "Donegal", "GLEBE", x=650_000, y=950_000),
    townland(6, "Donegal", "RAMELTON", x=610_000, y=920_000),
    townland(7, "Wexford", "BALLYNEW", x=700_000, y=600_000),
    townland(8, "Wexford", "BALLYOLD", x=720_000, y=600_000),
    townland(9, "Dublin", "CHURCHTOWN", x=716_000, y=736_000),
    townland(10, "Louth", "DROGHEDA", x=800_000, y=775_000),
    townland(11, "Meath", "COLPE", x=800_000, y=773_000),                 # across the border
]
BUAS = [
    bua("1", "Letterkenny", 600_000, 900_000, radius_m=2000),
    bua("2", "Ramelton", 610_000, 920_000),
    bua("3", "Newtown", 700_000, 600_000),
    bua("4", "Newtown", 720_000, 600_000),
    bua("5", "Dublin city and suburbs", 715_000, 735_000, radius_m=10_000),
    bua("6", "Drogheda", 800_000, 774_000, radius_m=3000),
    bua("7", "Wexford", 702_000, 601_000),                 # same name as its county
]


def pages(features):
    return [json.dumps({"features": features}).encode()]


@pytest.fixture(scope="module")
def reference():
    return build_reference(pages(TOWNLANDS), pages(BUAS))


@pytest.fixture(scope="module")
def norm(reference):
    return AddressNormaliser(reference, COUNTIES)


# --- gazetteer --------------------------------------------------------------------


def test_comparison_form_is_accent_and_case_insensitive():
    assert comparison_form("Bóthar Mór, St. Ann’s") == comparison_form("BOTHAR MOR ST ANNS")


def test_irish_and_english_names_alias_the_same_town(reference):
    a = reference.aliases
    letterkenny = a[(a.county == "Donegal") & (a.target_type == "bua")]
    assert set(letterkenny[letterkenny.target_id == "bua:1"].alias_norm) >= {
        "letterkenny", "leitir ceanainn"}


def test_border_town_belongs_to_both_counties(reference):
    assert reference.counties_by_bua["bua:6"] == ["Louth", "Meath"]


def test_same_named_towns_in_one_county_are_marked_ambiguous(reference):
    a = reference.aliases
    newtown = a[(a.alias_norm == "newtown") & (a.target_type == "bua")]
    assert set(newtown.target_id) == {"bua:3", "bua:4"} and (newtown.n_targets == 2).all()


def test_reference_version_depends_on_content_only():
    one = build_reference(pages(TOWNLANDS), pages(BUAS))
    two = build_reference(pages(list(TOWNLANDS)), pages(list(BUAS)))
    changed = build_reference(pages(TOWNLANDS[:-1]), pages(BUAS))
    assert one.version == two.version != changed.version


def test_arcgis_pagination_and_idempotent_acquisition(tmp_path):
    served = []

    def fetcher(url):
        served.append(url)
        if "Townlands" in url:
            offset = int(url.split("resultOffset=")[1].split("&")[0])
            feats = TOWNLANDS[offset: offset + 4]
            body = {"features": feats, "exceededTransferLimit": offset + 4 < len(TOWNLANDS)}
        else:
            body = {"features": BUAS}
        return SimpleNamespace(body=json.dumps(body).encode())

    got = fetch_arcgis_pages("https://x.test/Townlands/query", {}, page_size=4,
                             fetcher=fetcher)
    assert sum(len(json.loads(p)["features"]) for p in got) == len(TOWNLANDS)
    objects = ObjectStore(tmp_path)
    first = acquire_reference(objects, fetcher=fetcher)
    second = acquire_reference(objects, fetcher=fetcher)
    assert first == second  # identical content keeps the first-seen record


# --- golden fixtures -----------------------------------------------------------------


def test_golden_apartment_pair_has_a_common_unit_interpretation(norm):
    a = norm("Apt 4, The Grove, Letterkenny", "Donegal")
    b = norm("4 The Grove Apartments, Co. Donegal", "Donegal")
    assert (a.unit_type, a.unit_id, a.house_number) == ("apartment", "4", None)
    assert (b.unit_type, b.unit_id, b.house_number) == (None, None, "4")
    assert a.primary_number == b.primary_number == "4"
    assert a.street_core == b.street_core == "grove"
    # Shared features, not an identity claim: localities differ in specificity.
    assert a.locality_name == "Letterkenny" and b.locality_id is None


@pytest.mark.parametrize("left,right", [
    ("Apartment 4, The Grove, Letterkenny", "Apartment 14, The Grove, Letterkenny"),
    ("12 Main Street, Letterkenny", "12A Main Street, Letterkenny"),
    ("1 The Grove, Letterkenny", "1 The Grove, Ramelton"),        # repeated estate name
])
def test_distinctions_survive(norm, left, right):
    a, b = norm(left, "Donegal"), norm(right, "Donegal")
    assert (a.primary_number, a.street_core, a.locality_id) != (
        b.primary_number, b.street_core, b.locality_id)


def test_unit_number_is_not_a_street_number(norm):
    f = norm("Apt 4, 12 Main St, Letterkenny", "Donegal")
    assert (f.unit_id, f.house_number, f.primary_number) == ("4", "12", "4")
    assert f.street == "main street"


@pytest.mark.parametrize("raw,unit,house,rng,street", [
    ("FLAT C, 81 HOLLYBANK ROAD, DUBLIN 9", "c", "81", None, "hollybank road"),
    ("APT D16 ST ANGELAS, LETTERKENNY", "d16", None, None, "saint angelas"),
    ("APT 10, 18-21 MOUNT ST, DUBLIN 2", "10", None, "18-21", "mount street"),
    ("No 3 The Old Creamery, Letterkenny", None, "3", None, "the old creamery"),
    ("5 St Margaret's Rd, Letterkenny", None, "5", None, "saint margarets road"),
])
def test_structured_components(norm, raw, unit, house, rng, street):
    county = "Dublin" if "DUBLIN" in raw else "Donegal"
    f = norm(raw, county)
    assert (f.unit_id, f.house_number, f.house_range, f.street) == (unit, house, rng, street)


def test_punctuation_and_case_do_not_change_features(norm):
    a = norm("11 Glendale Manor, Letterkenny", "Donegal")
    b = norm("11,  GLENDALE MANOR., LETTERKENNY.", "Donegal")
    assert (a.house_number, a.street_core, a.locality_id) == (
        b.house_number, b.street_core, b.locality_id)


def test_irish_accents_and_irish_town_names(norm):
    a = norm("1 Bóthar Mór, Leitir Ceanainn", "Donegal")
    b = norm("1 Bothar Mor, Leitir Ceanainn", "Donegal")
    assert a.street == b.street == "bothar mor"
    assert a.locality_name == "Letterkenny" and a.locality_confidence == "low"


# --- locality mapping -------------------------------------------------------------------


def test_town_address_maps_with_high_confidence(norm):
    f = norm("11 GLENDALE MANOR, LETTERKENNY, DONEGAL", "Donegal")
    assert (f.locality_id, f.locality_method, f.locality_confidence) == (
        "bua:1", "bua_alias", "high")


def test_post_town_does_not_capture_a_more_specific_place(norm):
    f = norm("Magherablade, Ramelton, Letterkenny", "Donegal")
    assert f.locality_name == "Ramelton"


def test_unnumbered_rural_townland_is_county_level_only(norm):
    f = norm("BOMANY, LETTERKENNY, CO DONEGAL", "Donegal")
    assert f.locality_id is None and f.ambiguity_state == "rural_townland"
    assert f.townland_id == "td:3"


def test_townland_near_a_town_is_not_assigned_to_it(norm):
    # The circular footprint proved unreliable against Eircode routing keys (53%).
    f = norm("WOODHILL, CO DONEGAL", "Donegal")
    assert f.locality_id is None and f.ambiguity_state == "unique_townland"
    assert f.townland_id == "td:2"


def test_ambiguous_names_are_not_forced(norm):
    town = norm("1 Main Street, Newtown", "Wexford")
    assert town.locality_id is None and town.ambiguity_state == "ambiguous_bua"
    td = norm("GLEBE, CO DONEGAL", "Donegal")
    assert td.locality_id is None and td.ambiguity_state == "ambiguous_townland"


def test_missing_locality_falls_back_to_county(norm):
    f = norm("4 The Grove Apartments, Co. Donegal", "Donegal")
    assert (f.locality_id, f.locality_method, f.county) == (None, "county_only", "Donegal")


def test_dublin_postal_district(norm):
    f = norm("FLAT C, 81 HOLLYBANK ROAD, DUBLIN 9", "Dublin")
    assert f.postal_district == "D9" and f.locality_method == "postal_district"


def test_border_town_maps_from_either_county(norm):
    assert norm("1 Main St, Drogheda", "Louth").locality_id == "bua:6"
    assert norm("1 Main St, Drogheda", "Meath").locality_id == "bua:6"
    assert norm("1 Main St, Drogheda", "Donegal").locality_id is None


def test_town_name_inside_a_street_name_is_not_a_locality(norm):
    f = norm("2 Letterkenny Road, Ramelton", "Donegal")
    assert f.street == "letterkenny road" and f.locality_name == "Ramelton"


# --- determinism and source preservation ----------------------------------------------------


def test_normalisation_is_deterministic_and_idempotent(norm):
    raw = "Apt 2,  Block B &amp; C, Main St., LETTERKENNY"
    first, second = norm(raw, "Donegal"), norm(raw, "Donegal")
    assert first == second
    assert first.address_display == "Apt 2, Block B & C, Main St., LETTERKENNY"
    again = norm(first.address_display, "Donegal")
    assert (again.unit_id, again.street, again.locality_id) == (
        first.unit_id, first.street, first.locality_id)


def test_display_form_never_rewrites_content():
    assert display_form("  12A  Main St ,Letterkenny ") == "12A Main St,Letterkenny"


def test_featurising_does_not_modify_source_rows(norm):
    sales = pd.DataFrame({"snapshot_id": ["s"], "raw_row_id": ["r"], "sale_id": ["x"],
                          "address_raw": ["Apt 4, The Grove, Letterkenny"],
                          "county": ["Donegal"]})
    before = sales.copy()
    out = normalise_addresses(sales, norm, RunContext.new(["s"], "c", "k", 0))
    pd.testing.assert_frame_equal(sales, before)
    assert out.address_version.iloc[0] == norm.version
    assert json.loads(out.components_json.iloc[0]) == ["apartment 4", "the grove", "letterkenny"]


@pytest.mark.parametrize("raw,locality,method,confidence", [
    ("12 Main St, Wexford", "bua:7", "county_name_as_town", "low"),
    ("12 Main St, Co. Wexford", None, "county_only", "none"),
    ("12 Main St, Newtown, Wexford", None, "county_only", "none"),   # Newtown ambiguous
])
def test_bare_county_name_that_is_also_a_town(norm, raw, locality, method, confidence):
    f = norm(raw, "Wexford")
    assert (f.locality_id, f.locality_method, f.locality_confidence) == (
        locality, method, confidence)


def test_bare_county_name_that_is_not_a_town_is_dropped(norm):
    f = norm("11 Glendale Manor, Letterkenny, Donegal", "Donegal")
    assert f.locality_parts == ["letterkenny"] and f.locality_id == "bua:1"


def test_routing_key_validation_scores_assignments(norm):
    from ireland_rsi.addresses import routing_key_validation

    rows = [("a", "11 Glendale Manor, Letterkenny", "F92ABCD"),
            ("b", "12 Glendale Manor, Letterkenny", "F92ABCE"),
            ("c", "1 Main Street Letterkenny", "F92ABCF"),       # suffix match, agrees
            ("d", "2 Main Street Letterkenny", "F93ABCD"),       # suffix match, disagrees
            ("e", "FLAT C, 81 HOLLYBANK ROAD, DUBLIN 9", "D09XY12")]
    sales = pd.DataFrame({"snapshot_id": "s", "raw_row_id": [r[0] for r in rows],
                          "sale_id": [r[0] for r in rows], "address_raw": [r[1] for r in rows],
                          "county": ["Donegal"] * 4 + ["Dublin"]})
    features = normalise_addresses(sales, norm, RunContext.new(["s"], "c", "k", 0))
    eircodes = pd.DataFrame({"snapshot_id": "s", "raw_row_id": [r[0] for r in rows],
                             "eircode": [r[2] for r in rows]})
    report = routing_key_validation(features, eircodes)
    by = {(m["method"], m["confidence"]): m for m in report["agreement_by_method"]}
    assert by[("bua_alias", "high")]["agreement"] == 1.0
    assert by[("bua_alias_suffix", "medium")] == {
        "method": "bua_alias_suffix", "confidence": "medium", "dublin_city": False,
        "rows": 2, "agreement": 0.5}
    assert report["postal_district_exact"] == {"rows": 1, "agreement": 1.0}


def test_reference_version_ignores_missing_value_representation():
    from ireland_rsi.reference import _canonical_records

    with_nan = pd.DataFrame({"name_ga": ["Leitir Ceanainn", float("nan")], "x": [1.0, 2.0]})
    with_none = pd.DataFrame({"name_ga": ["Leitir Ceanainn", None], "x": [1.0, 2.0]})
    assert _canonical_records(with_nan) == _canonical_records(with_none)


@pytest.mark.parametrize("raw,unit_type,unit", [
    ("APPARTMENT 2, 24 MARLBOROUGH STREET, DUBLIN 1", "apartment", "2"),
    ("Site 5, Glen Road, Letterkenny", "site", "5"),
    ("House 2, The Paddocks, Letterkenny", "house", "2"),
    ("Duplex 3, Main Street, Letterkenny", "duplex", "3"),
])
def test_less_common_unit_words_and_misspellings(norm, raw, unit_type, unit):
    county = "Dublin" if "DUBLIN" in raw else "Donegal"
    f = norm(raw, county)
    assert (f.unit_type, f.unit_id, f.primary_number) == (unit_type, unit, unit)
