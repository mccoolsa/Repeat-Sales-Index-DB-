"""Constrained property histories."""

from datetime import date
from pathlib import Path

import pandas as pd

from ireland_rsi.entities import ConstrainedClusters, EntityConfig, crosswalk, resolve_entities

ROOT = Path(__file__).resolve().parents[1]
CFG = EntityConfig.load(ROOT / "config" / "entities.yml")


def sales(*rows):
    return pd.DataFrame(rows, columns=["sale_id", "sale_date", "primary_number", "eircode",
                                       "price_key"])


def edges(*rows):
    return pd.DataFrame(rows, columns=["sale_id_l", "sale_id_r", "match_weight",
                                       "match_probability"])


def groups(membership):
    return sorted(sorted(g.sale_id) for _, g in membership.groupby("property_id"))


def test_transitive_apartment_conflict_does_not_merge():
    s = sales(("a", date(2012, 1, 1), "4", None, 100), ("b", date(2015, 1, 1), None, None, 120),
              ("c", date(2019, 1, 1), "14", None, 150))
    # a-b and b-c are both strong; plain components would merge apartment 4 with 14.
    e = edges(("a", "b", 12.0, 0.99), ("b", "c", 10.0, 0.98))
    membership, rejected = resolve_entities(s, e, CFG, "v1")
    assert groups(membership) == [["a", "b"], ["c"]]
    assert rejected.reason.tolist() == ["unit_or_number_conflict"]


def test_strongest_edges_win_conflicts():
    s = sales(("a", date(2012, 1, 1), "4", None, 100), ("b", date(2015, 1, 1), None, None, 120),
              ("c", date(2019, 1, 1), "14", None, 150))
    e = edges(("a", "b", 8.0, 0.9), ("b", "c", 11.0, 0.99))
    membership, _ = resolve_entities(s, e, CFG, "v1")
    assert groups(membership) == [["a"], ["b", "c"]]


def test_eircode_and_same_day_price_conflicts_are_refused():
    s = sales(("a", date(2021, 1, 1), "1", "F92AC4D", 100),
              ("b", date(2023, 1, 1), "1", "F92XY12", 120),
              ("c", date(2021, 1, 1), "1", None, 101))
    e = edges(("a", "b", 9.0, 0.95), ("a", "c", 9.0, 0.95))
    _, rejected = resolve_entities(s, e, CFG, "v1")
    assert set(rejected.reason) == {"eircode_conflict", "same_day_price_conflict"}


def test_duplicate_filing_on_the_same_day_and_price_is_allowed():
    s = sales(("a", date(2021, 1, 1), "1", None, 100), ("b", date(2021, 1, 1), "1", None, 100))
    membership, rejected = resolve_entities(s, edges(("a", "b", 9.0, 0.95)), CFG, "v1")
    assert groups(membership) == [["a", "b"]] and rejected.empty


def test_implausibly_large_property_is_capped():
    n = CFG.raw["constraints"]["max_sales_per_property"] + 3
    s = sales(*[(f"s{i:02d}", date(2010 + i % 16, 1 + i % 12, 1), "7", None, 100 + i)
                for i in range(n)])
    e = edges(*[(f"s{i:02d}", f"s{i + 1:02d}", 9.0, 0.95) for i in range(n - 1)])
    membership, rejected = resolve_entities(s, e, CFG, "v1")
    assert membership.groupby("property_id").size().max() <= n - 3
    assert (rejected.reason == "too_many_sales").all() and len(rejected) >= 1


def test_property_id_is_named_after_earliest_sale_and_stays_when_later_sales_join():
    s1 = sales(("a", date(2012, 1, 1), "1", None, 100), ("b", date(2016, 1, 1), "1", None, 130))
    m1, _ = resolve_entities(s1, edges(("a", "b", 9.0, 0.95)), CFG, "v1")
    s2 = pd.concat([s1, sales(("c", date(2024, 1, 1), "1", None, 200))], ignore_index=True)
    m2, _ = resolve_entities(s2, edges(("a", "b", 9.0, 0.95), ("b", "c", 9.0, 0.95)), CFG, "v2")
    assert m1.property_id.nunique() == 1 and m2.property_id.nunique() == 1
    assert m1.property_id.iloc[0] == m2.property_id.iloc[0]


def test_crosswalk_labels_merges_and_splits():
    old = pd.DataFrame({"property_id": ["p1", "p1", "p2", "p3"], "sale_id": ["a", "b", "c", "d"]})
    new = pd.DataFrame({"property_id": ["q1", "q2", "q2", "q2"], "sale_id": ["a", "b", "c", "d"]})
    cw = crosswalk(old, new, "v1", "v2")
    rel = dict(zip(zip(cw.property_id_old, cw.property_id_new, strict=True), cw.relation,
                   strict=True))
    assert rel[("p1", "q1")] == "split"
    assert rel[("p2", "q2")] == "merge" and rel[("p1", "q2")] == "merge_and_split"


def test_cluster_union_is_idempotent():
    s = sales(("a", date(2012, 1, 1), "1", None, 100), ("b", date(2016, 1, 1), "1", None, 130))
    c = ConstrainedClusters(s, CFG.raw["constraints"])
    assert c.try_union("a", "b") is None and c.try_union("a", "b") is None
    assert len(c.clusters) == 1


def test_non_specific_rural_addresses_link_only_on_a_shared_eircode():
    s = sales(("a", date(2014, 7, 1), None, None, 150), ("b", date(2016, 10, 1), None, None, 30),
              ("c", date(2022, 1, 1), None, "F92AC4D", 240),
              ("d", date(2024, 6, 1), None, "F92AC4D", 390))
    s["address_specific"] = False          # e.g. "BALLYNESS, FALCARRAGH" on every sale
    e = edges(("a", "b", 9.0, 0.9), ("c", "d", 9.0, 0.9), ("b", "c", 9.0, 0.9))
    membership, rejected = resolve_entities(s, e, CFG, "v1")
    assert groups(membership) == [["a"], ["b"], ["c", "d"]]
    assert set(rejected.reason) == {"non_specific_address"} and len(rejected) == 2
