"""Candidate generation, rule bookkeeping, baseline and Splink scoring."""

import numpy as np
import pandas as pd
import pytest

from ireland_rsi.linkage import (
    BLOCKING_RULES,
    blocking_report,
    linkage_input,
    rule_hit_sql,
    rules_for,
    score_candidates,
    street_key,
    train,
)


def rec(i, street, number=None, unit=None, locality="bua:1", townland=None, county="Donegal",
        eircode=None, second=None, price=None):
    norm = " ".join(x for x in [f"apartment {unit}" if unit else None, number, street,
                                second, "letterkenny" if locality else None] if x)
    return {"sale_id": f"s{i:04d}", "county": county, "address_norm": norm,
            "street_core": street, "primary_number": unit or number, "unit_id": unit,
            "locality_id": locality, "townland_id": townland, "postal_district": None,
            "eircode": eircode, "second_part": second, "price": price}


def synthetic(n_streets=40, seed=1):
    """Repeat sales of houses on many streets, plus apartment blocks and rural homes."""
    rng = np.random.default_rng(seed)
    rows, i = [], 0
    for s in range(n_streets):
        street = f"street{s} park"
        for number in range(1, 7):
            for _ in range(int(rng.integers(1, 3))):     # 1-2 sales per house
                rows.append(rec(i, street, str(number), price=int(rng.integers(1e5, 4e5))))
                i += 1
    for b in range(10):                                   # apartment blocks
        for unit in range(1, 6):
            for _ in range(2):
                rows.append(rec(i, f"block{b} court", unit=str(unit)))
                i += 1
    for t in range(30):                                   # rural, unnumbered
        for _ in range(2):
            rows.append(rec(i, f"farm{t}", locality=None, townland=f"td:{t}"))
            i += 1
    return pd.DataFrame(rows)


def test_street_key_skips_generic_prefixes():
    assert street_key("the old creamery") == "old"
    assert street_key("lower kimmage road") == "kimmage"
    assert street_key(None) is None


def test_address_only_track_has_no_eircode_or_price():
    frame = synthetic().assign(eircode="F92AC4D")
    addr = linkage_input(frame, "address_only")
    prod = linkage_input(frame, "production")
    assert "eircode" not in addr.columns and "eircode" in prod.columns
    assert "price" not in addr.columns and "price" not in prod.columns
    assert not any(r.name == "recovery_eircode_any_county" for r in rules_for("address_only"))


def test_rule_sql_rewrites_to_prediction_columns():
    rule = next(r for r in BLOCKING_RULES if r.name == "number_street")
    assert rule_hit_sql(rule) == ("county_l = county_r AND street_core_l = street_core_r "
                                  "AND primary_number_l = primary_number_r")


@pytest.fixture(scope="module")
def scored():
    frame = synthetic()
    extra = pd.DataFrame([
        rec(9001, "glendale manor", "11", locality=None),      # missing town
        rec(9002, "glendale", "11"),
        rec(9003, "block0 court", unit="9"),                   # unit conflict pair below
        rec(9004, "block0 court", unit="19"),
    ])
    frame = pd.concat([frame, extra], ignore_index=True)
    df = linkage_input(frame, "address_only")
    model = train(df, "address_only", u_max_pairs=1e5, em_max_pairs=1e5)
    return frame, df, model, score_candidates(model)


def pair(preds, a, b):
    lo, hi = sorted([a, b])
    return preds[(preds.unique_id_l == lo) & (preds.unique_id_r == hi)]


def test_union_has_no_duplicate_edges_and_records_every_rule(scored):
    _, _, _, preds = scored
    assert not preds.duplicated(["unique_id_l", "unique_id_r"]).any()
    multi = preds[preds.rules_hit.str.contains(",")]
    assert len(multi) > 0  # exact-address pairs also satisfy number_street, etc.
    assert multi.rules_hit.str.contains("exact_address").any()


def test_missing_town_positive_enters_through_recovery_block(scored):
    _, _, _, preds = scored
    got = pair(preds, "s9001", "s9002")
    assert len(got) == 1 and "recovery_street_key_number" in got.rules_hit.item()


def test_unit_conflict_is_evidence_against_and_same_unit_for(scored):
    import math

    _, _, model, preds = scored
    unit = next(c for c in model.settings_json["comparisons"]
                if c["output_column_name"] == "unit_id")
    # Levels never observed in the synthetic data carry no m-probability; skip them.
    weights = {lvl["label_for_charts"]: math.log2(lvl["m_probability"] / lvl["u_probability"])
               for lvl in unit["comparison_levels"]
               if not lvl.get("is_null_level") and lvl.get("m_probability")
               and lvl.get("u_probability")}
    assert weights["Exact match on unit_id"] > 3
    assert weights["All other comparisons"] < 0
    # Different units are never generated as candidates: the unit is the primary number.
    assert pair(preds, "s9003", "s9004").empty
    same_unit = preds[(preds.unit_id_l == preds.unit_id_r) & preds.unit_id_l.notna()]
    assert same_unit.match_probability.median() > 0.5


def test_identity_scores_do_not_depend_on_price(scored):
    frame, df, model, preds = scored
    shuffled = frame.assign(price=frame.price.sample(frac=1, random_state=3).to_numpy())
    again = linkage_input(shuffled, "address_only")
    pd.testing.assert_frame_equal(df, again)  # price never reaches the model input


def test_baseline_links_identical_addresses(scored):
    _, _, _, preds = scored
    same_text = preds[preds.address_norm_l == preds.address_norm_r]
    assert same_text.baseline_link.all()


def test_blocking_report_counts_and_reduction():
    df = linkage_input(synthetic(), "address_only")
    report = blocking_report(df, "address_only")
    assert report["records"] == len(df)
    assert report["candidate_pairs"] == report["rules"][-1]["cumulative"]
    assert report["reduction_ratio"] >= 1
    assert sum(r["marginal"] for r in report["rules"]) == report["candidate_pairs"]


def test_model_version_is_reproducible():
    df = linkage_input(synthetic(n_streets=15), "address_only")
    one = train(df, "address_only", u_max_pairs=1e5, em_max_pairs=1e5)
    two = train(df, "address_only", u_max_pairs=1e5, em_max_pairs=1e5)
    assert one.settings_json["linker_uid"] != two.settings_json["linker_uid"]
    assert one.version == two.version
