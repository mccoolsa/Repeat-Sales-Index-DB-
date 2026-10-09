"""Sampling frame, strata, weak labels, leakage-safe splits and label accounting."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from ireland_rsi.labels import (
    LabelConfig,
    candidate_pairs,
    current_labels,
    draw_sample,
    mask_eircode,
    summarise_labels,
    weak_labels,
)

ROOT = Path(__file__).resolve().parents[1]
CFG = LabelConfig.load(ROOT / "config" / "labels.yml").raw


def sale(i, address, street, number=None, unit=None, house=None, county="Donegal",
         locality="bua:1", d=date(2012, 1, 1), eircode=None):
    return {"sale_id": f"s{i:03d}", "raw_row_id": f"r{i:03d}", "sale_date": d,
            "county": county, "eircode": eircode, "dwelling_status": "second_hand",
            "size_band": None, "address_display": address, "address_norm": address.lower(),
            "street_core": street, "primary_number": number, "unit_id": unit,
            "house_number": house, "house_range": None, "locality_id": locality,
            "townland_id": None}


def year(n):
    return date(2010 + n, 3, 1)


def frame(extra=()):
    rows = [
        sale(1, "7 Dun Ard, Letterkenny", "dun ard", "7", house="7", d=year(2)),
        sale(2, "7 Dun Ard, Letterkenny", "dun ard", "7", house="7", d=year(10)),
        sale(3, "11 Glendale, Letterkenny", "glendale", "11", house="11", d=year(1)),
        sale(4, "11 Glendale Manor, Letterkenny", "glendale", "11", house="11", d=year(9)),
        sale(5, "Apt 4, The Grove", "grove", "4", unit="4", d=year(3)),
        sale(6, "Apt 14, The Grove", "grove", "14", unit="14", d=year(8)),
        sale(7, "DOUGH, LAHINCH", "dough", d=year(4), locality=None),
        sale(8, "DOUGH, MILTOWN", "dough", d=year(12), locality=None),
        sale(9, "2 Taylor Hill Green", "taylor hill green", "2", house="2", d=year(5)),
        sale(10, "2 Taylor Hill Gardens", "taylor hill gardens", "2", house="2", d=year(11)),
        sale(11, "12 Mount Crescent", "mount crescent", "12", house="12", d=year(6)),
        sale(12, "10 Mount Crescent", "mount crescent", "10", house="10", d=year(13)),
        sale(13, "1 Same Day", "same day", "1", house="1", d=year(7)),
        sale(14, "1 Same Day", "same day", "1", house="1", d=year(7)),
        *extra,
    ]
    return pd.DataFrame(rows)


def test_mask_eircode_removes_codes_from_text():
    assert mask_eircode("7 Dun Ard, Letterkenny, F92 AC4D") == "7 Dun Ard, Letterkenny"
    assert mask_eircode("12 Main St D6W XY12") == "12 Main St"
    assert mask_eircode("No code here") == "No code here"


def test_strata_are_exclusive_and_chronological():
    df = frame()
    pairs, _ = candidate_pairs(df, CFG)
    got = {(df.sale_id[a], df.sale_id[b]): s
           for a, b, s in zip(pairs.ia, pairs.ib, pairs.stratum, strict=True)}
    assert got[("s001", "s002")] == "exact_address"
    assert got[("s003", "s004")] == "same_number_street"
    assert got[("s005", "s006")] == "apartment_unit_conflict"
    assert got[("s007", "s008")] == "rural_unnumbered"
    assert got[("s009", "s010")] == "similar_street_same_number"
    assert got[("s011", "s012")] == "neighbour_numbers"
    assert ("s013", "s014") not in got  # same sale date: a duplicate, not a repeat sale
    assert pairs.duplicated(["ia", "ib"]).sum() == 0
    assert (df.sale_date.to_numpy()[pairs.ia] < df.sale_date.to_numpy()[pairs.ib]).all()


def test_sampling_records_inclusion_probabilities_and_is_deterministic():
    df = frame()
    pairs, _ = candidate_pairs(df, CFG)
    one = draw_sample(df, pairs, CFG, "v1")
    two = draw_sample(df, pairs, CFG, "v1")
    pd.testing.assert_frame_equal(one, two)
    assert (one.sampling_prob == one.cell_sampled / one.cell_size).all()
    assert one.pair_id.is_unique and one.pair_id.str.startswith("p_").all()


def test_no_property_group_spans_splits():
    df = frame([sale(15, "7 Dun Ard, Letterkenny", "dun ard", "7", house="7",
                     d=date(2025, 5, 1))])
    pairs, _ = candidate_pairs(df, CFG)
    sample = draw_sample(df, pairs, CFG, "v1")
    assert (sample.groupby("group_id").split.nunique() == 1).all()
    # s001, s002 and s015 are connected; the 2025 sale puts the whole group in temporal test.
    dun_ard = sample[sample.address_a.str.contains("Dun Ard")]
    assert dun_ard.group_id.nunique() == 1 and set(dun_ard.split) == {"test_temporal"}


def test_reviewers_never_see_prices_or_eircodes():
    df = frame()
    df.loc[0, "eircode"] = "F92AC4D"
    pairs, _ = candidate_pairs(df, CFG)
    sample = draw_sample(df, pairs, CFG, "v1")
    assert not any("price" in c for c in sample.columns)
    shown = ["address_a", "address_b", "date_a", "date_b", "status_a", "status_b",
             "size_a", "size_b"]
    assert not sample[shown].astype(str).apply(lambda c: c.str.contains("F92")).any().any()


def test_weak_labels_are_positive_evidence_only():
    df = frame()
    df.loc[[0, 1], "eircode"] = "F92AC4D"   # same code, consistent: weak match
    df.loc[[4, 5], "eircode"] = "F92XY12"   # same code but unit 4 vs 14: conflict
    df.loc[2, "eircode"] = "F92AAAA"        # different codes: no label at all
    df.loc[3, "eircode"] = "F92BBBB"
    weak = weak_labels(df, "snap", "rs1")
    got = dict(zip(zip(weak.sale_id_a, weak.sale_id_b, strict=True), weak.weak_label,
                   strict=True))
    assert got == {("s001", "s002"): "eircode_match", ("s005", "s006"): "eircode_conflict"}


def _labels(rows):
    t0 = datetime(2026, 10, 7, tzinfo=UTC)
    return pd.DataFrame([{"sample_version": "v1", "pair_id": p, "reviewer": r,
                          "labelled_at": t0 + timedelta(minutes=m), "label": lab,
                          "rationale": None, "app_version": "a"}
                         for p, r, m, lab in rows])


def test_latest_label_wins_and_uncertain_is_counted_but_not_binary():
    labels = _labels([("p1", "me", 0, "match"), ("p1", "me", 5, "non_match"),
                      ("p2", "me", 1, "uncertain"), ("p3", "me", 2, "match"),
                      ("p3", "other", 3, "match"), ("p4", "me", 4, "match"),
                      ("p4", "other", 6, "non_match")])
    cur = current_labels(labels)
    assert cur[cur.pair_id == "p1"].label.item() == "non_match"
    s = summarise_labels(pd.DataFrame({"pair_id": ["p1", "p2", "p3", "p4", "p5"]}), labels)
    assert s["labelled_pairs"] == 4 and s["uncertain_pairs"] == 1 and s["binary_pairs"] == 3
    assert s["double_reviewed_pairs"] == 2 and s["reviewer_agreement"] == 0.5


def test_unknown_label_values_are_rejected():
    with pytest.raises(ValueError):
        current_labels(_labels([("p1", "me", 0, "probably")]))
